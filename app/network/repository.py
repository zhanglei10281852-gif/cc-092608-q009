from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def rows_dict(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


class NetworkRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def scenario_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM network_scenarios WHERE code=?", (code,)).fetchone()

    def scenario_by_id(self, scenario_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM network_scenarios WHERE id=?", (scenario_id,)).fetchone()

    def list_scenarios(self, *, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM network_scenarios"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY name,id"
        return rows_dict(self.connection.execute(sql, params).fetchall())

    def segment_by_code(self, scenario_id: int, code: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM network_segments WHERE scenario_id=? AND code=?",
            (scenario_id, code),
        ).fetchone()

    def segment_by_id(self, segment_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM network_segments WHERE id=?", (segment_id,)).fetchone()

    def segments(self, scenario_id: int) -> list[dict[str, Any]]:
        return rows_dict(
            self.connection.execute(
                "SELECT * FROM network_segments WHERE scenario_id=? ORDER BY sequence_no,id",
                (scenario_id,),
            ).fetchall()
        )

    def application_by_code(self, app_code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM application_profiles WHERE app_code=?", (app_code,)).fetchone()

    def application_by_id(self, app_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM application_profiles WHERE id=?", (app_id,)).fetchone()

    def list_applications(self, *, category: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM application_profiles"
        params: list[Any] = []
        if category:
            sql += " WHERE category=?"
            params.append(category)
        sql += " ORDER BY category,name,id"
        return rows_dict(self.connection.execute(sql, params).fetchall())

    def next_policy_version(self, scenario_id: int) -> int:
        return int(
            self.connection.execute(
                "SELECT COALESCE(MAX(version_no),0)+1 FROM policy_versions WHERE scenario_id=?",
                (scenario_id,),
            ).fetchone()[0]
        )

    def policy_by_id(self, policy_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM policy_versions WHERE id=?", (policy_id,)).fetchone()

    def policy_by_digest(self, scenario_id: int, digest: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM policy_versions WHERE scenario_id=? AND rules_digest=?",
            (scenario_id, digest),
        ).fetchone()

    def effective_policy(self, scenario_id: int, now: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM policy_versions WHERE scenario_id=? AND state='published' AND effective_from<=? "
            "ORDER BY effective_from DESC,version_no DESC LIMIT 1",
            (scenario_id, now),
        ).fetchone()

    def policies(self, scenario_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM policy_versions WHERE scenario_id=? ORDER BY version_no DESC",
            (scenario_id,),
        ).fetchall()
        return [self._policy(row) for row in rows]

    def sample_by_key(self, sample_key: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM experience_samples WHERE sample_key=?", (sample_key,)).fetchone()

    def sample_by_id(self, sample_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM experience_samples WHERE id=?", (sample_id,)).fetchone()

    def incident_by_sample(self, sample_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM quality_incidents WHERE sample_id=?", (sample_id,)).fetchone()

    def incident_by_id(self, incident_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM quality_incidents WHERE id=?", (incident_id,)).fetchone()

    def open_incidents(self, scenario_id: int | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM quality_incidents WHERE state IN ('open','accelerating')"
        params: list[Any] = []
        if scenario_id is not None:
            sql += " AND scenario_id=?"
            params.append(scenario_id)
        sql += " ORDER BY CASE severity WHEN 'critical' THEN 3 WHEN 'major' THEN 2 ELSE 1 END DESC,opened_at,id LIMIT ?"
        params.append(limit)
        return [self._incident(row) for row in self.connection.execute(sql, params).fetchall()]

    def session_by_incident(self, incident_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM acceleration_sessions WHERE incident_id=?", (incident_id,)).fetchone()

    def session_by_id(self, session_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM acceleration_sessions WHERE id=?", (session_id,)).fetchone()

    def session_events(self, session_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM session_events WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            result.append(item)
        return result

    def active_capacity(self, scenario_id: int, segment_id: int | None) -> dict[str, float]:
        row = self.connection.execute(
            "SELECT COALESCE(SUM(downlink_mbps),0),COALESCE(SUM(uplink_mbps),0),COUNT(*) "
            "FROM capacity_reservations WHERE scenario_id=? AND segment_id IS ? AND state='held'",
            (scenario_id, segment_id),
        ).fetchone()
        return {"downlink_mbps": float(row[0]), "uplink_mbps": float(row[1]), "sessions": int(row[2])}

    def active_entitlement(self, subscriber_hash: str, scenario_id: int, now: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM subscriber_entitlements WHERE subscriber_hash=? AND scenario_id=? AND state='active' "
            "AND valid_from<=? AND valid_until>? ORDER BY valid_until DESC,id DESC LIMIT 1",
            (subscriber_hash, scenario_id, now, now),
        ).fetchone()

    def session_detail(self, session_id: int) -> dict[str, Any] | None:
        row = self.session_by_id(session_id)
        if row is None:
            return None
        result = dict(row)
        result["events"] = self.session_events(session_id)
        reservation = self.connection.execute(
            "SELECT * FROM capacity_reservations WHERE session_id=?",
            (session_id,),
        ).fetchone()
        result["reservation"] = row_dict(reservation)
        return result

    def summary(self) -> dict[str, Any]:
        scenarios = self.connection.execute("SELECT status,COUNT(*) FROM network_scenarios GROUP BY status").fetchall()
        incidents = self.connection.execute("SELECT state,COUNT(*) FROM quality_incidents GROUP BY state").fetchall()
        sessions = self.connection.execute("SELECT status,COUNT(*) FROM acceleration_sessions GROUP BY status").fetchall()
        samples = int(self.connection.execute("SELECT COUNT(*) FROM experience_samples").fetchone()[0])
        return {
            "scenarios": {row[0]: row[1] for row in scenarios},
            "incidents": {row[0]: row[1] for row in incidents},
            "sessions": {row[0]: row[1] for row in sessions},
            "samples": samples,
        }

    @staticmethod
    def _policy(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["rules"] = json.loads(result.pop("rules_json"))
        return result

    @staticmethod
    def _incident(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["reasons"] = json.loads(result.pop("reasons_json"))
        return result
