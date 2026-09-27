from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.clock import FrozenClock, to_storage
from app.core.errors import ConflictError
from app.database import get_connection
from app.network.operations import NetworkOperationsService
from app.network.repository import NetworkRepository
from app.network.rules import DEFAULT_RULES
from app.network.service import NetworkAccelerationService

BASE = datetime(2026, 9, 26, 8, 0, tzinfo=UTC)


def ts(**values) -> str:
    return to_storage(BASE + timedelta(**values))


def prepare(client, scenario_code: str, segments=(("s1", 1, 400), ("s2", 2, 400), ("s3", 3, 400))) -> str:
    response = client.post(
        "/api/network/scenarios",
        json={"code": scenario_code, "name": "城际铁路", "scene_type": "railway", "timezone": "Asia/Shanghai", "max_concurrent_sessions": 50, "capacity_mbps": 2000},
    )
    assert response.status_code == 201, response.text
    for code, sequence, capacity in segments:
        response = client.post(
            f"/api/network/scenarios/{scenario_code}/segments",
            json={"code": code, "name": f"{code}-segment", "sequence_no": sequence, "expected_dwell_seconds": 600, "capacity_mbps": capacity},
        )
        assert response.status_code == 201, response.text
    app_code = f"game-{scenario_code}"
    response = client.post(
        "/api/network/applications",
        json={"app_code": app_code, "name": "手游", "category": "game", "latency_target_ms": 60, "packet_loss_target": 0.02, "min_downlink_mbps": 10, "min_uplink_mbps": 5, "default_priority": 60},
    )
    assert response.status_code == 201, response.text
    policy = client.post(f"/api/network/scenarios/{scenario_code}/policies", json={"rules": DEFAULT_RULES, "actor": "tests"}).json()
    published = client.post(f"/api/network/policies/{policy['id']}/publish", json={"actor": "tests", "effective_from": "2026-09-26T00:00:00Z"})
    assert published.status_code == 200, published.text
    return app_code


def make_incident(client, scenario_code: str, segment_code: str, app_code: str, tag: str) -> int:
    subscriber = f"subscriber-{tag}".ljust(20, "0")
    response = client.post(
        "/api/network/entitlements",
        json={"subscriber_hash": subscriber, "scenario_code": scenario_code, "product_code": "rail-boost", "valid_from": "2026-09-26T00:00:00Z", "valid_until": "2030-01-01T00:00:00Z", "source_order_id": f"order-{tag}"},
    )
    assert response.status_code == 201, response.text
    sample = client.post(
        "/api/network/samples",
        json={"sample_key": f"sample-{tag}", "scenario_code": scenario_code, "segment_code": segment_code, "app_code": app_code, "subscriber_hash": subscriber, "device_class": "phone", "train_speed_kmh": 300, "latency_ms": 400, "packet_loss": 0.2, "downlink_mbps": 1.0, "uplink_mbps": 0.3, "observed_at": "2026-09-26T07:59:00Z"},
    ).json()
    assert sample["incident_id"] is not None
    return int(sample["incident_id"])


def start_session(client, clock: FrozenClock, scenario_code: str, segment_code: str, app_code: str, tag: str) -> dict:
    incident_id = make_incident(client, scenario_code, segment_code, app_code, tag)
    return NetworkAccelerationService(get_connection(), clock).start_acceleration(incident_id, "tests")


def create_window(ops: NetworkOperationsService, scenario_code: str, segment_code: str | None, code: str, drain_mode: str, grace: int = 300, start=None, end=None) -> dict:
    return ops.create_maintenance({
        "scenario_code": scenario_code,
        "segment_code": segment_code,
        "code": code,
        "reason": "高铁沿线设备升级",
        "starts_at": start or ts(minutes=1),
        "ends_at": end or ts(minutes=10),
        "drain_mode": drain_mode,
        "grace_period_seconds": grace,
        "actor": "operator",
    })


def segment_ids(scenario_id: int) -> dict[str, int]:
    rows = get_connection().execute("SELECT id,code FROM network_segments WHERE scenario_id=?", (scenario_id,)).fetchall()
    return {row["code"]: int(row["id"]) for row in rows}


