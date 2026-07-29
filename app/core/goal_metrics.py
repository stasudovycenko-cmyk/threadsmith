"""Shared mapping from product goals to measurable metric dimensions."""

from typing import Any

from app.schemas.feedback import FeedbackMetric, GoalSelection

_GOAL_ALIASES = {
    "reach": {
        "reach",
        "views",
        "охват",
        "охваты",
        "просмотры",
    },
    "engagement": {
        "engagement",
        "engagement rate",
        "активность",
        "вовлечение",
        "вовлеченность",
        "вовлечённость",
    },
    "followers": {
        "follower",
        "followers",
        "подписчики",
        "рост подписчиков",
    },
    "traffic": {
        "clicks",
        "traffic",
        "клики",
        "переходы",
        "переходы по ссылке",
        "трафик",
    },
    "leads": {
        "lead",
        "leads",
        "лид",
        "лиды",
    },
}

_GOAL_METRICS: dict[str, FeedbackMetric] = {
    "reach": "views",
    "engagement": "engagement_rate",
}


def _raw_goal(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, dict):
        for key in ("value", "name", "key", "goal"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def normalize_goal(value: Any) -> GoalSelection:
    raw = _raw_goal(value)
    normalized_value = (
        " ".join(raw.casefold().split())
        if raw is not None
        else ""
    )
    normalized = "unknown"
    for goal, aliases in _GOAL_ALIASES.items():
        if normalized_value in aliases:
            normalized = goal
            break
    metric = _GOAL_METRICS.get(normalized)
    return GoalSelection(
        raw=raw,
        normalized=normalized,
        metric=metric,
        supported=metric is not None,
    )
