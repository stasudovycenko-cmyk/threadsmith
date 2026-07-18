"""
Воркер. На MVP - один процесс, APScheduler, джобы как async-функции.
Сейчас живёт только token_refresher. publisher / comment_poller /
library_crawler / insights_snapshotter заедут с Модулями 3 и 1 -
регистрировать их сюда же.

Переезд на ARQ+Redis потом: каждая джоба уже изолированная функция,
перенос - это замена планировщика, не переписывание логики.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import text

from app.core.crypto import decrypt_token, encrypt_token
from app.core.db import Session
from app.core.threads_api import refresh_long_lived
from app.worker.m1_jobs import insights_snapshotter, library_crawler
from app.worker.m4_jobs import neuro_hunter
from app.worker.m3_jobs import comment_poller, publisher

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("worker")


async def token_refresher():
    """Рефрешит long-lived токены за 7 дней до протухания.
    Протухший рефрешнуть нельзя - помечаем, юзеру придётся переподключиться."""
    async with Session() as s:
        rows = (await s.execute(text("""
            SELECT id, access_token_enc, user_id FROM threads_accounts
            WHERE expires_at < now() + interval '7 days'
              AND expires_at > now()
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
                """), {"tok": encrypt_token(new["access_token"]),
                       "exp": expires, "id": acc_id})
                log.info("refreshed token acc=%s", acc_id)
            except Exception:
                log.exception("refresh failed acc=%s user=%s", acc_id, user_id)
        await s.commit()


async def main():
    sched = AsyncIOScheduler()
    sched.add_job(token_refresher, "interval", hours=12)
    sched.add_job(publisher, "interval", minutes=1, max_instances=1)
    sched.add_job(comment_poller, "interval", minutes=5, max_instances=1)
    sched.add_job(library_crawler, "interval", hours=1, max_instances=1)
    sched.add_job(neuro_hunter, "interval", minutes=20, max_instances=1)
    sched.add_job(insights_snapshotter, "cron", hour=3, minute=30)
    sched.start()
    log.info("worker started")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
