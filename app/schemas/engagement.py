"""Typed contracts for account-scoped Radar and Neurocommenting v2."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.llm import LLMResponse

CommentStrategy = Literal[
    "useful_addition",
    "personal_observation",
    "clarifying_question",
    "gentle_disagreement",
    "short_insight",
    "specific_support",
    "mini_story",
    "professional_opinion",
]
RadarCandidateStatus = Literal[
    "discovered",
    "scoring",
    "ready",
    "generating",
    "pending",
    "commented",
    "rejected",
    "filtered",
    "score_failed",
    "score_blocked",
]
NeuroCommentStatus = Literal[
    "generating",
    "pending",
    "publishing",
    "posted",
    "rejected",
    "skipped",
    "failed",
    "unknown",
    "permission_denied",
]


class EngagementModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RadarSemanticScoreResponse(LLMResponse):
    relevant: bool
    topical_relevance: int = Field(ge=0, le=100)
    conversation_potential: int = Field(ge=0, le=100)
    safe: bool
    reason: str = Field(min_length=1, max_length=240)


class NeuroCommentV2Response(LLMResponse):
    relevant: bool
    skip_reason: str | None = Field(default=None, max_length=240)
    strategy: CommentStrategy
    comment: str | None = Field(default=None, max_length=280)


class RadarSettings(EngagementModel):
    user_id: int
    threads_account_id: int
    niche: str = ""
    keywords: list[str] = Field(default_factory=list)
    language: Literal["ru", "en", "any"] = "ru"
    max_age_hours: int = Field(default=72, ge=1, le=168)


class RadarCandidate(EngagementModel):
    id: int
    user_id: int
    threads_account_id: int
    threads_post_id: str
    author_key: str
    author_threads_id: str | None = None
    author_username: str | None = None
    text: str
    permalink: str | None = None
    published_at: datetime | None = None
    found_keywords: list[str] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)
    deterministic_score: int = Field(ge=0, le=100)
    semantic_score: int | None = Field(default=None, ge=0, le=100)
    final_score: int | None = Field(default=None, ge=0, le=100)
    score_reason: str | None = None
    status: RadarCandidateStatus


class DeterministicScore(EngagementModel):
    total: int = Field(ge=0, le=100)
    topical_relevance: int = Field(ge=0, le=35)
    freshness: int = Field(ge=0, le=20)
    engagement_potential: int = Field(ge=0, le=15)
    conversation_potential: int = Field(ge=0, le=20)
    author_penalty: int = Field(ge=0, le=20)
    duplicate_penalty: int = Field(ge=0, le=10)
    safe: bool
    filter_reason: str | None = None
    summary: str


class RadarSearchSummary(EngagementModel):
    run_id: int | None = None
    searched_keywords: int = 0
    results_seen: int = 0
    candidates_saved: int = 0
    filtered: int = 0
    duplicates: int = 0
    status: Literal["success", "permission_denied", "failed"] = "success"
    error_code: str | None = None


class PublishClaim(EngagementModel):
    comment_id: int
    claim_token: str
    user_id: int
    threads_account_id: int
    target_post_id: str
    comment_text: str
    threads_user_id: str
    access_token_enc: bytes
    author_key: str
    author_username: str | None = None
