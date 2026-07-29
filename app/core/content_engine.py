"""Deterministic planning, memory, and quality gates for Content Engine 2.0."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context_builder import (
    PATTERN_MIN_CONFIDENCE,
    PATTERN_MIN_SAMPLES,
)
from app.core.goal_metrics import normalize_goal
from app.schemas.content_engine import (
    ContentAngle,
    ContentBrief,
    ContentFormat,
    ContentGenerationResponse,
)
from app.schemas.llm import HookType
from app.schemas.social_brain import BrainTaskContext

log = logging.getLogger("content_engine")

CONTENT_ANGLES: tuple[ContentAngle, ...] = (
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
)

_GOAL_ANGLES: dict[str, tuple[ContentAngle, ...]] = {
    "reach": (
        "contrarian",
        "myth_busting",
        "comparison",
        "prediction",
        "observation",
        "mistake",
        "list",
        "case_study",
        "personal_story",
        "how_to",
        "confession",
    ),
    "engagement": (
        "confession",
        "personal_story",
        "observation",
        "contrarian",
        "mistake",
        "comparison",
        "prediction",
        "case_study",
        "myth_busting",
        "how_to",
        "list",
    ),
}

_ANGLE_HOOKS: dict[ContentAngle, tuple[HookType, ...]] = {
    "contrarian": ("unpopular", "provocation", "myth"),
    "personal_story": ("story", "insight", "question"),
    "mistake": ("pain", "ban", "number"),
    "case_study": ("number", "story", "insight"),
    "list": ("list", "number", "pain"),
    "observation": ("insight", "question", "provocation"),
    "confession": ("story", "unpopular", "insight"),
    "how_to": ("number", "pain", "list"),
    "prediction": ("provocation", "insight", "question"),
    "comparison": ("compare", "number", "myth"),
    "myth_busting": ("myth", "unpopular", "ban"),
}

_ANGLE_FORMAT: dict[ContentAngle, ContentFormat] = {
    "personal_story": "story",
    "confession": "story",
    "list": "list",
    "how_to": "how_to",
    "comparison": "comparison",
    "case_study": "case_study",
    "observation": "observation",
}

_BANNED_PHRASES = (
    "вот что понял:",
    "разложу по шагам:",
    "что это значит на практике:",
    "вот и весь секрет",
    "все думают",
    "все ищут",
    "все считают",
)
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF]",
    flags=re.UNICODE,
)
_TOKEN_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)

_LOAD_SCHEDULED_MEMORY_SQL = text("""
    SELECT text, content_metadata
    FROM scheduled_posts
    WHERE user_id = :uid
      AND threads_account_id = :account_id
      AND status IN ('pending', 'publishing', 'done')
    ORDER BY run_at DESC, id DESC
    LIMIT :memory_limit
""")

_LOAD_GENERATION_MEMORY_SQL = text("""
    SELECT output
    FROM generations
    WHERE user_id = :uid
      AND type = 'generate_post'
      AND output -> 'metadata' ->> 'threads_account_id' = :account_id
    ORDER BY created_at DESC, id DESC
    LIMIT :memory_limit
