"""Account-scoped Neurocommenting v2 worker jobs."""

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import text

from app.core import neuro
from app.core.ai_cost import (
    NEURO_MAX_CANDIDATES_PER_RUN,
    NEURO_MAX_LLM_CALLS_PER_RUN,
)
from app.core.config import settings
from app.core.crypto import decrypt_token
from app.core.db import Session

log = logging.getLogger("m4_jobs")
_bot = Bot(settings.BOT_TOKEN)


def approval_keyboard(comment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Опубликовать", callback_data=f"nc:ok:{comment_id}"
            ),
            InlineKeyboardButton(
                text="Другой вариант", callback_data=f"nc:variant:{comment_id}"
            ),
        ],
        [InlineKeyboardButton(
            text="Сменить стратегию", callback_data=f"nc:strategy:{comment_id}"
        )],
        [
            InlineKeyboardButton(
                text="Пропустить", callback_data=f"nc:no:{comment_id}"
            ),
            InlineKeyboardButton(
                text="Не показывать автора", callback_data=f"nc:hide:{comment_id}"
            ),
        ],
        [InlineKeyboardButton(text="Назад", callback_data="nc:menu")],
    ])


async def neuro_hunter() -> None:
    async with Session() as session:
        recovered = await neuro.recover_stale_claims(session)
        await session.commit()
        if any(recovered.values()):
            log.warning("neuro stale claims recovered result=%s", recovered)
        accounts = (await session.execute(text("""
            SELECT setting.user_id, setting.mode, setting.daily_cap,
                   account.id, account.threads_user_id,
                   account.access_token_enc, user_row.telegram_id
            FROM neuro_settings setting
            JOIN radar_settings radar
              ON radar.threads_account_id = setting.threads_account_id
             AND radar.user_id = setting.user_id
             AND cardinality(radar.keywords) > 0
            JOIN threads_accounts account
              ON account.id = setting.threads_account_id
             AND account.user_id = setting.user_id
             AND account.connection_status = 'connected'
             AND account.access_token_enc IS NOT NULL
             AND account.expires_at > now()
            JOIN users user_row ON user_row.id = setting.user_id
            WHERE setting.active
            ORDER BY setting.threads_account_id
        """))).all()

    for user_id, mode, cap, account_id, threads_uid, token_enc, telegram_id in accounts:
        try:
            await _hunt_for_user(
                user_id,
                mode,
                cap,
                "",
                {},
                threads_uid,
                token_enc,
                telegram_id,
                acc_id=account_id,
            )
        except Exception:
            log.exception(
                "neuro account run failed user=%s account=%s",
                user_id,
                account_id,
            )


async def _hunt_for_user(
    uid,
    mode,
    cap,
    niche,
    profile,
    threads_uid,
    tok_enc,
    tg_id,
    *,
    acc_id=None,
):
    """Compatibility signature; v2 reads all mutable data from the account DB rows."""
    if acc_id is None:
        return
    async with Session() as session:
        claimed = await neuro.claim_candidate_for_generation(
            session, user_id=uid, account_id=acc_id
        )
        await session.commit()
    if not claimed:
        return

    comment_id = int(claimed["comment_id"])
    async with Session() as session:
        generated = await neuro.generate_claimed_comment(
            session,
            user_id=uid,
            account_id=acc_id,
            comment_id=comment_id,
        )
        await session.commit()
    if not generated:
        return

    if mode == "approve":
        await _send_for_approval(
            tg_id,
            comment_id,
            claimed.get("author_username"),
            claimed["post_text"],
            generated["comment"],
            int(claimed.get("final_score") or 0),
            str(claimed.get("score_reason") or ""),
            generated["strategy"],
        )
        return

    async with Session() as session:
        publish_claim = await neuro.claim_comment_for_publish(
            session,
            user_id=uid,
            account_id=acc_id,
            comment_id=comment_id,
            require_auto=True,
        )
        await session.commit()
        if not publish_claim:
            return
        result = await neuro.publish_claimed_comment(session, publish_claim)
    if result in ("posted", "unknown", "permission_denied"):
        await _notify_publish_result(
            tg_id,
            claimed.get("author_username"),
            result,
        )


async def _send_for_approval(
    telegram_id: int,
    comment_id: int,
    author: str | None,
    post_text: str,
    comment: str,
    score: int,
    score_reason: str,
    strategy: str,
) -> None:
    try:
        preview = post_text[:260] + ("..." if len(post_text) > 260 else "")
        await _bot.send_message(
            telegram_id,
            f"Автор: @{author or 'unknown'}\n"
            f"Оценка: {score}/100\n"
            f"Почему выбран: {score_reason[:240]}\n"
            f"Стратегия: {strategy}\n\n"
            f"Пост:\n{preview}\n\n"
            f"Предложенный комментарий:\n{comment}",
            reply_markup=approval_keyboard(comment_id),
        )
    except Exception as error:
        log.warning(
            "approval notification failed comment=%s error_type=%s",
            comment_id,
            type(error).__name__,
        )


