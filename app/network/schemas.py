from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class ScenarioCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=120)
    scene_type: Literal["railway", "metro", "concert", "venue"]
    timezone: str = Field(default="Asia/Shanghai", min_length=3, max_length=80)
    max_concurrent_sessions: int = Field(default=1000, ge=1, le=1_000_000)
    capacity_mbps: int = Field(default=10_000, ge=1, le=10_000_000)


class SegmentCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
    name: str = Field(min_length=1, max_length=120)
    sequence_no: int = Field(ge=0, le=100_000)
    expected_dwell_seconds: int = Field(default=180, ge=1, le=86_400)
    capacity_mbps: int = Field(default=3000, ge=1, le=10_000_000)


class ApplicationCreate(BaseModel):
    app_code: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=1, max_length=120)
    category: Literal["game", "live", "video_call", "video", "office"]
    latency_target_ms: int = Field(ge=1, le=10_000)
    packet_loss_target: float = Field(ge=0, le=1)
    min_downlink_mbps: float = Field(ge=0, le=100_000)
    min_uplink_mbps: float = Field(ge=0, le=100_000)
    default_priority: int = Field(default=50, ge=0, le=100)


class PolicyCreate(BaseModel):
    rules: dict[str, Any]
    actor: str = Field(min_length=1, max_length=120)


class PolicyPublish(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    effective_from: str


class EntitlementCreate(BaseModel):
    subscriber_hash: str = Field(min_length=16, max_length=128)
    scenario_code: str = Field(min_length=2, max_length=64)
    product_code: str = Field(min_length=2, max_length=80)
    valid_from: str
    valid_until: str
    source_order_id: str = Field(min_length=4, max_length=160)


class ExperienceSampleCreate(BaseModel):
    sample_key: str = Field(min_length=6, max_length=160)
    scenario_code: str = Field(min_length=2, max_length=64)
    segment_code: str | None = Field(default=None, max_length=64)
    app_code: str = Field(min_length=2, max_length=80)
    subscriber_hash: str = Field(min_length=16, max_length=128)
    device_class: str = Field(min_length=1, max_length=80)
    train_speed_kmh: float = Field(default=0, ge=0, le=1000)
    latency_ms: float = Field(ge=0, le=1_000_000)
    packet_loss: float = Field(ge=0, le=1)
    downlink_mbps: float = Field(ge=0, le=100_000)
    uplink_mbps: float = Field(ge=0, le=100_000)
    observed_at: str


class AccelerationStart(BaseModel):
    actor: str = Field(min_length=1, max_length=120)


class SessionFinish(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=500)
    result: Literal["completed", "cancelled"] = "completed"


class BatchSamples(BaseModel):
    items: list[ExperienceSampleCreate] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_keys(self) -> "BatchSamples":
        keys = [item.sample_key for item in self.items]
        if len(keys) != len(set(keys)):
            raise ValueError("同一批次内 sample_key 不能重复")
        return self