""")


@dataclass(frozen=True)
class ContentMemoryItem:
    opening: str
    angle: str | None = None
    hook_type: str | None = None
    topic: str | None = None
    format: str | None = None
    source: str | None = None

    def prompt_dict(self) -> dict[str, str]:
        result = {"opening": self.opening[:100]}
        for key in ("angle", "hook_type", "topic", "format", "source"):
            value = getattr(self, key)
            if value:
                result[key] = value[:100]
        return result


@dataclass(frozen=True)
class ContentPlan:
    brief: ContentBrief
    pattern_ids: tuple[int, ...] = ()
    pattern_keys: tuple[str, ...] = ()
    goal_metric: str | None = None


@dataclass(frozen=True)
class QualityGateResult:
    passed: bool
    reasons: tuple[str, ...] = ()


def normalize_content_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    normalized = _PUNCT_RE.sub(" ", normalized)
    return " ".join(normalized.split())


def opening_line(value: str) -> str:
    for line in (value or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def token_overlap(left: str, right: str) -> float:
    left_tokens = set(_TOKEN_RE.findall(normalize_content_text(left)))
    right_tokens = set(_TOKEN_RE.findall(normalize_content_text(right)))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / min(
        len(left_tokens),
        len(right_tokens),
    )


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _memory_item(text_value: Any, metadata_value: Any) -> ContentMemoryItem:
    metadata = _json_dict(metadata_value)
    text_opening = opening_line(
        text_value if isinstance(text_value, str) else ""
    )
    return ContentMemoryItem(
        opening=str(metadata.get("selected_hook") or text_opening),
        angle=_optional_str(metadata.get("angle")),
        hook_type=_optional_str(metadata.get("hook_type")),
        topic=_optional_str(metadata.get("topic")),
        format=_optional_str(metadata.get("format")),
        source=_optional_str(metadata.get("source")),
    )


def _generation_memory_item(output_value: Any) -> ContentMemoryItem:
    output = _json_dict(output_value)
    metadata = _json_dict(output.get("metadata"))
    selected = _json_dict(output.get("selected_hook"))
    opening = str(
        metadata.get("selected_hook")
        or selected.get("text")
        or ""
    )
    if not opening:
        hooks = output.get("hooks")
        if isinstance(hooks, list) and hooks:
            hook = _json_dict(hooks[0])
            opening = str(hook.get("text") or "")
    return _memory_item(opening, metadata)


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


class ContentMemoryRepo:
    """Loads a compact, account-scoped anti-repeat window."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def load(
        self,
        user_id: int | None,
        threads_account_id: int | None,
        *,
        limit: int = 12,
    ) -> list[ContentMemoryItem]:
        if user_id is None or threads_account_id is None:
            return []
        per_source = max(1, min(limit, 12))
        params = {
            "uid": user_id,
            "account_id": str(threads_account_id),
            "memory_limit": per_source,
        }
        scheduled = (
            await self.session.execute(
                _LOAD_SCHEDULED_MEMORY_SQL,
                {
                    **params,
                    "account_id": threads_account_id,
                },
            )
        ).mappings().all()
        generations = (
            await self.session.execute(
                _LOAD_GENERATION_MEMORY_SQL,
                params,
            )
        ).mappings().all()

        candidates = [
            _memory_item(row.get("text"), row.get("content_metadata"))
            for row in scheduled
        ]
        candidates.extend(
            _generation_memory_item(row.get("output"))
            for row in generations
        )
        result = []
        seen = set()
        for item in candidates:
            normalized = normalize_content_text(item.opening)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            result.append(item)
            if len(result) >= limit:
                break
        return result


def _compact_value(value: Any, max_chars: int = 220) -> str | None:
    if value in (None, "", [], {}):
        return None
    if isinstance(value, str):
        compact = " ".join(value.split())
    else:
        compact = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return compact[:max_chars] or None


def _flatten_constraints(value: Any) -> list[str]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, dict):
        return [
            f"{key}: {_compact_value(item, 100)}"
            for key, item in value.items()
            if _compact_value(item, 100)
        ]
    if isinstance(value, list):
        return [
            compact
            for item in value
            if (compact := _compact_value(item, 120))
        ]
    compact = _compact_value(value, 120)
    return [compact] if compact else []


def _stable_offset(topic: str, goal: str, length: int) -> int:
    digest = hashlib.sha256(
        f"{goal}:{normalize_content_text(topic)}".encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:4], "big") % length


def _select_angle(
    topic: str,
    goal: str,
    memory: Sequence[ContentMemoryItem],
) -> ContentAngle:
    candidates = _GOAL_ANGLES.get(goal, CONTENT_ANGLES)
    offset = _stable_offset(topic, goal, len(candidates))
    ordered = candidates[offset:] + candidates[:offset]
    recent_angles = {
        item.angle
        for item in memory[:4]
        if item.angle
    }
    return next(
        (angle for angle in ordered if angle not in recent_angles),
        ordered[0],
    )


def _select_hook_strategy(
    angle: ContentAngle,
    memory: Sequence[ContentMemoryItem],
) -> str:
    recent_hooks = {
        item.hook_type
        for item in memory[:4]
        if item.hook_type
    }
    hooks = _ANGLE_HOOKS[angle]
    hook_type = next(
        (item for item in hooks if item not in recent_hooks),
        hooks[0],
    )
    return (
        f"{hook_type}: express the {angle} angle immediately; "
        "earn attention without clickbait"
    )


