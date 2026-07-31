"""
Модуль 3 - Автопилот. Логика публикации, воркеры дёргают отсюда.

Решения:
- Захват поста на публикацию - атомарный UPDATE status pending->publishing.
  Два воркера (или рестарт посреди джобы) не опубликуют один пост дважды.
- Лимит 250/сутки считаем по своей базе (done за 24ч по аккаунту). Упёрлись -
  двигаем run_at на час вперёд, юзеру шлём уведомление через outbox.
- Ссылка НЕ в посте: пост уходит без неё, ссылка с UTM - первым реплаем.
  Threads режет охват постов с внешними ссылками, вся механика ради этого.
"""
import logging
from datetime import datetime, timezone
from urllib.parse import urlencode, urlparse

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.autopost_status import (
    AutopostStatusService,
    SAFE_ERROR_MESSAGES,
    normalize_error,
)
from app.core.brain_writer import BrainWriter
from app.core.crypto import decrypt_token
from app.core.threads_api import create_container, publish_container

log = logging.getLogger("autopilot")

DAILY_POST_LIMIT = 250


def add_utm(link: str, post_id: int) -> str:
    """Дописывает UTM, не ломая существующие query-параметры."""
    utm = {"utm_source": "threads", "utm_medium": "post",
           "utm_campaign": f"ap_{post_id}"}
    sep = "&" if urlparse(link).query else "?"
    return f"{link}{sep}{urlencode(utm)}"


async def claim_due_posts(session: AsyncSession, limit: int = 10) -> list:
    """Атомарно захватывает посты, которым пора публиковаться."""
    rows = (await session.execute(text("""
        UPDATE scheduled_posts
        SET status = 'publishing', publish_started_at = now()
        WHERE id IN (
            SELECT id FROM scheduled_posts
            WHERE status = 'pending' AND run_at <= now()
            ORDER BY run_at LIMIT :lim
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, user_id, threads_account_id, text, media_url, link
    """), {"lim": limit})).all()
    return rows


async def daily_count(session: AsyncSession, account_id: int) -> int:
    row = (await session.execute(text("""
        SELECT count(*) FROM scheduled_posts
        WHERE threads_account_id = :acc AND status = 'done'
          AND run_at > now() - interval '24 hours'
    """), {"acc": account_id})).first()
    return row[0]


async def publish_one(session: AsyncSession, post_row) -> tuple[bool, str]:
    """Публикует один захваченный пост. Возвращает (ок, сообщение для юзера)."""
    post_id, user_id, acc_id, body, media_url, link = post_row
    state = (await session.execute(text("""
        SELECT status
        FROM scheduled_posts
        WHERE id = :id
          AND user_id = :uid
          AND threads_account_id = :acc
        FOR UPDATE
    """), {
        "id": post_id,
        "uid": user_id,
        "acc": acc_id,
    })).first()
    if not state or state[0] != "publishing":
        return False, "Публикация уже обработана."

    if await daily_count(session, acc_id) >= DAILY_POST_LIMIT:
        await session.execute(text("""
            UPDATE scheduled_posts
            SET status = 'pending',
                run_at = run_at + interval '1 hour',
                publish_started_at = NULL
            WHERE id = :id
        """), {"id": post_id})
        return False, "Упёрлись в лимит 250 постов/сутки, пост сдвинут на час."

    acc = (await session.execute(text("""
        SELECT threads_user_id, access_token_enc, expires_at
        FROM threads_accounts
        WHERE id = :acc
    """), {"acc": acc_id})).first()
    if not acc:
        code = "AUTH_EXPIRED"
        message = SAFE_ERROR_MESSAGES[code]
        await session.execute(text(
            "UPDATE scheduled_posts SET status='failed', error=:error "
            "WHERE id=:id"
        ), {"id": post_id, "error": f"{code}: {message}"})
        await AutopostStatusService(session).finish_for_post(
            post_id,
            status="failed",
            error_code=code,
            safe_error_message=message,
        )
        return False, f"❌ {message}"

    threads_uid, tok_enc, expires_at = acc
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            code = "AUTH_EXPIRED"
            message = SAFE_ERROR_MESSAGES[code]
            await session.execute(text("""
                UPDATE scheduled_posts
                SET status = 'failed', error = :error
                WHERE id = :id
            """), {"id": post_id, "error": f"{code}: {message}"})
            await AutopostStatusService(session).finish_for_post(
                post_id,
                status="failed",
                error_code=code,
                safe_error_message=message,
            )
            return False, f"❌ {message}"
    try:
        token = decrypt_token(tok_enc)
        container = await create_container(token, threads_uid, body,
                                           image_url=media_url)
        published_id = await publish_container(token, threads_uid, container)

        first_comment_note = ""
        if link:
            utm_link = add_utm(link, post_id)
            try:
                reply_c = await create_container(
                    token, threads_uid, utm_link, reply_to_id=published_id)
                await publish_container(token, threads_uid, reply_c)
                first_comment_note = " Ссылка ушла первым комментом."
            except Exception as exc:
                log.error(
                    "first-comment link failed post=%s error_type=%s",
                    post_id,
                    type(exc).__name__,
                )
                first_comment_note = " ⚠️ Пост вышел, но ссылка комментом не легла - закинь руками."

        await session.execute(text("""
            UPDATE scheduled_posts
            SET status = 'done', threads_post_id = :tpid, utm = :utm
            WHERE id = :id
        """), {"tpid": published_id, "id": post_id,
               "utm": add_utm(link, post_id) if link else None})

        # ставим пост на поллинг комментов (для автоответов)
        await session.execute(text("""
            INSERT INTO poll_state (threads_post_id, threads_account_id, tier)
            VALUES (:tpid, :acc, 0)
            ON CONFLICT (threads_post_id) DO NOTHING
        """), {"tpid": published_id, "acc": acc_id})

        try:
            async with session.begin_nested():
                await BrainWriter(session).record_post_published(
                    user_id,
                    acc_id,
                    scheduled_post_id=post_id,
                    threads_post_id=published_id,
                )
        except Exception as exc:
            log.warning(
                "Brain post_published event failed post=%s "
                "account=%s error_type=%s",
                post_id,
                acc_id,
                type(exc).__name__,
            )

        await AutopostStatusService(session).finish_for_post(
            post_id,
            status="success",
            threads_post_id=published_id,
        )
        return True, f"✅ Пост опубликован.{first_comment_note}"
    except Exception as e:
        log.error(
            "publish failed post=%s account=%s error_type=%s",
            post_id,
            acc_id,
            type(e).__name__,
        )
        code, message = normalize_error(e, stage="publication")
        await session.execute(text("""
            UPDATE scheduled_posts SET status = 'failed', error = :err
            WHERE id = :id
        """), {"err": f"{code}: {message}", "id": post_id})
        await AutopostStatusService(session).finish_for_post(
            post_id,
            status="failed",
            error_code=code,
            safe_error_message=message,
        )
        return False, f"❌ {message}"
