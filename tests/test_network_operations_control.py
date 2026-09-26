from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.clock import FrozenClock, to_storage
from app.database import get_connection
from app.network.operations import NetworkOperationsService
from app.network.rules import DEFAULT_RULES


def prepare(client):
    client.post(
        "/api/network/scenarios",
        json={"code": "venue-01", "name": "大型场馆", "scene_type": "venue", "timezone": "Asia/Shanghai", "max_concurrent_sessions": 100, "capacity_mbps": 1000},
    )
    for sequence, code in enumerate(("east", "west"), start=1):
        client.post(
            "/api/network/scenarios/venue-01/segments",
            json={"code": code, "name": f"{code}-zone", "sequence_no": sequence, "expected_dwell_seconds": 600, "capacity_mbps": 400},
        )
    client.post(
        "/api/network/applications",
        json={"app_code": "live-stream", "name": "移动直播", "category": "live", "latency_target_ms": 120, "packet_loss_target": 0.02, "min_downlink_mbps": 10, "min_uplink_mbps": 8, "default_priority": 75},
    )
    policy = client.post("/api/network/scenarios/venue-01/policies", json={"rules": DEFAULT_RULES, "actor": "tests"}).json()
    client.post(f"/api/network/policies/{policy['id']}/publish", json={"actor": "tests", "effective_from": "2026-09-26T00:00:00Z"})
    return policy


def test_rollout_campaign_lifecycle(client):
    policy = prepare(client)
    created = client.post(
        "/api/network/operations/campaigns",
        json={
            "scenario_code": "venue-01",
            "policy_id": policy["id"],
            "code": "venue-evening-rollout",
            "name": "晚间场馆发布",
            "strategy": "segments",
            "target_percentage": 100,
            "segment_codes": ["east", "west"],
            "cohort_keys": ["standard", "premium"],
            "actor": "operator",
        },
    )
    assert created.status_code == 201, created.text
    assert len(created.json()["targets"]) == 4
    campaign_id = created.json()["id"]
    started = client.post(f"/api/network/operations/campaigns/{campaign_id}/start", json={"actor": "operator", "reason": "进入发布窗口"})
    assert started.status_code == 200
    assert {item["state"] for item in started.json()["targets"]} == {"active"}
    paused = client.post(f"/api/network/operations/campaigns/{campaign_id}/pause", json={"actor": "operator", "reason": "观察错误率"})
    assert paused.status_code == 200
    assert paused.json()["state"] == "paused"
    resumed = client.post(f"/api/network/operations/campaigns/{campaign_id}/start", json={"actor": "operator", "reason": "指标稳定"})
    assert resumed.status_code == 200
    completed = client.post(f"/api/network/operations/campaigns/{campaign_id}/complete", json={"actor": "operator", "reason": "发布完成"})
    assert completed.status_code == 200
    assert completed.json()["state"] == "completed"
    assert [event["event_type"] for event in completed.json()["events"]] == ["created", "started", "paused", "started", "completed"]


def test_campaign_validates_policy_and_targets(client):
    policy = prepare(client)
    missing_segment = client.post(
        "/api/network/operations/campaigns",
        json={"scenario_code": "venue-01", "policy_id": policy["id"], "code": "bad-target", "name": "错误区段", "strategy": "segments", "segment_codes": ["missing"], "actor": "operator"},
    )
    assert missing_segment.status_code == 404
    duplicate = {
        "scenario_code": "venue-01", "policy_id": policy["id"], "code": "same-code", "name": "同编码活动", "strategy": "percentage", "target_percentage": 20, "actor": "operator"
    }
    assert client.post("/api/network/operations/campaigns", json=duplicate).status_code == 201
    assert client.post("/api/network/operations/campaigns", json=duplicate).status_code == 409


def test_maintenance_overlap_and_scheduler(client):
    prepare(client)
    now = datetime(2026, 9, 26, 8, 0, tzinfo=UTC)
    service = NetworkOperationsService(get_connection(), FrozenClock(now))
    first = service.create_maintenance({
        "scenario_code": "venue-01", "segment_code": "east", "code": "east-maintenance", "reason": "容量扩容", "starts_at": to_storage(now + timedelta(minutes=10)), "ends_at": to_storage(now + timedelta(minutes=40)), "drain_mode": "block_new", "actor": "operator"
    })
    assert first["state"] == "scheduled"
    try:
        service.create_maintenance({
            "scenario_code": "venue-01", "segment_code": "east", "code": "overlap", "reason": "重叠维护", "starts_at": to_storage(now + timedelta(minutes=20)), "ends_at": to_storage(now + timedelta(minutes=50)), "drain_mode": "finish_active", "actor": "operator"
        })
    except Exception as exc:
        assert getattr(exc, "code", "") == "conflict"
    else:
        raise AssertionError("重叠维护窗口应被拒绝")
    advanced = NetworkOperationsService(get_connection(), FrozenClock(now + timedelta(minutes=15))).activate_due_maintenance("scheduler")
    assert first["id"] in advanced["activated"]
    blocked = service.blocks_new_session(first["scenario_id"], first["segment_id"], to_storage(now + timedelta(minutes=15)))
    assert blocked and blocked["code"] == "east-maintenance"
    completed = NetworkOperationsService(get_connection(), FrozenClock(now + timedelta(minutes=45))).activate_due_maintenance("scheduler")
    assert first["id"] in completed["completed"]


def test_maintenance_blocks_acceleration(client):
    policy = prepare(client)
    client.post(
        "/api/network/entitlements",
        json={"subscriber_hash": "subscriber-maintenance-01", "scenario_code": "venue-01", "product_code": "venue-boost", "valid_from": "2026-09-26T00:00:00Z", "valid_until": "2026-09-27T00:00:00Z", "source_order_id": "maintenance-order"},
    )
    sample = client.post(
        "/api/network/samples",
        json={"sample_key": "maintenance-sample", "scenario_code": "venue-01", "segment_code": "east", "app_code": "live-stream", "subscriber_hash": "subscriber-maintenance-01", "device_class": "phone", "train_speed_kmh": 0, "latency_ms": 500, "packet_loss": 0.2, "downlink_mbps": 1, "uplink_mbps": 0.2, "observed_at": "2026-09-26T05:30:00Z"},
    ).json()
    created = client.post(
        "/api/network/operations/maintenance",
        json={"scenario_code": "venue-01", "segment_code": "east", "code": "active-maintenance", "reason": "射频调整", "starts_at": "2020-01-01T00:00:00Z", "ends_at": "2030-01-01T00:00:00Z", "drain_mode": "block_new", "actor": "operator"},
    )
    assert created.status_code == 201
    denied = client.post(f"/api/network/incidents/{sample['incident_id']}/accelerate", json={"actor": "operator"})
    assert denied.status_code == 409
    assert denied.json()["error"]["context"]["maintenance_code"] == "active-maintenance"
