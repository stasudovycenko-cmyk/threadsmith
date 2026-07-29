"""Budgeted, task-specific prompt contexts built from one Brain."""

import copy
import json
import math
from typing import Any

from app.core.brain_repo import BrainNotFoundError, BrainRepo
from app.schemas.social_brain import (
    BrainRecord,
    BrainTask,
    BrainTaskContext,
    drop_empty,
)

PATTERN_MIN_SAMPLES = 5
PATTERN_MIN_CONFIDENCE = 0.70
PATTERN_CONTEXT_LIMIT = 12

# Russian JSON commonly uses more characters per token than English.
# Three chars/token intentionally overestimates rather than underestimates.
ESTIMATED_CHARS_PER_TOKEN = 3

_SUPPORTED_TASKS = {
    "generation",
    "radar",
    "neuro",
    "analytics",
    "autocontent",
}

_TRIM_ORDER = {
    "generation": (
        "recent_examples",
        "style_examples",
        "manual_overrides",
        "patterns",
        "audience",
    ),
    "radar": (
        "performance",
        "patterns",
        "audience",
    ),
    "neuro": (
        "manual_overrides",
        "patterns",
        "audience",
    ),
    "analytics": (
        "patterns",
        "audience",
    ),
    "autocontent": (
        "recent_examples",
        "style_examples",
        "performance",
        "manual_overrides",
        "patterns",
        "audience",
    ),
}


def estimate_text_tokens(value: str) -> int:
    if not value:
        return 0
    return math.ceil(len(value) / ESTIMATED_CHARS_PER_TOKEN)


def estimate_tokens(value: dict[str, Any]) -> int:
    if not value:
        return 0
    serialized = json.dumps(
        drop_empty(value),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return estimate_text_tokens(serialized)


def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return copy.deepcopy(value)
    return None


def _merge_dicts(*values: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for value in values:
        if isinstance(value, dict):
            merged.update(copy.deepcopy(value))
    return merged


def _public_section(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(item)
        for key, item in value.items()
        if not key.startswith("_")
    }


def _brain_parts(brain: BrainRecord) -> dict[str, Any]:
    dna = _public_section(brain.dna)
    audience = _public_section(brain.audience)
    goals = _public_section(brain.goals)
    constraints = _public_section(brain.constraints)
    performance = _public_section(brain.performance)
    voice = dna.get("voice")

    style_examples = _first_present(
        dna,
        "style_examples",
        "sample_phrases",
    )
    if isinstance(voice, dict):
        style_examples = (
            style_examples
            or _first_present(voice, "sample_phrases")
        )
        voice.pop("sample_phrases", None)

    recent_examples = _first_present(
        dna,
        "recent_examples",
        "examples",
    )
    manual_overrides = _merge_dicts(
        dna.get("manual_overrides"),
        goals.get("manual_overrides"),
        constraints.get("manual_overrides"),
    )

    for key in (
        "style_examples",
        "sample_phrases",
        "recent_examples",
        "examples",
        "manual_overrides",
    ):
        dna.pop(key, None)

    primary_goal = _first_present(
        goals,
        "primary",
        "primary_goal",
    )
    critical_constraints = _first_present(
        constraints,
        "critical",
        "critical_constraints",
    )
    autocontent = _first_present(
        constraints,
        "autocontent",
        "autocontent_settings",
    )
    return {
        "dna": dna,
        "primary_goal": primary_goal,
        "critical_constraints": critical_constraints,
        "audience": audience,
        "manual_overrides": manual_overrides,
        "style_examples": style_examples,
        "recent_examples": recent_examples,
        "autocontent": autocontent,
        "performance": performance,
    }


def _task_payload(
    task: BrainTask,
    parts: dict[str, Any],
    patterns: list[dict[str, Any]],
) -> dict[str, Any]:
    if task == "generation":
        return {
            "dna": parts["dna"],
            "primary_goal": parts["primary_goal"],
            "critical_constraints": parts["critical_constraints"],
            "audience": parts["audience"],
            "patterns": patterns,
            "manual_overrides": parts["manual_overrides"],
            "style_examples": parts["style_examples"],
            "recent_examples": parts["recent_examples"],
        }
    if task == "radar":
        return {
            "primary_goal": parts["primary_goal"],
            "critical_constraints": parts["critical_constraints"],
            "audience": parts["audience"],
            "patterns": patterns,
            "performance": parts["performance"],
        }
    if task == "neuro":
        return {
            "dna": parts["dna"],
            "critical_constraints": parts["critical_constraints"],
            "audience": parts["audience"],
            "patterns": patterns,
            "manual_overrides": parts["manual_overrides"],
        }
    if task == "analytics":
        return {
            "primary_goal": parts["primary_goal"],
            "audience": parts["audience"],
            "performance": parts["performance"],
            "patterns": patterns,
        }
    return {
        "dna": parts["dna"],
        "primary_goal": parts["primary_goal"],
        "critical_constraints": parts["critical_constraints"],
        "audience": parts["audience"],
        "patterns": patterns,
        "manual_overrides": parts["manual_overrides"],
        "autocontent": parts["autocontent"],
        "performance": parts["performance"],
        "style_examples": parts["style_examples"],
        "recent_examples": parts["recent_examples"],
    }


def _trim_to_budget(
    payload: dict[str, Any],
    task: BrainTask,
    budget_tokens: int,
) -> tuple[dict[str, Any], list[str]]:
    result = drop_empty(copy.deepcopy(payload))
    trimmed: list[str] = []

    for field in _TRIM_ORDER[task]:
        while estimate_tokens(result) > budget_tokens:
            value = result.get(field)
            if value is None:
                break
            if isinstance(value, list) and len(value) > 1:
                value.pop()
            else:
                result.pop(field, None)
            if field not in trimmed:
                trimmed.append(field)
        if estimate_tokens(result) <= budget_tokens:
            break

    return result, trimmed


class ContextBuilder:
    """Builds compact contexts without process-local caching."""

    def __init__(self, repo: BrainRepo):
        self.repo = repo

    async def build_context(
        self,
        brain_id: int,
        task: BrainTask,
        budget_tokens: int,
    ) -> BrainTaskContext:
        if task not in _SUPPORTED_TASKS:
            raise ValueError(f"unsupported Brain task: {task}")
        if budget_tokens < 1:
            raise ValueError("budget_tokens must be positive")

        brain = await self.repo.get(brain_id)
        if brain is None:
            raise BrainNotFoundError(f"Brain {brain_id} does not exist")
        patterns = await self.repo.get_patterns(
            brain_id,
            min_samples=PATTERN_MIN_SAMPLES,
            min_confidence=PATTERN_MIN_CONFIDENCE,
            limit=PATTERN_CONTEXT_LIMIT,
        )
        payload = _task_payload(
            task,
            _brain_parts(brain),
            [pattern.prompt_dict() for pattern in patterns],
        )
        compact, trimmed = _trim_to_budget(
            payload,
            task,
            budget_tokens,
        )
        return BrainTaskContext(
            task=task,
            context=compact,
            budget_tokens=budget_tokens,
            estimated_tokens=estimate_tokens(compact),
            trimmed_fields=trimmed,
        )
