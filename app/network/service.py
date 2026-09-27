from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import request_fingerprint
from app.database import get_connection, transaction
from app.network.repository import NetworkRepository
from app.network.rules import DEFAULT_RULES, allocation_for, canonical_rules, judge_quality
from app.network.schema import ensure_network_schema


class NetworkAccelerationService:
    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_network_schema(self.connection)
        self.clock = clock or SystemClock()
        self.repository = NetworkRepository(self.connection)

    def create_scenario(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO network_scenarios(code,name,scene_type,timezone,max_concurrent_sessions,capacity_mbps,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (payload["code"], payload["name"], payload["scene_type"], payload["timezone"], payload["max_concurrent_sessions"], payload["capacity_mbps"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("场景编码已存在") from exc
            return dict(NetworkRepository(connection).scenario_by_id(cursor.lastrowid))

    def list_scenarios(self, status: str | None = None) -> list[dict[str, Any]]:
        return self.repository.list_scenarios(status=status)

    def add_segment(self, scenario_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        scenario = self._scenario(scenario_code)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO network_segments(scenario_id,code,name,sequence_no,expected_dwell_seconds,capacity_mbps,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (scenario["id"], payload["code"], payload["name"], payload["sequence_no"], payload["expected_dwell_seconds"], payload["capacity_mbps"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("区段编码或顺序已存在") from exc
            return dict(NetworkRepository(connection).segment_by_id(cursor.lastrowid))

    def create_application(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO application_profiles(app_code,name,category,latency_target_ms,packet_loss_target,min_downlink_mbps,min_uplink_mbps,default_priority,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (payload["app_code"], payload["name"], payload["category"], payload["latency_target_ms"], payload["packet_loss_target"], payload["min_downlink_mbps"], payload["min_uplink_mbps"], payload["default_priority"], now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("应用编码已存在") from exc
            return dict(NetworkRepository(connection).application_by_id(cursor.lastrowid))

    def list_applications(self, category: str | None = None) -> list[dict[str, Any]]:
        return self.repository.list_applications(category=category)

    def create_policy(self, scenario_code: str, rules: dict[str, Any], actor: str) -> dict[str, Any]:
        scenario = self._scenario(scenario_code)
        text, digest = canonical_rules(rules)
        existing = self.repository.policy_by_digest(scenario["id"], digest)
        if existing is not None:
            return NetworkRepository._policy(existing)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = NetworkRepository(connection)
            version = repository.next_policy_version(scenario["id"])
            cursor = connection.execute(
                "INSERT INTO policy_versions(scenario_id,version_no,rules_json,rules_digest,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (scenario["id"], version, text, digest, actor, now, now),
            )
            return NetworkRepository._policy(repository.policy_by_id(cursor.lastrowid))

    def publish_policy(self, policy_id: int, actor: str, effective_from: str) -> dict[str, Any]:
        policy = self.repository.policy_by_id(policy_id)
        if policy is None:
            raise NotFoundError("策略版本不存在")
        if policy["state"] == "retired":
            raise ConflictError("已退役策略不能发布")
        try:
            effective = to_storage(from_storage(effective_from))
        except ValueError as exc:
            raise ValidationError("生效时间格式不正确") from exc
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE policy_versions SET state='retired',retired_at=?,updated_at=? WHERE scenario_id=? AND state='published' AND id<>?",
                (now, now, policy["scenario_id"], policy_id),
            )
            connection.execute(
                "UPDATE policy_versions SET state='published',published_by=?,effective_from=?,retired_at=NULL,updated_at=? WHERE id=?",
                (actor, effective, now, policy_id),
            )
            return NetworkRepository._policy(NetworkRepository(connection).policy_by_id(policy_id))

    def add_entitlement(self, payload: dict[str, Any]) -> dict[str, Any]:
        scenario = self._scenario(payload["scenario_code"])
        try:
            start = to_storage(from_storage(payload["valid_from"]))
            end = to_storage(from_storage(payload["valid_until"]))
        except ValueError as exc:
            raise ValidationError("权益有效期格式不正确") from exc
        if end <= start:
            raise ValidationError("权益结束时间必须晚于开始时间")
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            existing = connection.execute("SELECT * FROM subscriber_entitlements WHERE source_order_id=?", (payload["source_order_id"],)).fetchone()
            if existing is not None:
                return dict(existing)
            cursor = connection.execute(
                "INSERT INTO subscriber_entitlements(subscriber_hash,scenario_id,product_code,valid_from,valid_until,source_order_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (payload["subscriber_hash"], scenario["id"], payload["product_code"], start, end, payload["source_order_id"], now, now),
            )
            return dict(connection.execute("SELECT * FROM subscriber_entitlements WHERE id=?", (cursor.lastrowid,)).fetchone())

    def ingest_sample(self, payload: dict[str, Any]) -> dict[str, Any]:
        scenario = self._scenario(payload["scenario_code"])
        app = self._application(payload["app_code"])
        segment = None
        if payload.get("segment_code"):
            segment = self.repository.segment_by_code(scenario["id"], payload["segment_code"])
            if segment is None:
                raise NotFoundError("场景区段不存在")
        try:
            observed = to_storage(from_storage(payload["observed_at"]))
        except ValueError as exc:
            raise ValidationError("观测时间格式不正确") from exc
        digest = request_fingerprint(payload)
        existing = self.repository.sample_by_key(payload["sample_key"])
        if existing is not None:
            if existing["payload_digest"] != digest:
                raise ConflictError("相同 sample_key 对应了不同观测内容")
            return self._sample_result(existing["id"])
        now = to_storage(self.clock.now())
        policy = self.repository.effective_policy(scenario["id"], now)
        rules = json.loads(policy["rules_json"]) if policy else DEFAULT_RULES
        decision = judge_quality(payload, dict(app), rules)
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "INSERT INTO experience_samples(sample_key,scenario_id,segment_id,app_id,subscriber_hash,device_class,train_speed_kmh,latency_ms,packet_loss,downlink_mbps,uplink_mbps,observed_at,received_at,payload_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (payload["sample_key"], scenario["id"], segment["id"] if segment else None, app["id"], payload["subscriber_hash"], payload["device_class"], payload["train_speed_kmh"], payload["latency_ms"], payload["packet_loss"], payload["downlink_mbps"], payload["uplink_mbps"], observed, now, digest),
            )
            incident_id = None
            if decision.degraded:
                incident = connection.execute(
                    "INSERT INTO quality_incidents(sample_id,scenario_id,segment_id,app_id,severity,reasons_json,opened_at) VALUES(?,?,?,?,?,?,?)",
                    (cursor.lastrowid, scenario["id"], segment["id"] if segment else None, app["id"], decision.severity, json.dumps(decision.as_dict(), ensure_ascii=False, sort_keys=True), now),
                )
                incident_id = incident.lastrowid
            return {"sample_id": cursor.lastrowid, "incident_id": incident_id, "quality": decision.as_dict()}

    def ingest_batch(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        results = []
        for item in items:
            results.append(self.ingest_sample(item))
        return {"items": results, "accepted": len(results)}

    def start_acceleration(self, incident_id: int, actor: str) -> dict[str, Any]:
        incident = self.repository.incident_by_id(incident_id)
        if incident is None:
            raise NotFoundError("质差事件不存在")
        existing = self.repository.session_by_incident(incident_id)
        if existing is not None:
            return self.repository.session_detail(existing["id"])
        if incident["state"] != "open":
            raise ConflictError("只有待处理事件可以启动加速")
        sample = self.repository.sample_by_id(incident["sample_id"])
        app = self.repository.application_by_id(incident["app_id"])
        now_value = self.clock.now()
        now = to_storage(now_value)
        entitlement = self.repository.active_entitlement(sample["subscriber_hash"], incident["scenario_id"], now)
        if entitlement is None:
            raise ConflictError("用户没有当前场景的有效加速权益")
        policy = self.repository.effective_policy(incident["scenario_id"], now)
        if policy is None:
            raise ConflictError("场景没有已生效的加速策略")
        rules = json.loads(policy["rules_json"])
        allocation = allocation_for(dict(app), incident["severity"], rules)
        scenario = self.repository.scenario_by_id(incident["scenario_id"])
        segment = self.repository.segment_by_id(incident["segment_id"]) if incident["segment_id"] else None
        from app.network.operations import NetworkOperationsService
        maintenance = NetworkOperationsService(self.connection, self.clock).blocks_new_session(incident["scenario_id"], incident["segment_id"], now)
        if maintenance is not None:
            raise ConflictError(
                "当前场景处于维护窗口，不能启动新的加速会话",
                context={"maintenance_code": maintenance["code"], "drain_mode": maintenance["drain_mode"]},
            )
        limit = int(segment["capacity_mbps"] if segment else scenario["capacity_mbps"])
        used = self.repository.active_capacity(incident["scenario_id"], incident["segment_id"])
        if used["sessions"] >= int(scenario["max_concurrent_sessions"]):
            raise ConflictError("场景并发加速会话已达到上限")
        if used["downlink_mbps"] + allocation.downlink_mbps > limit:
            raise ConflictError("区段下行加速容量不足")
        expires = to_storage(now_value + timedelta(seconds=allocation.duration_seconds))
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "INSERT INTO acceleration_sessions(incident_id,subscriber_hash,app_id,scenario_id,segment_id,policy_version_id,allocated_downlink_mbps,allocated_uplink_mbps,priority,started_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (incident_id, sample["subscriber_hash"], incident["app_id"], incident["scenario_id"], incident["segment_id"], policy["id"], allocation.downlink_mbps, allocation.uplink_mbps, allocation.priority, now, expires),
            )
            connection.execute(
                "INSERT INTO capacity_reservations(session_id,scenario_id,segment_id,downlink_mbps,uplink_mbps,held_at) VALUES(?,?,?,?,?,?)",
                (cursor.lastrowid, incident["scenario_id"], incident["segment_id"], allocation.downlink_mbps, allocation.uplink_mbps, now),
            )
            connection.execute("UPDATE quality_incidents SET state='accelerating',version=version+1 WHERE id=?", (incident_id,))
            self._event(connection, cursor.lastrowid, "started", actor, {"policy_version": policy["version_no"]}, now)
            return NetworkRepository(connection).session_detail(cursor.lastrowid)

    def finish_session(self, session_id: int, actor: str, reason: str, result: str) -> dict[str, Any]:
        session = self.repository.session_by_id(session_id)
        if session is None:
            raise NotFoundError("加速会话不存在")
        if session["status"] != "active":
            return self.repository.session_detail(session_id)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE acceleration_sessions SET status=?,ended_at=?,end_reason=?,version=version+1 WHERE id=? AND status='active'",
                (result, now, reason, session_id),
            )
            connection.execute("UPDATE capacity_reservations SET state='released',released_at=? WHERE session_id=? AND state='held'", (now, session_id))
            incident_state = "resolved" if result == "completed" else "open"
            connection.execute("UPDATE quality_incidents SET state=?,resolved_at=?,version=version+1 WHERE id=?", (incident_state, now if result == "completed" else None, session["incident_id"]))
            from app.network.operations import sync_session_drain_outcome
            sync_session_drain_outcome(connection, session_id, now)
            self._event(connection, session_id, result, actor, {"reason": reason}, now)
            return NetworkRepository(connection).session_detail(session_id)

    def expire_sessions(self, actor: str = "session-reaper") -> dict[str, Any]:
        now = to_storage(self.clock.now())
        rows = self.connection.execute("SELECT id FROM acceleration_sessions WHERE status='active' AND expires_at<=? ORDER BY id", (now,)).fetchall()
        expired = []
        for row in rows:
            with transaction(immediate=True) as connection:
                session = NetworkRepository(connection).session_by_id(row["id"])
                if session is None or session["status"] != "active":
                    continue
                connection.execute("UPDATE acceleration_sessions SET status='expired',ended_at=?,end_reason='duration_elapsed',version=version+1 WHERE id=?", (now, row["id"]))
                connection.execute("UPDATE capacity_reservations SET state='released',released_at=? WHERE session_id=? AND state='held'", (now, row["id"]))
                connection.execute("UPDATE quality_incidents SET state='open',version=version+1 WHERE id=?", (session["incident_id"],))
                from app.network.operations import sync_session_drain_outcome
                sync_session_drain_outcome(connection, row["id"], now)
                self._event(connection, row["id"], "expired", actor, {}, now)
                expired.append(row["id"])
        return {"expired": expired}

    def open_incidents(self, scenario_code: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        scenario_id = self._scenario(scenario_code)["id"] if scenario_code else None
        return self.repository.open_incidents(scenario_id, limit=limit)

    def get_session(self, session_id: int) -> dict[str, Any]:
        result = self.repository.session_detail(session_id)
        if result is None:
            raise NotFoundError("加速会话不存在")
        return result

    def summary(self) -> dict[str, Any]:
        return self.repository.summary()

    def seed_demo(self) -> dict[str, Any]:
        scenario = self.repository.scenario_by_code("gdh-rail")
        if scenario is None:
            scenario = self.create_scenario({"code": "gdh-rail", "name": "广深高铁", "scene_type": "railway", "timezone": "Asia/Shanghai", "max_concurrent_sessions": 5000, "capacity_mbps": 3000})
            self.add_segment("gdh-rail", {"code": "gz-sz-01", "name": "广州南至虎门", "sequence_no": 1, "expected_dwell_seconds": 900, "capacity_mbps": 1200})
        app = self.repository.application_by_code("video-call")
        if app is None:
            app = self.create_application({"app_code": "video-call", "name": "视频通话", "category": "video_call", "latency_target_ms": 100, "packet_loss_target": 0.01, "min_downlink_mbps": 8, "min_uplink_mbps": 4, "default_priority": 70})
        policy = self.create_policy("gdh-rail", DEFAULT_RULES, "demo")
        if policy["state"] != "published":
            policy = self.publish_policy(policy["id"], "demo", to_storage(self.clock.now()))
        return {"scenario": scenario, "application": app, "policy": policy}

    def _sample_result(self, sample_id: int) -> dict[str, Any]:
        sample = self.repository.sample_by_id(sample_id)
        incident = self.repository.incident_by_sample(sample_id)
        return {"sample_id": sample_id, "incident_id": incident["id"] if incident else None, "duplicate": True, "sample": dict(sample)}

    def _scenario(self, code: str) -> sqlite3.Row:
        row = self.repository.scenario_by_code(code)
        if row is None:
            raise NotFoundError("网络场景不存在")
        return row

    def _application(self, code: str) -> sqlite3.Row:
        row = self.repository.application_by_code(code)
        if row is None:
            raise NotFoundError("应用画像不存在")
        return row

    @staticmethod
    def _event(connection: sqlite3.Connection, session_id: int, event_type: str, actor: str, detail: dict[str, Any], now: str) -> None:
        connection.execute(
            "INSERT INTO session_events(session_id,event_type,actor,detail_json,created_at) VALUES(?,?,?,?,?)",
            (session_id, event_type, actor, json.dumps(detail, ensure_ascii=False, sort_keys=True), now),
        )
