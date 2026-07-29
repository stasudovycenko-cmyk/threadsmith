"""Pydantic contracts for structured LLM responses."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

HookType = Literal[
    "pain",
    "number",
    "myth",
    "list",
    "story",
    "ban",
    "compare",
    "question",
    "insight",
    "provocation",
    "unpopular",
]


class LLMResponse(BaseModel):
    # Preserve additional LLM fields just as the previous raw-dict path did.
    model_config = ConfigDict(extra="allow", strict=True)


class VoiceProfileResponse(LLMResponse):
    lexicon: list[str]
    sentence_length: str
    punctuation: str
    tone: str
    structure: str
    taboo: list[str]
    sample_phrases: list[str]


class Hook(LLMResponse):
    type: HookType
    text: str


class PostGenerationResponse(LLMResponse):
    hooks: list[Hook]
    body: str


class ThreadGenerationResponse(LLMResponse):
    posts: list[str]


class RadarAnalysisResponse(LLMResponse):
    hook: str
    structure: str
    trigger: str
    ending: str
    how_to_repeat: str
    hook_type: HookType


class NeuroCommentResponse(LLMResponse):
    relevant: bool
    skip_reason: str | None
    comment: str | None
