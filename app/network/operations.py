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

DRAIN_ACTIONS = ("awaiting", "migrated", "cancelled", "completed", "expired", "exempted")


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
        grace = payload.get("grace_period_seconds", 300)
        if not isinstance(grace, int) or isinstance(grace, bool) or grace < 0:
            raise ValidationError("排空宽限期必须是非负整数秒")
        overlap = self.connection.execute(
            "SELECT id FROM maintenance_windows WHERE scenario_id=? AND segment_id IS ? AND state IN ('scheduled','draining','active') AND starts_at<? AND ends_at>?",
            (scenario["id"], segment_id, ends_at, starts_at),
        ).fetchone()
        if overlap:
            raise ConflictError("相同范围已有重叠维护窗口")
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
        result["session_actions"] = [dict(item) for item in connection.execute(
            "SELECT * FROM maintenance_session_actions WHERE window_id=? ORDER BY id",
            (window_id,),
        ).fetchall()]
        result["events"] = self._events(connection, "maintenance", window_id)
        return result

    def drain_status(self, window_id: int) -> dict[str, Any]:
        window = self._window(window_id)
        now = to_storage(self.clock.now())
        block_only = window["drain_mode"] == "block_new"
        grace_deadline = None if block_only else self._grace_deadline(window)
        preview = window["state"] == "scheduled"
        summary = {action: 0 for action in DRAIN_ACTIONS}
        summary["unaffected"] = 0
        sessions: list[dict[str, Any]] = []
        if block_only or preview:
            for session in self._affected_active_sessions(self.connection, window):
                action = "unaffected" if block_only else "awaiting"
                sessions.append({
                    "session_id": session["id"],
                    "session_status": session["status"],
                    "action": action,
                    "deadline_at": None if block_only else self._session_deadline(window, session, grace_deadline),
                    "segment_id": session["segment_id"],
                    "from_segment_id": session["segment_id"],
                    "to_segment_id": None,
                    "migratable": False if block_only else self._migration_target(self.connection, window, session, now) is not None,
                    "manual": 0,
                    "actor": "",
                    "reason": "",
                    "resolved_at": None,
                })
                summary[action] += 1
        else:
            rows = self.connection.execute(
                "SELECT a.*,s.status AS session_status,s.segment_id AS session_segment_id,s.allocated_downlink_mbps "
                "FROM maintenance_session_actions a JOIN acceleration_sessions s ON s.id=a.session_id WHERE a.window_id=? ORDER BY a.id",
                (window_id,),
            ).fetchall()
            for row in rows:
                summary[row["action"]] += 1
                migratable = False
                if row["action"] == "awaiting" and row["session_status"] == "active":
                    probe = {"id": row["session_id"], "segment_id": row["session_segment_id"], "allocated_downlink_mbps": row["allocated_downlink_mbps"]}
                    migratable = self._migration_target(self.connection, window, probe, now) is not None
                sessions.append({
                    "session_id": row["session_id"],
                    "session_status": row["session_status"],
                    "action": row["action"],
                    "deadline_at": row["deadline_at"],
                    "segment_id": row["session_segment_id"],
                    "from_segment_id": row["from_segment_id"],
                    "to_segment_id": row["to_segment_id"],
                    "migratable": migratable,
                    "manual": row["manual"],
                    "actor": row["actor"],
                    "reason": row["reason"],
                    "resolved_at": row["resolved_at"],
                })
        drained = block_only or summary["awaiting"] == 0
        deadlines = [item["deadline_at"] for item in sessions if item["action"] == "awaiting" and item["deadline_at"]]
        return {
            "window_id": window["id"],
            "code": window["code"],
            "state": window["state"],
            "drain_mode": window["drain_mode"],
            "scenario_id": window["scenario_id"],
            "segment_id": window["segment_id"],
            "grace_period_seconds": window["grace_period_seconds"],
            "starts_at": window["starts_at"],
            "ends_at": window["ends_at"],
            "drain_started_at": window["drain_started_at"],
            "activated_at": window["activated_at"],
            "grace_deadline": grace_deadline,
            "preview": preview,
            "sessions": sessions,
            "summary": summary,
            "drained": drained,
            "drain_complete_by": max(deadlines) if deadlines else None,
        }

    def override_session(self, window_id: int, session_id: int, action: str, actor: str, reason: str) -> dict[str, Any]:
        if action not in {"cancel", "keep", "migrate"}:
            raise ValidationError("不支持的人工覆盖动作")
        window = self._window(window_id)
        if window["state"] != "draining":
            raise ConflictError("只有排空中的维护窗口可以人工覆盖")
        session = self.repository.session_by_id(session_id)
        if session is None:
            raise NotFoundError("加速会话不存在")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM maintenance_session_actions WHERE window_id=? AND session_id=?",
                (window_id, session_id),
            ).fetchone()
            if row is None or row["action"] != "awaiting":
                raise ConflictError("会话不在维护窗口的待排空列表中")
            if session["status"] != "active":
                raise ConflictError("会话已结束，无需人工覆盖")
            if action == "keep":
                self._resolve_action(connection, row["id"], "exempted", now, actor, 1, reason)
            elif action == "cancel":
                self._cancel_session(connection, window, session, now, actor)
                self._resolve_action(connection, row["id"], "cancelled", now, actor, 1, reason)
            else:
                target = self._migration_target(connection, window, session, now)
                if target is None:
                    raise ConflictError("没有可迁移的相邻区段")
                self._migrate_session(connection, window, session, target, now, actor)
                self._resolve_action(connection, row["id"], "migrated", now, actor, 1, reason, to_segment_id=target["id"])
            self._event(connection, "maintenance", window_id, "session_override", actor, {"session_id": session_id, "action": action, "reason": reason}, now)
        return self.maintenance_detail(window_id)

    def activate_due_maintenance(self, actor: str = "maintenance-scheduler") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        activated: list[int] = []
        completed: list[int] = []
        resumed: dict[int, int] = {}
        with transaction(immediate=True) as connection:
            due = connection.execute("SELECT * FROM maintenance_windows WHERE state='scheduled' AND starts_at<=? ORDER BY id", (now,)).fetchall()
            for window in due:
                connection.execute("UPDATE maintenance_windows SET state='draining',drain_started_at=?,updated_at=? WHERE id=?", (now, now, window["id"]))
                fresh = connection.execute("SELECT * FROM maintenance_windows WHERE id=?", (window["id"],)).fetchone()
                affected = self._snapshot_drain(connection, fresh, now)
                grace_deadline = None if fresh["drain_mode"] == "block_new" else self._grace_deadline(fresh)
                self._event(connection, "maintenance", fresh["id"], "drain_started", actor, {"drain_mode": fresh["drain_mode"], "affected_sessions": affected, "grace_deadline": grace_deadline}, now)
            pending = connection.execute("SELECT * FROM maintenance_windows WHERE state='draining' ORDER BY id").fetchall()
            for window in pending:
                self._process_drain(connection, window, now, actor)
                remaining = int(connection.execute(
                    "SELECT COUNT(*) FROM maintenance_session_actions WHERE window_id=? AND action='awaiting'",
                    (window["id"],),
                ).fetchone()[0])
                if remaining:
                    continue
                connection.execute("UPDATE maintenance_windows SET state='active',activated_at=?,updated_at=? WHERE id=?", (now, now, window["id"]))
                totals = {row[0]: int(row[1]) for row in connection.execute(
                    "SELECT action,COUNT(*) FROM maintenance_session_actions WHERE window_id=? GROUP BY action",
                    (window["id"],),
                ).fetchall()}
                detail = {action: totals.get(action, 0) for action in ("migrated", "cancelled", "completed", "expired", "exempted")}
                self._event(connection, "maintenance", window["id"], "activated", actor, detail, now)
                activated.append(window["id"])
            ended = connection.execute("SELECT * FROM maintenance_windows WHERE state='active' AND ends_at<=? ORDER BY id", (now,)).fetchall()
            for window in ended:
                connection.execute("UPDATE maintenance_windows SET state='completed',updated_at=? WHERE id=?", (now, window["id"]))
                candidates = self._open_incidents_in_scope(connection, window)
                resumed[window["id"]] = candidates
                self._event(connection, "maintenance", window["id"], "completed", actor, {"resumed_candidates": candidates}, now)
                completed.append(window["id"])
            draining = [int(row[0]) for row in connection.execute("SELECT id FROM maintenance_windows WHERE state='draining' ORDER BY id").fetchall()]
        return {"draining": draining, "activated": activated, "completed": completed, "resumed_candidates": resumed}

    def blocks_new_session(self, scenario_id: int, segment_id: int | None, now: str) -> dict[str, Any] | None:
        return self._blocking_window(self.connection, scenario_id, segment_id, now)

    @staticmethod
    def _blocking_window(connection: sqlite3.Connection, scenario_id: int, segment_id: int | None, now: str) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM maintenance_windows WHERE scenario_id=? AND (segment_id IS NULL OR segment_id IS ?) "
            "AND ((state='scheduled' AND starts_at<=? AND ends_at>?) OR state IN ('draining','active')) "
            "ORDER BY segment_id DESC,id LIMIT 1",
            (scenario_id, segment_id, now, now),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    @staticmethod
    def _affected_active_sessions(connection: sqlite3.Connection, window: sqlite3.Row | dict[str, Any]) -> list[sqlite3.Row]:
        if window["segment_id"] is None:
            return connection.execute(
                "SELECT * FROM acceleration_sessions WHERE scenario_id=? AND status='active' ORDER BY expires_at,id",
                (window["scenario_id"],),
            ).fetchall()
        return connection.execute(
            "SELECT * FROM acceleration_sessions WHERE scenario_id=? AND segment_id=? AND status='active' ORDER BY expires_at,id",
            (window["scenario_id"], window["segment_id"]),
        ).fetchall()

    def _snapshot_drain(self, connection: sqlite3.Connection, window: sqlite3.Row, now: str) -> int:
        if window["drain_mode"] == "block_new":
            return 0
        grace_deadline = self._grace_deadline(window)
        inserted = 0
        for session in self._affected_active_sessions(connection, window):
            cursor = connection.execute(
                "INSERT OR IGNORE INTO maintenance_session_actions(window_id,session_id,action,deadline_at,from_segment_id,created_at) VALUES(?,?,?,?,?,?)",
                (window["id"], session["id"], "awaiting", self._session_deadline(window, session, grace_deadline), session["segment_id"], now),
            )
            inserted += cursor.rowcount
        return inserted

    def _process_drain(self, connection: sqlite3.Connection, window: sqlite3.Row, now: str, actor: str) -> None:
        if window["drain_mode"] == "block_new":
            return
        self._snapshot_drain(connection, window, now)
        grace_deadline = self._grace_deadline(window)
        rows = connection.execute(
            "SELECT a.id AS action_id,s.* FROM maintenance_session_actions a JOIN acceleration_sessions s ON s.id=a.session_id "
            "WHERE a.window_id=? AND a.action='awaiting' ORDER BY s.expires_at,s.id",
            (window["id"],),
        ).fetchall()
        for row in rows:
            if row["status"] != "active":
                self._resolve_action(connection, row["action_id"], row["status"], row["ended_at"] or now, "", 0)
                continue
            target = self._migration_target(connection, window, row, now)
            if target is not None:
                self._migrate_session(connection, window, row, target, now, actor)
                self._resolve_action(connection, row["action_id"], "migrated", now, actor, 0, to_segment_id=target["id"])
                continue
            if window["drain_mode"] == "cancel_active" and now >= grace_deadline:
                self._cancel_session(connection, window, row, now, actor)
                self._resolve_action(connection, row["action_id"], "cancelled", now, actor, 0)

    def _migration_target(self, connection: sqlite3.Connection, window: sqlite3.Row | dict[str, Any], session: Any, now: str) -> sqlite3.Row | None:
        if window["segment_id"] is None or session["segment_id"] is None:
            return None
        source = connection.execute("SELECT * FROM network_segments WHERE id=?", (session["segment_id"],)).fetchone()
        if source is None:
            return None
        scenario = connection.execute("SELECT * FROM network_scenarios WHERE id=?", (window["scenario_id"],)).fetchone()
        candidates = connection.execute(
            "SELECT * FROM network_segments WHERE scenario_id=? AND status='active' AND sequence_no IN (?,?) ORDER BY sequence_no,id",
            (window["scenario_id"], int(source["sequence_no"]) - 1, int(source["sequence_no"]) + 1),
        ).fetchall()
        for target in candidates:
            if self._blocking_window(connection, window["scenario_id"], target["id"], now) is not None:
                continue
            used = self._held_capacity(connection, window["scenario_id"], target["id"])
            if used["sessions"] + 1 > int(scenario["max_concurrent_sessions"]):
                continue
            if used["downlink_mbps"] + float(session["allocated_downlink_mbps"]) > float(target["capacity_mbps"]):
                continue
            return target
        return None

    def _migrate_session(self, connection: sqlite3.Connection, window: sqlite3.Row | dict[str, Any], session: Any, target: sqlite3.Row, now: str, actor: str) -> None:
        connection.execute("UPDATE acceleration_sessions SET segment_id=?,version=version+1 WHERE id=? AND status='active'", (target["id"], session["id"]))
        connection.execute("UPDATE capacity_reservations SET segment_id=? WHERE session_id=? AND state='held'", (target["id"], session["id"]))
        connection.execute(
            "INSERT INTO session_events(session_id,event_type,actor,detail_json,created_at) VALUES(?,?,?,?,?)",
            (session["id"], "migrated", actor, json.dumps({"window_code": window["code"], "from_segment_id": session["segment_id"], "to_segment_id": target["id"], "to_segment_code": target["code"]}, ensure_ascii=False, sort_keys=True), now),
        )

    def _cancel_session(self, connection: sqlite3.Connection, window: sqlite3.Row | dict[str, Any], session: Any, now: str, actor: str) -> None:
        connection.execute("UPDATE acceleration_sessions SET status='cancelled',ended_at=?,end_reason='maintenance_drain',version=version+1 WHERE id=? AND status='active'", (now, session["id"]))
        connection.execute("UPDATE capacity_reservations SET state='released',released_at=? WHERE session_id=? AND state='held'", (now, session["id"]))
        connection.execute("UPDATE quality_incidents SET state='open',version=version+1 WHERE id=?", (session["incident_id"],))
        connection.execute(
            "INSERT INTO session_events(session_id,event_type,actor,detail_json,created_at) VALUES(?,?,?,?,?)",
            (session["id"], "cancelled", actor, json.dumps({"window_code": window["code"], "reason": "maintenance_drain"}, ensure_ascii=False, sort_keys=True), now),
        )

    @staticmethod
    def _resolve_action(connection: sqlite3.Connection, action_id: int, outcome: str, resolved_at: str, actor: str, manual: int, reason: str = "", to_segment_id: int | None = None) -> None:
        connection.execute(
            "UPDATE maintenance_session_actions SET action=?,resolved_at=?,actor=?,manual=?,reason=?,to_segment_id=? WHERE id=? AND action='awaiting'",
            (outcome, resolved_at, actor, manual, reason, to_segment_id, action_id),
        )

    @staticmethod
    def _held_capacity(connection: sqlite3.Connection, scenario_id: int, segment_id: int | None) -> dict[str, float]:
        row = connection.execute(
            "SELECT COALESCE(SUM(downlink_mbps),0),COALESCE(SUM(uplink_mbps),0),COUNT(*) "
            "FROM capacity_reservations WHERE scenario_id=? AND segment_id IS ? AND state='held'",
            (scenario_id, segment_id),
        ).fetchone()
        return {"downlink_mbps": float(row[0]), "uplink_mbps": float(row[1]), "sessions": int(row[2])}

    @staticmethod
    def _open_incidents_in_scope(connection: sqlite3.Connection, window: sqlite3.Row) -> int:
        if window["segment_id"] is None:
            return int(connection.execute(
                "SELECT COUNT(*) FROM quality_incidents WHERE scenario_id=? AND state='open'",
                (window["scenario_id"],),
            ).fetchone()[0])
        return int(connection.execute(
            "SELECT COUNT(*) FROM quality_incidents WHERE scenario_id=? AND segment_id=? AND state='open'",
            (window["scenario_id"], window["segment_id"]),
        ).fetchone()[0])

    @staticmethod
    def _grace_deadline(window: sqlite3.Row | dict[str, Any]) -> str:
        start = from_storage(window["drain_started_at"] or window["starts_at"])
        return to_storage(start + timedelta(seconds=int(window["grace_period_seconds"])))

    @staticmethod
    def _session_deadline(window: sqlite3.Row | dict[str, Any], session: Any, grace_deadline: str) -> str:
        if window["drain_mode"] == "cancel_active":
            return min(session["expires_at"], grace_deadline)
        return session["expires_at"]

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
