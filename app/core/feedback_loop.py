"""Deterministic account-scoped analytics for Social Brain."""

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from statistics import median
from typing import Any, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.brain_repo import (
    BrainNotFoundError,
    BrainOwnershipError,
    BrainRepo,
)
from app.core.brain_writer import BrainWriter
from app.core.context_builder import (
    PATTERN_MIN_CONFIDENCE,
    PATTERN_MIN_SAMPLES,
)
from app.schemas.feedback import (
    AccountFeedbackResult,
    BrainPatternWrite,
    FeedbackMetric,
    GoalScore,
    GoalSelection,
    PatternAggregate,
    PostBaseline,
    PostFeature,
    PostPerformance,
)
from app.schemas.social_brain import BrainRecord

THREADS_METRICS = (
    "views",
    "likes",
    "replies",
    "reposts",
    "quotes",
    "shares",
)
ENGAGEMENT_COMPONENTS = (
    "likes",
    "replies",
    "reposts",
    "quotes",
    "shares",
)
FEEDBACK_METRICS: tuple[FeedbackMetric, ...] = (
    "views",
    "engagement_rate",
)

MIN_BASELINE_SAMPLES = 3
MIN_PATTERN_OBSERVATIONS = 2
CONFIDENCE_PRIOR_SAMPLES = 2
CONFIDENCE_DISPERSION_SCALE = 0.5
SHORT_POST_MAX_CHARS = 160
MEDIUM_POST_MAX_CHARS = 320
FLOAT_PRECISION = 6

MANAGED_PATTERN_KINDS = (
    "has_link",
    "length_bucket",
    "publication_window_utc",
)

_LOAD_POSTS_SQL = text("""
    SELECT
        post.id AS scheduled_post_id,
        post.user_id,
        post.threads_account_id,
        post.threads_post_id,
        post.text,
        post.link,
        post.run_at AS published_at,
        snapshot.snapshot_date,
        snapshot.metrics_json
    FROM scheduled_posts post
    JOIN LATERAL (
        SELECT
            insight.snapshot_date,
            insight.metrics_json
        FROM insights_snapshots insight
        WHERE insight.threads_post_id = post.threads_post_id
        ORDER BY insight.snapshot_date DESC
        LIMIT 1
    ) snapshot ON true
    WHERE post.user_id = :uid
      AND post.threads_account_id = :account_id
      AND post.status = 'done'
      AND post.threads_post_id IS NOT NULL
    ORDER BY post.run_at, post.id
""")

_CANONICAL_GOAL_SQL = text("""
    SELECT
        (
            SELECT count(*)
            FROM threads_accounts account
            WHERE account.user_id = :uid
        ) AS account_count,
        settings.goal
    FROM (SELECT 1) seed
    LEFT JOIN autocontent_settings settings
      ON settings.user_id = :uid
""")

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


def _round(value: float) -> float:
    return round(value, FLOAT_PRECISION)


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


def _metric_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if math.isfinite(value) and value >= 0 and value.is_integer():
            return int(value)
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return None


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


def normalize_post(row: Mapping[str, Any]) -> PostPerformance:
    metrics = _json_dict(row.get("metrics_json"))
    normalized = {
        metric: _metric_int(metrics.get(metric))
        for metric in THREADS_METRICS
    }
    engagement = None
    if all(
        normalized[metric] is not None
        for metric in ENGAGEMENT_COMPONENTS
    ):
        engagement = sum(
            normalized[metric] or 0
            for metric in ENGAGEMENT_COMPONENTS
        )
    views = normalized["views"]
    engagement_rate = None
    if engagement is not None and views is not None and views > 0:
        engagement_rate = engagement / views

    text_value = row.get("text")
    post_text = text_value if isinstance(text_value, str) else ""
    available = [
        metric
        for metric in THREADS_METRICS
        if normalized[metric] is not None
    ]
    if engagement is not None:
        available.append("engagement")
    if engagement_rate is not None:
        available.append("engagement_rate")

    return PostPerformance(
        scheduled_post_id=row["scheduled_post_id"],
        user_id=row["user_id"],
        threads_account_id=row["threads_account_id"],
        threads_post_id=str(row["threads_post_id"]),
        published_at=row["published_at"],
        snapshot_date=row["snapshot_date"],
        text=post_text,
        text_length=len(post_text),
        has_link=bool(str(row.get("link") or "").strip()),
        **normalized,
        engagement=engagement,
        engagement_rate=(
            _round(engagement_rate)
            if engagement_rate is not None
            else None
        ),
        available_metrics=tuple(available),
    )