async def _notify_publish_result(
    telegram_id: int,
    author: str | None,
    result: str,
) -> None:
    messages = {
        "posted": f"Комментарий для @{author or 'автора'} опубликован.",
        "unknown": (
            "Threads мог принять комментарий, но ответ API потерян. "
            "Автоповтор отключён; проверьте публикацию вручную."
        ),
        "permission_denied": (
            "Threads отклонил публикацию по разрешениям. "
            "Проверьте App Review и переподключите аккаунт."
        ),
    }
    try:
        await _bot.send_message(telegram_id, messages[result])
    except Exception:
        pass


async def neuro_reply_poller() -> None:
    await _publish_pending_auto_follow_ups()
    async with Session() as session:
        accounts = (await session.execute(text("""
            SELECT DISTINCT comment.user_id, comment.threads_account_id,
                   account.access_token_enc, account.username,
                   user_row.telegram_id, setting.mode,
                   setting.auto_follow_up
            FROM neuro_comments comment
            JOIN threads_accounts account
              ON account.id = comment.threads_account_id
             AND account.user_id = comment.user_id
             AND account.connection_status = 'connected'
             AND account.access_token_enc IS NOT NULL
             AND account.expires_at > now()
            JOIN users user_row ON user_row.id = comment.user_id
            JOIN neuro_settings setting
              ON setting.threads_account_id = comment.threads_account_id
             AND setting.user_id = comment.user_id
            WHERE comment.status = 'posted'
              AND comment.author_replied = false
              AND comment.reply_poll_status <> 'permission_denied'
              AND comment.published_threads_id IS NOT NULL
              AND (comment.reply_checked_at IS NULL
                   OR comment.reply_checked_at < now() - interval '30 minutes')
        """))).all()

    for (user_id, account_id, token_enc, username, telegram_id,
         mode, auto_follow_up) in accounts:
        try:
            async with Session() as session:
                replies = await neuro.poll_account_replies(
                    session,
                    user_id=user_id,
                    account_id=account_id,
                    token=decrypt_token(token_enc),
                    own_username=username,
                )
                await session.commit()
            for reply in replies:
                if mode == "auto" and auto_follow_up:
                    async with Session() as session:
                        generated = await neuro.generate_follow_up(
                            session,
                            user_id=user_id,
                            account_id=account_id,
                            comment_id=reply["comment_id"],
                        )
                        await session.commit()
                    if generated:
                        async with Session() as session:
                            claim = await neuro.claim_follow_up_publish(
                                session,
                                user_id=user_id,
                                account_id=account_id,
                                comment_id=reply["comment_id"],
                                require_auto=True,
                            )
                            await session.commit()
                            if claim:
                                await neuro.publish_follow_up(session, claim)
                    continue
                await _bot.send_message(
                    telegram_id,
                    f"@{reply.get('username') or 'Автор'} ответил:\n"
                    f"{reply.get('reply_text', '')[:500]}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(
                            text="Подготовить ответ",
                            callback_data=f"nc:follow:{reply['comment_id']}",
                        )
                    ]]),
                )
        except Exception:
            log.exception("reply poll failed account=%s", account_id)
    await _publish_pending_auto_follow_ups()


async def _publish_pending_auto_follow_ups() -> None:
    async with Session() as session:
        rows = (await session.execute(text("""
            SELECT DISTINCT ON (comment.threads_account_id)
                   comment.user_id, comment.threads_account_id, comment.id
            FROM neuro_comments comment
            JOIN neuro_settings setting
              ON setting.threads_account_id = comment.threads_account_id
             AND setting.user_id = comment.user_id
             AND setting.active AND setting.mode = 'auto'
             AND setting.auto_follow_up
            WHERE comment.status = 'posted'
              AND comment.follow_up_status = 'pending'
              AND comment.follow_up_count = 0
            ORDER BY comment.threads_account_id, comment.replied_at
        """))).all()
    for user_id, account_id, comment_id in rows:
        async with Session() as session:
            claim = await neuro.claim_follow_up_publish(
                session,
                user_id=user_id,
                account_id=account_id,
                comment_id=comment_id,
                require_auto=True,
            )
            await session.commit()
            if claim:
                await neuro.publish_follow_up(session, claim)


__all__ = [
    "NEURO_MAX_CANDIDATES_PER_RUN",
    "NEURO_MAX_LLM_CALLS_PER_RUN",
    "approval_keyboard",
    "neuro_hunter",
    "neuro_reply_poller",
]