def _pattern_hint(pattern: Mapping[str, Any]) -> str:
    lift = float(pattern.get("lift") or 0)
    direction = "+" if lift >= 0 else ""
    return (
        f"{pattern.get('kind')}={pattern.get('key')} currently correlates "
        f"with {direction}{round(lift * 100)}% "
        f"{pattern.get('metric')} (n={pattern.get('samples')}, "
        f"confidence={round(float(pattern.get('confidence') or 0), 2)}); "
        "use as a preference hint, not a rule"
    )


def _mature_patterns(
    context: dict[str, Any],
    metric: str | None,
) -> list[dict[str, Any]]:
    if metric is None:
        return []
    result = []
    for item in context.get("patterns", []):
        if not isinstance(item, dict):
            continue
        if item.get("metric") != metric:
            continue
        if int(item.get("samples") or 0) < PATTERN_MIN_SAMPLES:
            continue
        if float(item.get("confidence") or 0) < PATTERN_MIN_CONFIDENCE:
            continue
        result.append(item)
    return result


def _pattern_metadata(
    brain: BrainTaskContext | None,
    patterns: Sequence[Mapping[str, Any]],
) -> tuple[tuple[int, ...], tuple[str, ...]]:
    identities = {
        (
            str(item.get("kind")),
            str(item.get("key")),
            str(item.get("metric")),
        )
        for item in patterns
    }
    if brain is None:
        return (), ()
    selected_ids = []
    selected_keys = []
    brain_pattern_keys = getattr(brain, "pattern_keys", [])
    brain_pattern_ids = getattr(brain, "pattern_ids", [])
    for index, key in enumerate(brain_pattern_keys):
        parts = key.split(":", 2)
        if len(parts) != 3 or tuple(parts) not in identities:
            continue
        selected_keys.append(key)
        if index < len(brain_pattern_ids):
            selected_ids.append(brain_pattern_ids[index])
    if not selected_keys:
        selected_keys = [
            f"{kind}:{key}:{metric}"
            for kind, key, metric in sorted(identities)
        ]
    return tuple(selected_ids), tuple(selected_keys)


def build_content_plan(
    *,
    profile: dict[str, Any],
    topic: str,
    brain: BrainTaskContext | None,
    memory: Sequence[ContentMemoryItem] = (),
    fallback_goal: str | None = None,
    source: str = "manual",
) -> ContentPlan:
    try:
        context = brain.compact_dict() if brain is not None else {}
    except Exception:
        log.warning(
            "Content Engine Brain context unavailable; using fallback brief"
        )
        context = {}
    raw_goal = context.get("primary_goal") or fallback_goal
    goal = normalize_goal(raw_goal)
    goal_name = (
        goal.normalized
        if goal.normalized != "unknown"
        else goal.raw or "unknown"
    )
    patterns = _mature_patterns(context, goal.metric)
    pattern_ids, pattern_keys = _pattern_metadata(brain, patterns)
    angle = _select_angle(topic, goal.normalized, memory)
    content_format = _ANGLE_FORMAT.get(angle, "compact_post")

    constraints = (
        _flatten_constraints(context.get("critical_constraints"))[:3]
    )

    avoid = [
        f"opening: {item.opening[:90]}"
        for item in memory[:4]
        if item.opening
    ]
    avoid.extend(
        f"strategy: {item.angle}/{item.hook_type}/{item.format}"
        for item in memory[:4]
        if item.angle or item.hook_type or item.format
    )

    desired_action = {
        "reach": (
            "make the right reader stop, finish, and want to share; "
            "CTA is optional"
        ),
        "engagement": (
            "invite a concrete opinion or personal experience in comments"
        ),
    }.get(
        goal.normalized,
        "follow the editorial intent without feedback-metric optimization",
    )
    return ContentPlan(
        brief=ContentBrief(
            goal=str(goal_name),
            topic=topic.strip(),
            audience=_compact_value(context.get("audience")),
            desired_action=desired_action,
            angle=angle,
            hook_strategy=_select_hook_strategy(angle, memory),
            format=content_format,
            tone=_compact_value(context.get("dna"), 180),
            constraints=constraints[:8],
            pattern_hints=[
                _pattern_hint(pattern)
                for pattern in patterns[:6]
            ],
            performance_context=_compact_value(
                context.get("performance"),
                180,
            ),
            avoid=avoid[:8],
            source=source,
        ),
        pattern_ids=pattern_ids,
        pattern_keys=pattern_keys,
        goal_metric=goal.metric,
    )


