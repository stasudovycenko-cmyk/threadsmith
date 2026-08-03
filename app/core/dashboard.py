"""Read-only aggregation for the account Dashboard."""

import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.accounts import ThreadsAccount
from app.schemas.ux import (
    DashboardAnalytics,
    DashboardAutopilot,
    DashboardBalance,
    DashboardData,
    DashboardNeuro,
    DashboardRadar,
    InterfaceMode,
)

log = logging.getLogger("dashboard")


class DashboardService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def load(
        self,
        user_id: int,
        account: ThreadsAccount,
        *,
        mode: InterfaceMode,
    ) -> DashboardData:
        autopilot = await self._block(
            "autopilot",
            """
            SELECT setting.active, setting.posts_per_day, setting.timezone,
                   (SELECT count(*) FROM scheduled_posts post
                    WHERE post.user_id = :user_id
                      AND post.threads_account_id = :account_id
                      AND post.status = 'done'
                      AND (post.run_at AT TIME ZONE setting.timezone)::date =
                          (now() AT TIME ZONE setting.timezone)::date
                   ) AS posts_today,
                   (SELECT count(*) FROM scheduled_posts post
                    WHERE post.user_id = :user_id
                      AND post.threads_account_id = :account_id
                      AND post.status IN ('pending', 'publishing')
                   ) AS queue_size,
                   (SELECT min(post.run_at) FROM scheduled_posts post
                    WHERE post.user_id = :user_id
                      AND post.threads_account_id = :account_id
                      AND post.status = 'pending'
                      AND post.run_at >= now()
                   ) AS next_post_at
            FROM autocontent_settings setting
            WHERE setting.user_id = :user_id
              AND setting.threads_account_id = :account_id
            """,
            user_id,
            account.id,
        )
        radar = await self._block(
            "radar",
            """
            SELECT
              (SELECT count(*) FROM radar_candidates candidate
               WHERE candidate.user_id = :user_id
                 AND candidate.threads_account_id = :account_id
                 AND candidate.status = 'ready') AS ready_count,
              latest.started_at AS last_search_at,
              latest.status AS last_status
            FROM (SELECT 1) seed
            LEFT JOIN LATERAL (
              SELECT started_at, status
              FROM radar_search_runs
              WHERE user_id = :user_id
                AND threads_account_id = :account_id
              ORDER BY started_at DESC, id DESC LIMIT 1
            ) latest ON true
            """,
            user_id,
            account.id,
        )
        neuro = await self._block(
            "neuro",
            """
            SELECT setting.active,
              (SELECT count(*) FROM neuro_comments comment
               WHERE comment.user_id = :user_id
                 AND comment.threads_account_id = :account_id
                 AND comment.status = 'posted'
                 AND (
                   coalesce(comment.posted_at, comment.created_at)
                   AT TIME ZONE coalesce(content.timezone, 'Europe/Moscow')
                 )::date = (
                   now() AT TIME ZONE coalesce(
                     content.timezone, 'Europe/Moscow'
                   )
                 )::date) AS posted_today,
              (SELECT count(*) FROM neuro_comments comment
               WHERE comment.user_id = :user_id
                 AND comment.threads_account_id = :account_id
                 AND comment.status = 'pending') AS pending_count
            FROM neuro_settings setting
            LEFT JOIN autocontent_settings content
              ON content.user_id = setting.user_id
             AND content.threads_account_id = setting.threads_account_id
            WHERE setting.user_id = :user_id
              AND setting.threads_account_id = :account_id
            """,
            user_id,
            account.id,
        )
        analytics = await self._block(
            "analytics",
            """
            SELECT count(post.id) AS posts_30d,
                   sum(post.current_views) AS views_30d,
                   avg(post.engagement_rate) AS avg_er,
                   summary.brain_score
            FROM (SELECT 1) seed
            LEFT JOIN analytics_post_summary post
              ON post.user_id = :user_id
             AND post.threads_account_id = :account_id
             AND post.published_at >= now() - interval '30 days'
            LEFT JOIN analytics_account_summary summary
              ON summary.user_id = :user_id
             AND summary.threads_account_id = :account_id
            GROUP BY summary.brain_score
            """,
            user_id,
            account.id,
        )
        balance = await self._block(
            "balance",
            """
            SELECT users.credits_balance,
                   coalesce(subscription.plan, 'free') AS plan
            FROM users
            LEFT JOIN subscriptions subscription
              ON subscription.user_id = users.id
            WHERE users.id = :user_id
            """,
            user_id,
            account.id,
        )
        return DashboardData(
            user_id=user_id,
            account_id=account.id,
            username=account.username or str(account.id),
            connection_status=account.connection_status,
            interface_mode=mode,
            autopilot=self._autopilot(autopilot),
            radar=self._radar(radar),
            neuro=self._neuro(neuro),
            analytics=self._analytics(analytics),
            balance=self._balance(balance),
        )

    async def _block(
        self,
        name: str,
        statement: str,
        user_id: int,
        account_id: int,
    ) -> Mapping[str, Any] | None:
        try:
            async with self.session.begin_nested():
                return (
                    await self.session.execute(
                        text(statement),
                        {"user_id": user_id, "account_id": account_id},
                    )
                ).mappings().first()
        except Exception as error:
            log.warning(
                "dashboard block unavailable block=%s user=%s account=%s "
                "error_type=%s",
                name,
                user_id,
                account_id,
                type(error).__name__,
            )
            return None

    @staticmethod
    def _autopilot(row: Mapping[str, Any] | None) -> DashboardAutopilot:
        if row is None:
            return DashboardAutopilot(
                available=False,
                warning="Статус Автопилота временно недоступен.",
            )
        return DashboardAutopilot(
            enabled=bool(row.get("active")),
            posts_today=int(row.get("posts_today") or 0),
            daily_limit=int(row.get("posts_per_day") or 0),
            queue_size=int(row.get("queue_size") or 0),
            next_post_at=row.get("next_post_at"),
            timezone=str(row.get("timezone") or "Europe/Moscow"),
        )

    @staticmethod
    def _radar(row: Mapping[str, Any] | None) -> DashboardRadar:
        if row is None:
            return DashboardRadar(
                available=False,
                warning="Статус Radar временно недоступен.",
            )
        return DashboardRadar(
            ready_count=int(row.get("ready_count") or 0),
            last_search_at=row.get("last_search_at"),
            last_status=row.get("last_status"),
        )

    @staticmethod
    def _neuro(row: Mapping[str, Any] | None) -> DashboardNeuro:
        if row is None:
            return DashboardNeuro(
                available=False,
                warning="Статус Neuro временно недоступен.",
            )
        return DashboardNeuro(
            enabled=bool(row.get("active")),
            posted_today=int(row.get("posted_today") or 0),
            pending_count=int(row.get("pending_count") or 0),
        )

    @staticmethod
    def _analytics(row: Mapping[str, Any] | None) -> DashboardAnalytics:
        if row is None:
            return DashboardAnalytics(
                available=False,
                warning="Аналитика временно недоступна.",
            )
        posts = int(row.get("posts_30d") or 0)
        return DashboardAnalytics(
            posts_30d=posts,
            views_30d=(
                int(row["views_30d"])
                if row.get("views_30d") is not None
                else None
            ),
            avg_er=(
                float(row["avg_er"])
                if row.get("avg_er") is not None
                else None
            ),
            brain_score=(
                float(row["brain_score"])
                if row.get("brain_score") is not None
                else None
            ),
        )

    @staticmethod
    def _balance(row: Mapping[str, Any] | None) -> DashboardBalance:
        if row is None:
            return DashboardBalance(
                available=False,
                warning="Баланс временно недоступен.",
            )
        return DashboardBalance(
            credits=int(row.get("credits_balance") or 0),
            plan=str(row.get("plan") or "free"),
        )
