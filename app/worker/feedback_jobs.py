"""Periodic deterministic Feedback Loop jobs."""

import logging

from app.core.brain_repo import BrainRepo
from app.core.db import Session
from app.core.feedback_loop import FeedbackLoop

log = logging.getLogger("worker.feedback")


async def feedback_loop_job() -> dict[str, int]:
    """Analyze every Brain with transaction isolation per account."""
    async with Session() as session:
        brains = await BrainRepo(session).list_all()

    totals = {
        "brains": len(brains),
        "changed": 0,
        "unchanged": 0,
        "failed": 0,
    }
    for brain in brains:
        async with Session() as session:
            try:
                result = await FeedbackLoop(session).analyze_account(
                    brain.id,
                    user_id=brain.user_id,
                    account_id=brain.threads_account_id,
                )
                await session.commit()
                key = "changed" if result.changed else "unchanged"
                totals[key] += 1
            except Exception as exc:
                totals["failed"] += 1
                try:
                    await session.rollback()
                except Exception:
                    log.exception(
                        "feedback rollback failed brain=%s account=%s",
                        brain.id,
                        brain.threads_account_id,
                    )
                log.error(
                    "feedback failed brain=%s user=%s account=%s",
                    brain.id,
                    brain.user_id,
                    brain.threads_account_id,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

    log.info(
        "feedback complete brains=%s changed=%s unchanged=%s failed=%s",
        totals["brains"],
        totals["changed"],
        totals["unchanged"],
        totals["failed"],
    )
    return totals
