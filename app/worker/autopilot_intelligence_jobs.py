"""Periodic, account-isolated decision calculation with no side effects."""

import logging

from sqlalchemy import text

from app.core.autopilot_intelligence.service import AutopilotIntelligenceService
from app.core.db import Session

log = logging.getLogger("autopilot_intelligence_worker")


async def autopilot_intelligence_job() -> dict[str, int]:
    async with Session() as session:
        accounts = (
            await session.execute(text("""
                SELECT user_id, id
                FROM threads_accounts
                ORDER BY id
            """))
        ).all()

    totals = {
        "accounts": len(accounts),
        "evaluated": 0,
        "failed": 0,
    }
    for user_id, account_id in accounts:
        async with Session() as session:
            try:
                await AutopilotIntelligenceService(session).evaluate_account(
                    int(user_id), int(account_id)
                )
                await session.commit()
                totals["evaluated"] += 1
            except Exception as error:
                await session.rollback()
                totals["failed"] += 1
                log.warning(
                    "autopilot intelligence failed user=%s account=%s "
                    "error_type=%s",
                    user_id,
                    account_id,
                    type(error).__name__,
                )
    log.info("autopilot intelligence complete result=%s", totals)
    return totals
