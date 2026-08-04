"""Build an account-scoped decision context from existing services."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.analytics.repository import AnalyticsRepository
from app.core.autopost_status import AutopostStatusService, resolve_timezone
from app.core.brain_repo import BrainRepo
from app.core.autopilot_intelligence.models import (
    AnalyticsSummary,
    BrainSummary,
    DecisionContext,
    DecisionStatus,
    LastDecisionSummary,
    NeuroSummary,
    QueueHealth,
    RadarSummary,
    SubscriptionSummary,
)

log = logging.getLogger("autopilot_intelligence.context")

ANALYTICS_MAX_AGE = timedelta(hours=2)
RADAR_SEARCH_INTERVAL = timedelta(hours=6)
T = TypeVar("T")


class DecisionContextError(RuntimeError):
    pass


class DecisionOwnershipError(DecisionContextError):
    pass


def _mapping(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    value = getattr(row, "_mapping", row)
    return dict(value) if isinstance(value, Mapping) else {}


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _topics(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str):
        return ()
    normalized = value.replace(",", "\n")
    return tuple(
        item.strip()[:120]
        for item in normalized.splitlines()
        if item.strip()
    )[:20]


def _performance_posts(performance: dict[str, Any]) -> int:
    feedback = _json_object(performance.get("feedback_v1"))
    analytics = _json_object(performance.get("analytics_v2"))
    return max(
        0,
        int(
            feedback.get("posts_analyzed")
            or analytics.get("posts_total")
            or 0
        ),
    )


def queue_health(
    *,
    active: bool,
    queue_size: int,
    posts_per_day: int,
    failed_today: int,
    stuck_publishing: int,
    unknown_publications: int,
) -> QueueHealth:
    if not active:
        return QueueHealth.DISABLED
    if stuck_publishing or unknown_publications:
        return QueueHealth.RECOVERY_REQUIRED
    if queue_size == 0:
        return QueueHealth.EMPTY
    expected = max(1, posts_per_day)
    if queue_size < expected * 2:
        return QueueHealth.LOW
    if queue_size > expected * 7:
        return QueueHealth.FULL
    if failed_today:
        return QueueHealth.LOW
    return QueueHealth.HEALTHY


class DecisionContextBuilder:
    def __init__(
        self,
        session: AsyncSession,
        *,
        autopost: AutopostStatusService | None = None,
        analytics: AnalyticsRepository | None = None,
        brains: BrainRepo | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.session = session
        self.autopost = autopost or AutopostStatusService(session)
        self.analytics = analytics or AnalyticsRepository(session)
        self.brains = brains or BrainRepo(session)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def _optional(
        self,
        source: str,
        loader: Callable[[], Awaitable[T]],
        default: T,
    ) -> T:
        try:
            async with self.session.begin_nested():
                return await loader()
        except Exception as error:
            log.warning(
                "decision context source unavailable source=%s error_type=%s",
                source,
                type(error).__name__,
            )
            return default

    async def _load_account(
        self,
        user_id: int,
        account_id: int,
    ) -> dict[str, Any]:
        row = (
            await self.session.execute(text("""
                SELECT account.id, account.connection_status,
                       account.expires_at,
                       (account.access_token_enc IS NOT NULL)
                         AS has_access_token,
                       users.credits_balance,
                       coalesce(subscription.plan, 'free')
                         AS subscription_plan,
                       coalesce(subscription.status, 'active')
                         AS subscription_status,
                       coalesce(setting.active, false) AS planner_enabled,
                       coalesce(setting.posts_per_day, 0) AS posts_per_day,
                       coalesce(setting.topics, '') AS topics,
                       coalesce(setting.goal, '') AS goal,
                       coalesce(setting.timezone, 'Europe/Moscow') AS timezone
                FROM threads_accounts account
                JOIN users ON users.id = account.user_id
                LEFT JOIN subscriptions subscription
                  ON subscription.user_id = account.user_id
                LEFT JOIN autocontent_settings setting
                  ON setting.user_id = account.user_id
                 AND setting.threads_account_id = account.id
                WHERE account.user_id = :user_id
                  AND account.id = :account_id
            """), {"user_id": user_id, "account_id": account_id})
        ).mappings().first()
        data = _mapping(row)
        if not data:
            raise DecisionOwnershipError("Threads account is not owned")
        return data

    async def _load_daily(
        self,
        user_id: int,
        account_id: int,
        timezone_name: str,
        now: datetime,
    ) -> dict[str, Any]:
        row = (
            await self.session.execute(text("""
                SELECT
                  count(*) FILTER (
                    WHERE (post.run_at AT TIME ZONE :timezone)::date =
                          (:now AT TIME ZONE :timezone)::date
                  ) AS scheduled_today,
                  count(*) FILTER (
                    WHERE post.status = 'done'
                      AND (post.run_at AT TIME ZONE :timezone)::date =
                          (:now AT TIME ZONE :timezone)::date
                  ) AS published_today,
                  (SELECT count(*) FROM autopost_runs failed
                   WHERE failed.user_id = :user_id
                     AND failed.threads_account_id = :account_id
                     AND failed.status = 'failed'
                     AND (coalesce(failed.finished_at, failed.started_at)
                          AT TIME ZONE :timezone)::date =
                         (:now AT TIME ZONE :timezone)::date)
                    AS failed_today,
                  count(*) FILTER (
                    WHERE post.status = 'publishing'
                      AND (
                        post.publish_started_at IS NULL
                        OR post.publish_started_at < :now - interval '15 minutes'
                      )
                  ) AS stuck_publishing,
                  (SELECT count(*) FROM autopost_runs run
                   WHERE run.user_id = :user_id
                     AND run.threads_account_id = :account_id
                     AND run.started_at >= :now - interval '24 hours'
                     AND run.error_code = 'UNKNOWN_ERROR'
                     AND coalesce(run.safe_error_message, '') ~*
                         '(not confirmed|interrupted|не подтвержден)')
                    AS unknown_publications
                FROM scheduled_posts post
                WHERE post.user_id = :user_id
                  AND post.threads_account_id = :account_id
            """), {
                "user_id": user_id,
                "account_id": account_id,
                "timezone": timezone_name,
                "now": now,
            })
        ).mappings().first()
        return _mapping(row)

    async def _load_radar(
        self,
        user_id: int,
        account_id: int,
    ) -> dict[str, Any]:
        row = (
            await self.session.execute(text("""
                SELECT cardinality(setting.keywords) AS keyword_count,
                       latest.started_at AS last_search_at,
                       latest.status AS last_status,
                       (SELECT count(*) FROM radar_candidates candidate
                        WHERE candidate.user_id = :user_id
                          AND candidate.threads_account_id = :account_id
                          AND candidate.status = 'ready') AS ready_count,
                       (SELECT max(candidate.final_score)
                        FROM radar_candidates candidate
                        WHERE candidate.user_id = :user_id
                          AND candidate.threads_account_id = :account_id
                          AND candidate.status = 'ready') AS best_score
                FROM radar_settings setting
                LEFT JOIN LATERAL (
                  SELECT run.started_at, run.status
                  FROM radar_search_runs run
                  WHERE run.user_id = setting.user_id
                    AND run.threads_account_id = setting.threads_account_id
                  ORDER BY run.started_at DESC, run.id DESC LIMIT 1
                ) latest ON true
                WHERE setting.user_id = :user_id
                  AND setting.threads_account_id = :account_id
            """), {"user_id": user_id, "account_id": account_id})
        ).mappings().first()
        return _mapping(row)

    async def _load_neuro(
        self,
        user_id: int,
        account_id: int,
        timezone_name: str,
        now: datetime,
    ) -> dict[str, Any]:
        row = (
            await self.session.execute(text("""
                SELECT setting.active, setting.mode, setting.daily_cap,
                       count(comment.id) FILTER (
                         WHERE comment.status = 'pending'
                       ) AS pending_count,
                       count(comment.id) FILTER (
                         WHERE comment.status = 'posted'
                           AND (coalesce(comment.posted_at, comment.created_at)
                                AT TIME ZONE :timezone)::date =
                               (:now AT TIME ZONE :timezone)::date
                       ) AS posted_today,
                       count(comment.id) FILTER (
                         WHERE comment.status = 'publishing'
                       ) AS publishing_count,
                       count(comment.id) FILTER (
                         WHERE comment.status = 'unknown'
                       ) AS unknown_count,
                       count(comment.id) FILTER (
                         WHERE comment.status = 'failed'
                       ) AS failed_count,
                       bool_or(comment.status = 'permission_denied')
                         AS permission_denied
                FROM neuro_settings setting
                LEFT JOIN neuro_comments comment
                  ON comment.user_id = setting.user_id
                 AND comment.threads_account_id = setting.threads_account_id
                WHERE setting.user_id = :user_id
                  AND setting.threads_account_id = :account_id
                GROUP BY setting.active, setting.mode, setting.daily_cap
            """), {
                "user_id": user_id,
                "account_id": account_id,
                "timezone": timezone_name,
                "now": now,
            })
        ).mappings().first()
        return _mapping(row)

    async def _load_last_decision(
        self,
        user_id: int,
        account_id: int,
    ) -> dict[str, Any]:
        row = (
            await self.session.execute(text("""
                SELECT decision_hash, status, health_score, created_at
                FROM decision_runs
                WHERE user_id = :user_id
                  AND threads_account_id = :account_id
                ORDER BY created_at DESC, id DESC LIMIT 1
            """), {"user_id": user_id, "account_id": account_id})
        ).mappings().first()
        return _mapping(row)

    async def build(
        self,
        user_id: int,
        threads_account_id: int,
    ) -> DecisionContext:
        now = _aware(self.clock()) or datetime.now(timezone.utc)
        account = await self._load_account(user_id, threads_account_id)
        timezone_name = resolve_timezone(account.get("timezone")).key

        status = await self._optional(
            "autopost_status",
            lambda: self.autopost.get_status(
                user_id, threads_account_id, now=now
            ),
            None,
        )
        queue = await self._optional(
            "autopost_queue",
            lambda: self.autopost.queue_summary(
                user_id, threads_account_id, now=now
            ),
            None,
        )
        daily = await self._optional(
            "publishing",
            lambda: self._load_daily(
                user_id, threads_account_id, timezone_name, now
            ),
            {},
        )
        analytics_row = await self._optional(
            "analytics",
            lambda: self.analytics.overview(user_id, threads_account_id),
            None,
        )
        brain = await self._optional(
            "brain",
            lambda: self.brains.get_by_account(user_id, threads_account_id),
            None,
        )
        radar_row = await self._optional(
            "radar",
            lambda: self._load_radar(user_id, threads_account_id),
            {},
        )
        neuro_row = await self._optional(
            "neuro",
            lambda: self._load_neuro(
                user_id, threads_account_id, timezone_name, now
            ),
            {},
        )
        decision_row = await self._optional(
            "decision_history",
            lambda: self._load_last_decision(user_id, threads_account_id),
            {},
        )

        analytics_data = _mapping(analytics_row)
        analytics_updated = _aware(analytics_data.get("updated_at"))
        analytics_posts = int(analytics_data.get("posts_total") or 0)
        analytics_available = bool(analytics_data) and analytics_posts > 0
        analytics_summary = AnalyticsSummary(
            available=analytics_available,
            stale=bool(
                analytics_available
                and analytics_updated
                and now - analytics_updated > ANALYTICS_MAX_AGE
            ),
            posts_total=analytics_posts,
            average_views=(
                float(analytics_data["avg_views"])
                if analytics_data.get("avg_views") is not None else None
            ),
            engagement_rate=(
                float(analytics_data["avg_er"])
                if analytics_data.get("avg_er") is not None else None
            ),
            brain_score=(
                float(analytics_data["brain_score"])
                if analytics_data.get("brain_score") is not None else None
            ),
            best_topic=analytics_data.get("best_topic"),
            best_hour=(
                int(analytics_data["best_hour"])
                if analytics_data.get("best_hour") is not None else None
            ),
            updated_at=analytics_updated,
        )

        goals = _json_object(getattr(brain, "goals", {}))
        performance = _json_object(getattr(brain, "performance", {}))
        brain_summary = BrainSummary(
            available=brain is not None,
            version=getattr(brain, "version", None),
            primary_goal=str(goals.get("primary") or ""),
            performance_posts=_performance_posts(performance),
            updated_at=_aware(getattr(brain, "updated_at", None)),
        )

        radar_last = _aware(radar_row.get("last_search_at"))
        radar_active = int(radar_row.get("keyword_count") or 0) > 0
        radar_summary = RadarSummary(
            available=bool(radar_row),
            active=radar_active,
            ready_count=int(radar_row.get("ready_count") or 0),
            best_score=(
                float(radar_row["best_score"])
                if radar_row.get("best_score") is not None else None
            ),
            last_search_at=radar_last,
            last_status=radar_row.get("last_status"),
            search_due=bool(
                radar_active
                and (
                    radar_last is None
                    or now - radar_last > RADAR_SEARCH_INTERVAL
                )
            ),
        )
        neuro_summary = NeuroSummary(
            available=bool(neuro_row),
            active=bool(neuro_row.get("active")),
            mode=str(neuro_row.get("mode") or "approve"),
            pending_count=int(neuro_row.get("pending_count") or 0),
            posted_today=int(neuro_row.get("posted_today") or 0),
            daily_cap=int(neuro_row.get("daily_cap") or 0),
            publishing_count=int(neuro_row.get("publishing_count") or 0),
            unknown_count=int(neuro_row.get("unknown_count") or 0),
            failed_count=int(neuro_row.get("failed_count") or 0),
            permission_denied=bool(neuro_row.get("permission_denied")),
        )
        last_decision = (
            LastDecisionSummary(
                decision_hash=str(decision_row["decision_hash"]),
                status=DecisionStatus(str(decision_row["status"])),
                health_score=int(decision_row["health_score"]),
                created_at=decision_row["created_at"],
            )
            if decision_row else None
        )

        planner_enabled = bool(
            status.settings.enabled if status else account.get("planner_enabled")
        )
        queue_size = len(queue.posts) if queue is not None else 0
        posts_per_day = int(
            status.settings.posts_per_day
            if status else account.get("posts_per_day") or 0
        )
        token_expires_at = _aware(account.get("expires_at"))
        has_token = bool(account.get("has_access_token"))
        connected = account.get("connection_status") == "connected"
        publisher_enabled = bool(
            connected
            and has_token
            and (token_expires_at is None or token_expires_at > now)
        )
        failed_today = int(daily.get("failed_today") or 0)
        stuck = int(daily.get("stuck_publishing") or 0)
        unknown = int(daily.get("unknown_publications") or 0)

        return DecisionContext(
            user_id=user_id,
            threads_account_id=threads_account_id,
            connection_status=str(account.get("connection_status") or "error"),
            has_access_token=has_token,
            token_expires_at=token_expires_at,
            queue_size=queue_size,
            scheduled_today=int(daily.get("scheduled_today") or 0),
            published_today=int(daily.get("published_today") or 0),
            failed_today=failed_today,
            stuck_publishing=stuck,
            unknown_publications=unknown,
            analytics_summary=analytics_summary,
            brain_summary=brain_summary,
            radar_summary=radar_summary,
            neuro_summary=neuro_summary,
            credits_balance=max(0, int(account.get("credits_balance") or 0)),
            subscription=SubscriptionSummary(
                plan=str(account.get("subscription_plan") or "free"),
                status=str(account.get("subscription_status") or "active"),
            ),
            timezone=timezone_name,
            goal=str(account.get("goal") or brain_summary.primary_goal or ""),
            topics=_topics(account.get("topics")),
            posts_per_day=max(0, min(5, posts_per_day)),
            planner_enabled=planner_enabled,
            publisher_enabled=publisher_enabled,
            analytics_available=analytics_available,
            last_publish=(status.last_success_at if status else None),
            last_generation=(status.last_run_at if status else None),
            last_decision=last_decision,
            queue_health=queue_health(
                active=planner_enabled,
                queue_size=queue_size,
                posts_per_day=posts_per_day,
                failed_today=failed_today,
                stuck_publishing=stuck,
                unknown_publications=unknown,
            ),
            autopilot_active=planner_enabled,
            current_time=now,
        )