def test_finish_active_drains_before_activation_and_resumes_candidates(client):
    app_code = prepare(client, "rail-01")
    clock = FrozenClock(BASE)
    accel = NetworkAccelerationService(get_connection(), clock)
    ops = NetworkOperationsService(get_connection(), clock)
    first = start_session(client, clock, "rail-01", "s1", app_code, "f1")
    second = start_session(client, clock, "rail-01", "s1", app_code, "f2")
    waiting_incident = make_incident(client, "rail-01", "s1", app_code, "f3")
    window = create_window(ops, "rail-01", None, "window-finish", "finish_active")

    preview = ops.drain_status(window["id"])
    assert preview["preview"] is True
    assert preview["drained"] is False
    assert preview["grace_deadline"] == ts(minutes=6)
    assert {item["session_id"] for item in preview["sessions"]} == {first["id"], second["id"]}
    assert all(item["action"] == "awaiting" for item in preview["sessions"])
    assert all(item["deadline_at"] == first["expires_at"] for item in preview["sessions"])
    assert preview["drain_complete_by"] == first["expires_at"]

    clock.current = BASE + timedelta(minutes=1, seconds=30)
    with pytest.raises(ConflictError):
        accel.start_acceleration(waiting_incident, "tests")
    result = ops.activate_due_maintenance("scheduler")
    assert window["id"] in result["draining"]
    assert window["id"] not in result["activated"]
    status = ops.drain_status(window["id"])
    assert status["state"] == "draining"
    assert status["preview"] is False
    assert status["summary"]["awaiting"] == 2
    assert status["grace_deadline"] == ts(minutes=6, seconds=30)

    clock.current = BASE + timedelta(minutes=2)
    accel.finish_session(first["id"], "tests", "体验恢复", "completed")
    result = ops.activate_due_maintenance("scheduler")
    assert window["id"] in result["draining"]
    assert accel.get_session(second["id"])["status"] == "active"

    clock.current = BASE + timedelta(minutes=3, seconds=1)
    assert second["id"] in accel.expire_sessions("tests")["expired"]
    result = ops.activate_due_maintenance("scheduler")
    assert window["id"] in result["activated"]
    detail = ops.maintenance_detail(window["id"])
    assert detail["state"] == "active"
    assert detail["activated_at"] == ts(minutes=3, seconds=1)
    actions = {row["session_id"]: row for row in detail["session_actions"]}
    assert actions[first["id"]]["action"] == "completed"
    assert actions[first["id"]]["resolved_at"] == ts(minutes=2)
    assert actions[second["id"]]["action"] == "expired"
    assert actions[second["id"]]["resolved_at"] == ts(minutes=3, seconds=1)
    repository = NetworkRepository(get_connection())
    assert repository.active_capacity(window["scenario_id"], segment_ids(window["scenario_id"])["s1"])["sessions"] == 0

    clock.current = BASE + timedelta(minutes=10)
    result = ops.activate_due_maintenance("scheduler")
    assert window["id"] in result["completed"]
    assert result["resumed_candidates"][window["id"]] == 2
    assert [event["event_type"] for event in ops.maintenance_detail(window["id"])["events"]] == ["scheduled", "drain_started", "activated", "completed"]

    promoted = accel.start_acceleration(waiting_incident, "tests")
    assert promoted["status"] == "active"


def test_cancel_active_cancels_after_grace_and_releases_reservations(client):
    app_code = prepare(client, "rail-02")
    clock = FrozenClock(BASE)
    accel = NetworkAccelerationService(get_connection(), clock)
    ops = NetworkOperationsService(get_connection(), clock)
    first = start_session(client, clock, "rail-02", "s1", app_code, "c1")
    second = start_session(client, clock, "rail-02", "s1", app_code, "c2")
    window = create_window(ops, "rail-02", None, "window-cancel", "cancel_active", grace=60)

    clock.current = BASE + timedelta(minutes=1)
    result = ops.activate_due_maintenance("scheduler")
    assert window["id"] in result["draining"]
    status = ops.drain_status(window["id"])
    assert status["grace_deadline"] == ts(minutes=2)
    assert all(item["deadline_at"] == ts(minutes=2) for item in status["sessions"])

    clock.current = BASE + timedelta(minutes=1, seconds=30)
    ops.activate_due_maintenance("scheduler")
    assert accel.get_session(first["id"])["status"] == "active"
    assert accel.get_session(second["id"])["status"] == "active"

    clock.current = BASE + timedelta(minutes=2)
    result = ops.activate_due_maintenance("scheduler")
    assert window["id"] in result["activated"]
    for session_id in (first["id"], second["id"]):
        detail = accel.get_session(session_id)
        assert detail["status"] == "cancelled"
        assert detail["end_reason"] == "maintenance_drain"
        assert detail["ended_at"] == ts(minutes=2)
        assert detail["reservation"]["state"] == "released"
        assert detail["events"][-1]["event_type"] == "cancelled"
    repository = NetworkRepository(get_connection())
    assert repository.active_capacity(window["scenario_id"], segment_ids(window["scenario_id"])["s1"]) == {"downlink_mbps": 0.0, "uplink_mbps": 0.0, "sessions": 0}
    incidents = get_connection().execute("SELECT state FROM quality_incidents").fetchall()
    assert {row["state"] for row in incidents} == {"open"}
    status = ops.drain_status(window["id"])
    assert status["summary"]["cancelled"] == 2
    assert status["drained"] is True

    clock.current = BASE + timedelta(minutes=10)
    result = ops.activate_due_maintenance("scheduler")
    assert result["resumed_candidates"][window["id"]] == 2


