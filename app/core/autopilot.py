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
from urllib.parse import urlencode, urlparse

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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
        UPDATE scheduled_posts SET status = 'publishing'
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

    if await daily_count(session, acc_id) >= DAILY_POST_LIMIT:
        await session.execute(text("""
            UPDATE scheduled_posts
            SET status = 'pending', run_at = run_at + interval '1 hour'
            WHERE id = :id
        """), {"id": post_id})
        return False, "Упёрлись в лимит 250 постов/сутки, пост сдвинут на час."

    acc = (await session.execute(text("""
        SELECT threads_user_id, access_token_enc FROM threads_accounts
        WHERE id = :acc
    """), {"acc": acc_id})).first()
    if not acc:
        await session.execute(text(
            "UPDATE scheduled_posts SET status='failed', error='no account' WHERE id=:id"
        ), {"id": post_id})
        return False, "Threads-аккаунт отвязан, пост не ушёл."

    threads_uid, tok_enc = acc
    token = decrypt_token(tok_enc)

    try:
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
            except Exception:
                log.exception("first-comment link failed post=%s", post_id)
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

        return True, f"✅ Пост опубликован.{first_comment_note}"
    except Exception as e:
        log.exception("publish failed post=%s", post_id)
        await session.execute(text("""
            UPDATE scheduled_posts SET status = 'failed', error = :err
            WHERE id = :id
        """), {"err": str(e)[:500], "id": post_id})
        return False, "❌ Публикация упала. Пост в очереди помечен failed."
