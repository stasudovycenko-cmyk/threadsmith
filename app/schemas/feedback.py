"""Typed boundaries for deterministic goal-aware feedback."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

GoalName = Literal[
    "reach",
    "engagement",
    "followers",
    "traffic",
    "leads",
    "unknown",
]
FeedbackMetric = Literal["views", "engagement_rate"]
FeedbackStatus = Literal[
    "ok",
    "insufficient_data",
    "unsupported",
    "no_new_data",
]


class FeedbackModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class GoalSelection(FeedbackModel):
    raw: str | None = None
    normalized: GoalName
    metric: FeedbackMetric | None = None
    supported: bool


class PostPerformance(FeedbackModel):
    scheduled_post_id: int
    user_id: int
    threads_account_id: int
    threads_post_id: str = Field(min_length=1)
    published_at: datetime
    snapshot_date: date
    text: str = Field(default="", exclude=True)
    text_length: int = Field(ge=0)
    has_link: bool
    views: int | None = Field(default=None, ge=0)
    likes: int | None = Field(default=None, ge=0)
    replies: int | None = Field(default=None, ge=0)
    reposts: int | None = Field(default=None, ge=0)
    quotes: int | None = Field(default=None, ge=0)
    shares: int | None = Field(default=None, ge=0)
    engagement: int | None = Field(default=None, ge=0)
    engagement_rate: float | None = Field(default=None, ge=0)
    available_metrics: tuple[str, ...] = ()


class PostBaseline(FeedbackModel):
    metric: FeedbackMetric
    value: float | None = None
    samples: int = Field(ge=0)


class GoalScore(FeedbackModel):
    goal: GoalName
    metric: FeedbackMetric | None = None
    post_value: float | None = None
    baseline_value: float | None = None
    baseline_samples: int = Field(default=0, ge=0)
    lift: float | None = None
    status: Literal["ok", "insufficient_data", "unsupported"]


class PostFeature(FeedbackModel):
    kind: str = Field(min_length=1)
    key: str = Field(min_length=1)


class BrainPatternWrite(FeedbackModel):
    kind: str = Field(min_length=1)
    key: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    lift: float
    samples: int = Field(ge=1)
    confidence: float = Field(ge=0, le=1)


class PatternAggregate(BrainPatternWrite):
    metric: FeedbackMetric
    dispersion: float = Field(ge=0)


class AccountFeedbackResult(FeedbackModel):
    brain_id: int
    user_id: int
    threads_account_id: int
    status: FeedbackStatus
    changed: bool
    posts_analyzed: int = Field(ge=0)
    patterns_written: int = Field(ge=0)
    brain_version: int = Field(ge=1)
