"""Typed contracts for account-scoped performance analytics."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

AnalyticsDimension = Literal[
    "topic",
    "hook_type",
    "cta_type",
    "publish_hour",
    "weekday",
]


class AnalyticsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class AnalyticsMetrics(AnalyticsModel):
    views: int | None = Field(default=None, ge=0)
    likes: int | None = Field(default=None, ge=0)
    replies: int | None = Field(default=None, ge=0)
    quotes: int | None = Field(default=None, ge=0)
    reposts: int | None = Field(default=None, ge=0)
    shares: int | None = Field(default=None, ge=0)
    profile_visits: int | None = Field(default=None, ge=0)
    followers: int | None = Field(default=None, ge=0)
    engagement_rate: float | None = Field(default=None, ge=0)


class ProviderAnalyticsPost(AnalyticsModel):
    post_id: str = Field(min_length=1)
    published_at: AwareDatetime
    text: str = ""


class PublishedAnalyticsPost(AnalyticsModel):
    scheduled_post_id: int | None = None
    user_id: int
    threads_account_id: int
    threads_post_id: str = Field(min_length=1)
    published_at: AwareDatetime
    text: str = ""
    timezone: str = "UTC"
    hook_type: str | None = None
    cta_type: str | None = None
    topic: str | None = None


class PreviousAnalyticsSnapshot(AnalyticsModel):
    snapshot_at: AwareDatetime
    metrics: AnalyticsMetrics


class AnalyticsBaseline(AnalyticsModel):
    avg_views: float | None = Field(default=None, ge=0)
    avg_engagement_rate: float | None = Field(default=None, ge=0)


class AnalyticsFeatureBenchmarks(AnalyticsModel):
    topic_score: float = Field(default=50, ge=0, le=100)
    hook_score: float = Field(default=50, ge=0, le=100)
    cta_score: float = Field(default=50, ge=0, le=100)


class AnalyticsScores(AnalyticsModel):
    performance_score: float = Field(ge=0, le=100)
    virality_score: float = Field(ge=0, le=100)
    brain_score: float = Field(ge=0, le=100)


class AnalyticsSnapshotWrite(AnalyticsModel):
    post: PublishedAnalyticsPost
    provider: str = Field(min_length=1)
    snapshot_at: AwareDatetime
    bucket_at: AwareDatetime
    metrics: AnalyticsMetrics
    scores: AnalyticsScores


class AnalyticsAccountSummary(AnalyticsModel):
    user_id: int
    threads_account_id: int
    posts_total: int = Field(ge=0)
    views_total: int | None = Field(default=None, ge=0)
    likes_total: int | None = Field(default=None, ge=0)
    comments_total: int | None = Field(default=None, ge=0)
    shares_total: int | None = Field(default=None, ge=0)
    avg_er: float | None = Field(default=None, ge=0)
    avg_views: float | None = Field(default=None, ge=0)
    best_post_id: str | None = None
    worst_post_id: str | None = None
    best_hour: int | None = Field(default=None, ge=0, le=23)
    best_weekday: int | None = Field(default=None, ge=0, le=6)
    best_topic: str | None = None
    best_hook: str | None = None
    best_cta: str | None = None
    brain_score: float | None = Field(default=None, ge=0, le=100)
    updated_at: AwareDatetime


class AnalyticsCollectionResult(AnalyticsModel):
    user_id: int
    threads_account_id: int
    posts_seen: int = Field(ge=0)
    snapshots_written: int = Field(ge=0)
    failures: int = Field(ge=0)
    summary: AnalyticsAccountSummary | None = None
