from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.network.repository import NetworkRepository
from app.network.schema import ensure_network_schema

DEFAULT_GRACE_PERIOD_SECONDS = 300
DRAIN_OUTCOME_BY_SESSION_STATUS = {"completed": "completed", "expired": "expired", "cancelled": "cancelled"}


def sync_session_drain_outcome(connection: sqlite3.Connection, session_id: int, now: str) -> None:
    """把会话的最终状态同步到维护排空清单，保证维护详情里的逐会话结果始终准确。"""
    session = connection.execute("SELECT status FROM acceleration_sessions WHERE id=?", (session_id,)).fetchone()
    if session is None:
        return
    outcome = DRAIN_OUTCOME_BY_SESSION_STATUS.get(str(session["status"]))
    if outcome is None:
        return
    connection.execute(
        "UPDATE maintenance_drain_sessions SET result=?,resolved_at=?,updated_at=? WHERE session_id=? AND result='pending'",
        (outcome, now, now, session_id),
    )


class NetworkOperationsService:
    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_network_schema(self.connection)
        self.clock = clock or SystemClock()
        self.repository = NetworkRepository(self.connection)

    def create_campaign(self, payload: dict[str, Any]) -> dict[str, Any]:
        scenario = self._scenario(payload["scenario_code"])
        policy = self.repository.policy_by_id(payload["policy_id"])
        if policy is None or policy["scenario_id"] != scenario["id"]:
            raise ValidationError("发布策略不属于目标场景")
        if policy["state"] not in {"draft", "published"}:
            raise ConflictError("退役策略不能用于新的发布活动")
        starts_at = self._optional_time(payload.get("starts_at"), "开始时间")
        ends_at = self._optional_time(payload.get("ends_at"), "结束时间")
        if starts_at and ends_at and ends_at <= starts_at:
            raise ValidationError("发布结束时间必须晚于开始时间")
        segment_ids = self._segment_ids(scenario["id"], payload.get("segment_codes", []))
        cohorts = payload.get("cohort_keys") or [""]
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO rollout_campaigns(scenario_id,code,name,strategy,target_percentage,policy_version_id,starts_at,ends_at,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (scenario["id"], payload["code"], payload["name"], payload["strategy"], payload["target_percentage"], policy["id"], starts_at, ends_at, payload["actor"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("发布活动编码已存在") from exc
            targets = segment_ids or [None]
            for segment_id in targets:
                for cohort in cohorts:
                    connection.execute(
                        "INSERT INTO rollout_targets(campaign_id,segment_id,cohort_key) VALUES(?,?,?)",
                        (cursor.lastrowid, segment_id, cohort),
                    )
            self._event(connection, "campaign", cursor.lastrowid, "created", payload["actor"], {"targets": len(targets) * len(cohorts)}, now)
            return self.campaign_detail(cursor.lastrowid, connection)

    def campaign_detail(self, campaign_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        campaign = connection.execute("SELECT * FROM rollout_campaigns WHERE id=?", (campaign_id,)).fetchone()
        if campaign is None:
            raise NotFoundError("发布活动不存在")
        result = dict(campaign)
        result["targets"] = [dict(row) for row in connection.execute(
            "SELECT t.*,s.code AS segment_code,s.name AS segment_name FROM rollout_targets t LEFT JOIN network_segments s ON s.id=t.segment_id WHERE t.campaign_id=? ORDER BY t.id",
            (campaign_id,),
        ).fetchall()]
        result["events"] = self._events(connection, "campaign", campaign_id)
        return result

    def list_campaigns(self, scenario_code: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if scenario_code:
            clauses.append("n.code=?")
            params.append(scenario_code)
        if state:
            clauses.append("c.state=?")
            params.append(state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT c.*,n.code AS scenario_code,n.name AS scenario_name FROM rollout_campaigns c JOIN network_scenarios n ON n.id=c.scenario_id" + where + " ORDER BY c.created_at DESC,c.id DESC",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def start_campaign(self, campaign_id: int, actor: str, reason: str) -> dict[str, Any]:
        campaign = self._campaign(campaign_id)
        if campaign["state"] not in {"draft", "scheduled", "paused"}:
            raise ConflictError("当前发布活动状态不能启动")
        now = to_storage(self.clock.now())
        if campaign["starts_at"] and campaign["starts_at"] > now:
            raise ConflictError("发布活动尚未到开始时间")
        with transaction(immediate=True) as connection:
            connection.execute("UPDATE rollout_campaigns SET state='running',updated_at=? WHERE id=?", (now, campaign_id))
            connection.execute("UPDATE rollout_targets SET state='active',activated_at=COALESCE(activated_at,?),version=version+1 WHERE campaign_id=? AND state IN ('pending','paused')", (now, campaign_id))
            self._event(connection, "campaign", campaign_id, "started", actor, {"reason": reason}, now)
            return self.campaign_detail(campaign_id, connection)

    def pause_campaign(self, campaign_id: int, actor: str, reason: str) -> dict[str, Any]:
        campaign = self._campaign(campaign_id)
        if campaign["state"] != "running":
            raise ConflictError("只有运行中的发布活动可以暂停")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            connection.execute("UPDATE rollout_campaigns SET state='paused',updated_at=? WHERE id=?", (now, campaign_id))
            connection.execute("UPDATE rollout_targets SET state='paused',version=version+1 WHERE campaign_id=? AND state='active'", (campaign_id,))
            self._event(connection, "campaign", campaign_id, "paused", actor, {"reason": reason}, now)
            return self.campaign_detail(campaign_id, connection)

    def complete_campaign(self, campaign_id: int, actor: str, reason: str) -> dict[str, Any]:
        campaign = self._campaign(campaign_id)
        if campaign["state"] not in {"running", "paused"}:
            raise ConflictError("当前发布活动状态不能完成")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            connection.execute("UPDATE rollout_campaigns SET state='completed',ends_at=COALESCE(ends_at,?),updated_at=? WHERE id=?", (now, now, campaign_id))
            connection.execute("UPDATE rollout_targets SET state='completed',completed_at=?,version=version+1 WHERE campaign_id=? AND state IN ('active','paused')", (now, campaign_id))
            self._event(connection, "campaign", campaign_id, "completed", actor, {"reason": reason}, now)
            return self.campaign_detail(campaign_id, connection)

    def create_maintenance(self, payload: dict[str, Any]) -> dict[str, Any]:
        scenario = self._scenario(payload["scenario_code"])
        segment_id = None
        if payload.get("segment_code"):
            segment = self.repository.segment_by_code(scenario["id"], payload["segment_code"])
            if segment is None:
                raise NotFoundError("维护区段不存在")
            segment_id = segment["id"]
        starts_at = self._required_time(payload["starts_at"], "开始时间")
        ends_at = self._required_time(payload["ends_at"], "结束时间")
        if ends_at <= starts_at:
            raise ValidationError("维护结束时间必须晚于开始时间")
        grace = payload.get("grace_period_seconds", DEFAULT_GRACE_PERIOD_SECONDS)
        if not isinstance(grace, int) or grace < 0:
            raise ValidationError("宽限期必须是不小于零的整数秒")
        overlap = self.connection.execute(
            "SELECT id FROM maintenance_windows WHERE scenario_id=? AND state IN ('scheduled','draining','active') AND starts_at<? AND ends_at>? "
            "AND (segment_id IS NULL OR ? IS NULL OR segment_id IS ?)",
            (scenario["id"], ends_at, starts_at, segment_id, segment_id),
        ).fetchone()
        if overlap:
            raise ConflictError("相同或包含范围已有重叠维护窗口")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO maintenance_windows(scenario_id,segment_id,code,reason,starts_at,ends_at,drain_mode,grace_period_seconds,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (scenario["id"], segment_id, payload["code"], payload["reason"], starts_at, ends_at, payload["drain_mode"], grace, payload["actor"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("维护窗口编码已存在") from exc
            self._event(connection, "maintenance", cursor.lastrowid, "scheduled", payload["actor"], {"drain_mode": payload["drain_mode"], "grace_period_seconds": grace}, now)
            return self.maintenance_detail(cursor.lastrowid, connection)

    def maintenance_detail(self, window_id: int, connection: sqlite3.Connection | None = None) -> dict[str, Any]:
        connection = connection or self.connection
        row = connection.execute(
            "SELECT w.*,n.code AS scenario_code,n.name AS scenario_name,s.code AS segment_code,s.name AS segment_name FROM maintenance_windows w JOIN network_scenarios n ON n.id=w.scenario_id LEFT JOIN network_segments s ON s.id=w.segment_id WHERE w.id=?",
            (window_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("维护窗口不存在")
        result = dict(row)
        result["drain_deadline_at"] = self._drain_deadline(result)
        drain_rows = connection.execute(
            "SELECT d.*,s.status AS session_status,t.code AS target_segment_code,t.name AS target_segment_name "
            "FROM maintenance_drain_sessions d JOIN acceleration_sessions s ON s.id=d.session_id "
            "LEFT JOIN network_segments t ON t.id=d.target_segment_id WHERE d.window_id=? ORDER BY d.id",
            (window_id,),
        ).fetchall()
        sessions = [dict(item) for item in drain_rows]
        by_result: dict[str, int] = {}
        for item in sessions:
            by_result[item["result"]] = by_result.get(item["result"], 0) + 1
        result["drain_sessions"] = sessions
        result["drain_summary"] = {"total": len(sessions), "pending": by_result.get("pending", 0), "by_result": by_result}
        result["events"] = self._events(connection, "maintenance", window_id)
        return result

    def activate_due_maintenance(self, actor: str = "maintenance-scheduler") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        draining: list[int] = []
        activated: list[int] = []
        completed: list[int] = []
        migrated: list[int] = []
        force_cancelled: list[int] = []
        with transaction(immediate=True) as connection:
            due = connection.execute("SELECT * FROM maintenance_windows WHERE state='scheduled' AND starts_at<=? ORDER BY id", (now,)).fetchall()
            for window in due:
                connection.execute("UPDATE maintenance_windows SET state='draining',updated_at=? WHERE id=?", (now, window["id"]))
                migrated.extend(self._begin_drain(connection, window, actor, now))
                draining.append(window["id"])
            pending = connection.execute("SELECT * FROM maintenance_windows WHERE state='draining' ORDER BY id").fetchall()
            for window in pending:
                outcome = self._enforce_drain(connection, window, actor, now)
                migrated.extend(outcome["migrated"])
                force_cancelled.extend(outcome["cancelled"])
                if self._drain_satisfied(connection, window):
                    connection.execute("UPDATE maintenance_windows SET state='active',updated_at=? WHERE id=?", (now, window["id"]))
                    self._event(connection, "maintenance", window["id"], "activated", actor, {}, now)
                    activated.append(window["id"])
            ended = connection.execute("SELECT * FROM maintenance_windows WHERE state IN ('draining','active') AND ends_at<=? ORDER BY id", (now,)).fetchall()
            for window in ended:
                self._complete_window(connection, window, actor, now)
                completed.append(window["id"])
        return {"draining": draining, "activated": activated, "completed": completed, "migrated": migrated, "force_cancelled": force_cancelled}

    def override_drain_session(self, window_id: int, session_id: int, actor: str, action: str, reason: str) -> dict[str, Any]:
        window = self._window(window_id)
        if window["state"] != "draining":
            raise ConflictError("只有排空中的维护窗口可以人工覆盖")
        if action not in {"wait", "migrate", "cancel"}:
            raise ValidationError("覆盖动作必须是 wait、migrate 或 cancel")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM maintenance_drain_sessions WHERE window_id=? AND session_id=?",
                (window_id, session_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("会话不在维护排空清单中")
            if row["result"] != "pending":
                raise ConflictError("该会话的排空处理已完成，不能覆盖")
            session = NetworkRepository(connection).session_by_id(session_id)
            if action == "wait":
                connection.execute(
                    "UPDATE maintenance_drain_sessions SET action='wait',result='kept',overridden_by=?,override_reason=?,overridden_at=?,resolved_at=?,updated_at=? WHERE id=?",
                    (actor, reason, now, now, now, row["id"]),
                )
            else:
                if session is None or session["status"] != "active":
                    raise ConflictError("会话已结束，无法执行覆盖动作")
                if action == "cancel":
                    self._cancel_session(connection, window, session, actor, now, "manual_override")
                else:
                    target = self._migration_target(connection, window, session, now)
                    if target is None:
                        raise ConflictError("没有可迁移的相邻区段")
                    self._migrate_session(connection, window, session, target, actor, now)
                connection.execute(
                    "UPDATE maintenance_drain_sessions SET overridden_by=?,override_reason=?,overridden_at=?,updated_at=? WHERE id=?",
                    (actor, reason, now, now, row["id"]),
                )
            self._event(connection, "maintenance", window_id, "session_overridden", actor, {"session_id": session_id, "action": action, "reason": reason}, now)
            if self._drain_satisfied(connection, window):
                connection.execute("UPDATE maintenance_windows SET state='active',updated_at=? WHERE id=?", (now, window_id))
                self._event(connection, "maintenance", window_id, "activated", actor, {"trigger": "manual_override"}, now)
            return self.maintenance_detail(window_id, connection)

    def blocks_new_session(self, scenario_id: int, segment_id: int | None, now: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM maintenance_windows WHERE scenario_id=? AND (segment_id IS NULL OR segment_id IS ?) AND state IN ('scheduled','draining','active') AND starts_at<=? AND ends_at>? ORDER BY segment_id DESC,id LIMIT 1",
            (scenario_id, segment_id, now, now),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def _begin_drain(self, connection: sqlite3.Connection, window: sqlite3.Row, actor: str, now: str) -> list[int]:
        sessions = connection.execute(
            "SELECT * FROM acceleration_sessions WHERE scenario_id=? AND status='active' AND (? IS NULL OR segment_id IS ?) ORDER BY id",
            (window["scenario_id"], window["segment_id"], window["segment_id"]),
        ).fetchall()
        deadline = self._drain_deadline(window)
        self._event(
            connection, "maintenance", window["id"], "drain_started", actor,
            {"affected_sessions": len(sessions), "drain_deadline_at": deadline},
            now,
        )
        migrated: list[int] = []
        for session in sessions:
            action = "wait"
            target = None
            row_deadline = session["expires_at"]
            if window["drain_mode"] == "cancel_active":
                row_deadline = deadline
                target = self._migration_target(connection, window, session, now)
                action = "migrate" if target is not None else "cancel"
            connection.execute(
                "INSERT INTO maintenance_drain_sessions(window_id,session_id,action,deadline_at,target_segment_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (window["id"], session["id"], action, row_deadline, target["id"] if target else None, now, now),
            )
            if action == "migrate":
                self._migrate_session(connection, window, session, target, actor, now)
                migrated.append(int(session["id"]))
        return migrated

    def _enforce_drain(self, connection: sqlite3.Connection, window: sqlite3.Row, actor: str, now: str) -> dict[str, list[int]]:
        outcome: dict[str, list[int]] = {"migrated": [], "cancelled": []}
        deadline = self._drain_deadline(window)
        if deadline is None or now < deadline:
            return outcome
        rows = connection.execute(
            "SELECT * FROM maintenance_drain_sessions WHERE window_id=? AND result='pending' ORDER BY id",
            (window["id"],),
        ).fetchall()
        repository = NetworkRepository(connection)
        for row in rows:
            session = repository.session_by_id(row["session_id"])
            if session is None or session["status"] != "active":
                sync_session_drain_outcome(connection, row["session_id"], now)
                continue
            target = self._migration_target(connection, window, session, now)
            if target is not None:
                self._migrate_session(connection, window, session, target, actor, now)
                outcome["migrated"].append(int(session["id"]))
            else:
                self._cancel_session(connection, window, session, actor, now, "grace_period_elapsed")
                outcome["cancelled"].append(int(session["id"]))
        return outcome

    def _drain_satisfied(self, connection: sqlite3.Connection, window: sqlite3.Row) -> bool:
        if window["drain_mode"] == "block_new":
            return True
        pending = connection.execute(
            "SELECT COUNT(*) FROM maintenance_drain_sessions WHERE window_id=? AND result='pending'",
            (window["id"],),
        ).fetchone()[0]
        return int(pending) == 0

    def _complete_window(self, connection: sqlite3.Connection, window: sqlite3.Row, actor: str, now: str) -> None:
        candidates = connection.execute(
            "SELECT id FROM quality_incidents WHERE state='open' AND scenario_id=? AND (? IS NULL OR segment_id IS ?) ORDER BY id",
            (window["scenario_id"], window["segment_id"], window["segment_id"]),
        ).fetchall()
        connection.execute("UPDATE maintenance_windows SET state='completed',updated_at=? WHERE id=?", (now, window["id"]))
        self._event(
            connection, "maintenance", window["id"], "completed", actor,
            {"activated": window["state"] == "active", "resumed_candidate_incidents": [int(row["id"]) for row in candidates]},
            now,
        )

    def _migrate_session(self, connection: sqlite3.Connection, window: sqlite3.Row, session: sqlite3.Row, target: sqlite3.Row, actor: str, now: str) -> None:
        connection.execute(
            "UPDATE acceleration_sessions SET segment_id=?,version=version+1 WHERE id=? AND status='active'",
            (target["id"], session["id"]),
        )
        connection.execute(
            "UPDATE capacity_reservations SET segment_id=? WHERE session_id=? AND state='held'",
            (target["id"], session["id"]),
        )
        self._session_event(
            connection, session["id"], "migrated", actor,
            {"maintenance_code": window["code"], "from_segment_id": session["segment_id"], "to_segment_id": target["id"], "to_segment_code": target["code"]},
            now,
        )
        connection.execute(
            "UPDATE maintenance_drain_sessions SET action='migrate',result='migrated',target_segment_id=?,resolved_at=?,updated_at=? WHERE window_id=? AND session_id=?",
            (target["id"], now, now, window["id"], session["id"]),
        )
        self._event(connection, "maintenance", window["id"], "session_migrated", actor, {"session_id": session["id"], "target_segment_code": target["code"]}, now)

    def _cancel_session(self, connection: sqlite3.Connection, window: sqlite3.Row, session: sqlite3.Row, actor: str, now: str, reason: str) -> None:
        connection.execute(
            "UPDATE acceleration_sessions SET status='cancelled',ended_at=?,end_reason='maintenance_drain',version=version+1 WHERE id=? AND status='active'",
            (now, session["id"]),
        )
        connection.execute("UPDATE capacity_reservations SET state='released',released_at=? WHERE session_id=? AND state='held'", (now, session["id"]))
        connection.execute("UPDATE quality_incidents SET state='open',version=version+1 WHERE id=?", (session["incident_id"],))
        self._session_event(connection, session["id"], "cancelled", actor, {"reason": reason, "maintenance_code": window["code"]}, now)
        connection.execute(
            "UPDATE maintenance_drain_sessions SET result='cancelled',resolved_at=?,updated_at=? WHERE window_id=? AND session_id=?",
            (now, now, window["id"], session["id"]),
        )
        self._event(connection, "maintenance", window["id"], "session_cancelled", actor, {"session_id": session["id"], "reason": reason}, now)

    def _migration_target(self, connection: sqlite3.Connection, window: sqlite3.Row, session: sqlite3.Row, now: str) -> sqlite3.Row | None:
        if window["segment_id"] is None or session["segment_id"] != window["segment_id"]:
            return None
        current = NetworkRepository(connection).segment_by_id(window["segment_id"])
        if current is None:
            return None
        # 高铁列车沿顺序号行驶，优先迁移到下一区段，其次上一区段
        candidates = connection.execute(
            "SELECT * FROM network_segments WHERE scenario_id=? AND status='active' AND sequence_no IN (?,?) "
            "ORDER BY CASE WHEN sequence_no=? THEN 0 ELSE 1 END,sequence_no",
            (window["scenario_id"], current["sequence_no"] + 1, current["sequence_no"] - 1, current["sequence_no"] + 1),
        ).fetchall()
        repository = NetworkRepository(connection)
        for candidate in candidates:
            if self._segment_blocked(connection, window["scenario_id"], candidate["id"], now):
                continue
            used = repository.active_capacity(window["scenario_id"], candidate["id"])
            if used["downlink_mbps"] + float(session["allocated_downlink_mbps"]) > float(candidate["capacity_mbps"]):
                continue
            return candidate
        return None

    @staticmethod
    def _segment_blocked(connection: sqlite3.Connection, scenario_id: int, segment_id: int, now: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM maintenance_windows WHERE scenario_id=? AND (segment_id IS NULL OR segment_id IS ?) "
            "AND state IN ('scheduled','draining','active') AND starts_at<=? AND ends_at>? LIMIT 1",
            (scenario_id, segment_id, now, now),
        ).fetchone()
        return row is not None

    @staticmethod
    def _drain_deadline(window: Any) -> str | None:
        if window["drain_mode"] != "cancel_active":
            return None
        starts_at = from_storage(window["starts_at"])
        if starts_at is None:
            return None
        return to_storage(starts_at + timedelta(seconds=int(window["grace_period_seconds"])))

    def _window(self, window_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM maintenance_windows WHERE id=?", (window_id,)).fetchone()
        if row is None:
            raise NotFoundError("维护窗口不存在")
        return row

    def _campaign(self, campaign_id: int) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM rollout_campaigns WHERE id=?", (campaign_id,)).fetchone()
        if row is None:
            raise NotFoundError("发布活动不存在")
        return row

    def _scenario(self, code: str) -> sqlite3.Row:
        row = self.repository.scenario_by_code(code)
        if row is None:
            raise NotFoundError("网络场景不存在")
        return row

    def _segment_ids(self, scenario_id: int, codes: list[str]) -> list[int]:
        result = []
        for code in codes:
            segment = self.repository.segment_by_code(scenario_id, code)
            if segment is None:
                raise NotFoundError(f"发布区段不存在：{code}")
            result.append(int(segment["id"]))
        return result

    @staticmethod
    def _required_time(value: str, label: str) -> str:
        try:
            return to_storage(from_storage(value))
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{label}格式不正确") from exc

    @classmethod
    def _optional_time(cls, value: str | None, label: str) -> str | None:
        return cls._required_time(value, label) if value else None

    @staticmethod
    def _event(connection: sqlite3.Connection, resource_type: str, resource_id: int, event_type: str, actor: str, detail: dict[str, Any], now: str) -> None:
        connection.execute(
            "INSERT INTO operation_events(resource_type,resource_id,event_type,actor,detail_json,created_at) VALUES(?,?,?,?,?,?)",
            (resource_type, resource_id, event_type, actor, json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )

    @staticmethod
    def _session_event(connection: sqlite3.Connection, session_id: int, event_type: str, actor: str, detail: dict[str, Any], now: str) -> None:
        connection.execute(
            "INSERT INTO session_events(session_id,event_type,actor,detail_json,created_at) VALUES(?,?,?,?,?)",
            (session_id, event_type, actor, json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )

    @staticmethod
    def _events(connection: sqlite3.Connection, resource_type: str, resource_id: int) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM operation_events WHERE resource_type=? AND resource_id=? ORDER BY id",
            (resource_type, resource_id),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            result.append(item)
        return result
