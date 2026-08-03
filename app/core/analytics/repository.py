"""SQL storage boundary for Analytics V2."""

import json
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.analytics import (
    AnalyticsAccountSummary,
    AnalyticsBaseline,
    AnalyticsFeatureBenchmarks,
    AnalyticsMetrics,
    AnalyticsSnapshotWrite,
    PreviousAnalyticsSnapshot,
    PublishedAnalyticsPost,
)

_METRIC_COLUMNS = (
    "views",
    "likes",
    "replies",
    "quotes",
    "reposts",
    "shares",
    "profile_visits",
    "followers",
    "engagement_rate",
)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _feature(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    compact = value.strip().casefold()
    return compact[:160] or None


class AnalyticsRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def account_timezone(self, user_id: int, account_id: int) -> str:
        row = (
            await self.session.execute(text("""
                SELECT coalesce(setting.timezone, 'UTC')
                FROM threads_accounts account
                LEFT JOIN autocontent_settings setting
                  ON setting.user_id = account.user_id
                 AND setting.threads_account_id = account.id
                WHERE account.user_id = :user_id
                  AND account.id = :account_id
            """), {"user_id": user_id, "account_id": account_id})
        ).first()
        return str(row[0]) if row and row[0] else "UTC"

    async def load_published_posts(
        self,
        user_id: int,
        account_id: int,
        *,
        lookback_days: int = 30,
        limit: int = 100,
    ) -> list[PublishedAnalyticsPost]:
        rows = (
            await self.session.execute(text("""
                SELECT
                    post.id AS scheduled_post_id,
                    post.user_id,
                    post.threads_account_id,
                    post.threads_post_id,
                    post.run_at AS published_at,
                    post.text,
                    post.content_metadata,
                    coalesce(setting.timezone, 'UTC') AS timezone
                FROM scheduled_posts post
                LEFT JOIN autocontent_settings setting
                  ON setting.user_id = post.user_id
                 AND setting.threads_account_id = post.threads_account_id
                WHERE post.user_id = :user_id
                  AND post.threads_account_id = :account_id
                  AND post.status = 'done'
                  AND post.threads_post_id IS NOT NULL
                  AND post.run_at >= now() - make_interval(days => :days)
                ORDER BY post.run_at DESC, post.id DESC
                LIMIT :post_limit
            """), {
                "user_id": user_id,
                "account_id": account_id,
                "days": lookback_days,
                "post_limit": limit,
            })
        ).mappings().all()
        posts = []
        for row in rows:
            item = dict(row)
            metadata = _json_object(item.pop("content_metadata", None))
            posts.append(PublishedAnalyticsPost(
                **item,
                hook_type=_feature(metadata.get("hook_type")),
                cta_type=_feature(metadata.get("cta_type")),
                topic=_feature(metadata.get("topic")),
            ))
        return posts

    async def previous_snapshot(
        self,
        account_id: int,
        post_id: str,
        *,
        before_bucket: datetime,
    ) -> PreviousAnalyticsSnapshot | None:
        row = (
            await self.session.execute(text("""
                SELECT snapshot_at, views, likes, replies, quotes, reposts,
                       shares, profile_visits, followers, engagement_rate
                FROM analytics_snapshots
                WHERE threads_account_id = :account_id
                  AND threads_post_id = :post_id
                  AND snapshot_bucket < :before_bucket
                ORDER BY snapshot_at DESC
                LIMIT 1
            """), {
                "account_id": account_id,
                "post_id": post_id,
                "before_bucket": before_bucket,
            })
        ).mappings().first()
        if row is None:
            return None
        data = dict(row)
        snapshot_at = data.pop("snapshot_at")
        return PreviousAnalyticsSnapshot(
            snapshot_at=snapshot_at,
            metrics=AnalyticsMetrics.model_validate(data),
        )

    async def account_baseline(
        self,
        user_id: int,
        account_id: int,
    ) -> AnalyticsBaseline:
        row = (
            await self.session.execute(text("""
                SELECT avg(current_views)::double precision AS avg_views,
                       avg(engagement_rate)::double precision AS avg_engagement_rate
                FROM analytics_post_summary
                WHERE user_id = :user_id
                  AND threads_account_id = :account_id
            """), {"user_id": user_id, "account_id": account_id})
        ).mappings().first()
        return AnalyticsBaseline.model_validate(dict(row or {}))

    async def feature_benchmarks(
        self,
        post: PublishedAnalyticsPost,
    ) -> AnalyticsFeatureBenchmarks:
        row = (
            await self.session.execute(text("""
                SELECT
                  coalesce((SELECT avg(performance_score)
                    FROM analytics_post_summary
                    WHERE user_id = :user_id
                      AND threads_account_id = :account_id
                      AND topic = :topic), 50)::double precision AS topic_score,
                  coalesce((SELECT avg(performance_score)
                    FROM analytics_post_summary
                    WHERE user_id = :user_id
                      AND threads_account_id = :account_id
                      AND hook_type = :hook_type), 50)::double precision AS hook_score,
                  coalesce((SELECT avg(performance_score)
                    FROM analytics_post_summary
                    WHERE user_id = :user_id
                      AND threads_account_id = :account_id
                      AND cta_type = :cta_type), 50)::double precision AS cta_score
            """), {
                "user_id": post.user_id,
                "account_id": post.threads_account_id,
                "topic": post.topic,
                "hook_type": post.hook_type,
                "cta_type": post.cta_type,
            })
        ).mappings().first()
        return AnalyticsFeatureBenchmarks.model_validate(dict(row or {}))

    async def save_snapshot(self, item: AnalyticsSnapshotWrite) -> int:
        metrics = item.metrics.model_dump()
        result = await self.session.execute(text("""
            INSERT INTO analytics_snapshots (
              user_id, threads_account_id, scheduled_post_id,
              provider, threads_post_id, snapshot_at,
              snapshot_bucket,
              views, likes, replies, quotes, reposts, shares,
              profile_visits, followers, engagement_rate,
              performance_score, virality_score, brain_score,
              raw_metrics
            ) VALUES (
              :user_id, :account_id, :scheduled_post_id,
              :provider, :post_id, :snapshot_at,
              :snapshot_bucket,
              :views, :likes, :replies, :quotes, :reposts, :shares,
              :profile_visits, :followers, :engagement_rate,
              :performance_score, :virality_score, :brain_score,
              CAST(:raw_metrics AS jsonb)
            )
            ON CONFLICT (
              threads_account_id, provider, threads_post_id, snapshot_bucket
            ) DO UPDATE SET
              snapshot_at = excluded.snapshot_at,
              views = excluded.views,
              likes = excluded.likes,
              replies = excluded.replies,
              quotes = excluded.quotes,
              reposts = excluded.reposts,
              shares = excluded.shares,
              profile_visits = excluded.profile_visits,
              followers = excluded.followers,
              engagement_rate = excluded.engagement_rate,
              performance_score = excluded.performance_score,
              virality_score = excluded.virality_score,
              brain_score = excluded.brain_score,
              raw_metrics = excluded.raw_metrics
            RETURNING id
        """), {
            "user_id": item.post.user_id,
            "account_id": item.post.threads_account_id,
            "scheduled_post_id": item.post.scheduled_post_id,
            "provider": item.provider,
            "post_id": item.post.threads_post_id,
            "snapshot_at": item.snapshot_at,
            "snapshot_bucket": item.bucket_at,
            **metrics,
            **item.scores.model_dump(),
            "raw_metrics": json.dumps(
                item.metrics.model_dump(exclude_none=True),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        })
        row = result.first()
        if row is None:
            raise RuntimeError("analytics snapshot was not persisted")
        return int(row[0])

    async def save_legacy_daily_snapshot(
        self,
        post_id: str,
        snapshot_at: datetime,
        metrics: AnalyticsMetrics,
    ) -> None:
        legacy_metrics = {
            key: value
            for key, value in metrics.model_dump().items()
            if key in _METRIC_COLUMNS[:-1] and value is not None
        }
        await self.session.execute(text("""
            INSERT INTO insights_snapshots (
              threads_post_id, snapshot_date, metrics_json
            ) VALUES (:post_id, :snapshot_date, CAST(:metrics AS jsonb))
            ON CONFLICT (threads_post_id, snapshot_date)
            DO UPDATE SET metrics_json = excluded.metrics_json
        """), {
            "post_id": post_id,
            "snapshot_date": snapshot_at.date(),
            "metrics": json.dumps(legacy_metrics, separators=(",", ":")),
        })

    async def upsert_post_summary(
        self,
        item: AnalyticsSnapshotWrite,
        *,
        publish_hour: int,
        weekday: int,
    ) -> None:
        metrics = item.metrics.model_dump()
        await self.session.execute(text("""
            INSERT INTO analytics_post_summary (
              user_id, threads_account_id, scheduled_post_id,
              provider, threads_post_id, published_at,
              first_seen, last_updated,
              peak_views, current_views, likes, replies, quotes,
              reposts, shares, profile_visits, followers,
              engagement_rate, performance_score, virality_score,
              brain_score, hook_type, cta_type, topic,
              publish_hour, weekday
            ) VALUES (
              :user_id, :account_id, :scheduled_post_id,
              :provider, :post_id, :published_at,
              :snapshot_at, :snapshot_at,
              :views, :views, :likes, :replies, :quotes,
              :reposts, :shares, :profile_visits, :followers,
              :engagement_rate, :performance_score, :virality_score,
              :brain_score, :hook_type, :cta_type, :topic,
              :publish_hour, :weekday
            )
            ON CONFLICT (threads_account_id, provider, threads_post_id)
            DO UPDATE SET
              first_seen = least(
                analytics_post_summary.first_seen, excluded.first_seen
              ),
              last_updated = greatest(
                analytics_post_summary.last_updated, excluded.last_updated
              ),
              published_at = least(
                analytics_post_summary.published_at, excluded.published_at
              ),
              peak_views = CASE
                WHEN excluded.current_views IS NULL
                  THEN analytics_post_summary.peak_views
                WHEN analytics_post_summary.peak_views IS NULL
                  THEN excluded.current_views
                ELSE greatest(
                  analytics_post_summary.peak_views, excluded.current_views
                )
              END,
              current_views = excluded.current_views,
              likes = excluded.likes,
              replies = excluded.replies,
              quotes = excluded.quotes,
              reposts = excluded.reposts,
              shares = excluded.shares,
              profile_visits = excluded.profile_visits,
              followers = excluded.followers,
              engagement_rate = excluded.engagement_rate,
              performance_score = excluded.performance_score,
              virality_score = excluded.virality_score,
              brain_score = excluded.brain_score,
              hook_type = coalesce(excluded.hook_type,
                                   analytics_post_summary.hook_type),
              cta_type = coalesce(excluded.cta_type,
                                  analytics_post_summary.cta_type),
              topic = coalesce(excluded.topic,
                               analytics_post_summary.topic),
              publish_hour = excluded.publish_hour,
              weekday = excluded.weekday
        """), {
            "user_id": item.post.user_id,
            "account_id": item.post.threads_account_id,
            "scheduled_post_id": item.post.scheduled_post_id,
            "provider": item.provider,
            "post_id": item.post.threads_post_id,
            "published_at": item.post.published_at,
            "snapshot_at": item.snapshot_at,
            "hook_type": item.post.hook_type,
            "cta_type": item.post.cta_type,
            "topic": item.post.topic,
            "publish_hour": publish_hour,
            "weekday": weekday,
            **metrics,
            **item.scores.model_dump(),
        })

    async def rebuild_account(
        self,
        user_id: int,
        account_id: int,
    ) -> AnalyticsAccountSummary | None:
        params = {"user_id": user_id, "account_id": account_id}
        await self.session.execute(text("""
            WITH ranked AS (
              SELECT id,
                     round((100 * percent_rank() OVER (
                       ORDER BY performance_score
                     ))::numeric, 2) AS percentile
              FROM analytics_post_summary
              WHERE user_id = :user_id
                AND threads_account_id = :account_id
                AND performance_score IS NOT NULL
            )
            UPDATE analytics_post_summary summary
            SET performance_percentile = ranked.percentile
            FROM ranked
            WHERE summary.id = ranked.id
        """), params)
        await self.session.execute(text("""
            DELETE FROM analytics_aggregates
            WHERE user_id = :user_id
              AND threads_account_id = :account_id
        """), params)
        await self.session.execute(text("""
            INSERT INTO analytics_aggregates (
              user_id, threads_account_id, dimension, dimension_key,
              posts_count, views_total, avg_views, avg_er,
              avg_replies, avg_brain_score, avg_virality_score,
              avg_ctr, updated_at
            )
            SELECT
              post.user_id,
              post.threads_account_id,
              dimension.name,
              dimension.value,
              count(*),
              sum(post.current_views),
              avg(post.current_views),
              avg(post.engagement_rate),
              avg(post.replies),
              avg(post.brain_score),
              avg(post.virality_score),
              NULL::numeric,
              now()
            FROM analytics_post_summary post
            CROSS JOIN LATERAL (VALUES
              ('topic', post.topic),
              ('hook_type', post.hook_type),
              ('cta_type', post.cta_type),
              ('publish_hour', post.publish_hour::text),
              ('weekday', post.weekday::text)
            ) AS dimension(name, value)
            WHERE post.user_id = :user_id
              AND post.threads_account_id = :account_id
              AND dimension.value IS NOT NULL
              AND btrim(dimension.value) <> ''
            GROUP BY post.user_id, post.threads_account_id,
                     dimension.name, dimension.value
        """), params)
        row = (
            await self.session.execute(text("""
                INSERT INTO analytics_account_summary (
                  user_id, threads_account_id, posts_total,
                  views_total, likes_total, comments_total, shares_total,
                  avg_er, avg_views, best_post_id, worst_post_id,
                  best_hour, best_weekday, best_topic, best_hook,
                  best_cta, brain_score, metric_coverage, updated_at
                )
                SELECT
                  :user_id,
                  :account_id,
                  count(*),
                  sum(current_views),
                  sum(likes),
                  sum(replies),
                  sum(reposts) + sum(quotes) + coalesce(sum(shares), 0),
                  avg(engagement_rate),
                  avg(current_views),
                  (array_agg(threads_post_id ORDER BY
                    performance_score DESC NULLS LAST, id))[1],
                  (array_agg(threads_post_id ORDER BY
                    performance_score ASC NULLS LAST, id))[1],
                  (SELECT dimension_key::integer
                   FROM analytics_aggregates aggregate
                   WHERE aggregate.user_id = :user_id
                     AND aggregate.threads_account_id = :account_id
                     AND aggregate.dimension = 'publish_hour'
                   ORDER BY avg_brain_score DESC NULLS LAST,
                            posts_count DESC, dimension_key
                   LIMIT 1),
                  (SELECT dimension_key::integer
                   FROM analytics_aggregates aggregate
                   WHERE aggregate.user_id = :user_id
                     AND aggregate.threads_account_id = :account_id
                     AND aggregate.dimension = 'weekday'
                   ORDER BY avg_brain_score DESC NULLS LAST,
                            posts_count DESC, dimension_key
                   LIMIT 1),
                  (SELECT dimension_key FROM analytics_aggregates aggregate
                   WHERE aggregate.user_id = :user_id
                     AND aggregate.threads_account_id = :account_id
                     AND aggregate.dimension = 'topic'
                   ORDER BY avg_brain_score DESC NULLS LAST,
                            posts_count DESC, dimension_key LIMIT 1),
                  (SELECT dimension_key FROM analytics_aggregates aggregate
                   WHERE aggregate.user_id = :user_id
                     AND aggregate.threads_account_id = :account_id
                     AND aggregate.dimension = 'hook_type'
                   ORDER BY avg_brain_score DESC NULLS LAST,
                            posts_count DESC, dimension_key LIMIT 1),
                  (SELECT dimension_key FROM analytics_aggregates aggregate
                   WHERE aggregate.user_id = :user_id
                     AND aggregate.threads_account_id = :account_id
                     AND aggregate.dimension = 'cta_type'
                   ORDER BY avg_brain_score DESC NULLS LAST,
                            posts_count DESC, dimension_key LIMIT 1),
                  avg(brain_score),
                  jsonb_build_object(
                    'views', count(current_views),
                    'likes', count(likes),
                    'replies', count(replies),
                    'quotes', count(quotes),
                    'reposts', count(reposts),
                    'shares', count(shares),
                    'profile_visits', count(profile_visits),
                    'followers', count(followers),
                    'engagement_rate', count(engagement_rate)
                  ),
                  now()
                FROM analytics_post_summary
                WHERE user_id = :user_id
                  AND threads_account_id = :account_id
                HAVING count(*) > 0
                ON CONFLICT (threads_account_id) DO UPDATE SET
                  posts_total = excluded.posts_total,
                  views_total = excluded.views_total,
                  likes_total = excluded.likes_total,
                  comments_total = excluded.comments_total,
                  shares_total = excluded.shares_total,
                  avg_er = excluded.avg_er,
                  avg_views = excluded.avg_views,
                  best_post_id = excluded.best_post_id,
                  worst_post_id = excluded.worst_post_id,
                  best_hour = excluded.best_hour,
                  best_weekday = excluded.best_weekday,
                  best_topic = excluded.best_topic,
                  best_hook = excluded.best_hook,
                  best_cta = excluded.best_cta,
                  brain_score = excluded.brain_score,
                  metric_coverage = excluded.metric_coverage,
                  updated_at = excluded.updated_at
                RETURNING user_id, threads_account_id, posts_total,
                          views_total, likes_total, comments_total,
                          shares_total, avg_er::double precision,
                          avg_views::double precision, best_post_id,
                          worst_post_id, best_hour, best_weekday,
                          best_topic, best_hook, best_cta,
                          brain_score::double precision, updated_at
            """), params)
        ).mappings().first()
        if row is None:
            return None
        return AnalyticsAccountSummary.model_validate(dict(row))

    async def overview(
        self,
        user_id: int,
        account_id: int,
    ) -> Mapping[str, Any] | None:
        return (
            await self.session.execute(text("""
                SELECT * FROM analytics_account_summary
                WHERE user_id = :user_id
                  AND threads_account_id = :account_id
            """), {"user_id": user_id, "account_id": account_id})
        ).mappings().first()

    async def top_posts(
        self,
        user_id: int,
        account_id: int,
        *,
        limit: int = 10,
    ) -> list[Mapping[str, Any]]:
        return list((
            await self.session.execute(text("""
                SELECT threads_post_id, current_views, engagement_rate,
                       topic, published_at, brain_score
                FROM analytics_post_summary
                WHERE user_id = :user_id
                  AND threads_account_id = :account_id
                ORDER BY current_views DESC NULLS LAST,
                         engagement_rate DESC NULLS LAST
                LIMIT :post_limit
            """), {
                "user_id": user_id,
                "account_id": account_id,
                "post_limit": limit,
            })
        ).mappings().all())

    async def dimension_stats(
        self,
        user_id: int,
        account_id: int,
        dimension: str,
        *,
        limit: int = 10,
    ) -> list[Mapping[str, Any]]:
        return list((
            await self.session.execute(text("""
                SELECT dimension_key, posts_count, avg_views, avg_er,
                       avg_replies, avg_brain_score,
                       avg_virality_score, avg_ctr
                FROM analytics_aggregates
                WHERE user_id = :user_id
                  AND threads_account_id = :account_id
                  AND dimension = :dimension
                ORDER BY avg_brain_score DESC NULLS LAST,
                         posts_count DESC, dimension_key
                LIMIT :row_limit
            """), {
                "user_id": user_id,
                "account_id": account_id,
                "dimension": dimension,
                "row_limit": limit,
            })
        ).mappings().all())

    async def growth_history(
        self,
        user_id: int,
        account_id: int,
        *,
        post_limit: int = 5,
    ) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(text("""
                SELECT summary.threads_post_id, summary.published_at,
                       snapshot.snapshot_at, snapshot.views
                FROM analytics_post_summary summary
                JOIN analytics_snapshots snapshot
                  ON snapshot.threads_account_id = summary.threads_account_id
                 AND snapshot.provider = summary.provider
                 AND snapshot.threads_post_id = summary.threads_post_id
                WHERE summary.user_id = :user_id
                  AND summary.threads_account_id = :account_id
                  AND summary.threads_post_id IN (
                    SELECT recent.threads_post_id
                    FROM analytics_post_summary recent
                    WHERE recent.user_id = :user_id
                      AND recent.threads_account_id = :account_id
                    ORDER BY recent.last_updated DESC
                    LIMIT :post_limit
                  )
                ORDER BY summary.published_at DESC, snapshot.snapshot_at
            """), {
                "user_id": user_id,
                "account_id": account_id,
                "post_limit": post_limit,
            })
        ).mappings().all()
        grouped: dict[str, dict[str, Any]] = defaultdict(dict)
        snapshots: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            post_id = str(row["threads_post_id"])
            grouped[post_id] = {
                "threads_post_id": post_id,
                "published_at": row["published_at"],
            }
            snapshots[post_id].append(row)
        targets = (
            ("30m", timedelta(minutes=30), timedelta(minutes=30)),
            ("2h", timedelta(hours=2), timedelta(minutes=45)),
            ("24h", timedelta(hours=24), timedelta(hours=3)),
            ("7d", timedelta(days=7), timedelta(hours=12)),
        )
        result = []
        for post_id, item in grouped.items():
            for label, delta, tolerance in targets:
                target = item["published_at"] + delta
                candidates = [
                    row for row in snapshots[post_id]
                    if abs(row["snapshot_at"] - target) <= tolerance
                ]
                match = min(
                    candidates,
                    key=lambda row: abs(row["snapshot_at"] - target),
                    default=None,
                )
                item[label] = match["views"] if match else None
            result.append(item)
        return result
