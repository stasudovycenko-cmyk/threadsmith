"""
Джоба Модуля 4 - neuro_hunter.

Гоняется раз в 20 минут, но для каждого юзера решает вероятностно,
работать ли в этот прогон - чтобы комменты ложились вразнобой по времени,
а не пачкой в :00. Роботизированный ритм = самый палевный признак.

За прогон - максимум 1-2 коммента на юзера. Кэп размазывается по дню,
а не отстреливается утром за полчаса.
"""
import logging
import random
import uuid

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import text

from app.core import neuro
from app.core.ai_cost import (
    AIUsageContext,
    NEURO_MAX_CANDIDATES_PER_RUN,
    NEURO_MAX_LLM_CALLS_PER_RUN,
)
from app.core.config import settings
from app.core.crypto import decrypt_token
from app.core.db import Session
from app.core.llm import LLMGuardError
from app.core.threads_api import create_container, publish_container

log = logging.getLogger("m4_jobs")
_bot = Bot(settings.BOT_TOKEN)


async def neuro_hunter():
    async with Session() as s:
        users = (await s.execute(text("""
            SELECT ns.user_id, ns.mode, ns.daily_cap,
                   un.niche, vp.profile_json,
                   ta.id, ta.threads_user_id, ta.access_token_enc,
                   u.telegram_id
            FROM neuro_settings ns
            JOIN user_niches un ON un.user_id = ns.user_id
            JOIN voice_profiles vp ON vp.user_id = ns.user_id
            JOIN threads_accounts ta
              ON ta.id = ns.threads_account_id
             AND ta.user_id = ns.user_id
             AND ta.expires_at > now()
             AND ta.connection_status = 'connected'
             AND ta.access_token_enc IS NOT NULL
            JOIN users u ON u.id = ns.user_id
            WHERE ns.active
        """))).all()

    for (uid, mode, cap, niche, profile, acc_id, threads_uid,
         tok_enc, tg_id) in users:
        # рандомный пропуск прогона - размазываем активность по дню
        if random.random() > 0.5:
            continue
        try:
            await _hunt_for_user(uid, mode, cap, niche, profile,
                                 threads_uid, tok_enc, tg_id,
                                 acc_id=acc_id)
        except Exception:
            log.exception("neuro_hunter failed uid=%s", uid)


async def _hunt_for_user(uid, mode, cap, niche, profile,
                         threads_uid, tok_enc, tg_id, *,
                         acc_id=None):
    async with Session() as s:
        done_today = await neuro.today_count(s, uid, acc_id)
        if done_today >= cap:
            return
        candidates = await neuro.pick_candidates(
            s,
            uid,
            acc_id,
            niche,
            limit=NEURO_MAX_CANDIDATES_PER_RUN,
        )

    budget = min(2, cap - done_today)  # не больше 2 за прогон
    posted = 0
    llm_calls = 0
    usage_context = AIUsageContext(
        user_id=uid,
        threads_account_id=acc_id,
        run_id=f"neuro:{uuid.uuid4().hex}:{uid}:{acc_id or 'none'}",
    )

    for post_id, author, post_text in candidates:
        if posted >= budget:
            break
        if llm_calls >= NEURO_MAX_LLM_CALLS_PER_RUN:
            log.warning(
                "neuro run cap uid=%s account=%s calls=%s limit=%s",
                uid,
                acc_id,
                llm_calls,
                NEURO_MAX_LLM_CALLS_PER_RUN,
            )
            break
        async with Session() as s:
            if await neuro.author_commented_today(
                s,
                uid,
                acc_id,
                author,
            ):
                continue

        llm_calls += 1
        try:
            result = await neuro.generate_comment(
                profile,
                niche,
                post_text,
                author,
                usage_context=usage_context,
            )
        except LLMGuardError as error:
            log.warning(
                "neuro AI guard stop uid=%s account=%s reason=%s",
                uid,
                acc_id,
                error,
            )
            return
        if not result.get("relevant") or not result.get("comment"):
            log.info("neuro skip uid=%s post=%s: %s",
                     uid, post_id, result.get("skip_reason"))
            continue
        comment = result["comment"]

        async with Session() as s:
            # unique(user_id, target_post_id) закрывает гонку двух прогонов
            ins = (await s.execute(text("""
                INSERT INTO neuro_comments
                    (user_id, threads_account_id, target_post_id,
                     target_author, target_text, comment_text, status)
                VALUES (:uid, :account_id, :pid, :a, :tt, :c, 'pending')
                ON CONFLICT DO NOTHING
                RETURNING id
            """), {
                "uid": uid,
                "account_id": acc_id,
                "pid": post_id,
                "a": author,
                "tt": post_text[:500],
                "c": comment,
            })).first()
            await s.commit()
        if not ins:
            continue
        nc_id = ins[0]
        posted += 1

        if mode == "approve":
            await _send_for_approval(tg_id, nc_id, author, post_text, comment)
        else:
            await _publish_comment(nc_id, threads_uid, tok_enc, post_id,
                                   comment, tg_id, author)


async def _send_for_approval(tg_id, nc_id, author, post_text, comment):
    try:
        await _bot.send_message(
            tg_id,
            f"Нейрокоммент на модерацию.\n\n"
            f"Пост @{author}:\n«{post_text[:200]}...»\n\n"
            f"Коммент:\n«{comment}»",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Постим", callback_data=f"nc:ok:{nc_id}"),
                InlineKeyboardButton(text="❌ Мимо", callback_data=f"nc:no:{nc_id}"),
            ]]),
        )
    except Exception:
        log.exception("approval send failed nc=%s", nc_id)


async def _publish_comment(nc_id, threads_uid, tok_enc, post_id,
                           comment, tg_id, author):
    try:
        token = decrypt_token(tok_enc)
        cont = await create_container(token, threads_uid, comment,
                                      reply_to_id=post_id)
        await publish_container(token, threads_uid, cont)
        async with Session() as s:
            await s.execute(text("""
                UPDATE neuro_comments SET status='posted', posted_at=now()
                WHERE id = :id
            """), {"id": nc_id})
            await s.commit()
        try:
            await _bot.send_message(
                tg_id, f"🤖 Закинул коммент под пост @{author}")
        except Exception:
            pass
    except Exception:
        log.exception("neuro publish failed nc=%s", nc_id)
        async with Session() as s:
            await s.execute(text(
                "UPDATE neuro_comments SET status='failed' WHERE id = :id"
            ), {"id": nc_id})
            await s.commit()
