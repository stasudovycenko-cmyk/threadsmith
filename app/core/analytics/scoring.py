"""Deterministic, provider-neutral analytics scoring."""

from datetime import datetime

from app.schemas.analytics import (
    AnalyticsBaseline,
    AnalyticsFeatureBenchmarks,
    AnalyticsMetrics,
    AnalyticsScores,
    PreviousAnalyticsSnapshot,
)

ENGAGEMENT_COMPONENTS = ("likes", "replies", "reposts", "quotes")


def _clamp(value: float, low: float = 0, high: float = 1) -> float:
    return max(low, min(high, value))


def _signal(value: float | None, cap: float) -> float:
    if value is None or cap <= 0:
        return 0
    return _clamp(value / cap)


def _rate(value: int | None, views: int | None) -> float | None:
    if value is None or views is None or views <= 0:
        return None
    return value / views


def with_engagement_rate(metrics: AnalyticsMetrics) -> AnalyticsMetrics:
    """Derive ER only from the audited stable interaction subset."""
    if metrics.views is None or metrics.views <= 0:
        return metrics
    values = [getattr(metrics, key) for key in ENGAGEMENT_COMPONENTS]
    if any(value is None for value in values):
        return metrics
    engagement = sum(value for value in values if value is not None)
    return metrics.model_copy(update={
        "engagement_rate": round(engagement / metrics.views, 6)
    })


def _age_hours(published_at: datetime, snapshot_at: datetime) -> float:
    return max((snapshot_at - published_at).total_seconds() / 3600, 0.5)


def _view_velocity(
    metrics: AnalyticsMetrics,
    previous: PreviousAnalyticsSnapshot | None,
    *,
    published_at: datetime,
    snapshot_at: datetime,
) -> float | None:
    if metrics.views is None:
        return None
    if previous is not None and previous.metrics.views is not None:
        elapsed = max(
            (snapshot_at - previous.snapshot_at).total_seconds() / 3600,
            0.5,
        )
        return max(metrics.views - previous.metrics.views, 0) / elapsed
    return metrics.views / _age_hours(published_at, snapshot_at)


class ViralityScoreService:
    """Score growth, ER, replies and redistribution on a 0-100 scale."""

    def calculate(
        self,
        metrics: AnalyticsMetrics,
        previous: PreviousAnalyticsSnapshot | None,
        *,
        published_at: datetime,
        snapshot_at: datetime,
    ) -> float:
        velocity = _view_velocity(
            metrics,
            previous,
            published_at=published_at,
            snapshot_at=snapshot_at,
        )
        replies_rate = _rate(metrics.replies, metrics.views)
        redistribution = None
        if metrics.reposts is not None and metrics.quotes is not None:
            redistributed = metrics.reposts + metrics.quotes
            if metrics.shares is not None:
                redistributed += metrics.shares
            redistribution = _rate(redistributed, metrics.views)
        score = 100 * (
            0.35 * _signal(velocity, 250)
            + 0.30 * _signal(metrics.engagement_rate, 0.10)
            + 0.20 * _signal(replies_rate, 0.02)
            + 0.15 * _signal(redistribution, 0.03)
        )
        return round(_clamp(score, 0, 100), 2)


class PerformanceScoreService:
    """Score absolute response plus age-adjusted growth against the account."""

    def calculate(
        self,
        metrics: AnalyticsMetrics,
        baseline: AnalyticsBaseline,
        previous: PreviousAnalyticsSnapshot | None,
        *,
        published_at: datetime,
        snapshot_at: datetime,
    ) -> float:
        if metrics.views is None:
            views_signal = 0
        elif baseline.avg_views and baseline.avg_views > 0:
            views_signal = _signal(metrics.views, baseline.avg_views * 2)
        else:
            views_signal = 0.5 if metrics.views > 0 else 0
        velocity = _view_velocity(
            metrics,
            previous,
            published_at=published_at,
            snapshot_at=snapshot_at,
        )
        score = 100 * (
            0.30 * views_signal
            + 0.25 * _signal(metrics.engagement_rate, 0.10)
            + 0.15 * _signal(_rate(metrics.likes, metrics.views), 0.08)
            + 0.15 * _signal(_rate(metrics.replies, metrics.views), 0.02)
            + 0.15 * _signal(velocity, 250)
        )
        return round(_clamp(score, 0, 100), 2)


class BrainScoreService:
    """Blend virality with learned feature quality and conversation depth."""

    def calculate(
        self,
        metrics: AnalyticsMetrics,
        virality_score: float,
        features: AnalyticsFeatureBenchmarks,
    ) -> float:
        reply_signal = 100 * _signal(
            _rate(metrics.replies, metrics.views),
            0.02,
        )
        score = (
            0.45 * virality_score
            + 0.15 * features.topic_score
            + 0.15 * features.hook_score
            + 0.10 * features.cta_score
            + 0.15 * reply_signal
        )
        return round(_clamp(score, 0, 100), 2)


def calculate_scores(
    metrics: AnalyticsMetrics,
    baseline: AnalyticsBaseline,
    features: AnalyticsFeatureBenchmarks,
    previous: PreviousAnalyticsSnapshot | None,
    *,
    published_at: datetime,
    snapshot_at: datetime,
) -> AnalyticsScores:
    virality = ViralityScoreService().calculate(
        metrics,
        previous,
        published_at=published_at,
        snapshot_at=snapshot_at,
    )
    performance = PerformanceScoreService().calculate(
        metrics,
        baseline,
        previous,
        published_at=published_at,
        snapshot_at=snapshot_at,
    )
    brain = BrainScoreService().calculate(metrics, virality, features)
    return AnalyticsScores(
        performance_score=performance,
        virality_score=virality,
        brain_score=brain,
    )