def post_features(post: PostPerformance) -> tuple[PostFeature, ...]:
    if post.text_length <= SHORT_POST_MAX_CHARS:
        length_key = "short"
    elif post.text_length <= MEDIUM_POST_MAX_CHARS:
        length_key = "medium"
    else:
        length_key = "long"

    published_at = post.published_at
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    hour = published_at.astimezone(timezone.utc).hour
    if hour < 6:
        window = "night"
    elif hour < 12:
        window = "morning"
    elif hour < 18:
        window = "afternoon"
    else:
        window = "evening"

    return (
        PostFeature(kind="length_bucket", key=length_key),
        PostFeature(
            kind="publication_window_utc",
            key=window,
        ),
        PostFeature(
            kind="has_link",
            key="true" if post.has_link else "false",
        ),
    )


def metric_value(
    post: PostPerformance,
    metric: FeedbackMetric,
) -> float | None:
    value = getattr(post, metric)
    return float(value) if value is not None else None


def baseline_for(
    previous_posts: Sequence[PostPerformance],
    metric: FeedbackMetric,
) -> PostBaseline:
    values = [
        value
        for post in previous_posts
        if (value := metric_value(post, metric)) is not None
    ]
    if len(values) < MIN_BASELINE_SAMPLES:
        return PostBaseline(
            metric=metric,
            samples=len(values),
        )
    return PostBaseline(
        metric=metric,
        value=_round(float(median(values))),
        samples=len(values),
    )


def confidence_for(observations: Sequence[float]) -> tuple[float, float]:
    if not observations:
        return 0.0, 0.0
    center = float(median(observations))
    dispersion = float(
        median(abs(value - center) for value in observations)
    )
    sample_factor = len(observations) / (
        len(observations) + CONFIDENCE_PRIOR_SAMPLES
    )
    consistency = 1 / (
        1 + dispersion / CONFIDENCE_DISPERSION_SCALE
    )
    confidence = min(1.0, max(0.0, sample_factor * consistency))
    return _round(confidence), _round(dispersion)


def _score_metric(
    post: PostPerformance,
    previous_posts: Sequence[PostPerformance],
    goal: GoalSelection,
) -> GoalScore:
    if goal.metric is None:
        return GoalScore(
            goal=goal.normalized,
            status="unsupported",
        )
    baseline = baseline_for(previous_posts, goal.metric)
    post_value = metric_value(post, goal.metric)
    if (
        post_value is None
        or baseline.value is None
        or baseline.value <= 0
    ):
        return GoalScore(
            goal=goal.normalized,
            metric=goal.metric,
            post_value=post_value,
            baseline_value=baseline.value,
            baseline_samples=baseline.samples,
            status="insufficient_data",
        )
    return GoalScore(
        goal=goal.normalized,
        metric=goal.metric,
        post_value=_round(post_value),
        baseline_value=baseline.value,
        baseline_samples=baseline.samples,
        lift=_round(
            (post_value - baseline.value) / baseline.value
        ),
        status="ok",
    )


def rebuild_patterns(
    posts: Sequence[PostPerformance],
) -> list[PatternAggregate]:
    observations: dict[
        tuple[str, str, FeedbackMetric],
        list[float],
    ] = defaultdict(list)
    previous_by_metric: dict[
        FeedbackMetric,
        list[PostPerformance],
    ] = {metric: [] for metric in FEEDBACK_METRICS}

    ordered = sorted(
        posts,
        key=lambda post: (
            post.published_at,
            post.scheduled_post_id,
        ),
    )
    for post in ordered:
        for metric in FEEDBACK_METRICS:
            baseline = baseline_for(
                previous_by_metric[metric],
                metric,
            )
            current = metric_value(post, metric)
            if (
                current is not None
                and baseline.value is not None
                and baseline.value > 0
            ):
                lift = (current - baseline.value) / baseline.value
                for feature in post_features(post):
                    observations[
                        (feature.kind, feature.key, metric)
                    ].append(lift)
            if current is not None:
                previous_by_metric[metric].append(post)

    patterns = []
    for identity, lifts in observations.items():
        if len(lifts) < MIN_PATTERN_OBSERVATIONS:
            continue
        kind, key, metric = identity
        center = _round(float(median(lifts)))
        confidence, dispersion = confidence_for(lifts)
        patterns.append(PatternAggregate(
            kind=kind,
            key=key,
            metric=metric,
            lift=center,
            samples=len(lifts),
            confidence=confidence,
            dispersion=dispersion,
        ))
    return sorted(
        patterns,
        key=lambda pattern: (
            pattern.kind,
            pattern.key,
            pattern.metric,
        ),
    )


