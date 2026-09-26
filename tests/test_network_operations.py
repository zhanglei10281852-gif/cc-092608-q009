from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.clock import FrozenClock, to_storage
from app.database import get_connection
from app.network.rules import DEFAULT_RULES, allocation_for, judge_quality
from app.network.service import NetworkAccelerationService


def scenario_payload(**overrides):
    payload = {
        "code": "gdh-rail",
        "name": "广深高铁",
        "scene_type": "railway",
        "timezone": "Asia/Shanghai",
        "max_concurrent_sessions": 10,
        "capacity_mbps": 3000,
    }
    payload.update(overrides)
    return payload


def app_payload(**overrides):
    payload = {
        "app_code": "video-call",
        "name": "视频通话",
        "category": "video_call",
        "latency_target_ms": 100,
        "packet_loss_target": 0.01,
        "min_downlink_mbps": 8,
        "min_uplink_mbps": 4,
        "default_priority": 70,
    }
    payload.update(overrides)
    return payload


def sample_payload(**overrides):
    payload = {
        "sample_key": "sample-000001",
        "scenario_code": "gdh-rail",
        "segment_code": "gz-sz-01",
        "app_code": "video-call",
        "subscriber_hash": "subscriber-000000000001",
        "device_class": "phone",
        "train_speed_kmh": 300,
        "latency_ms": 350,
        "packet_loss": 0.08,
        "downlink_mbps": 1.5,
        "uplink_mbps": 0.5,
        "observed_at": "2026-09-26T05:30:00Z",
    }
    payload.update(overrides)
    return payload


def prepare(client):
    scenario = client.post("/api/network/scenarios", json=scenario_payload())
    assert scenario.status_code == 201, scenario.text
    segment = client.post(
        "/api/network/scenarios/gdh-rail/segments",
        json={"code": "gz-sz-01", "name": "广州南至虎门", "sequence_no": 1, "expected_dwell_seconds": 900, "capacity_mbps": 1200},
    )
    assert segment.status_code == 201, segment.text
    app = client.post("/api/network/applications", json=app_payload())
    assert app.status_code == 201, app.text
    policy = client.post("/api/network/scenarios/gdh-rail/policies", json={"rules": DEFAULT_RULES, "actor": "tests"})
    assert policy.status_code == 201, policy.text
    published = client.post(
        f"/api/network/policies/{policy.json()['id']}/publish",
        json={"actor": "tests", "effective_from": "2026-09-26T00:00:00Z"},
    )
    assert published.status_code == 200, published.text
    return {"scenario": scenario.json(), "app": app.json(), "policy": published.json()}


def test_quality_rule_engine_is_deterministic():
    profile = app_payload()
    healthy = judge_quality(sample_payload(latency_ms=80, packet_loss=0.005, downlink_mbps=20, uplink_mbps=8), profile, DEFAULT_RULES)
    assert healthy.degraded is False
    degraded = judge_quality(sample_payload(), profile, DEFAULT_RULES)
    assert degraded.degraded is True
    assert degraded.severity in {"major", "critical"}
    assert set(degraded.reasons) == {"latency", "packet_loss", "downlink", "uplink"}
    allocation = allocation_for(profile, degraded.severity, DEFAULT_RULES)
    assert allocation.downlink_mbps >= profile["min_downlink_mbps"]
    assert allocation.priority > profile["default_priority"]


def test_scenario_app_policy_and_idempotent_sample(client):
    prepare(client)
    first = client.post("/api/network/samples", json=sample_payload())
    assert first.status_code == 202, first.text
    assert first.json()["incident_id"] is not None
    duplicate = client.post("/api/network/samples", json=sample_payload())
    assert duplicate.status_code == 202
    assert duplicate.json()["sample_id"] == first.json()["sample_id"]
    conflict = client.post("/api/network/samples", json=sample_payload(latency_ms=999))
    assert conflict.status_code == 409


