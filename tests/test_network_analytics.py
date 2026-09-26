from __future__ import annotations

from app.database import get_connection
from app.network.analytics import NetworkAnalytics, ReportWindow
from app.network.rules import DEFAULT_RULES


def seed(client):
    client.post(
        "/api/network/scenarios",
        json={"code": "metro-01", "name": "地铁一号线", "scene_type": "metro", "timezone": "Asia/Shanghai", "max_concurrent_sessions": 100, "capacity_mbps": 1000},
    )
    client.post(
        "/api/network/scenarios/metro-01/segments",
        json={"code": "central", "name": "中心站区段", "sequence_no": 1, "expected_dwell_seconds": 120, "capacity_mbps": 300},
    )
    client.post(
        "/api/network/applications",
        json={"app_code": "mobile-game", "name": "移动游戏", "category": "game", "latency_target_ms": 60, "packet_loss_target": 0.01, "min_downlink_mbps": 5, "min_uplink_mbps": 2, "default_priority": 80},
    )
    policy = client.post("/api/network/scenarios/metro-01/policies", json={"rules": DEFAULT_RULES, "actor": "analytics"}).json()
    client.post(f"/api/network/policies/{policy['id']}/publish", json={"actor": "analytics", "effective_from": "2026-09-26T00:00:00Z"})


def test_empty_reports(client):
    analytics = NetworkAnalytics(get_connection())
    assert analytics.quality_overview()["samples"] == 0
    assert analytics.acceleration_outcomes()["total"] == 0
    assert analytics.event_timeline() == {"items": [], "next_cursor": 0, "has_more": False}


def test_quality_and_capacity_reports(client):
    seed(client)
    healthy = {
        "sample_key": "analytics-healthy",
        "scenario_code": "metro-01",
        "segment_code": "central",
        "app_code": "mobile-game",
        "subscriber_hash": "subscriber-analytics-0001",
        "device_class": "phone",
        "train_speed_kmh": 60,
        "latency_ms": 40,
        "packet_loss": 0.002,
        "downlink_mbps": 20,
        "uplink_mbps": 5,
        "observed_at": "2026-09-26T02:00:00Z",
    }
    degraded = {**healthy, "sample_key": "analytics-degraded", "subscriber_hash": "subscriber-analytics-0002", "latency_ms": 300, "packet_loss": 0.1, "downlink_mbps": 1, "uplink_mbps": 0.2}
    assert client.post("/api/network/samples", json=healthy).status_code == 202
    assert client.post("/api/network/samples", json=degraded).status_code == 202
    report = client.get("/api/network/analytics/quality")
    assert report.status_code == 200
    assert report.json()["overview"]["samples"] == 2
    assert report.json()["overview"]["incidents"] == 1
    assert report.json()["overview"]["degraded_ratio"] == 0.5
    assert report.json()["segments"][0]["incidents"] == 1
    assert report.json()["severity"]["total"] == 1
    capacity = client.get("/api/network/analytics/capacity")
    assert capacity.status_code == 200
    assert capacity.json()["items"][0]["available_downlink_mbps"] == 300


def test_report_window_and_event_cursor(client):
    seed(client)
    analytics = NetworkAnalytics(get_connection())
    window = ReportWindow("2026-09-26T00:00:00Z", "2026-09-27T00:00:00Z")
    assert analytics.quality_overview(window)["samples"] == 0
    connection = get_connection()
    scenario = connection.execute("SELECT id FROM network_scenarios WHERE code='metro-01'").fetchone()[0]
    segment = connection.execute("SELECT id FROM network_segments WHERE scenario_id=?", (scenario,)).fetchone()[0]
    app = connection.execute("SELECT id FROM application_profiles WHERE app_code='mobile-game'").fetchone()[0]
    policy = connection.execute("SELECT id FROM policy_versions WHERE scenario_id=?", (scenario,)).fetchone()[0]
    connection.execute(
        "INSERT INTO experience_samples(sample_key,scenario_id,segment_id,app_id,subscriber_hash,device_class,train_speed_kmh,latency_ms,packet_loss,downlink_mbps,uplink_mbps,observed_at,received_at,payload_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("analytics-manual", scenario, segment, app, "subscriber-analytics-0003", "phone", 60, 300, 0.1, 1, 0.2, "2026-09-26T02:00:00Z", "2026-09-26T02:00:00Z", "manual-digest"),
    )
    sample_id = connection.execute("SELECT id FROM experience_samples WHERE sample_key='analytics-manual'").fetchone()[0]
    connection.execute(
        "INSERT INTO quality_incidents(sample_id,scenario_id,segment_id,app_id,severity,reasons_json,opened_at) VALUES(?,?,?,?, 'major','{}','2026-09-26T02:00:00Z')",
        (sample_id, scenario, segment, app),
    )
    incident = connection.execute("SELECT id FROM quality_incidents WHERE sample_id=?", (sample_id,)).fetchone()[0]
    connection.execute(
        "INSERT INTO acceleration_sessions(incident_id,subscriber_hash,app_id,scenario_id,segment_id,policy_version_id,allocated_downlink_mbps,allocated_uplink_mbps,priority,started_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (incident, "subscriber-analytics-0003", app, scenario, None, policy, 10, 4, 90, "2026-09-26T02:00:00Z", "2026-09-26T02:03:00Z"),
    )
    session = connection.execute("SELECT id FROM acceleration_sessions WHERE incident_id=?", (incident,)).fetchone()[0]
    for event_type in ("started", "capacity-held", "completed"):
        connection.execute(
            "INSERT INTO session_events(session_id,event_type,actor,detail_json,created_at) VALUES(?,?,?,?,?)",
            (session, event_type, "tests", "{}", "2026-09-26T02:00:00Z"),
        )
    first = analytics.event_timeline(limit=2)
    assert len(first["items"]) == 2
    assert first["has_more"] is True
    second = analytics.event_timeline(after_id=first["next_cursor"], limit=2)
    assert [item["event_type"] for item in second["items"]] == ["completed"]
    stale = analytics.stale_open_incidents("2026-09-27T00:00:00Z")
    assert stale[0]["id"] == incident
