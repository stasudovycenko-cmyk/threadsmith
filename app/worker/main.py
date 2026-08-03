"""
Воркер. Один процесс, APScheduler, джобы как async-функции.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import text

from app.core.autopost_status import recover_autopost_state
from app.core.crypto import decrypt_token, encrypt_token
from app.core.db import Session
from app.core.threads_api import refresh_long_lived
from app.worker.autocontent import autocontent_planner
from app.worker.feedback_jobs import feedback_loop_job
from app.worker.m1_jobs import insights_snapshotter, library_crawler
from app.worker.m3_jobs import comment_poller, publisher
from app.worker.m4_jobs import neuro_hunter, neuro_reply_poller

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("worker")


async def token_refresher():
    """Рефрешит long-lived токены за 7 дней до протухания."""
    async with Session() as s:
        rows = (await s.execute(text("""
            SELECT id, access_token_enc, user_id FROM threads_accounts
            WHERE expires_at < now() + interval '7 days'
              AND expires_at > now()
              AND connection_status = 'connected'
              AND access_token_enc IS NOT NULL
        """))).all()

        for acc_id, tok_enc, user_id in rows:
            try:
                new = await refresh_long_lived(decrypt_token(tok_enc))
                expires = datetime.now(timezone.utc) + timedelta(
                    seconds=new["expires_in"])
                await s.execute(text("""
                    UPDATE threads_accounts
                    SET access_token_enc = :tok, expires_at = :exp
                    WHERE id = :id
                      AND user_id = :user_id
                      AND connection_status = 'connected'
                """), {"tok": encrypt_token(new["access_token"]),
                       "exp": expires, "id": acc_id,
                       "user_id": user_id})
                log.info("refreshed token acc=%s", acc_id)
            except Exception as error:
                log.warning(
                    "refresh failed acc=%s user=%s error_type=%s",
                    acc_id,
                    user_id,
                    type(error).__name__,
                )
        await s.commit()


async def autopost_recovery_job():
    async with Session() as session:
        result = await recover_autopost_state(session)
        await session.commit()
    if any(result.values()):
        log.warning("autopost recovery result=%s", result)
    return result


def build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(
        timezone=timezone.utc,
        job_defaults={
            "coalesce": True,
            "misfire_grace_time": 60,
            "max_instances": 1,
        },
    )
    scheduler.add_job(token_refresher, "interval", hours=12)
    scheduler.add_job(
        publisher,
        "interval",
        minutes=1,
        misfire_grace_time=30,
    )
    # comment_poller disabled until threads_manage_replies is approved by Meta
    scheduler.add_job(library_crawler, "interval", hours=1)
    scheduler.add_job(neuro_hunter, "interval", minutes=20)
    scheduler.add_job(neuro_reply_poller, "interval", minutes=30)
    scheduler.add_job(
        autocontent_planner,
        "interval",
        minutes=5,
        next_run_time=datetime.now(timezone.utc),
    )
    scheduler.add_job(
        autopost_recovery_job,
        "interval",
        minutes=5,
    )
    scheduler.add_job(insights_snapshotter, "cron", hour=3, minute=30)
    scheduler.add_job(
        feedback_loop_job,
        "cron",
        hour=4,
        minute=15,
        max_instances=1,
        coalesce=True,
    )
    return scheduler


async def main():
    recovery = await autopost_recovery_job()
    log.info("autopost recovery complete result=%s", recovery)

    sched = build_scheduler()
    sched.start()
    log.info("worker started")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
