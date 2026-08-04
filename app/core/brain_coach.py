"""Deterministic recommendations based on Analytics V2 data."""

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.analytics.repository import AnalyticsRepository
from app.schemas.ux import BrainRecommendation

MIN_ACCOUNT_POSTS = 5
MIN_DIMENSION_POSTS = 3


def build_recommendations(
    overview: Mapping[str, Any] | None,
    topics: Sequence[Mapping[str, Any]],
    hooks: Sequence[Mapping[str, Any]],
    trend: Mapping[str, Any] | None,
) -> list[BrainRecommendation]:
    posts = int(overview.get("posts_total") or 0) if overview else 0
    if posts < MIN_ACCOUNT_POSTS:
        return [BrainRecommendation(
            kind="insufficient_data",
            title="Пока рано менять стратегию",
            detail=(
                f"Собрано постов: {posts} из {MIN_ACCOUNT_POSTS}. "
                "Продолжайте публиковать, чтобы рекомендации стали надёжнее."
            ),
            sample_size=posts,
        )]

    result: list[BrainRecommendation] = []
    if overview and overview.get("best_hour") is not None:
        hour = int(overview["best_hour"])
        result.append(BrainRecommendation(
            kind="best_time",
            title=f"Лучшее время: {hour:02d}:00",
            detail="В этот час публикации показывают лучший результат.",
            sample_size=posts,
        ))
    reliable_topics = [
        row for row in topics
        if int(row.get("posts_count") or 0) >= MIN_DIMENSION_POSTS
    ]
    if reliable_topics:
        best = reliable_topics[0]
        result.append(BrainRecommendation(
            kind="strong_topic",
            title=f"Сильная тема: {best['dimension_key']}",
            detail="Эта тема стабильно опережает другие по итоговой оценке.",
            sample_size=int(best["posts_count"]),
        ))
        if len(reliable_topics) > 1:
            weak = reliable_topics[-1]
            result.append(BrainRecommendation(
                kind="weak_topic",
                title=f"Тема для проверки: {weak['dimension_key']}",
                detail="Не отказывайтесь от неё сразу: сначала накопите ещё данные.",
                sample_size=int(weak["posts_count"]),
            ))
    reliable_hooks = [
        row for row in hooks
        if int(row.get("posts_count") or 0) >= MIN_DIMENSION_POSTS
    ]
    if reliable_hooks:
        best_hook = reliable_hooks[0]
        result.append(BrainRecommendation(
            kind="best_hook",
            title=f"Лучшее начало: {best_hook['dimension_key']}",
            detail="Используйте этот тип начала как основной ориентир.",
            sample_size=int(best_hook["posts_count"]),
        ))
    if trend:
        recent = trend.get("recent_avg")
        previous = trend.get("previous_avg")
        if recent is not None and previous and float(recent) < float(previous) * 0.8:
            result.append(BrainRecommendation(
                kind="views_decline",
                title="Средние просмотры снизились",
                detail="Проверьте темы и время публикации последних двух недель.",
                sample_size=posts,
            ))
    return result[:5]


class BrainCoachService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def recommendations(
        self,
        user_id: int,
        account_id: int,
    ) -> list[BrainRecommendation]:
        repository = AnalyticsRepository(self.session)
        overview = await repository.overview(user_id, account_id)
        topics = await repository.dimension_stats(
            user_id, account_id, "topic", limit=20
        )
        hooks = await repository.dimension_stats(
            user_id, account_id, "hook_type", limit=20
        )
        trend = (
            await self.session.execute(text("""
                SELECT
                  avg(current_views) FILTER (
                    WHERE published_at >= now() - interval '14 days'
                  ) AS recent_avg,
                  avg(current_views) FILTER (
                    WHERE published_at >= now() - interval '28 days'
                      AND published_at < now() - interval '14 days'
                  ) AS previous_avg
                FROM analytics_post_summary
                WHERE user_id = :user_id
                  AND threads_account_id = :account_id
            """), {"user_id": user_id, "account_id": account_id})
        ).mappings().first()
        return build_recommendations(overview, topics, hooks, trend)
