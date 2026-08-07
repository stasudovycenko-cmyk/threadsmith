"""Typed contracts for the Content Engine 2.0 pipeline."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.llm import HookType

ContentAngle = Literal[
    "contrarian",
    "personal_story",
    "mistake",
    "case_study",
    "list",
    "observation",
    "confession",
    "how_to",
    "prediction",
    "comparison",
    "myth_busting",
]
ContentFormat = Literal[
    "compact_post",
    "story",
    "list",
    "how_to",
    "comparison",
    "case_study",
    "observation",
]


class ContentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ContentBrief(ContentModel):
    goal: str | None = None
    topic: str | None = None
    audience: str | None = None
    desired_action: str | None = None
    angle: ContentAngle | None = None
    hook_strategy: str | None = None
    format: ContentFormat | None = None
    tone: str | None = None
    constraints: list[str] = Field(default_factory=list)
    pattern_hints: list[str] = Field(default_factory=list)
    performance_context: str | None = None
    avoid: list[str] = Field(default_factory=list)
    source: str | None = None


class ContentHook(ContentModel):
    type: HookType
    text: str = Field(min_length=1)
    intent: str = Field(min_length=1)


class ContentQuality(ContentModel):
    clarity: float = Field(ge=0, le=1)
    hook_strength: float = Field(ge=0, le=1)
    specificity: float = Field(ge=0, le=1)
    voice_match: float = Field(ge=0, le=1)
    goal_fit: float = Field(ge=0, le=1)


class ContentDraftHook(ContentModel):
    """Minimal hook shape produced by the LLM."""

    type: HookType
    text: str = Field(min_length=1)


class ContentGenerationDraft(ContentModel):
    """Private low-token response before deterministic canonicalization."""

    hooks: list[ContentDraftHook] = Field(min_length=3, max_length=3)
    body: str
    selected_hook_index: int = Field(default=0, ge=0, le=2)
    specificity: float = Field(default=0.5, ge=0, le=1)


class ContentMetadata(ContentModel):
    goal: str
    angle: ContentAngle
    hook_type: HookType
    format: ContentFormat
    topic: str
    has_cta: bool
    cta_type: str | None = None
    source: str
    brain_version: int | None = Field(default=None, ge=1)
    pattern_ids: list[int] = Field(default_factory=list)
    pattern_keys: list[str] = Field(default_factory=list)
    selected_hook_index: int = Field(default=0, ge=0, le=2)
    selected_hook: str = ""
    pipeline_stage: Literal["generate", "repair"] = "generate"
    deterministic_fixes: list[str] = Field(default_factory=list)
    repair_reasons: list[str] = Field(default_factory=list)
    user_id: int | None = None
    threads_account_id: int | None = None


class ContentGenerationResponse(ContentModel):
    brief: ContentBrief = Field(default_factory=ContentBrief)
    hooks: list[ContentHook] = Field(min_length=3, max_length=3)
    body: str
    metadata: ContentMetadata
    quality: ContentQuality

    @model_validator(mode="after")
    def selected_hook_is_consistent(self):
        selected = self.hooks[self.metadata.selected_hook_index]
        if selected.type != self.metadata.hook_type:
            raise ValueError(
                "metadata.hook_type must match the selected hook"
            )
        return self
