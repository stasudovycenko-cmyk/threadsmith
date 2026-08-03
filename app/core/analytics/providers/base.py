"""Provider contract for post-level analytics sources."""

from datetime import datetime
from typing import Protocol

from app.schemas.analytics import (
    AnalyticsMetrics,
    AnalyticsProviderName,
    ProviderAnalyticsPost,
)


class AnalyticsProvider(Protocol):
    name: AnalyticsProviderName

    async def list_recent_posts(
        self,
        *,
        since: datetime,
        limit: int,
    ) -> list[ProviderAnalyticsPost]:
        """Return recent posts owned by the connected source account."""

    async def get_post_metrics(self, post_id: str) -> AnalyticsMetrics:
        """Return only metrics actually supplied by the source."""
