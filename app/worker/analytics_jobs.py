"""Periodic account-isolated Analytics V2 collection."""

import logging

from sqlalchemy import text

from app.core.analytics.providers.threads import ThreadsAnalyticsProvider
from app.core.analytics.repository import AnalyticsRepository
from app.core.analytics.service import (
    AnalyticsCollector,
    SocialBrainAnalyticsFeedback,
)
from app.core.brain_writer import BrainWriter
from app.core.crypto import decrypt_token
from app.core.db import Session

log = logging.getLogger("analytics_worker")


async def analytics_collector():
    """Collect recent own-post metrics for every connected account."""
    async with Session() as session:
        accounts = (
            await session.execute(text("""
                SELECT user_id, id, access_token_enc
                FROM threads_accounts
                WHERE connection_status = 'connected'
                  AND access_token_enc IS NOT NULL
                  AND expires_at > now()
                ORDER BY id
            """))
        ).all()

    results = []
    for user_id, account_id, encrypted_token in accounts:
        async with Session() as session:
            try:
                repository = AnalyticsRepository(session)
                collector = AnalyticsCollector(
                    repository,
                    feedback=SocialBrainAnalyticsFeedback(
                        BrainWriter(session)
                    ),
                )
                result = await collector.collect_account(
                    int(user_id),
                    int(account_id),
                    ThreadsAnalyticsProvider(
                        decrypt_token(encrypted_token)
                    ),
                )
                await session.commit()
                results.append(result)
                log.info(
                    "analytics account=%s posts=%s snapshots=%s failures=%s",
                    account_id,
                    result.posts_seen,
                    result.snapshots_written,
                    result.failures,
                )
            except Exception as error:
                await session.rollback()
                log.warning(
                    "analytics account collection failed account=%s "
                    "error_type=%s",
                    account_id,
                    type(error).__name__,
                )
    return results
