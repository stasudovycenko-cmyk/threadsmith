"""Threads implementation of the provider-neutral analytics boundary."""

import math
from datetime import datetime
from typing import Any

from app.core.threads_api import get_insights, get_own_threads
from app.schemas.analytics import (
    AnalyticsMetrics,
    AnalyticsProviderName,
    ProviderAnalyticsPost,
)

_COUNT_METRICS = (
    "views",
    "likes",
    "replies",
    "quotes",
    "reposts",
    "shares",
    "profile_visits",
    "followers",
)


def _count(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if math.isfinite(value) and value >= 0 and value.is_integer():
            return int(value)
        return None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


class ThreadsAnalyticsProvider:
    name: AnalyticsProviderName = "threads"

    def __init__(self, access_token: str):
        self._access_token = access_token

    async def list_recent_posts(
        self,
        *,
        since: datetime,
        limit: int,
    ) -> list[ProviderAnalyticsPost]:
        rows = await get_own_threads(self._access_token, limit=limit)
        posts = []
        for row in rows:
            post_id = row.get("id")
            timestamp = row.get("timestamp")
            if not post_id or not timestamp:
                continue
            try:
                post = ProviderAnalyticsPost(
                    post_id=str(post_id),
                    published_at=timestamp,
                    text=row.get("text") or "",
                )
            except (TypeError, ValueError):
                continue
            if post.published_at >= since:
                posts.append(post)
        return posts

    async def get_post_metrics(self, post_id: str) -> AnalyticsMetrics:
        raw = await get_insights(self._access_token, post_id)
        return AnalyticsMetrics(**{
            key: normalized
            for key in _COUNT_METRICS
            if (normalized := _count(raw.get(key))) is not None
        })
