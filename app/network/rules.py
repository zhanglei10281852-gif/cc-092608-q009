from __future__ import annotations

import json
from typing import Any

from app.core.errors import ValidationError
from app.core.security import request_fingerprint
from app.network.types import Allocation, QualityDecision

DEFAULT_RULES: dict[str, Any] = {
    "score": {
        "latency_weight": 0.35,
        "packet_loss_weight": 0.30,
        "downlink_weight": 0.20,
        "uplink_weight": 0.15,
        "major_threshold": 1.5,
        "critical_threshold": 2.5,
    },
    "allocation": {
        "minor_multiplier": 1.15,
        "major_multiplier": 1.50,
        "critical_multiplier": 2.00,
        "duration_seconds": 180,
        "max_downlink_mbps": 200.0,
        "max_uplink_mbps": 50.0,
    },
}


def canonical_rules(rules: dict[str, Any]) -> tuple[str, str]:
    validate_rules(rules)
    text = json.dumps(rules, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text, request_fingerprint(rules)


def validate_rules(rules: dict[str, Any]) -> None:
    score = rules.get("score")
    allocation = rules.get("allocation")
    if not isinstance(score, dict) or not isinstance(allocation, dict):
        raise ValidationError("策略必须同时包含 score 与 allocation")
    weights = [score.get(name) for name in ("latency_weight", "packet_loss_weight", "downlink_weight", "uplink_weight")]
    if any(not isinstance(value, (int, float)) or value < 0 for value in weights):
        raise ValidationError("评分权重必须是非负数")
    if abs(sum(float(value) for value in weights) - 1.0) > 0.0001:
        raise ValidationError("评分权重之和必须等于 1")
    major = score.get("major_threshold")
    critical = score.get("critical_threshold")
    if not isinstance(major, (int, float)) or not isinstance(critical, (int, float)) or not 0 < major < critical:
        raise ValidationError("重大和严重阈值必须递增")
    for key in ("minor_multiplier", "major_multiplier", "critical_multiplier"):
        value = allocation.get(key)
        if not isinstance(value, (int, float)) or value < 1:
            raise ValidationError(f"{key} 必须不小于 1")
    duration = allocation.get("duration_seconds")
    if not isinstance(duration, int) or not 30 <= duration <= 3600:
        raise ValidationError("加速时长必须在 30 到 3600 秒之间")


def judge_quality(sample: dict[str, Any], profile: dict[str, Any], rules: dict[str, Any]) -> QualityDecision:
    score_rules = rules["score"]
    ratios = {
        "latency": max(0.0, float(sample["latency_ms"]) / float(profile["latency_target_ms"]) - 1.0),
        "packet_loss": max(0.0, float(sample["packet_loss"]) / max(float(profile["packet_loss_target"]), 0.0001) - 1.0),
        "downlink": max(0.0, float(profile["min_downlink_mbps"]) / max(float(sample["downlink_mbps"]), 0.001) - 1.0),
        "uplink": max(0.0, float(profile["min_uplink_mbps"]) / max(float(sample["uplink_mbps"]), 0.001) - 1.0),
    }
    weights = {
        "latency": float(score_rules["latency_weight"]),
        "packet_loss": float(score_rules["packet_loss_weight"]),
        "downlink": float(score_rules["downlink_weight"]),
        "uplink": float(score_rules["uplink_weight"]),
    }
    score = round(sum(ratios[key] * weights[key] for key in ratios), 6)
    reasons = tuple(key for key, ratio in ratios.items() if ratio > 0)
    if not reasons:
        return QualityDecision(False, None, (), 0.0)
    if score >= float(score_rules["critical_threshold"]):
        severity = "critical"
    elif score >= float(score_rules["major_threshold"]):
        severity = "major"
    else:
        severity = "minor"
    return QualityDecision(True, severity, reasons, score)


def allocation_for(profile: dict[str, Any], severity: str, rules: dict[str, Any]) -> Allocation:
    allocation = rules["allocation"]
    multiplier = float(allocation[f"{severity}_multiplier"])
    downlink = min(float(profile["min_downlink_mbps"]) * multiplier, float(allocation["max_downlink_mbps"]))
    uplink = min(float(profile["min_uplink_mbps"]) * multiplier, float(allocation["max_uplink_mbps"]))
    priority_bonus = {"minor": 5, "major": 15, "critical": 25}[severity]
    priority = min(100, int(profile["default_priority"]) + priority_bonus)
    return Allocation(round(downlink, 3), round(uplink, 3), priority, int(allocation["duration_seconds"]))
