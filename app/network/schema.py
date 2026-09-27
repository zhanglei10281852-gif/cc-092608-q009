from __future__ import annotations

import sqlite3

MAINTENANCE_WINDOWS_COLUMNS = """
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id INTEGER NOT NULL REFERENCES network_scenarios(id),
    segment_id INTEGER REFERENCES network_segments(id),
    code TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'scheduled' CHECK(state IN ('scheduled','draining','active','completed','cancelled')),
    drain_mode TEXT NOT NULL DEFAULT 'finish_active' CHECK(drain_mode IN ('finish_active','cancel_active','block_new')),
    grace_period_seconds INTEGER NOT NULL DEFAULT 300 CHECK(grace_period_seconds >= 0),
    drain_started_at TEXT,
    activated_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
"""

NETWORK_SCHEMA = (
    r'''
CREATE TABLE IF NOT EXISTS network_scenarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    scene_type TEXT NOT NULL CHECK(scene_type IN ('railway','metro','concert','venue')),
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','retired')),
    max_concurrent_sessions INTEGER NOT NULL CHECK(max_concurrent_sessions > 0),
    capacity_mbps INTEGER NOT NULL CHECK(capacity_mbps > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS network_segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id INTEGER NOT NULL REFERENCES network_scenarios(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    sequence_no INTEGER NOT NULL CHECK(sequence_no >= 0),
    expected_dwell_seconds INTEGER NOT NULL CHECK(expected_dwell_seconds > 0),
    capacity_mbps INTEGER NOT NULL CHECK(capacity_mbps > 0),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','maintenance','disabled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(scenario_id, code),
    UNIQUE(scenario_id, sequence_no)
);
CREATE TABLE IF NOT EXISTS application_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('game','live','video_call','video','office')),
    latency_target_ms INTEGER NOT NULL CHECK(latency_target_ms > 0),
    packet_loss_target REAL NOT NULL CHECK(packet_loss_target >= 0 AND packet_loss_target <= 1),
    min_downlink_mbps REAL NOT NULL CHECK(min_downlink_mbps >= 0),
    min_uplink_mbps REAL NOT NULL CHECK(min_uplink_mbps >= 0),
    default_priority INTEGER NOT NULL CHECK(default_priority BETWEEN 0 AND 100),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','retired')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id INTEGER NOT NULL REFERENCES network_scenarios(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','published','retired')),
    rules_json TEXT NOT NULL,
    rules_digest TEXT NOT NULL,
    created_by TEXT NOT NULL,
    published_by TEXT,
    effective_from TEXT,
    retired_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(scenario_id, version_no),
    UNIQUE(scenario_id, rules_digest)
);
CREATE INDEX IF NOT EXISTS idx_policy_effective ON policy_versions(scenario_id,state,effective_from);
CREATE TABLE IF NOT EXISTS experience_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_key TEXT NOT NULL UNIQUE,
    scenario_id INTEGER NOT NULL REFERENCES network_scenarios(id),
    segment_id INTEGER REFERENCES network_segments(id),
    app_id INTEGER NOT NULL REFERENCES application_profiles(id),
    subscriber_hash TEXT NOT NULL,
    device_class TEXT NOT NULL,
    train_speed_kmh REAL NOT NULL CHECK(train_speed_kmh >= 0),
    latency_ms REAL NOT NULL CHECK(latency_ms >= 0),
    packet_loss REAL NOT NULL CHECK(packet_loss >= 0 AND packet_loss <= 1),
    downlink_mbps REAL NOT NULL CHECK(downlink_mbps >= 0),
    uplink_mbps REAL NOT NULL CHECK(uplink_mbps >= 0),
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    payload_digest TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_scene_time ON experience_samples(scenario_id,observed_at);
CREATE TABLE IF NOT EXISTS quality_incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id INTEGER NOT NULL UNIQUE REFERENCES experience_samples(id) ON DELETE CASCADE,
    scenario_id INTEGER NOT NULL REFERENCES network_scenarios(id),
    segment_id INTEGER REFERENCES network_segments(id),
    app_id INTEGER NOT NULL REFERENCES application_profiles(id),
    severity TEXT NOT NULL CHECK(severity IN ('minor','major','critical')),
    reasons_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','accelerating','resolved','expired')),
    opened_at TEXT NOT NULL,
    resolved_at TEXT,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_incidents_open ON quality_incidents(state,severity,opened_at);
CREATE TABLE IF NOT EXISTS acceleration_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES quality_incidents(id),
    subscriber_hash TEXT NOT NULL,
    app_id INTEGER NOT NULL REFERENCES application_profiles(id),
    scenario_id INTEGER NOT NULL REFERENCES network_scenarios(id),
    segment_id INTEGER REFERENCES network_segments(id),
    policy_version_id INTEGER NOT NULL REFERENCES policy_versions(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','completed','cancelled','expired')),
    allocated_downlink_mbps REAL NOT NULL CHECK(allocated_downlink_mbps >= 0),
    allocated_uplink_mbps REAL NOT NULL CHECK(allocated_uplink_mbps >= 0),
    priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 100),
    started_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(incident_id)
);
CREATE INDEX IF NOT EXISTS idx_sessions_capacity ON acceleration_sessions(scenario_id,segment_id,status,expires_at);
CREATE TABLE IF NOT EXISTS capacity_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES acceleration_sessions(id) ON DELETE CASCADE,
    scenario_id INTEGER NOT NULL REFERENCES network_scenarios(id),
    segment_id INTEGER REFERENCES network_segments(id),
    downlink_mbps REAL NOT NULL,
    uplink_mbps REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'held' CHECK(state IN ('held','released')),
    held_at TEXT NOT NULL,
    released_at TEXT,
    UNIQUE(session_id)
);
CREATE TABLE IF NOT EXISTS session_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES acceleration_sessions(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_events ON session_events(session_id,id);
CREATE TABLE IF NOT EXISTS subscriber_entitlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscriber_hash TEXT NOT NULL,
    scenario_id INTEGER NOT NULL REFERENCES network_scenarios(id),
    product_code TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended','expired','cancelled')),
    source_order_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entitlements_lookup ON subscriber_entitlements(subscriber_hash,scenario_id,state,valid_from,valid_until);
CREATE TABLE IF NOT EXISTS rollout_campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id INTEGER NOT NULL REFERENCES network_scenarios(id),
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    strategy TEXT NOT NULL CHECK(strategy IN ('percentage','segments','scheduled')),
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','scheduled','running','paused','completed','cancelled')),
    target_percentage INTEGER NOT NULL DEFAULT 100 CHECK(target_percentage BETWEEN 1 AND 100),
    policy_version_id INTEGER NOT NULL REFERENCES policy_versions(id),
    starts_at TEXT,
    ends_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rollout_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES rollout_campaigns(id) ON DELETE CASCADE,
    segment_id INTEGER REFERENCES network_segments(id),
    cohort_key TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','active','paused','completed','failed')),
    activated_at TEXT,
    completed_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(campaign_id,segment_id,cohort_key)
);
CREATE INDEX IF NOT EXISTS idx_rollout_targets_state ON rollout_targets(campaign_id,state,id);
'''
    + f"CREATE TABLE IF NOT EXISTS maintenance_windows ({MAINTENANCE_WINDOWS_COLUMNS});"
    + r'''
CREATE INDEX IF NOT EXISTS idx_maintenance_active ON maintenance_windows(scenario_id,segment_id,state,starts_at,ends_at);
CREATE TABLE IF NOT EXISTS maintenance_session_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id INTEGER NOT NULL REFERENCES maintenance_windows(id) ON DELETE CASCADE,
    session_id INTEGER NOT NULL REFERENCES acceleration_sessions(id),
    action TEXT NOT NULL DEFAULT 'awaiting' CHECK(action IN ('awaiting','migrated','cancelled','completed','expired','exempted')),
    deadline_at TEXT,
    from_segment_id INTEGER REFERENCES network_segments(id),
    to_segment_id INTEGER REFERENCES network_segments(id),
    manual INTEGER NOT NULL DEFAULT 0 CHECK(manual IN (0,1)),
    actor TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE(window_id, session_id)
);
CREATE INDEX IF NOT EXISTS idx_maintenance_actions_window ON maintenance_session_actions(window_id,action);
CREATE TABLE IF NOT EXISTS operation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_type TEXT NOT NULL,
    resource_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_operation_events_resource ON operation_events(resource_type,resource_id,id);
'''
)

MAINTENANCE_WINDOWS_MIGRATION = (
    f"CREATE TABLE maintenance_windows_new ({MAINTENANCE_WINDOWS_COLUMNS});"
    + r'''
INSERT INTO maintenance_windows_new(id,scenario_id,segment_id,code,reason,starts_at,ends_at,state,drain_mode,created_by,created_at,updated_at)
SELECT id,scenario_id,segment_id,code,reason,starts_at,ends_at,state,drain_mode,created_by,created_at,updated_at FROM maintenance_windows;
DROP TABLE maintenance_windows;
ALTER TABLE maintenance_windows_new RENAME TO maintenance_windows;
CREATE INDEX IF NOT EXISTS idx_maintenance_active ON maintenance_windows(scenario_id,segment_id,state,starts_at,ends_at);
'''
)


def ensure_network_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(NETWORK_SCHEMA)
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='maintenance_windows'",
    ).fetchone()
    if row is not None and "'draining'" not in str(row[0]):
        connection.executescript(MAINTENANCE_WINDOWS_MIGRATION)
