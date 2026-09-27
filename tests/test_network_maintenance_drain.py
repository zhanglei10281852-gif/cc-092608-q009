from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.clock import FrozenClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection
from app.network.operations import NetworkOperationsService
from app.network.rules import DEFAULT_RULES
from app.network.service import NetworkAccelerationService

T0 = datetime(2026, 10, 1, 8, 0, 0, tzinfo=UTC)


def prepare_rail(client, code: str = "rail-drain") -> None:
    scenario = client.post(
        "/api/network/scenarios",
        json={"code": code, "name": "测试高铁", "scene_type": "railway", "timezone": "Asia/Shanghai", "max_concurrent_sessions": 100, "capacity_mbps": 2000},
    )
    assert scenario.status_code == 201, scenario.text
    for sequence in (1, 2, 3):
        segment = client.post(
            f"/api/network/scenarios/{code}/segments",
            json={"code": f"seg-{sequence}", "name": f"区段{sequence}", "sequence_no": sequence, "expected_dwell_seconds": 600, "capacity_mbps": 400},
        )
        assert segment.status_code == 201, segment.text
    applications = client.get("/api/network/applications").json()["items"]
    if not any(item["app_code"] == "live-stream" for item in applications):
        app = client.post(
            "/api/network/applications",
            json={"app_code": "live-stream", "name": "移动直播", "category": "live", "latency_target_ms": 120, "packet_loss_target": 0.02, "min_downlink_mbps": 10, "min_uplink_mbps": 8, "default_priority": 75},
        )
        assert app.status_code == 201, app.text
    policy = client.post(f"/api/network/scenarios/{code}/policies", json={"rules": DEFAULT_RULES, "actor": "tests"})
    assert policy.status_code == 201, policy.text
    published = client.post(f"/api/network/policies/{policy.json()['id']}/publish", json={"actor": "tests", "effective_from": "2026-09-26T00:00:00Z"})
    assert published.status_code == 200, published.text


def make_incident(client, scenario: str, index: int, segment_code: str) -> int:
    subscriber = f"subscriber-drain-{scenario}-{index:04d}"
    entitlement = client.post(
        "/api/network/entitlements",
        json={"subscriber_hash": subscriber, "scenario_code": scenario, "product_code": "rail-boost", "valid_from": "2026-09-26T00:00:00Z", "valid_until": "2036-09-27T00:00:00Z", "source_order_id": f"drain-order-{scenario}-{index:04d}"},
    )
    assert entitlement.status_code == 201, entitlement.text
    sample = client.post(
        "/api/network/samples",
        json={"sample_key": f"drain-sample-{scenario}-{index:04d}", "scenario_code": scenario, "segment_code": segment_code, "app_code": "live-stream", "subscriber_hash": subscriber, "device_class": "phone", "train_speed_kmh": 300, "latency_ms": 500, "packet_loss": 0.2, "downlink_mbps": 1, "uplink_mbps": 0.2, "observed_at": "2026-10-01T07:00:00Z"},
    )
    assert sample.status_code == 202, sample.text
    return sample.json()["incident_id"]


def start_session(client, scenario: str, index: int, segment_code: str, moment: datetime) -> dict:
    incident_id = make_incident(client, scenario, index, segment_code)
    return NetworkAccelerationService(get_connection(), FrozenClock(moment)).start_acceleration(incident_id, "tests")


def ops(moment: datetime) -> NetworkOperationsService:
    return NetworkOperationsService(get_connection(), FrozenClock(moment))


def accel(moment: datetime) -> NetworkAccelerationService:
    return NetworkAccelerationService(get_connection(), FrozenClock(moment))


def create_window(moment: datetime, scenario: str, code: str, segment: str | None, mode: str, starts_at: datetime, ends_at: datetime, grace: int = 300) -> dict:
    payload = {
        "scenario_code": scenario,
        "code": code,
        "reason": "沿线设备升级",
        "starts_at": to_storage(starts_at),
        "ends_at": to_storage(ends_at),
        "drain_mode": mode,
        "grace_period_seconds": grace,
        "actor": "operator",
    }
    if segment:
        payload["segment_code"] = segment
    return ops(moment).create_maintenance(payload)


