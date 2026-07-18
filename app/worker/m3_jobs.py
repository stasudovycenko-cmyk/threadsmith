"""
Джобы Модуля 3.

publisher - раз в минуту забирает scheduled_posts с наступившим run_at.

comment_poller - тянет комменты к постам юзеров и отвечает по кодовым
словам. Частота зависит от возраста поста (тиры):
  tier 0: пост < 24ч   -> поллим каждый прогон (5 мин)
  tier 1: пост < 3 дня -> раз в 30 мин
  tier 2: пост < 7 дней -> раз в 3 часа
  tier 3: старше недели -> не поллим
Без тиров десяток активных юзеров = сотни запросов каждые 5 минут,
привет рейт-лимиты Meta.

Дедуп автоответов - через PK replies_log.comment_id: INSERT ... ON CONFLICT
DO NOTHING RETURNING. Вставилось - отвечаем, нет - уже отвечали. Гонка
двух воркеров закрыта на уровне базы, не кода.
"""
import logging

from aiogram import Bot
from sqlalchemy import text

from app.core import autopilot
from app.core.config import settings
from app.core.crypto import decrypt_token
from app.core.db import Session
from app.core.threads_api import (create_container, get_replies,
                                  publish_container)

log = logging.getLogger("m3_jobs")
_bot = Bot(settings.BOT_TOKEN)


async def publisher():
    async with Session() as s:
        posts = await autopilot.claim_due_posts(s)
        await s.commit()

    for row in posts:
        async with Session() as s:
            ok, note = await autopilot.publish_one(s, row)
            tg = (await s.execute(text(
                "SELECT telegram_id FROM users WHERE id = :uid"
            ), {"uid": row[1]})).first()
            await s.commit()
        if tg:
            try:
                await _bot.send_message(tg[0], note)
            except Exception:
                pass


async def comment_poller():
    async with Session() as s:
        # выбираем посты, которым пора поллиться по тиру + апдейтим тиры
        await s.execute(text("""
            UPDATE poll_state ps SET tier =
              CASE
                WHEN sp.run_at > now() - interval '24 hours' THEN 0
                WHEN sp.run_at > now() - interval '3 days'  THEN 1
                WHEN sp.run_at > now() - interval '7 days'  THEN 2
                ELSE 3
              END
            FROM scheduled_posts sp
            WHERE sp.threads_post_id = ps.threads_post_id
        """))
        due = (await s.execute(text("""
            SELECT ps.threads_post_id, ps.threads_account_id
            FROM poll_state ps
            WHERE (ps.tier = 0 AND (ps.last_polled_at IS NULL OR ps.last_polled_at < now() - interval '5 minutes'))
               OR (ps.tier = 1 AND (ps.last_polled_at IS NULL OR ps.last_polled_at < now() - interval '30 minutes'))
               OR (ps.tier = 2 AND (ps.last_polled_at IS NULL OR ps.last_polled_at < now() - interval '3 hours'))
            LIMIT 50
        """))).all()
        await s.commit()

    for post_id, acc_id in due:
        try:
            await _poll_post(post_id, acc_id)
        except Exception:
            log.exception("poll failed post=%s", post_id)


async def _poll_post(post_id: str, acc_id: int):
    async with Session() as s:
        acc = (await s.execute(text("""
            SELECT ta.threads_user_id, ta.access_token_enc, ta.user_id
            FROM threads_accounts ta WHERE ta.id = :acc
        """), {"acc": acc_id})).first()
        if not acc:
            return
        threads_uid, tok_enc, user_id = acc
        rules = (await s.execute(text("""
            SELECT keyword, response_text FROM reply_rules
            WHERE user_id = :uid AND active
        """), {"uid": user_id})).all()
        await s.execute(text(
            "UPDATE poll_state SET last_polled_at = now() WHERE threads_post_id = :p"
        ), {"p": post_id})
        await s.commit()

    if not rules:
        return

    token = decrypt_token(tok_enc)
    comments = await get_replies(token, post_id)

    for c in comments:
        ctext = (c.get("text") or "").lower()
        matched = next((kw for kw, _ in rules if kw.lower() in ctext), None)
        if not matched:
            continue
        response = next(r for kw, r in rules if kw == matched)

        async with Session() as s:
            # дедуп на PK: вставилось - наш коммент, отвечаем
            ins = (await s.execute(text("""
                INSERT INTO replies_log (comment_id, threads_post_id, matched_keyword)
                VALUES (:cid, :pid, :kw)
                ON CONFLICT (comment_id) DO NOTHING
                RETURNING comment_id
            """), {"cid": c["id"], "pid": post_id, "kw": matched})).first()
            await s.commit()
        if not ins:
            continue

        try:
            cont = await create_container(token, threads_uid, response,
                                          reply_to_id=c["id"])
            await publish_container(token, threads_uid, cont)
            log.info("auto-replied comment=%s kw=%s", c["id"], matched)
        except Exception:
            log.exception("auto-reply failed comment=%s", c["id"])
            # откатываем дедуп, чтобы попробовать в следующий прогон
            async with Session() as s:
                await s.execute(text(
                    "DELETE FROM replies_log WHERE comment_id = :cid"
                ), {"cid": c["id"]})
                await s.commit()
