"""Typed contracts for the account-scoped Telegram UX."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

InterfaceMode = Literal["simple", "advanced"]
OnboardingStatus = Literal[
    "not_started",
    "in_progress",
    "completed",
    "skipped",
]


class UXModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserUXPreferences(UXModel):
    user_id: int
    interface_mode: InterfaceMode = "simple"


class OnboardingProgress(UXModel):
    user_id: int
    threads_account_id: int
    status: OnboardingStatus = "not_started"
    current_step: int = Field(default=0, ge=0, le=9)
    data: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime | None = None


class DashboardAutopilot(UXModel):
    available: bool = True
    enabled: bool = False
    posts_today: int | None = None
    daily_limit: int | None = None
    queue_size: int | None = None
    next_post_at: datetime | None = None
    timezone: str = "Europe/Moscow"
    warning: str | None = None


class DashboardRadar(UXModel):
    available: bool = True
    ready_count: int | None = None
    last_search_at: datetime | None = None
    last_status: str | None = None
    warning: str | None = None


class DashboardNeuro(UXModel):
    available: bool = True
    enabled: bool = False
    posted_today: int | None = None
    pending_count: int | None = None
    warning: str | None = None


class DashboardAnalytics(UXModel):
    available: bool = True
    posts_7d: int | None = None
    views_7d: int | None = None
    avg_er_7d: float | None = None
    posts_30d: int | None = None
    views_30d: int | None = None
    avg_er: float | None = None
    brain_score: float | None = None
    warning: str | None = None


class DashboardBalance(UXModel):
    available: bool = True
    credits: int = 0
    plan: str = "free"
    warning: str | None = None


class DashboardIntelligence(UXModel):
    available: bool = False
    run_id: int | None = None
    status: str | None = None
    health_score: int | None = Field(default=None, ge=0, le=100)
    human_message: str | None = None
    safe_action: str | None = None
    created_at: datetime | None = None
    warning: str | None = None


class DashboardData(UXModel):
    user_id: int
    account_id: int
    username: str
    connection_status: str = "connected"
    interface_mode: InterfaceMode = "simple"
    autopilot: DashboardAutopilot = Field(default_factory=DashboardAutopilot)
    radar: DashboardRadar = Field(default_factory=DashboardRadar)
    neuro: DashboardNeuro = Field(default_factory=DashboardNeuro)
    analytics: DashboardAnalytics = Field(default_factory=DashboardAnalytics)
    balance: DashboardBalance = Field(default_factory=DashboardBalance)
    intelligence: DashboardIntelligence = Field(
        default_factory=DashboardIntelligence
    )


class AccountUXSettings(UXModel):
    user_id: int
    threads_account_id: int
    username: str
    manual_style: str | None = None
    style_examples: list[str] = Field(default_factory=list, max_length=10)
    topics: list[str] = Field(default_factory=list)
    radar_keywords: list[str] = Field(default_factory=list)
    timezone: str = "Europe/Moscow"
    autopilot_enabled: bool = False
    publish_notifications_enabled: bool = True


class ActivityItem(UXModel):
    event_type: str
    occurred_at: datetime
    title: str
    detail: str | None = None


class BrainRecommendation(UXModel):
    kind: str
    title: str
    detail: str
    sample_size: int = Field(default=0, ge=0)