def held_capacity(client, scenario: str) -> dict[str, float]:
    items = client.get("/api/network/analytics/capacity").json()["items"]
    return {item["segment_code"]: item["held_downlink_mbps"] for item in items if item["scenario_code"] == scenario}


def test_finish_active_waits_for_natural_endings_and_resumes_candidates(client):
    prepare_rail(client)
    session_a = start_session(client, "rail-drain", 1, "seg-2", T0)
    session_b = start_session(client, "rail-drain", 2, "seg-2", T0)
    window = create_window(T0, "rail-drain", "drain-finish", "seg-2", "finish_active", T0 + timedelta(minutes=1), T0 + timedelta(hours=1))

    early = ops(T0 + timedelta(seconds=30)).activate_due_maintenance("tests")
    assert early["draining"] == [] and early["activated"] == []

    started = ops(T0 + timedelta(minutes=1)).activate_due_maintenance("tests")
    assert started["draining"] == [window["id"]]
    assert started["activated"] == []
    detail = ops(T0 + timedelta(minutes=1)).maintenance_detail(window["id"])
    assert detail["state"] == "draining"
    assert detail["drain_deadline_at"] is None
    assert detail["drain_summary"] == {"total": 2, "pending": 2, "by_result": {"pending": 2}}
    deadlines = {row["session_id"]: row["deadline_at"] for row in detail["drain_sessions"]}
    assert deadlines == {session_a["id"]: session_a["expires_at"], session_b["id"]: session_b["expires_at"]}
    assert {row["action"] for row in detail["drain_sessions"]} == {"wait"}

    # 排空期间新申请被冻结，质差事件成为候补
    incident_c = make_incident(client, "rail-drain", 3, "seg-2")
    with pytest.raises(ConflictError) as blocked:
        accel(T0 + timedelta(minutes=2)).start_acceleration(incident_c, "tests")
    assert blocked.value.context["maintenance_code"] == "drain-finish"
    assert blocked.value.context["drain_mode"] == "finish_active"

    accel(T0 + timedelta(minutes=3)).finish_session(session_a["id"], "tests", "体验恢复", "completed")
    still_draining = ops(T0 + timedelta(minutes=4)).activate_due_maintenance("tests")
    assert still_draining["activated"] == []
    detail = ops(T0 + timedelta(minutes=4)).maintenance_detail(window["id"])
    rows = {row["session_id"]: row for row in detail["drain_sessions"]}
    assert rows[session_a["id"]]["result"] == "completed"
    assert rows[session_a["id"]]["resolved_at"] == to_storage(T0 + timedelta(minutes=3))
    assert rows[session_b["id"]]["result"] == "pending"

    accel(T0 + timedelta(minutes=5)).finish_session(session_b["id"], "tests", "体验恢复", "completed")
    drained = ops(T0 + timedelta(minutes=6)).activate_due_maintenance("tests")
    assert drained["activated"] == [window["id"]]

    finished = ops(T0 + timedelta(hours=1)).activate_due_maintenance("tests")
    assert finished["completed"] == [window["id"]]
    detail = ops(T0 + timedelta(hours=1)).maintenance_detail(window["id"])
    assert detail["state"] == "completed"
    assert [event["event_type"] for event in detail["events"]] == ["scheduled", "drain_started", "activated", "completed"]
    assert detail["events"][-1]["detail"]["activated"] is True
    assert detail["events"][-1]["detail"]["resumed_candidate_incidents"] == [incident_c]
    assert detail["drain_summary"]["pending"] == 0
    assert held_capacity(client, "rail-drain") == {"seg-1": 0, "seg-2": 0, "seg-3": 0}


