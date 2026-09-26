from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.network.repository import NetworkRepository
from app.network.schema import ensure_network_schema


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
        overlap = self.connection.execute(
            "SELECT id FROM maintenance_windows WHERE scenario_id=? AND segment_id IS ? AND state IN ('scheduled','active') AND starts_at<? AND ends_at>?",
            (scenario["id"], segment_id, ends_at, starts_at),
        ).fetchone()
        if overlap:
            raise ConflictError("相同范围已有重叠维护窗口")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO maintenance_windows(scenario_id,segment_id,code,reason,starts_at,ends_at,drain_mode,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (scenario["id"], segment_id, payload["code"], payload["reason"], starts_at, ends_at, payload["drain_mode"], payload["actor"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("维护窗口编码已存在") from exc
            self._event(connection, "maintenance", cursor.lastrowid, "scheduled", payload["actor"], {"drain_mode": payload["drain_mode"]}, now)
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
        result["events"] = self._events(connection, "maintenance", window_id)
        return result

    def activate_due_maintenance(self, actor: str = "maintenance-scheduler") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        activated: list[int] = []
        completed: list[int] = []
        with transaction(immediate=True) as connection:
            due = connection.execute("SELECT * FROM maintenance_windows WHERE state='scheduled' AND starts_at<=? ORDER BY id", (now,)).fetchall()
            for window in due:
                connection.execute("UPDATE maintenance_windows SET state='active',updated_at=? WHERE id=?", (now, window["id"]))
                self._event(connection, "maintenance", window["id"], "activated", actor, {}, now)
                activated.append(window["id"])
            ended = connection.execute("SELECT * FROM maintenance_windows WHERE state='active' AND ends_at<=? ORDER BY id", (now,)).fetchall()
            for window in ended:
                connection.execute("UPDATE maintenance_windows SET state='completed',updated_at=? WHERE id=?", (now, window["id"]))
                self._event(connection, "maintenance", window["id"], "completed", actor, {}, now)
                completed.append(window["id"])
        return {"activated": activated, "completed": completed}

    def blocks_new_session(self, scenario_id: int, segment_id: int | None, now: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM maintenance_windows WHERE scenario_id=? AND (segment_id IS NULL OR segment_id IS ?) AND state IN ('scheduled','active') AND starts_at<=? AND ends_at>? ORDER BY segment_id DESC,id LIMIT 1",
            (scenario_id, segment_id, now, now),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

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