def test_acceleration_requires_entitlement_and_releases_capacity(client):
    prepare(client)
    sample = client.post("/api/network/samples", json=sample_payload()).json()
    denied = client.post(f"/api/network/incidents/{sample['incident_id']}/accelerate", json={"actor": "tests"})
    assert denied.status_code == 409
    entitlement = client.post(
        "/api/network/entitlements",
        json={
            "subscriber_hash": sample_payload()["subscriber_hash"],
            "scenario_code": "gdh-rail",
            "product_code": "rail-boost-day",
            "valid_from": "2026-09-26T00:00:00Z",
            "valid_until": "2026-09-27T00:00:00Z",
            "source_order_id": "order-000001",
        },
    )
    assert entitlement.status_code == 201
    started = client.post(f"/api/network/incidents/{sample['incident_id']}/accelerate", json={"actor": "tests"})
    assert started.status_code == 200, started.text
    repeated = client.post(f"/api/network/incidents/{sample['incident_id']}/accelerate", json={"actor": "tests"})
    assert repeated.status_code == 200
    assert repeated.json()["id"] == started.json()["id"]
    finished = client.post(
        f"/api/network/sessions/{started.json()['id']}/finish",
        json={"actor": "tests", "reason": "体验恢复", "result": "completed"},
    )
    assert finished.status_code == 200
    assert finished.json()["status"] == "completed"
    assert finished.json()["reservation"]["state"] == "released"
    assert [event["event_type"] for event in finished.json()["events"]] == ["started", "completed"]


def test_expired_session_reopens_incident_with_fixed_clock(client):
    prepare(client)
    client.post(
        "/api/network/entitlements",
        json={
            "subscriber_hash": sample_payload()["subscriber_hash"],
            "scenario_code": "gdh-rail",
            "product_code": "rail-boost-day",
            "valid_from": "2026-09-26T00:00:00Z",
            "valid_until": "2026-09-27T00:00:00Z",
            "source_order_id": "order-000002",
        },
    )
    sample = client.post("/api/network/samples", json=sample_payload(sample_key="sample-000002")).json()
    started = client.post(f"/api/network/incidents/{sample['incident_id']}/accelerate", json={"actor": "tests"}).json()
    connection = get_connection()
    expiry = datetime.fromisoformat(started["expires_at"].replace("Z", "+00:00"))
    service = NetworkAccelerationService(connection, FrozenClock(expiry + timedelta(seconds=1)))
    result = service.expire_sessions("tests")
    assert started["id"] in result["expired"]
    detail = service.get_session(started["id"])
    assert detail["status"] == "expired"
    assert detail["reservation"]["state"] == "released"
    incident = connection.execute("SELECT state FROM quality_incidents WHERE id=?", (sample["incident_id"],)).fetchone()
    assert incident["state"] == "open"


def test_capacity_limit_rejects_second_session(client):
    prepare(client)
    connection = get_connection()
    connection.execute("UPDATE network_segments SET capacity_mbps=20 WHERE code='gz-sz-01'")
    for index in (1, 2):
        subscriber = f"subscriber-{index:018d}"
        client.post(
            "/api/network/entitlements",
            json={
                "subscriber_hash": subscriber,
                "scenario_code": "gdh-rail",
                "product_code": "rail-boost-day",
                "valid_from": "2026-09-26T00:00:00Z",
                "valid_until": "2026-09-27T00:00:00Z",
                "source_order_id": f"order-capacity-{index:03d}",
            },
        )
        sample = client.post("/api/network/samples", json=sample_payload(sample_key=f"sample-capacity-{index:03d}", subscriber_hash=subscriber)).json()
        response = client.post(f"/api/network/incidents/{sample['incident_id']}/accelerate", json={"actor": "tests"})
        if index == 1:
            assert response.status_code == 200
        else:
            assert response.status_code == 409


def test_policy_versions_replace_previous_publication(client):
    prepared = prepare(client)
    changed = {**DEFAULT_RULES, "allocation": {**DEFAULT_RULES["allocation"], "duration_seconds": 240}}
    draft = client.post("/api/network/scenarios/gdh-rail/policies", json={"rules": changed, "actor": "tests"})
    assert draft.status_code == 201
    publish = client.post(
        f"/api/network/policies/{draft.json()['id']}/publish",
        json={"actor": "tests", "effective_from": "2026-09-26T01:00:00Z"},
    )
    assert publish.status_code == 200
    old = get_connection().execute("SELECT state FROM policy_versions WHERE id=?", (prepared["policy"]["id"],)).fetchone()
    assert old["state"] == "retired"


def test_demo_seed_and_summary(client):
    seeded = client.post("/api/network/demo/seed")
    assert seeded.status_code == 200
    repeated = client.post("/api/network/demo/seed")
    assert repeated.status_code == 200
    summary = client.get("/api/network/summary")
    assert summary.status_code == 200
    assert summary.json()["scenarios"]["active"] == 1
