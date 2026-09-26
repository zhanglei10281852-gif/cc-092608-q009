from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class QualityDecision:
    degraded: bool
    severity: str | None
    reasons: tuple[str, ...]
    score: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "degraded": self.degraded,
            "severity": self.severity,
            "reasons": list(self.reasons),
            "score": self.score,
        }


@dataclass(frozen=True, slots=True)
class Allocation:
    downlink_mbps: float
    uplink_mbps: float
    priority: int
    duration_seconds: int


@dataclass(frozen=True, slots=True)
class ActiveWindow:
    starts_at: datetime
    ends_at: datetime

    def contains(self, value: datetime) -> bool:
        return self.starts_at <= value < self.ends_at