def _source_signature(
    goal: GoalSelection,
    posts: Sequence[PostPerformance],
) -> str:
    source = {
        "goal": goal.model_dump(mode="json"),
        "posts": [
            post.model_dump(
                mode="json",
                exclude={"available_metrics"},
            )
            for post in posts
        ],
    }
    encoded = json.dumps(
        source,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _without_none(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if item is not None
    }


class FeedbackLoop:
    """Rebuilds one Brain's performance projection from canonical data."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        repo: BrainRepo | None = None,
        writer: BrainWriter | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.session = session
        self.repo = repo or BrainRepo(session)
        self.writer = writer or BrainWriter(session, self.repo)
        self.clock = clock or (
            lambda: datetime.now(timezone.utc)
        )

    async def _load_posts(
        self,
        user_id: int,
        account_id: int,
    ) -> list[PostPerformance]:
        result = await self.session.execute(
            _LOAD_POSTS_SQL,
            {"uid": user_id, "account_id": account_id},
        )
        return [
            normalize_post(dict(row))
            for row in result.mappings().all()
        ]

    async def _canonical_goal(self, user_id: int) -> str | None:
        result = await self.session.execute(
            _CANONICAL_GOAL_SQL,
            {"uid": user_id},
        )
        row = result.mappings().first()
        if row is None or int(row.get("account_count") or 0) != 1:
            return None
        return _raw_goal(row.get("goal"))

    async def _resolve_goal(self, brain: BrainRecord) -> GoalSelection:
        raw = _raw_goal(brain.goals.get("primary"))
        if raw is None:
            raw = _raw_goal(brain.goals.get("primary_goal"))
        if raw is None:
            raw = await self._canonical_goal(brain.user_id)
        return normalize_goal(raw)

    def analyze_post(
        self,
        post: PostPerformance,
        previous_posts: Sequence[PostPerformance],
        goal: GoalSelection,
    ) -> GoalScore:
        return _score_metric(post, previous_posts, goal)

    def rebuild_patterns(
        self,
        posts: Sequence[PostPerformance],
    ) -> list[PatternAggregate]:
        return rebuild_patterns(posts)

    def _performance_summary(
        self,
        posts: Sequence[PostPerformance],
        goal: GoalSelection,
        patterns: Sequence[PatternAggregate],
        signature: str,
        now: datetime,
    ) -> tuple[dict[str, Any], str]:
        scores = [
            self.analyze_post(post, posts[:index], goal)
            for index, post in enumerate(posts)
        ]
        successful = [
            score
            for score in scores
            if score.status == "ok" and score.lift is not None
        ]
        if not goal.supported:
            status = "unsupported"
        elif scores:
            status = scores[-1].status
        else:
            status = "insufficient_data"

        latest = scores[-1] if scores else GoalScore(
            goal=goal.normalized,
            metric=goal.metric,
            status=(
                "insufficient_data"
                if goal.supported
                else "unsupported"
            ),
        )
        goal_summary = _without_none({
            "status": latest.status,
            "metric": latest.metric,
            "post_value": latest.post_value,
            "baseline_value": latest.baseline_value,
            "baseline_samples": latest.baseline_samples,
            "lift": latest.lift,
            "scored_posts": len(successful),
            "median_lift": (
                _round(float(median(
                    score.lift for score in successful
                    if score.lift is not None
                )))
                if successful
                else None
            ),
        })

        best_metrics: dict[str, dict[str, Any]] = {}
        for metric in FEEDBACK_METRICS:
            candidates = [
                pattern
                for pattern in patterns
                if pattern.metric == metric
                and pattern.samples >= PATTERN_MIN_SAMPLES
                and pattern.confidence >= PATTERN_MIN_CONFIDENCE
            ]
            if not candidates:
                continue
            best = max(
                candidates,
                key=lambda pattern: (
                    pattern.lift,
                    pattern.confidence,
                    pattern.samples,
                    pattern.kind,
                    pattern.key,
                ),
            )
            best_metrics[metric] = {
                "kind": best.kind,
                "key": best.key,
                "lift": best.lift,
                "samples": best.samples,
                "confidence": best.confidence,
            }

        mature_count = sum(
            pattern.samples >= PATTERN_MIN_SAMPLES
            and pattern.confidence >= PATTERN_MIN_CONFIDENCE
            for pattern in patterns
        )
        baseline = _without_none({
            "metric": latest.metric,
            "value": latest.baseline_value,
            "samples": latest.baseline_samples,
        })
        summary = {
            "status": status,
            "goal": goal.model_dump(mode="json"),
            "baseline": baseline if latest.metric is not None else None,
            "goal_summary": goal_summary,
            "posts_analyzed": len(posts),
            "best_metrics": best_metrics,
            "patterns": {
                "total": len(patterns),
                "mature": mature_count,
            },
            "last_snapshot_date": (
                max(post.snapshot_date for post in posts).isoformat()
                if posts
                else None
            ),
            "source_signature": signature,
            "last_feedback_at": now.isoformat(),
        }
        return _without_none(summary), status

    async def analyze_account(
        self,
        brain_id: int,
        *,
        user_id: int,
        account_id: int,
        force: bool = False,
    ) -> AccountFeedbackResult:
        brain = await self.repo.get(
            brain_id,
            user_id=user_id,
            account_id=account_id,
        )
        if brain is None:
            raise BrainNotFoundError(
                f"Brain {brain_id} does not exist in the requested scope"
            )

        posts = await self._load_posts(user_id, account_id)
        if any(
            post.user_id != user_id
            or post.threads_account_id != account_id
            for post in posts
        ):
            raise BrainOwnershipError(
                "Feedback source contains posts outside account scope"
            )
        posts.sort(key=lambda post: (
            post.published_at,
            post.scheduled_post_id,
        ))
        goal = await self._resolve_goal(brain)
        signature = _source_signature(goal, posts)

        previous = brain.performance.get("feedback_v1")
        if (
            not force
            and isinstance(previous, dict)
            and previous.get("source_signature") == signature
        ):
            pattern_count = previous.get("patterns", {})
            if not isinstance(pattern_count, dict):
                pattern_count = {}
            return AccountFeedbackResult(
                brain_id=brain.id,
                user_id=user_id,
                threads_account_id=account_id,
                status="no_new_data",
                changed=False,
                posts_analyzed=len(posts),
                patterns_written=int(pattern_count.get("total") or 0),
                brain_version=brain.version,
            )

        patterns = self.rebuild_patterns(posts)
        await self.repo.replace_patterns(
            brain.id,
            [
                BrainPatternWrite(
                    kind=pattern.kind,
                    key=pattern.key,
                    metric=pattern.metric,
                    lift=pattern.lift,
                    samples=pattern.samples,
                    confidence=pattern.confidence,
                )
                for pattern in patterns
            ],
            managed_kinds=MANAGED_PATTERN_KINDS,
            user_id=user_id,
            account_id=account_id,
        )
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        summary, status = self._performance_summary(
            posts,
            goal,
            patterns,
            signature,
            now,
        )
        performance = dict(brain.performance)
        performance["feedback_v1"] = summary
        updated = await self.repo.update_section(
            brain.id,
            "performance",
            performance,
            user_id=user_id,
            account_id=account_id,
        )
        await self.writer.record_event(
            brain.id,
            "feedback_rebuilt",
            payload={
                "status": status,
                "goal": goal.normalized,
                "posts_analyzed": len(posts),
                "patterns_written": len(patterns),
                "source_signature": signature,
            },
            source_type="feedback_loop",
            source_id=account_id,
            event_key=f"feedback_rebuilt:{signature}",
            occurred_at=now,
        )
        return AccountFeedbackResult(
            brain_id=brain.id,
            user_id=user_id,
            threads_account_id=account_id,
            status=status,
            changed=True,
            posts_analyzed=len(posts),
            patterns_written=len(patterns),
            brain_version=updated.version,
        )