def test_cancel_active_cancels_after_grace_and_releases_reservations(client):
    prepare_rail(client)
    session_a = start_session(client, "rail-drain", 1, "seg-2", T0)
    session_b = start_session(client, "rail-drain", 2, "seg-3", T0)
    window = create_window(T0, "rail-drain", "drain-force", None, "cancel_active", T0 + timedelta(minutes=1), T0 + timedelta(hours=1), grace=300)

    started = ops(T0 + timedelta(minutes=1)).activate_due_maintenance("tests")
    assert started["draining"] == [window["id"]]
    assert started["activated"] == []
    detail = ops(T0 + timedelta(minutes=1)).maintenance_detail(window["id"])
    assert detail["drain_deadline_at"] == to_storage(T0 + timedelta(minutes=6))
    assert {row["action"] for row in detail["drain_sessions"]} == {"cancel"}
    assert {row["deadline_at"] for row in detail["drain_sessions"]} == {to_storage(T0 + timedelta(minutes=6))}

    waiting = ops(T0 + timedelta(minutes=4)).activate_due_maintenance("tests")
    assert waiting["activated"] == [] and waiting["force_cancelled"] == []
    assert accel(T0 + timedelta(minutes=4)).get_session(session_a["id"])["status"] == "active"
    assert held_capacity(client, "rail-drain")["seg-2"] == 20

    enforced = ops(T0 + timedelta(minutes=7)).activate_due_maintenance("tests")
    assert enforced["activated"] == [window["id"]]
    assert sorted(enforced["force_cancelled"]) == sorted([session_a["id"], session_b["id"]])
    for session in (session_a, session_b):
        ended = accel(T0 + timedelta(minutes=7)).get_session(session["id"])
        assert ended["status"] == "cancelled"
        assert ended["end_reason"] == "maintenance_drain"
        assert ended["reservation"]["state"] == "released"
        assert [event["event_type"] for event in ended["events"]] == ["started", "cancelled"]
        assert ended["events"][-1]["detail"]["reason"] == "grace_period_elapsed"
        assert ended["events"][-1]["detail"]["maintenance_code"] == "drain-force"
        incident = get_connection().execute("SELECT state FROM quality_incidents WHERE id=?", (session["incident_id"],)).fetchone()
        assert incident["state"] == "open"
    assert held_capacity(client, "rail-drain") == {"seg-1": 0, "seg-2": 0, "seg-3": 0}

    detail = ops(T0 + timedelta(minutes=7)).maintenance_detail(window["id"])
    assert {row["result"] for row in detail["drain_sessions"]} == {"cancelled"}
    assert {row["resolved_at"] for row in detail["drain_sessions"]} == {to_storage(T0 + timedelta(minutes=7))}

    finished = ops(T0 + timedelta(hours=1)).activate_due_maintenance("tests")
    assert finished["completed"] == [window["id"]]
    detail = ops(T0 + timedelta(hours=1)).maintenance_detail(window["id"])
    assert [event["event_type"] for event in detail["events"]] == ["scheduled", "drain_started", "session_cancelled", "session_cancelled", "activated", "completed"]
    resumed = detail["events"][-1]["detail"]["resumed_candidate_incidents"]
    assert sorted(resumed) == sorted([session_a["incident_id"], session_b["incident_id"]])


