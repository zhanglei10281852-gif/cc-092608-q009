from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class RolloutCampaignCreate(BaseModel):
    scenario_code: str = Field(min_length=2, max_length=64)
    policy_id: int = Field(gt=0)
    code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=160)
    strategy: Literal["percentage", "segments", "scheduled"]
    target_percentage: int = Field(default=100, ge=1, le=100)
    segment_codes: list[str] = Field(default_factory=list, max_length=500)
    cohort_keys: list[str] = Field(default_factory=list, max_length=100)
    starts_at: str | None = None
    ends_at: str | None = None
    actor: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_targets(self) -> "RolloutCampaignCreate":
        if self.strategy == "segments" and not self.segment_codes:
            raise ValueError("区段发布必须至少选择一个区段")
        if self.strategy == "scheduled" and not self.starts_at:
            raise ValueError("定时发布必须提供开始时间")
        if len(self.segment_codes) != len(set(self.segment_codes)):
            raise ValueError("发布区段不能重复")
        if len(self.cohort_keys) != len(set(self.cohort_keys)):
            raise ValueError("用户分群不能重复")
        return self


class CampaignAction(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=500)


class MaintenanceCreate(BaseModel):
    scenario_code: str = Field(min_length=2, max_length=64)
    segment_code: str | None = Field(default=None, max_length=64)
    code: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    reason: str = Field(min_length=2, max_length=500)
    starts_at: str
    ends_at: str
    drain_mode: Literal["finish_active", "cancel_active", "block_new"] = "finish_active"
    grace_period_seconds: int = Field(default=300, ge=0, le=86400)
    actor: str = Field(min_length=1, max_length=120)


class MaintenanceAction(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=500)


class MaintenanceOverride(BaseModel):
    action: Literal["cancel", "keep", "migrate"]
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=500)