def memory_prompt(
    memory: Sequence[ContentMemoryItem],
    *,
    limit: int = 6,
) -> str:
    return json.dumps(
        [item.prompt_dict() for item in memory[:limit]],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def detect_cta(text_value: str) -> str | None:
    normalized = normalize_content_text(text_value)
    tail = normalized[-180:]
    if any(word in tail for word in ("подпиш", "следи за", "оставайся")):
        return "follow"
    if any(
        word in tail
        for word in (
            "напиши",
            "расскажи",
            "поделись",
            "в комментар",
            "как у тебя",
            "а ты",
        )
    ):
        return "comment"
    if "ссылк" in tail or "первом комментар" in tail:
        return "link"
    if "?" in (text_value or "")[-180:]:
        return "question"
    return None


def canonicalize_response(
    response: ContentGenerationResponse,
    *,
    plan: ContentPlan,
    usage_user_id: int | None,
    usage_account_id: int | None,
    brain_version: int | None,
    pipeline_stage: str,
) -> ContentGenerationResponse:
    data = response.model_dump(mode="json")
    selected_index = response.metadata.selected_hook_index
    selected = response.hooks[selected_index]
    combined = f"{selected.text}\n\n{response.body}"
    cta_type = detect_cta(combined)
    data["brief"] = plan.brief.model_dump(mode="json")
    data["metadata"].update({
        "goal": plan.brief.goal or "unknown",
        "angle": plan.brief.angle,
        "hook_type": selected.type,
        "format": plan.brief.format,
        "topic": plan.brief.topic or "",
        "has_cta": cta_type is not None,
        "cta_type": cta_type,
        "source": plan.brief.source or "manual",
        "brain_version": brain_version,
        "pattern_ids": list(plan.pattern_ids),
        "pattern_keys": list(plan.pattern_keys),
        "selected_hook": selected.text,
        "pipeline_stage": pipeline_stage,
        "user_id": usage_user_id,
        "threads_account_id": usage_account_id,
    })
    return ContentGenerationResponse.model_validate(data)


def repeated_reason(
    response: ContentGenerationResponse,
    memory: Sequence[ContentMemoryItem],
) -> str | None:
    selected = response.hooks[response.metadata.selected_hook_index]
    opening_normalized = normalize_content_text(selected.text)
    topic_normalized = normalize_content_text(response.metadata.topic)
    for item in memory:
        previous_opening = normalize_content_text(item.opening)
        if opening_normalized and opening_normalized == previous_opening:
            return "repeated_opening_exact"
        if token_overlap(selected.text, item.opening) >= 0.72:
            return "repeated_opening_similar"
        if (
            topic_normalized
            and topic_normalized == normalize_content_text(item.topic or "")
            and response.metadata.angle == item.angle
            and response.metadata.hook_type == item.hook_type
            and response.metadata.format == item.format
        ):
            return "repeated_content_strategy"
    return None


def quality_gate(
    response: ContentGenerationResponse,
    *,
    memory: Sequence[ContentMemoryItem] = (),
) -> QualityGateResult:
    reasons = []
    selected = response.hooks[response.metadata.selected_hook_index]
    body = response.body.strip()
    combined = f"{selected.text.strip()}\n\n{body}".strip()
    if not selected.text.strip():
        reasons.append("missing_hook")
    if len(body) < 20:
        reasons.append("empty_or_too_short_body")
    if len(combined) > 420:
        reasons.append("post_too_long")
    normalized_hooks = [
        normalize_content_text(hook.text)
        for hook in response.hooks
    ]
    if len(set(normalized_hooks)) != len(normalized_hooks):
        reasons.append("duplicate_hooks")
    combined_normalized = normalize_content_text(combined)
    if any(
        normalize_content_text(phrase) in combined_normalized
        for phrase in _BANNED_PHRASES
    ):
        reasons.append("banned_phrase")
    if len(_EMOJI_RE.findall(combined)) > 3:
        reasons.append("too_many_emojis")
    repeated = repeated_reason(response, memory)
    if repeated:
        reasons.append(repeated)
    goal = normalize_goal(response.metadata.goal)
    if goal.normalized == "engagement" and not response.metadata.has_cta:
        reasons.append("missing_engagement_cta")
    if response.quality.specificity < 0.25 and len(body) < 80:
        reasons.append("weak_specificity")
    return QualityGateResult(
        passed=not reasons,
        reasons=tuple(dict.fromkeys(reasons)),
    )