def test_cancel_active_migrates_to_adjacent_segments_before_cancelling(client):
    prepare_rail(client)
    connection = get_connection()
    connection.execute("UPDATE network_segments SET capacity_mbps=20 WHERE code IN ('seg-1','seg-3')")
    session_a = start_session(client, "rail-drain", 1, "seg-2", T0)
    session_b = start_session(client, "rail-drain", 2, "seg-2", T0)
    session_c = start_session(client, "rail-drain", 3, "seg-2", T0)
    window = create_window(T0, "rail-drain", "drain-migrate", "seg-2", "cancel_active", T0 + timedelta(minutes=1), T0 + timedelta(hours=1), grace=300)

    started = ops(T0 + timedelta(minutes=1)).activate_due_maintenance("tests")
    assert started["draining"] == [window["id"]]
    assert started["migrated"] == [session_a["id"], session_b["id"]]
    assert started["activated"] == []

    segments = {row["code"]: row["id"] for row in connection.execute("SELECT id,code FROM network_segments").fetchall()}
    moved_a = accel(T0 + timedelta(minutes=1)).get_session(session_a["id"])
    moved_b = accel(T0 + timedelta(minutes=1)).get_session(session_b["id"])
    assert moved_a["status"] == "active" and moved_a["segment_id"] == segments["seg-3"]
    assert moved_b["status"] == "active" and moved_b["segment_id"] == segments["seg-1"]
    assert moved_a["events"][-1]["event_type"] == "migrated"
    assert moved_a["events"][-1]["detail"]["to_segment_code"] == "seg-3"
    assert moved_a["events"][-1]["detail"]["maintenance_code"] == "drain-migrate"
    assert moved_a["reservation"]["state"] == "held"
    assert moved_a["reservation"]["segment_id"] == segments["seg-3"]

    detail = ops(T0 + timedelta(minutes=1)).maintenance_detail(window["id"])
    rows = {row["session_id"]: row for row in detail["drain_sessions"]}
    assert rows[session_a["id"]]["result"] == "migrated" and rows[session_a["id"]]["target_segment_code"] == "seg-3"
    assert rows[session_b["id"]]["result"] == "migrated" and rows[session_b["id"]]["target_segment_code"] == "seg-1"
    assert rows[session_c["id"]]["result"] == "pending" and rows[session_c["id"]]["action"] == "cancel"
    assert held_capacity(client, "rail-drain") == {"seg-1": 20, "seg-2": 20, "seg-3": 20}

    enforced = ops(T0 + timedelta(minutes=7)).activate_due_maintenance("tests")
    assert enforced["force_cancelled"] == [session_c["id"]]
    assert enforced["activated"] == [window["id"]]
    assert held_capacity(client, "rail-drain") == {"seg-1": 20, "seg-2": 0, "seg-3": 20}
    detail = ops(T0 + timedelta(minutes=7)).maintenance_detail(window["id"])
    assert [event["event_type"] for event in detail["events"]] == ["scheduled", "drain_started", "session_migrated", "session_migrated", "session_cancelled", "activated"]


def test_migration_skips_segments_under_maintenance(client):
    prepare_rail(client)
    get_connection().execute("UPDATE network_segments SET capacity_mbps=5 WHERE code='seg-3'")
    session = start_session(client, "rail-drain", 1, "seg-2", T0)
    create_window(T0, "rail-drain", "neighbor-window", "seg-1", "block_new", T0, T0 + timedelta(hours=2))
    window = create_window(T0, "rail-drain", "drain-no-target", "seg-2", "cancel_active", T0 + timedelta(minutes=1), T0 + timedelta(hours=1), grace=60)

    started = ops(T0 + timedelta(minutes=1)).activate_due_maintenance("tests")
    assert started["migrated"] == []
    detail = ops(T0 + timedelta(minutes=1)).maintenance_detail(window["id"])
    assert detail["drain_sessions"][0]["action"] == "cancel"

    enforced = ops(T0 + timedelta(minutes=3)).activate_due_maintenance("tests")
    assert enforced["force_cancelled"] == [session["id"]]
    assert accel(T0 + timedelta(minutes=3)).get_session(session["id"])["status"] == "cancelled"


