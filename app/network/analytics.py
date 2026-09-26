from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.core.errors import ValidationError


@dataclass(frozen=True, slots=True)
class ReportWindow:
    started_at: str | None = None
    ended_at: str | None = None

    def clauses(self, column: str) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if self.started_at:
            clauses.append(f"{column}>=?")
            params.append(self.started_at)
        if self.ended_at:
            clauses.append(f"{column}<?")
            params.append(self.ended_at)
        return clauses, params


class NetworkAnalytics:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def quality_overview(self, window: ReportWindow | None = None) -> dict[str, Any]:
        window = window or ReportWindow()
        clauses, params = window.clauses("s.observed_at")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        row = self.connection.execute(
            "SELECT COUNT(*) AS samples,COUNT(i.id) AS incidents,"
            "COALESCE(AVG(s.latency_ms),0) AS average_latency_ms,"
            "COALESCE(AVG(s.packet_loss),0) AS average_packet_loss,"
            "COALESCE(AVG(s.downlink_mbps),0) AS average_downlink_mbps "
            "FROM experience_samples s LEFT JOIN quality_incidents i ON i.sample_id=s.id" + where,
            params,
        ).fetchone()
        samples = int(row["samples"])
        incidents = int(row["incidents"])
        return {
            "samples": samples,
            "incidents": incidents,
            "degraded_ratio": round(incidents / samples, 6) if samples else 0.0,
            "average_latency_ms": round(float(row["average_latency_ms"]), 3),
            "average_packet_loss": round(float(row["average_packet_loss"]), 6),
            "average_downlink_mbps": round(float(row["average_downlink_mbps"]), 3),
        }

    def scenario_breakdown(self, window: ReportWindow | None = None) -> list[dict[str, Any]]:
        window = window or ReportWindow()
        clauses, params = window.clauses("s.observed_at")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT n.code,n.name,n.scene_type,COUNT(s.id) AS samples,COUNT(i.id) AS incidents,"
            "COALESCE(AVG(s.latency_ms),0) AS average_latency_ms,"
            "COALESCE(AVG(s.downlink_mbps),0) AS average_downlink_mbps "
            "FROM network_scenarios n LEFT JOIN experience_samples s ON s.scenario_id=n.id "
            "LEFT JOIN quality_incidents i ON i.sample_id=s.id" + where + " GROUP BY n.id ORDER BY incidents DESC,n.code",
            params,
        ).fetchall()
        result = []
        for row in rows:
            samples = int(row["samples"])
            incidents = int(row["incidents"])
            result.append({
                "scenario_code": row["code"],
                "scenario_name": row["name"],
                "scene_type": row["scene_type"],
                "samples": samples,
                "incidents": incidents,
                "degraded_ratio": round(incidents / samples, 6) if samples else 0.0,
                "average_latency_ms": round(float(row["average_latency_ms"]), 3),
                "average_downlink_mbps": round(float(row["average_downlink_mbps"]), 3),
            })
        return result

    def application_breakdown(self, window: ReportWindow | None = None) -> list[dict[str, Any]]:
        window = window or ReportWindow()
        clauses, params = window.clauses("s.observed_at")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT a.app_code,a.name,a.category,COUNT(s.id) AS samples,COUNT(i.id) AS incidents,"
            "COALESCE(AVG(s.latency_ms),0) AS average_latency_ms,"
            "COALESCE(AVG(s.packet_loss),0) AS average_packet_loss "
            "FROM application_profiles a LEFT JOIN experience_samples s ON s.app_id=a.id "
            "LEFT JOIN quality_incidents i ON i.sample_id=s.id" + where + " GROUP BY a.id ORDER BY incidents DESC,a.app_code",
            params,
        ).fetchall()
        result = []
        for row in rows:
            samples = int(row["samples"])
            incidents = int(row["incidents"])
            result.append({
                "app_code": row["app_code"],
                "app_name": row["name"],
                "category": row["category"],
                "samples": samples,
                "incidents": incidents,
                "degraded_ratio": round(incidents / samples, 6) if samples else 0.0,
                "average_latency_ms": round(float(row["average_latency_ms"]), 3),
                "average_packet_loss": round(float(row["average_packet_loss"]), 6),
            })
        return result

    def segment_breakdown(self, scenario_code: str | None = None, window: ReportWindow | None = None) -> list[dict[str, Any]]:
        window = window or ReportWindow()
        clauses, params = window.clauses("x.observed_at")
        if scenario_code:
            clauses.append("n.code=?")
            params.append(scenario_code)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT n.code AS scenario_code,n.name AS scenario_name,g.code AS segment_code,g.name AS segment_name,"
            "COUNT(x.id) AS samples,COUNT(i.id) AS incidents,"
            "COALESCE(AVG(x.latency_ms),0) AS average_latency_ms,"
            "COALESCE(AVG(x.packet_loss),0) AS average_packet_loss,"
            "COALESCE(AVG(x.downlink_mbps),0) AS average_downlink_mbps "
            "FROM network_segments g JOIN network_scenarios n ON n.id=g.scenario_id "
            "LEFT JOIN experience_samples x ON x.segment_id=g.id "
            "LEFT JOIN quality_incidents i ON i.sample_id=x.id" + where +
            " GROUP BY g.id ORDER BY incidents DESC,n.code,g.sequence_no,g.id",
            params,
        ).fetchall()
        result = []
        for row in rows:
            samples = int(row["samples"])
            incidents = int(row["incidents"])
            result.append({
                "scenario_code": row["scenario_code"],
                "scenario_name": row["scenario_name"],
                "segment_code": row["segment_code"],
                "segment_name": row["segment_name"],
                "samples": samples,
                "incidents": incidents,
                "degraded_ratio": round(incidents / samples, 6) if samples else 0.0,
                "average_latency_ms": round(float(row["average_latency_ms"]), 3),
                "average_packet_loss": round(float(row["average_packet_loss"]), 6),
                "average_downlink_mbps": round(float(row["average_downlink_mbps"]), 3),
            })
        return result

    def severity_distribution(self, scenario_code: str | None = None, window: ReportWindow | None = None) -> dict[str, Any]:
        window = window or ReportWindow()
        clauses, params = window.clauses("i.opened_at")
        if scenario_code:
            clauses.append("n.code=?")
            params.append(scenario_code)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT i.severity,i.state,COUNT(*) AS amount FROM quality_incidents i "
            "JOIN network_scenarios n ON n.id=i.scenario_id" + where +
            " GROUP BY i.severity,i.state ORDER BY i.severity,i.state",
            params,
        ).fetchall()
        severities: dict[str, int] = {}
        states: dict[str, int] = {}
        matrix: list[dict[str, Any]] = []
        for row in rows:
            amount = int(row["amount"])
            severities[row["severity"]] = severities.get(row["severity"], 0) + amount
            states[row["state"]] = states.get(row["state"], 0) + amount
            matrix.append({"severity": row["severity"], "state": row["state"], "amount": amount})
        total = sum(severities.values())
        critical = severities.get("critical", 0)
        return {
            "total": total,
            "severities": severities,
            "states": states,
            "matrix": matrix,
            "critical_ratio": round(critical / total, 6) if total else 0.0,
        }

    def stale_open_incidents(self, before: str, *, limit: int = 100) -> list[dict[str, Any]]:
        if not before:
            raise ValidationError("必须提供截止时间")
        if not 1 <= limit <= 500:
            raise ValidationError("每页事件数量必须在 1 到 500 之间")
        rows = self.connection.execute(
            "SELECT i.id,i.severity,i.state,i.opened_at,n.code AS scenario_code,g.code AS segment_code,"
            "a.app_code,x.subscriber_hash FROM quality_incidents i "
            "JOIN network_scenarios n ON n.id=i.scenario_id "
            "LEFT JOIN network_segments g ON g.id=i.segment_id "
            "JOIN application_profiles a ON a.id=i.app_id "
            "JOIN experience_samples x ON x.id=i.sample_id "
            "WHERE i.state IN ('open','accelerating') AND i.opened_at<? "
            "ORDER BY CASE i.severity WHEN 'critical' THEN 3 WHEN 'major' THEN 2 ELSE 1 END DESC,i.opened_at,i.id LIMIT ?",
            (before, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def capacity_snapshot(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT n.code AS scenario_code,n.name AS scenario_name,g.code AS segment_code,g.name AS segment_name,"
            "COALESCE(g.capacity_mbps,n.capacity_mbps) AS capacity_mbps,"
            "COALESCE(SUM(CASE WHEN r.state='held' THEN r.downlink_mbps ELSE 0 END),0) AS held_downlink_mbps,"
            "COUNT(DISTINCT CASE WHEN r.state='held' THEN r.session_id END) AS active_sessions "
            "FROM network_scenarios n LEFT JOIN network_segments g ON g.scenario_id=n.id "
            "LEFT JOIN capacity_reservations r ON r.scenario_id=n.id AND r.segment_id IS g.id "
            "GROUP BY n.id,g.id ORDER BY n.code,g.sequence_no,g.id"
        ).fetchall()
        result = []
        for row in rows:
            capacity = float(row["capacity_mbps"])
            held = float(row["held_downlink_mbps"])
            result.append({
                "scenario_code": row["scenario_code"],
                "scenario_name": row["scenario_name"],
                "segment_code": row["segment_code"],
                "segment_name": row["segment_name"],
                "capacity_mbps": capacity,
                "held_downlink_mbps": round(held, 3),
                "available_downlink_mbps": round(max(0.0, capacity - held), 3),
                "utilization": round(held / capacity, 6) if capacity else 0.0,
                "active_sessions": int(row["active_sessions"]),
            })
        return result

    def acceleration_outcomes(self, window: ReportWindow | None = None) -> dict[str, Any]:
        window = window or ReportWindow()
        clauses, params = window.clauses("started_at")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            "SELECT status,COUNT(*) AS amount,COALESCE(AVG(allocated_downlink_mbps),0) AS average_downlink "
            "FROM acceleration_sessions" + where + " GROUP BY status",
            params,
        ).fetchall()
        states = {row["status"]: int(row["amount"]) for row in rows}
        total = sum(states.values())
        completed = states.get("completed", 0)
        return {
            "states": states,
            "total": total,
            "completion_ratio": round(completed / total, 6) if total else 0.0,
            "average_allocated_downlink_mbps": round(
                sum(float(row["average_downlink"]) * int(row["amount"]) for row in rows) / total,
                3,
            ) if total else 0.0,
        }

    def event_timeline(self, *, after_id: int = 0, limit: int = 100) -> dict[str, Any]:
        if after_id < 0:
            raise ValidationError("游标不能小于零")
        if not 1 <= limit <= 500:
            raise ValidationError("每页事件数量必须在 1 到 500 之间")
        rows = self.connection.execute(
            "SELECT e.id,e.session_id,e.event_type,e.actor,e.detail_json,e.created_at,s.scenario_id,n.code AS scenario_code "
            "FROM session_events e JOIN acceleration_sessions s ON s.id=e.session_id "
            "JOIN network_scenarios n ON n.id=s.scenario_id WHERE e.id>? ORDER BY e.id LIMIT ?",
            (after_id, limit + 1),
        ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        items = []
        for row in selected:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            items.append(item)
        next_cursor = items[-1]["id"] if items else after_id
        return {"items": items, "next_cursor": next_cursor, "has_more": has_more}