def test_drain_migrates_sessions_to_adjacent_segment(client):
    app_code = prepare(client, "rail-03")
    clock = FrozenClock(BASE)
    accel = NetworkAccelerationService(get_connection(), clock)
    ops = NetworkOperationsService(get_connection(), clock)
    first = start_session(client, clock, "rail-03", "s2", app_code, "m1")
    second = start_session(client, clock, "rail-03", "s2", app_code, "m2")
    window = create_window(ops, "rail-03", "s2", "window-migrate", "finish_active")

    clock.current = BASE + timedelta(minutes=1)
    result = ops.activate_due_maintenance("scheduler")
    assert window["id"] in result["activated"]
    segments = segment_ids(window["scenario_id"])
    for session_id in (first["id"], second["id"]):
        detail = accel.get_session(session_id)
        assert detail["status"] == "active"
        assert detail["segment_id"] == segments["s1"]
        assert detail["reservation"]["segment_id"] == segments["s1"]
        assert detail["reservation"]["state"] == "held"
        assert [event["event_type"] for event in detail["events"]] == ["started", "migrated"]
    repository = NetworkRepository(get_connection())
    assert repository.active_capacity(window["scenario_id"], segments["s1"])["downlink_mbps"] == 40.0
    assert repository.active_capacity(window["scenario_id"], segments["s2"])["sessions"] == 0
    detail = ops.maintenance_detail(window["id"])
    assert {row["action"] for row in detail["session_actions"]} == {"migrated"}
    assert all(row["to_segment_id"] == segments["s1"] for row in detail["session_actions"])
    assert all(row["from_segment_id"] == segments["s2"] for row in detail["session_actions"])


def test_migration_falls_back_to_cancel_when_adjacent_capacity_insufficient(client):
    app_code = prepare(client, "rail-04", segments=(("s1", 1, 25), ("s2", 2, 400)))
    clock = FrozenClock(BASE)
    accel = NetworkAccelerationService(get_connection(), clock)
    ops = NetworkOperationsService(get_connection(), clock)
    first = start_session(client, clock, "rail-04", "s2", app_code, "x1")
    second = start_session(client, clock, "rail-04", "s2", app_code, "x2")
    window = create_window(ops, "rail-04", "s2", "window-partial", "cancel_active", grace=60)

    clock.current = BASE + timedelta(minutes=1)
    ops.activate_due_maintenance("scheduler")
    status = ops.drain_status(window["id"])
    by_session = {item["session_id"]: item for item in status["sessions"]}
    assert by_session[first["id"]]["action"] == "migrated"
    assert by_session[second["id"]]["action"] == "awaiting"
    assert by_session[second["id"]]["migratable"] is False

    clock.current = BASE + timedelta(minutes=2)
    result = ops.activate_due_maintenance("scheduler")
    assert window["id"] in result["activated"]
    assert accel.get_session(first["id"])["status"] == "active"
    assert accel.get_session(second["id"])["status"] == "cancelled"
    segments = segment_ids(window["scenario_id"])
    repository = NetworkRepository(get_connection())
    assert repository.active_capacity(window["scenario_id"], segments["s1"])["downlink_mbps"] == 20.0
    assert repository.active_capacity(window["scenario_id"], segments["s2"])["sessions"] == 0