def test_manual_override_records_and_unblocks_window(client):
    prepare_rail(client)
    session_a = start_session(client, "rail-drain", 1, "seg-2", T0)
    session_b = start_session(client, "rail-drain", 2, "seg-2", T0)
    window = create_window(T0, "rail-drain", "drain-override", "seg-2", "finish_active", T0 + timedelta(minutes=1), T0 + timedelta(hours=1))
    ops(T0 + timedelta(minutes=1)).activate_due_maintenance("tests")

    kept = ops(T0 + timedelta(minutes=2)).override_drain_session(window["id"], session_a["id"], "ops-lead", "wait", "重点客户保障")
    assert kept["state"] == "draining"
    rows = {row["session_id"]: row for row in kept["drain_sessions"]}
    assert rows[session_a["id"]]["result"] == "kept"
    assert rows[session_a["id"]]["overridden_by"] == "ops-lead"
    assert rows[session_a["id"]]["override_reason"] == "重点客户保障"
    assert rows[session_a["id"]]["overridden_at"] == to_storage(T0 + timedelta(minutes=2))

    done = ops(T0 + timedelta(minutes=3)).override_drain_session(window["id"], session_b["id"], "ops-lead", "cancel", "紧急施工")
    assert done["state"] == "active"
    cancelled = accel(T0 + timedelta(minutes=3)).get_session(session_b["id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["reservation"]["state"] == "released"
    rows = {row["session_id"]: row for row in done["drain_sessions"]}
    assert rows[session_b["id"]]["result"] == "cancelled"
    assert rows[session_b["id"]]["overridden_by"] == "ops-lead"
    assert [event["event_type"] for event in done["events"]] == ["scheduled", "drain_started", "session_overridden", "session_cancelled", "session_overridden", "activated"]
    assert done["events"][-1]["detail"]["trigger"] == "manual_override"

    # 被保留的会话不受排空影响，结束后记录照常归档
    assert accel(T0 + timedelta(minutes=3)).get_session(session_a["id"])["status"] == "active"
    accel(T0 + timedelta(minutes=4)).finish_session(session_a["id"], "tests", "体验恢复", "completed")
    detail = ops(T0 + timedelta(minutes=4)).maintenance_detail(window["id"])
    rows = {row["session_id"]: row for row in detail["drain_sessions"]}
    assert rows[session_a["id"]]["result"] == "kept"
    assert rows[session_a["id"]]["session_status"] == "completed"


def test_override_validation_errors(client):
    prepare_rail(client)
    session = start_session(client, "rail-drain", 1, "seg-2", T0)
    window = create_window(T0, "rail-drain", "drain-errors", "seg-2", "finish_active", T0 + timedelta(minutes=1), T0 + timedelta(hours=1))

    with pytest.raises(ConflictError):
        ops(T0).override_drain_session(window["id"], session["id"], "ops", "cancel", "尚未排空")
    with pytest.raises(NotFoundError):
        ops(T0).override_drain_session(999999, session["id"], "ops", "cancel", "窗口不存在")

    ops(T0 + timedelta(minutes=1)).activate_due_maintenance("tests")
    with pytest.raises(NotFoundError):
        ops(T0 + timedelta(minutes=2)).override_drain_session(window["id"], 999999, "ops", "cancel", "会话不存在")
    with pytest.raises(ValidationError):
        ops(T0 + timedelta(minutes=2)).override_drain_session(window["id"], session["id"], "ops", "restart", "非法动作")

    ops(T0 + timedelta(minutes=2)).override_drain_session(window["id"], session["id"], "ops", "cancel", "紧急施工")
    with pytest.raises(ConflictError):
        ops(T0 + timedelta(minutes=3)).override_drain_session(window["id"], session["id"], "ops", "wait", "重复覆盖")

    # 场景级窗口没有可迁移的相邻区段
    session_two = start_session(client, "rail-drain", 2, "seg-2", T0 + timedelta(hours=2))
    later = create_window(T0 + timedelta(hours=2), "rail-drain", "drain-scenario", None, "cancel_active", T0 + timedelta(hours=2), T0 + timedelta(hours=3), grace=600)
    ops(T0 + timedelta(hours=2)).activate_due_maintenance("tests")
    with pytest.raises(ConflictError) as denied:
        ops(T0 + timedelta(hours=2)).override_drain_session(later["id"], session_two["id"], "ops", "migrate", "无处可去")
    assert "相邻区段" in denied.value.message


def test_expired_sessions_resolve_drain_rows(client):
    prepare_rail(client)
    session = start_session(client, "rail-drain", 1, "seg-2", T0)
    window = create_window(T0, "rail-drain", "drain-expire", "seg-2", "finish_active", T0 + timedelta(minutes=1), T0 + timedelta(hours=1))
    ops(T0 + timedelta(minutes=1)).activate_due_maintenance("tests")

    expired = accel(T0 + timedelta(minutes=4)).expire_sessions("tests")
    assert session["id"] in expired["expired"]
    detail = ops(T0 + timedelta(minutes=4)).maintenance_detail(window["id"])
    assert detail["drain_sessions"][0]["result"] == "expired"
    assert detail["drain_sessions"][0]["resolved_at"] == to_storage(T0 + timedelta(minutes=4))

    activated = ops(T0 + timedelta(minutes=5)).activate_due_maintenance("tests")
    assert activated["activated"] == [window["id"]]


def test_block_new_window_activates_immediately_and_promotion_resumes(client):
    prepare_rail(client)
    session = start_session(client, "rail-drain", 1, "seg-2", T0)
    window = create_window(T0, "rail-drain", "drain-block", "seg-2", "block_new", T0 + timedelta(minutes=1), T0 + timedelta(minutes=30))

    started = ops(T0 + timedelta(minutes=1)).activate_due_maintenance("tests")
    assert started["draining"] == [window["id"]]
    assert started["activated"] == [window["id"]]
    detail = ops(T0 + timedelta(minutes=1)).maintenance_detail(window["id"])
    assert detail["drain_sessions"][0]["action"] == "wait"
    assert detail["drain_sessions"][0]["result"] == "pending"
    assert accel(T0 + timedelta(minutes=1)).get_session(session["id"])["status"] == "active"

    incident = make_incident(client, "rail-drain", 2, "seg-2")
    with pytest.raises(ConflictError):
        accel(T0 + timedelta(minutes=2)).start_acceleration(incident, "tests")

    finished = ops(T0 + timedelta(minutes=30)).activate_due_maintenance("tests")
    assert finished["completed"] == [window["id"]]
    detail = ops(T0 + timedelta(minutes=30)).maintenance_detail(window["id"])
    assert detail["events"][-1]["detail"]["resumed_candidate_incidents"] == [incident]

    promoted = accel(T0 + timedelta(minutes=31)).start_acceleration(incident, "tests")
    assert promoted["status"] == "active"

    accel(T0 + timedelta(minutes=40)).finish_session(session["id"], "tests", "体验恢复", "completed")
    detail = ops(T0 + timedelta(minutes=40)).maintenance_detail(window["id"])
    assert detail["drain_sessions"][0]["result"] == "completed"


def test_drain_outcome_consistent_across_ending_orders(client):
    prepare_rail(client, "rail-a")
    prepare_rail(client, "rail-b")
    sessions: dict[str, tuple[dict, dict]] = {}
    windows: dict[str, dict] = {}
    for code in ("rail-a", "rail-b"):
        first = start_session(client, code, 1, "seg-2", T0)
        second = start_session(client, code, 2, "seg-2", T0)
        sessions[code] = (first, second)
        windows[code] = create_window(T0, code, f"drain-{code}", "seg-2", "finish_active", T0 + timedelta(minutes=1), T0 + timedelta(hours=1))
        ops(T0 + timedelta(minutes=1)).activate_due_maintenance("tests")

    # 两个场景以相反顺序结束会话，固定时钟下的最终状态、容量与时间线必须一致
    accel(T0 + timedelta(minutes=3)).finish_session(sessions["rail-a"][0]["id"], "tests", "恢复", "completed")
    accel(T0 + timedelta(minutes=5)).finish_session(sessions["rail-a"][1]["id"], "tests", "恢复", "completed")
    accel(T0 + timedelta(minutes=5)).finish_session(sessions["rail-b"][1]["id"], "tests", "恢复", "completed")
    accel(T0 + timedelta(minutes=3)).finish_session(sessions["rail-b"][0]["id"], "tests", "恢复", "completed")

    drained = ops(T0 + timedelta(minutes=6)).activate_due_maintenance("tests")
    assert sorted(drained["activated"]) == sorted(window["id"] for window in windows.values())
    finished = ops(T0 + timedelta(hours=1)).activate_due_maintenance("tests")
    assert sorted(finished["completed"]) == sorted(window["id"] for window in windows.values())
    details = {code: ops(T0 + timedelta(hours=1)).maintenance_detail(windows[code]["id"]) for code in ("rail-a", "rail-b")}

    for code in ("rail-a", "rail-b"):
        detail = details[code]
        assert detail["state"] == "completed"
        assert [event["event_type"] for event in detail["events"]] == ["scheduled", "drain_started", "activated", "completed"]
        assert detail["drain_summary"]["pending"] == 0
        assert {row["result"] for row in detail["drain_sessions"]} == {"completed"}
        for row in detail["drain_sessions"]:
            ended = accel(T0 + timedelta(hours=1)).get_session(row["session_id"])
            assert ended["status"] == "completed"
            assert row["resolved_at"] == ended["ended_at"]
            assert [event["event_type"] for event in ended["events"]] == ["started", "completed"]

    def normalized(detail: dict) -> list[tuple]:
        return sorted((row["action"], row["result"], row["deadline_at"]) for row in detail["drain_sessions"])

    assert normalized(details["rail-a"]) == normalized(details["rail-b"])
    assert held_capacity(client, "rail-a") == held_capacity(client, "rail-b") == {"seg-1": 0, "seg-2": 0, "seg-3": 0}


def test_maintenance_drain_via_api(client):
    prepare_rail(client)
    incident = make_incident(client, "rail-drain", 1, "seg-2")
    started = client.post(f"/api/network/incidents/{incident}/accelerate", json={"actor": "tests"})
    assert started.status_code == 200, started.text
    session_id = started.json()["id"]

    created = client.post(
        "/api/network/operations/maintenance",
        json={"scenario_code": "rail-drain", "segment_code": "seg-2", "code": "api-drain", "reason": "设备升级", "starts_at": "2026-09-26T00:00:00Z", "ends_at": "2036-09-26T00:00:00Z", "drain_mode": "finish_active", "grace_period_seconds": 120, "actor": "operator"},
    )
    assert created.status_code == 201, created.text
    window_id = created.json()["id"]
    assert created.json()["grace_period_seconds"] == 120
    assert created.json()["drain_sessions"] == []

    advanced = client.post("/api/network/operations/maintenance/advance")
    assert advanced.status_code == 200
    assert advanced.json()["draining"] == [window_id]
    assert advanced.json()["activated"] == []

    overridden = client.post(
        f"/api/network/operations/maintenance/{window_id}/sessions/{session_id}/override",
        json={"actor": "ops-lead", "action": "cancel", "reason": "紧急施工"},
    )
    assert overridden.status_code == 200, overridden.text
    assert overridden.json()["state"] == "active"
    row = overridden.json()["drain_sessions"][0]
    assert row["session_id"] == session_id
    assert row["result"] == "cancelled"
    assert row["overridden_by"] == "ops-lead"

    detail = client.get(f"/api/network/operations/maintenance/{window_id}").json()
    assert detail["drain_summary"]["pending"] == 0
    assert [event["event_type"] for event in detail["events"]] == ["scheduled", "drain_started", "session_cancelled", "session_overridden", "activated"]

    missing = client.post(
        f"/api/network/operations/maintenance/{window_id}/sessions/{session_id}/override",
        json={"actor": "ops-lead", "action": "wait", "reason": "重复覆盖"},
    )
    assert missing.status_code == 409
    unknown = client.post(
        "/api/network/operations/maintenance/999999/sessions/1/override",
        json={"actor": "ops-lead", "action": "cancel", "reason": "窗口不存在"},
    )
    assert unknown.status_code == 404
