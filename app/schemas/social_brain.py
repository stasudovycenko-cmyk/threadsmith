"""Typed boundaries for account-scoped Social Brain data."""

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

BrainSection = Literal[
    "dna",
    "audience",
    "goals",
    "constraints",
    "performance",
]
BrainTask = Literal[
    "generation",
    "radar",
    "neuro",
    "analytics",
    "autocontent",
]


def drop_empty(value: Any) -> Any:
    if isinstance(value, dict):
        result = {
            key: drop_empty(item)
            for key, item in value.items()
            if item is not None
        }
        return {
            key: item
            for key, item in result.items()
            if item not in (None, "", [], {})
        }
    if isinstance(value, list):
        return [
            compact
            for item in value
            if (compact := drop_empty(item)) not in (None, "", [], {})
        ]
    return value


class BrainModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BrainRecord(BrainModel):
    id: int
    user_id: int
    threads_account_id: int
    dna: dict[str, Any] = Field(default_factory=dict)
    audience: dict[str, Any] = Field(default_factory=dict)
    goals: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    performance: dict[str, Any] = Field(default_factory=dict)
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime


class BrainPattern(BrainModel):
    id: int
    brain_id: int
    kind: str = Field(min_length=1)
    key: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    lift: float
    samples: int = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    updated_at: datetime

    def prompt_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "metric": self.metric,
            "lift": self.lift,
            "samples": self.samples,
            "confidence": self.confidence,
        }


class BrainEvent(BrainModel):
    id: int
    brain_id: int
    type: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    source_type: str | None = None
    source_id: str | None = None
    event_key: str | None = None
    occurred_at: datetime
    created_at: datetime


class BrainTaskContext(BrainModel):
    """Compact prompt payload plus non-prompt sizing metadata."""

    task: BrainTask
    context: dict[str, Any] = Field(default_factory=dict)
    budget_tokens: int = Field(ge=1)
    estimated_tokens: int = Field(ge=0)
    trimmed_fields: list[str] = Field(default_factory=list)

    @property
    def within_budget(self) -> bool:
        return self.estimated_tokens <= self.budget_tokens

    def compact_dict(self) -> dict[str, Any]:
        return drop_empty(self.context)

    def compact_json(self) -> str:
        return json.dumps(
            self.compact_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def character_count(self) -> int:
        return len(self.compact_json())
