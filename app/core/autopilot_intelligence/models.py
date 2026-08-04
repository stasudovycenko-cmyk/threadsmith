"""Typed boundaries for deterministic Autopilot decisions."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTEXT_VERSION = 1
RULES_VERSION = 1


class IntelligenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class QueueHealth(StrEnum):
    DISABLED = "disabled"
    EMPTY = "empty"
    LOW = "low"
    HEALTHY = "healthy"
    FULL = "full"
    RECOVERY_REQUIRED = "recovery_required"


class RuleKind(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    BLOCKER = "BLOCKER"
    RECOMMENDATION = "RECOMMENDATION"
    ACTION = "ACTION"


class DecisionStatus(StrEnum):
    HEALTHY = "healthy"
    ATTENTION = "attention"
    BLOCKED = "blocked"
    WAITING = "waiting"
    INSUFFICIENT_DATA = "insufficient_data"


class ActionType(StrEnum):
    NONE = "NONE"
    RECONNECT_ACCOUNT = "RECONNECT_ACCOUNT"
    OPEN_BALANCE = "OPEN_BALANCE"
    OPEN_QUEUE = "OPEN_QUEUE"
    OPEN_RECOVERY = "OPEN_RECOVERY"
    OPEN_RADAR = "OPEN_RADAR"
    OPEN_NEURO = "OPEN_NEURO"
    OPEN_ANALYTICS = "OPEN_ANALYTICS"
    OPEN_SCHEDULE = "OPEN_SCHEDULE"


class SubscriptionSummary(IntelligenceModel):
    plan: str = "free"
    status: str = "active"


class AnalyticsSummary(IntelligenceModel):
    available: bool = False
    stale: bool = False
    posts_total: int = Field(default=0, ge=0)
    average_views: float | None = Field(default=None, ge=0)
    engagement_rate: float | None = Field(default=None, ge=0)
    brain_score: float | None = Field(default=None, ge=0, le=100)
    best_topic: str | None = None
    best_hour: int | None = Field(default=None, ge=0, le=23)
    updated_at: datetime | None = None


class BrainSummary(IntelligenceModel):
    available: bool = False
    version: int | None = Field(default=None, ge=1)
    primary_goal: str = ""
    performance_posts: int = Field(default=0, ge=0)
    updated_at: datetime | None = None


class RadarSummary(IntelligenceModel):
    available: bool = False
    active: bool = False
    ready_count: int = Field(default=0, ge=0)
    best_score: float | None = Field(default=None, ge=0, le=100)
    last_search_at: datetime | None = None
    last_status: str | None = None
    search_due: bool = False


class NeuroSummary(IntelligenceModel):
    available: bool = False
    active: bool = False
    mode: str = "approve"
    pending_count: int = Field(default=0, ge=0)
    posted_today: int = Field(default=0, ge=0)
    daily_cap: int = Field(default=0, ge=0)
    publishing_count: int = Field(default=0, ge=0)
    unknown_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    permission_denied: bool = False


class LastDecisionSummary(IntelligenceModel):
    decision_hash: str
    status: DecisionStatus
    health_score: int = Field(ge=0, le=100)
    created_at: datetime


class DecisionContext(IntelligenceModel):
    context_version: int = Field(default=CONTEXT_VERSION, ge=1)
    user_id: int
    threads_account_id: int
    connection_status: str
    has_access_token: bool
    token_expires_at: datetime | None = None
    queue_size: int = Field(default=0, ge=0)
    scheduled_today: int = Field(default=0, ge=0)
    published_today: int = Field(default=0, ge=0)
    failed_today: int = Field(default=0, ge=0)
    stuck_publishing: int = Field(default=0, ge=0)
    unknown_publications: int = Field(default=0, ge=0)
    analytics_summary: AnalyticsSummary = Field(default_factory=AnalyticsSummary)
    brain_summary: BrainSummary = Field(default_factory=BrainSummary)
    radar_summary: RadarSummary = Field(default_factory=RadarSummary)
    neuro_summary: NeuroSummary = Field(default_factory=NeuroSummary)
    credits_balance: int = Field(default=0, ge=0)
    subscription: SubscriptionSummary = Field(
        default_factory=SubscriptionSummary
    )
    timezone: str = "Europe/Moscow"
    goal: str = ""
    topics: tuple[str, ...] = ()
    posts_per_day: int = Field(default=0, ge=0, le=5)
    planner_enabled: bool = False
    publisher_enabled: bool = False
    analytics_available: bool = False
    last_publish: datetime | None = None
    last_generation: datetime | None = None
    last_decision: LastDecisionSummary | None = None
    queue_health: QueueHealth = QueueHealth.DISABLED
    autopilot_active: bool = False
    current_time: datetime

    def stable_payload(self) -> dict[str, Any]:
        payload = self.model_dump(
            mode="json",
            exclude={"current_time", "last_decision"},
        )
        return payload

    def context_hash(self) -> str:
        encoded = json.dumps(
            self.stable_payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class RuleResult(IntelligenceModel):
    rule_id: str = Field(min_length=1, max_length=100)
    rule_version: int = Field(default=1, ge=1)
    kind: RuleKind
    reason_code: str = Field(min_length=1, max_length=80)
    priority: int = Field(default=0, ge=0, le=100)
    action: ActionType = ActionType.NONE
    parameters: dict[str, Any] = Field(default_factory=dict)


class HealthBreakdown(IntelligenceModel):
    token: int = Field(ge=0, le=20)
    credits: int = Field(ge=0, le=15)
    queue: int = Field(ge=0, le=20)
    analytics: int = Field(ge=0, le=15)
    radar: int = Field(ge=0, le=10)
    neuro: int = Field(ge=0, le=10)
    publishing: int = Field(ge=0, le=10)
    total: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def total_matches_components(self):
        expected = (
            self.token
            + self.credits
            + self.queue
            + self.analytics
            + self.radar
            + self.neuro
            + self.publishing
        )
        if self.total != expected:
            raise ValueError("health total must equal component sum")
        return self


class DecisionResult(IntelligenceModel):
    status: DecisionStatus
    health_score: int = Field(ge=0, le=100)
    health_breakdown: HealthBreakdown
    priority: int = Field(ge=0, le=100)
    recommendation: str = Field(min_length=1, max_length=80)
    reason_codes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    safe_action: ActionType = ActionType.NONE
    next_check: datetime
    next_recommended_action: ActionType = ActionType.NONE
    human_message: str
    generated_at: datetime
    rules_version: int = Field(default=RULES_VERSION, ge=1)

    def stable_payload(self) -> dict[str, Any]:
        return self.model_dump(
            mode="json",
            exclude={"generated_at", "next_check"},
        )

    def decision_hash(self) -> str:
        encoded = json.dumps(
            self.stable_payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class DecisionRun(IntelligenceModel):
    id: int
    user_id: int
    threads_account_id: int
    context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: DecisionResult
    created_at: datetime


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