def test_manual_overrides_are_recorded_in_maintenance_detail(client):
    app_code = prepare(client, "rail-05", segments=(("s1", 1, 400), ("s2", 2, 25), ("s3", 3, 400)))
    clock = FrozenClock(BASE)
    accel = NetworkAccelerationService(get_connection(), clock)
    ops = NetworkOperationsService(get_connection(), clock)
    holder = start_session(client, clock, "rail-05", "s2", app_code, "o0")
    first = start_session(client, clock, "rail-05", "s1", app_code, "o1")
    second = start_session(client, clock, "rail-05", "s1", app_code, "o2")
    third = start_session(client, clock, "rail-05", "s1", app_code, "o3")
    window = create_window(ops, "rail-05", "s1", "window-override", "finish_active")

    with pytest.raises(ConflictError):
        ops.override_session(window["id"], first["id"], "keep", "operator", "保留重要客户")

    clock.current = BASE + timedelta(minutes=1)
    ops.activate_due_maintenance("scheduler")
    status = ops.drain_status(window["id"])
    assert status["summary"]["awaiting"] == 3
    assert all(item["migratable"] is False for item in status["sessions"])

    clock.current = BASE + timedelta(minutes=2)
    accel.finish_session(holder["id"], "tests", "体验恢复", "completed")
    detail = ops.override_session(window["id"], first["id"], "migrate", "operator", "迁往相邻区段")
    actions = {row["session_id"]: row for row in detail["session_actions"]}
    segments = segment_ids(window["scenario_id"])
    assert actions[first["id"]]["action"] == "migrated"
    assert actions[first["id"]]["manual"] == 1
    assert actions[first["id"]]["actor"] == "operator"
    assert actions[first["id"]]["to_segment_id"] == segments["s2"]
    assert accel.get_session(first["id"])["segment_id"] == segments["s2"]

    detail = ops.override_session(window["id"], second["id"], "keep", "operator", "保留重要客户")
    actions = {row["session_id"]: row for row in detail["session_actions"]}
    assert actions[second["id"]]["action"] == "exempted"
    assert actions[second["id"]]["manual"] == 1
    assert actions[second["id"]]["reason"] == "保留重要客户"

    ops.override_session(window["id"], third["id"], "cancel", "operator", "紧急维护")
    assert accel.get_session(third["id"])["status"] == "cancelled"
    with pytest.raises(ConflictError):
        ops.override_session(window["id"], third["id"], "keep", "operator", "重复操作")

    result = ops.activate_due_maintenance("scheduler")
    assert window["id"] in result["activated"]
    assert accel.get_session(first["id"])["status"] == "active"
    assert accel.get_session(second["id"])["status"] == "active"
    detail = ops.maintenance_detail(window["id"])
    overrides = [event for event in detail["events"] if event["event_type"] == "session_override"]
    assert [event["detail"]["action"] for event in overrides] == ["migrate", "keep", "cancel"]
    repository = NetworkRepository(get_connection())
    assert repository.active_capacity(window["scenario_id"], segments["s1"])["downlink_mbps"] == 20.0
    assert repository.active_capacity(window["scenario_id"], segments["s2"])["downlink_mbps"] == 20.0


def test_drain_outcome_consistent_across_completion_orders(client):
    app_a = prepare(client, "rail-a")
    app_b = prepare(client, "rail-b")
    clock = FrozenClock(BASE)
    accel = NetworkAccelerationService(get_connection(), clock)
    ops = NetworkOperationsService(get_connection(), clock)
    sessions, windows = {}, {}
    for code, app_code in (("rail-a", app_a), ("rail-b", app_b)):
        sessions[code] = (
            start_session(client, clock, code, "s1", app_code, f"{code}-1"),
            start_session(client, clock, code, "s1", app_code, f"{code}-2"),
        )
        windows[code] = create_window(ops, code, None, f"window-{code}", "finish_active")

    clock.current = BASE + timedelta(minutes=1)
    ops.activate_due_maintenance("scheduler")
    clock.current = BASE + timedelta(minutes=2)
    accel.finish_session(sessions["rail-a"][0]["id"], "tests", "体验恢复", "completed")
    accel.finish_session(sessions["rail-b"][1]["id"], "tests", "体验恢复", "completed")
    clock.current = BASE + timedelta(minutes=2, seconds=30)
    accel.finish_session(sessions["rail-a"][1]["id"], "tests", "体验恢复", "completed")
    accel.finish_session(sessions["rail-b"][0]["id"], "tests", "体验恢复", "completed")

    clock.current = BASE + timedelta(minutes=3)
    result = ops.activate_due_maintenance("scheduler")
    assert set(result["activated"]) == {windows["rail-a"]["id"], windows["rail-b"]["id"]}
    details = {code: ops.maintenance_detail(windows[code]["id"]) for code in ("rail-a", "rail-b")}
    repository = NetworkRepository(get_connection())
    for code in ("rail-a", "rail-b"):
        detail = details[code]
        assert detail["state"] == "active"
        assert detail["activated_at"] == ts(minutes=3)
        assert [event["event_type"] for event in detail["events"]] == ["scheduled", "drain_started", "activated"]
        assert {row["action"] for row in detail["session_actions"]} == {"completed"}
        held = repository.active_capacity(windows[code]["scenario_id"], segment_ids(windows[code]["scenario_id"])["s1"])
        assert held == {"downlink_mbps": 0.0, "uplink_mbps": 0.0, "sessions": 0}
    resolved = {
        code: sorted(row["resolved_at"] for row in details[code]["session_actions"])
        for code in ("rail-a", "rail-b")
    }
    assert resolved["rail-a"] == resolved["rail-b"] == [ts(minutes=2), ts(minutes=2, seconds=30)]


