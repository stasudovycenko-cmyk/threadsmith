"""Collection orchestration, independent from provider and Telegram UI."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.analytics.providers.base import AnalyticsProvider
from app.core.analytics.repository import AnalyticsRepository
from app.core.analytics.scoring import calculate_scores, with_engagement_rate
from app.core.brain_writer import BrainWriter
from app.schemas.analytics import (
    AnalyticsAccountSummary,
    AnalyticsCollectionResult,
    AnalyticsSnapshotWrite,
    PublishedAnalyticsPost,
)

log = logging.getLogger("analytics")
ANALYTICS_LOOKBACK_DAYS = 30
ANALYTICS_POST_LIMIT = 50


class AnalyticsFeedback(Protocol):
    async def record_post(
        self,
        snapshot_id: int,
        item: AnalyticsSnapshotWrite,
    ) -> None: ...

    async def sync_account(
        self,
        summary: AnalyticsAccountSummary,
    ) -> None: ...


class NullAnalyticsFeedback:
    async def record_post(
        self,
        snapshot_id: int,
        item: AnalyticsSnapshotWrite,
    ) -> None:
        return None

    async def sync_account(
        self,
        summary: AnalyticsAccountSummary,
    ) -> None:
        return None


class SocialBrainAnalyticsFeedback:
    """Project aggregate analytics and append idempotent Brain events."""

    def __init__(self, writer: BrainWriter):
        self.writer = writer

    async def record_post(
        self,
        snapshot_id: int,
        item: AnalyticsSnapshotWrite,
    ) -> None:
        metrics = item.metrics.model_dump(exclude_none=True)
        await self.writer.record_post_performance_updated(
            item.post.user_id,
            item.post.threads_account_id,
            analytics_snapshot_id=snapshot_id,
            threads_post_id=item.post.threads_post_id,
            snapshot_at=item.snapshot_at,
            scores=item.scores.model_dump(),
            available_metrics=list(metrics),
        )
        await self.writer.record_insights_snapshot(
            item.post.user_id,
            item.post.threads_account_id,
            threads_post_id=item.post.threads_post_id,
            snapshot_date=item.snapshot_at.date(),
            metrics=metrics,
            occurred_at=item.snapshot_at,
        )

    async def sync_account(
        self,
        summary: AnalyticsAccountSummary,
    ) -> None:
        repo = self.writer.repo
        brain = await repo.get_or_create(
            summary.user_id,
            summary.threads_account_id,
        )
        projection = summary.model_dump(mode="json")
        projection.pop("user_id", None)
        projection.pop("threads_account_id", None)
        performance = dict(brain.performance)
        if performance.get("analytics_v2") == projection:
            return
        performance["analytics_v2"] = projection
        await repo.update_section(
            brain.id,
            "performance",
            performance,
            user_id=summary.user_id,
            account_id=summary.threads_account_id,
        )


def snapshot_bucket(now: datetime) -> datetime:
    """Return the stable UTC half-hour bucket used for idempotency."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    value = now.astimezone(timezone.utc)
    minute = 30 if value.minute >= 30 else 0
    return value.replace(minute=minute, second=0, microsecond=0)


def publication_slot(post: PublishedAnalyticsPost) -> tuple[int, int]:
    try:
        zone = ZoneInfo(post.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = timezone.utc
    published = post.published_at
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    local = published.astimezone(zone)
    return local.hour, local.weekday()


class AnalyticsCollector:
    def __init__(
        self,
        repository: AnalyticsRepository,
        *,
        feedback: AnalyticsFeedback | None = None,
        clock=None,
    ):
        self.repository = repository
        self.feedback = feedback or NullAnalyticsFeedback()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def collect_account(
        self,
        user_id: int,
        account_id: int,
        provider: AnalyticsProvider,
    ) -> AnalyticsCollectionResult:
        observed_at = self.clock()
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        observed_at = observed_at.astimezone(timezone.utc)
        bucket_at = snapshot_bucket(observed_at)
        local_posts = await self.repository.load_published_posts(
            user_id,
            account_id,
        )
        timezone_name = await self.repository.account_timezone(
            user_id,
            account_id,
        )
        try:
            provider_posts = await provider.list_recent_posts(
                since=observed_at - timedelta(days=ANALYTICS_LOOKBACK_DAYS),
                limit=ANALYTICS_POST_LIMIT,
            )
        except Exception as error:
            provider_posts = []
            log.warning(
                "analytics post listing failed account=%s error_type=%s; "
                "using local publication history",
                account_id,
                type(error).__name__,
            )
        merged = {
            item.post_id: PublishedAnalyticsPost(
                user_id=user_id,
                threads_account_id=account_id,
                threads_post_id=item.post_id,
                published_at=item.published_at,
                text=item.text,
                timezone=timezone_name,
            )
            for item in provider_posts
        }
        for post in local_posts:
            merged[post.threads_post_id] = post
        posts = sorted(
            merged.values(),
            key=lambda post: (post.published_at, post.threads_post_id),
            reverse=True,
        )
        baseline = await self.repository.account_baseline(
            user_id,
            account_id,
        )
        written = 0
        failures = 0
        for post in posts:
            try:
                metrics = with_engagement_rate(
                    await provider.get_post_metrics(post.threads_post_id)
                )
                previous = await self.repository.previous_snapshot(
                    account_id,
                    post.threads_post_id,
                    before_bucket=bucket_at,
                )
                features = await self.repository.feature_benchmarks(post)
                scores = calculate_scores(
                    metrics,
                    baseline,
                    features,
                    previous,
                    published_at=post.published_at,
                    snapshot_at=observed_at,
                )
                item = AnalyticsSnapshotWrite(
                    post=post,
                    provider=provider.name,
                    snapshot_at=observed_at,
                    bucket_at=bucket_at,
                    metrics=metrics,
                    scores=scores,
                )
                snapshot_id = await self.repository.save_snapshot(item)
                hour, weekday = publication_slot(post)
                await self.repository.upsert_post_summary(
                    item,
                    publish_hour=hour,
                    weekday=weekday,
                )
                if post.scheduled_post_id is not None:
                    await self.repository.save_legacy_daily_snapshot(
                        post.threads_post_id,
                        observed_at,
                        metrics,
                    )
                await self.feedback.record_post(snapshot_id, item)
                written += 1
            except Exception as error:
                failures += 1
                log.warning(
                    "analytics post collection failed account=%s post=%s "
                    "error_type=%s",
                    account_id,
                    post.threads_post_id,
                    type(error).__name__,
                )
        summary = await self.repository.rebuild_account(
            user_id,
            account_id,
        )
        if summary is not None:
            await self.feedback.sync_account(summary)
        return AnalyticsCollectionResult(
            user_id=user_id,
            threads_account_id=account_id,
            posts_seen=len(posts),
            snapshots_written=written,
            failures=failures,
            summary=summary,
        )