def test_drain_and_override_endpoints(client):
    app_code = prepare(client, "rail-api")
    client.post(
        "/api/network/entitlements",
        json={"subscriber_hash": "subscriber-api-1", "scenario_code": "rail-api", "product_code": "rail-boost", "valid_from": "2026-09-26T00:00:00Z", "valid_until": "2030-01-01T00:00:00Z", "source_order_id": "order-api-1"},
    )
    sample = client.post(
        "/api/network/samples",
        json={"sample_key": "sample-api-1", "scenario_code": "rail-api", "segment_code": "s1", "app_code": app_code, "subscriber_hash": "subscriber-api-1", "device_class": "phone", "train_speed_kmh": 300, "latency_ms": 400, "packet_loss": 0.2, "downlink_mbps": 1.0, "uplink_mbps": 0.3, "observed_at": "2026-09-26T07:59:00Z"},
    ).json()
    started = client.post(f"/api/network/incidents/{sample['incident_id']}/accelerate", json={"actor": "tests"})
    assert started.status_code == 200, started.text
    session_id = started.json()["id"]
    created = client.post(
        "/api/network/operations/maintenance",
        json={"scenario_code": "rail-api", "segment_code": None, "code": "api-window", "reason": "高铁沿线设备升级", "starts_at": "2020-01-01T00:00:00Z", "ends_at": "2030-01-01T00:00:00Z", "drain_mode": "finish_active", "grace_period_seconds": 120, "actor": "operator"},
    )
    assert created.status_code == 201, created.text
    window_id = created.json()["id"]
    assert created.json()["grace_period_seconds"] == 120

    preview = client.get(f"/api/network/operations/maintenance/{window_id}/drain")
    assert preview.status_code == 200
    assert preview.json()["preview"] is True
    assert preview.json()["summary"]["awaiting"] == 1

    blocked_incident = make_incident(client, "rail-api", "s1", app_code, "api-2")
    denied = client.post(f"/api/network/incidents/{blocked_incident}/accelerate", json={"actor": "tests"})
    assert denied.status_code == 409

    advanced = client.post("/api/network/operations/maintenance/advance")
    assert window_id in advanced.json()["draining"]
    draining = client.get(f"/api/network/operations/maintenance/{window_id}/drain").json()
    assert draining["state"] == "draining"
    assert draining["summary"]["awaiting"] == 1
    assert draining["sessions"][0]["deadline_at"] == started.json()["expires_at"]

    override = client.post(
        f"/api/network/operations/maintenance/{window_id}/sessions/{session_id}/override",
        json={"action": "keep", "actor": "operator", "reason": "重要客户保障"},
    )
    assert override.status_code == 200, override.text
    assert override.json()["session_actions"][0]["action"] == "exempted"
    assert override.json()["session_actions"][0]["manual"] == 1
    repeated = client.post(
        f"/api/network/operations/maintenance/{window_id}/sessions/{session_id}/override",
        json={"action": "cancel", "actor": "operator", "reason": "重复操作"},
    )
    assert repeated.status_code == 409
    advanced = client.post("/api/network/operations/maintenance/advance")
    assert window_id in advanced.json()["activated"]
