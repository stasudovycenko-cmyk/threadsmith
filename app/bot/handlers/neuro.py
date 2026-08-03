"""Account-scoped Neurocommenting v2 Telegram UI."""

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import text

from app.core import neuro
from app.core.accounts import ThreadsAccountService, authorization_status
from app.core.db import Session

log = logging.getLogger("neuro_bot")
router = Router()


class SettingValue(StatesGroup):
    cap = State()
    score = State()
    interval = State()


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


def neuro_kb(active: bool, mode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Выключить" if active else "Включить",
            callback_data="nc:toggle",
        )],
        [InlineKeyboardButton(
            text=f"Режим: {'approve' if mode == 'approve' else 'auto'}",
            callback_data="nc:mode",
        )],
        [
            InlineKeyboardButton(text="Daily cap", callback_data="nc:cap"),
            InlineKeyboardButton(text="Minimum score", callback_data="nc:score"),
        ],
        [
            InlineKeyboardButton(text="Интервал", callback_data="nc:interval"),
            InlineKeyboardButton(text="История", callback_data="nc:stats"),
        ],
        [InlineKeyboardButton(text="Главная", callback_data="home")],
    ])


async def _selected(telegram_id: int):
    async with Session() as session:
        service = ThreadsAccountService(session)
        user_id = await service.user_id_for_telegram(telegram_id)
        account = (
            await service.selected_credentials(user_id)
            if user_id is not None
            else None
        )
        if account:
            await service.ensure_settings(user_id, account.id)
        await session.commit()
    return user_id, account


async def _require_selected(cb: CallbackQuery):
    user_id, account = await _selected(cb.from_user.id)
    if account is None:
        await cb.answer("Подключите Threads-аккаунт", show_alert=True)
        return user_id, None
    if authorization_status(account) == "EXPIRED":
        await cb.answer("Авторизация Threads истекла", show_alert=True)
        return user_id, None
    return user_id, account


async def _settings(user_id: int, account_id: int) -> tuple:
    async with Session() as session:
        row = (await session.execute(text("""
            SELECT setting.active, setting.mode, setting.daily_cap,
                   setting.minimum_score, setting.minimum_interval_minutes,
                   (SELECT coalesce(sum(
                      CASE WHEN comment.status IN (
                        'publishing', 'posted', 'unknown'
                      ) AND coalesce(
                        comment.posted_at,
                        comment.publish_claimed_at,
                        comment.created_at
                      )::date = current_date THEN 1 ELSE 0 END
                      + CASE WHEN comment.follow_up_status IN (
                        'publishing', 'posted', 'unknown'
                      ) AND coalesce(
                        comment.follow_up_claimed_at,
                        comment.replied_at,
                        comment.created_at
                      )::date = current_date THEN 1 ELSE 0 END
                    ), 0) FROM neuro_comments comment
                    WHERE comment.user_id = :user_id
                      AND comment.threads_account_id = :account_id
                   ),
                   (SELECT max(greatest(
                       coalesce(
                         comment.posted_at,
                         comment.publish_claimed_at,
                         comment.created_at
                       ),
                       coalesce(
                         comment.follow_up_claimed_at,
                         '-infinity'::timestamptz
                       )
                    )) + make_interval(mins => setting.minimum_interval_minutes)
                    FROM neuro_comments comment
                    WHERE comment.user_id = :user_id
                      AND comment.threads_account_id = :account_id
                      AND (
                        comment.status IN ('publishing', 'posted', 'unknown')
                        OR comment.follow_up_status IN (
                          'publishing', 'posted', 'unknown'
                        )
                      ))
            FROM neuro_settings setting
            WHERE setting.user_id = :user_id
              AND setting.threads_account_id = :account_id
        """), {"user_id": user_id, "account_id": account_id})).first()
    return row


@router.callback_query(F.data == "nc:menu")
async def cb_menu(cb: CallbackQuery):
    user_id, account = await _require_selected(cb)
    if account is None:
        return
    async with Session() as session:
        ready = (await session.execute(text("""
            SELECT
              exists(SELECT 1 FROM voice_profiles WHERE user_id = :user_id),
              exists(
                SELECT 1 FROM radar_settings
                WHERE user_id = :user_id AND threads_account_id = :account_id
                  AND cardinality(keywords) > 0
              ),
              coalesce((SELECT plan FROM subscriptions
                        WHERE user_id = :user_id), 'free')
        """), {"user_id": user_id, "account_id": account.id})).first()
    has_voice, has_keywords, plan = ready
    if plan == "free":
        await cb.message.answer("Neurocommenting доступен на платных тарифах.")
        await cb.answer()
        return
    missing = []
    if not has_voice:
        missing.append("voice profile")
    if not has_keywords:
        missing.append("Radar keywords для выбранного аккаунта")
    if missing:
        await cb.message.answer("Сначала настройте: " + ", ".join(missing))
        await cb.answer()
        return
    active, mode, cap, score, interval, used, next_at = await _settings(
        user_id, account.id
    )
    await cb.message.answer(
        f"Neurocommenting для @{account.username or account.id}\n"
        f"Статус: {'включён' if active else 'выключен'}\n"
        f"Режим: {mode}\n"
        f"Daily cap: {cap}; использовано сегодня: {used}\n"
        f"Minimum score: {score}\n"
        f"Минимальный интервал: {interval} мин.\n"
        f"Следующий допустимый комментарий: "
        f"{next_at.strftime('%d.%m %H:%M') if next_at else '-'}",
        reply_markup=neuro_kb(active, mode),
    )
    await cb.answer()


@router.callback_query(F.data == "nc:toggle")
async def cb_toggle(cb: CallbackQuery):
    user_id, account = await _require_selected(cb)
    if account is None:
        return
    async with Session() as session:
        row = (await session.execute(text("""
            UPDATE neuro_settings SET active = NOT active
            WHERE user_id = :user_id AND threads_account_id = :account_id
            RETURNING active, mode
        """), {"user_id": user_id, "account_id": account.id})).first()
        await session.commit()
    if not row:
        await cb.answer("Настройки не найдены", show_alert=True)
        return
    await cb.message.edit_reply_markup(reply_markup=neuro_kb(row[0], row[1]))
    await cb.answer("Включено" if row[0] else "Выключено")


@router.callback_query(F.data == "nc:mode")
async def cb_mode(cb: CallbackQuery):
    user_id, account = await _require_selected(cb)
    if account is None:
        return
    async with Session() as session:
        row = (await session.execute(text("""
            UPDATE neuro_settings
            SET mode = CASE WHEN mode = 'approve' THEN 'auto' ELSE 'approve' END
            WHERE user_id = :user_id AND threads_account_id = :account_id
            RETURNING active, mode
        """), {"user_id": user_id, "account_id": account.id})).first()
        await session.commit()
    if not row:
        await cb.answer("Настройки не найдены", show_alert=True)
        return
    await cb.message.edit_reply_markup(reply_markup=neuro_kb(row[0], row[1]))
    if row[1] == "auto":
        await cb.message.answer(
            "Auto публикует без предпросмотра только после score, cap и interval checks."
        )
    await cb.answer(f"Режим: {row[1]}")


async def _begin_setting(
    cb: CallbackQuery,
    state: FSMContext,
    target_state: State,
    prompt: str,
) -> None:
    _, account = await _require_selected(cb)
    if account is None:
        return
    await state.set_state(target_state)
    await state.update_data(account_id=account.id)
    await cb.message.answer(prompt)
    await cb.answer()


@router.callback_query(F.data == "nc:cap")
async def cb_cap(cb: CallbackQuery, state: FSMContext):
    await _begin_setting(cb, state, SettingValue.cap, "Daily cap: число 1-30")


@router.callback_query(F.data == "nc:score")
async def cb_score(cb: CallbackQuery, state: FSMContext):
    await _begin_setting(cb, state, SettingValue.score, "Minimum score: число 0-100")


@router.callback_query(F.data == "nc:interval")
async def cb_interval(cb: CallbackQuery, state: FSMContext):
    await _begin_setting(cb, state, SettingValue.interval, "Интервал в минутах: 5-1440")


async def _save_numeric_setting(
    msg: Message,
    state: FSMContext,
    *,
    column: str,
    minimum: int,
    maximum: int,
) -> None:
    try:
        value = int((msg.text or "").strip())
    except ValueError:
        await msg.answer(f"Введите число {minimum}-{maximum}")
        return
    if not minimum <= value <= maximum:
        await msg.answer(f"Допустимый диапазон: {minimum}-{maximum}")
        return
    data = await state.get_data()
    await state.clear()
    user_id, account = await _selected(msg.from_user.id)
    if account is None or account.id != data.get("account_id"):
        await msg.answer("Выбранный аккаунт изменился. Откройте настройки заново.")
        return
    allowed_columns = {
        "daily_cap", "minimum_score", "minimum_interval_minutes"
    }
    if column not in allowed_columns:
        raise ValueError("unsupported settings column")
    async with Session() as session:
        await session.execute(text(f"""
            UPDATE neuro_settings SET {column} = :value
            WHERE user_id = :user_id AND threads_account_id = :account_id
        """), {
            "value": value, "user_id": user_id, "account_id": account.id,
        })
        await session.commit()
    await msg.answer(f"Сохранено: {value}")


@router.message(SettingValue.cap)
async def cap_value(msg: Message, state: FSMContext):
    await _save_numeric_setting(
        msg, state, column="daily_cap", minimum=1, maximum=30
    )


@router.message(SettingValue.score)
async def score_value(msg: Message, state: FSMContext):
    await _save_numeric_setting(
        msg, state, column="minimum_score", minimum=0, maximum=100
    )


@router.message(SettingValue.interval)
async def interval_value(msg: Message, state: FSMContext):
    await _save_numeric_setting(
        msg, state, column="minimum_interval_minutes", minimum=5, maximum=1440
    )


@router.callback_query(F.data == "nc:stats")
async def cb_stats(cb: CallbackQuery):
    user_id, account = await _require_selected(cb)
    if account is None:
        return
    async with Session() as session:
        rows = (await session.execute(text("""
            SELECT target_author, status, score, strategy, created_at,
                   publish_error_code, author_replied
            FROM neuro_comments
            WHERE user_id = :user_id AND threads_account_id = :account_id
            ORDER BY created_at DESC LIMIT 10
        """), {"user_id": user_id, "account_id": account.id})).all()
    if not rows:
        await cb.message.answer("История пока пуста.")
    else:
        lines = [f"История @{account.username or account.id}:"]
        for author, status, score, strategy, created_at, error, replied in rows:
            lines.append(
                f"{created_at:%d.%m %H:%M} @{author or 'unknown'}: "
                f"{status}, score={score or '-'}, strategy={strategy or '-'}"
                f"{f', {error}' if error else ''}"
                f"{', есть ответ' if replied else ''}"
            )
        await cb.message.answer("\n".join(lines))
    await cb.answer()


@router.callback_query(F.data.startswith("nc:ok:"))
async def cb_approve(cb: CallbackQuery):
    comment_id = int(cb.data.rsplit(":", 1)[1])
    user_id, account = await _require_selected(cb)
    if account is None:
        return
    async with Session() as session:
        claim = await neuro.claim_comment_for_publish(
            session,
            user_id=user_id,
            account_id=account.id,
            comment_id=comment_id,
            require_auto=False,
        )
        await session.commit()
        if claim is None:
            await cb.answer(
                "Уже обработан, достигнут лимит или выбран другой аккаунт",
                show_alert=True,
            )
            return
        await cb.answer("Публикую...")
        result = await neuro.publish_claimed_comment(session, claim)
    messages = {
        "posted": "Опубликован",
        "unknown": "Результат неизвестен. Проверьте Threads вручную; автоповтора не будет.",
        "permission_denied": "Threads отклонил разрешение. Проверьте App Review.",
        "failed": "Публикация не завершена.",
    }
    await cb.message.edit_text(cb.message.text + f"\n\n{messages[result]}")


async def _regenerate(
    cb: CallbackQuery,
    comment_id: int,
    strategy: str | None = None,
) -> None:
    user_id, account = await _require_selected(cb)
    if account is None:
        return
    if strategy is not None and strategy not in neuro.COMMENT_STRATEGIES:
        await cb.answer("Неизвестная стратегия", show_alert=True)
        return
    async with Session() as session:
        claimed = await neuro.claim_variant(
            session,
            user_id=user_id,
            account_id=account.id,
            comment_id=comment_id,
        )
        await session.commit()
    if not claimed:
        await cb.answer("Уже обработан или выбран другой аккаунт", show_alert=True)
        return
    await cb.answer("Готовлю новый вариант...")
    async with Session() as session:
        generated = await neuro.generate_claimed_comment(
            session,
            user_id=user_id,
            account_id=account.id,
            comment_id=comment_id,
            requested_strategy=strategy,
        )
        await session.commit()
    if not generated:
        await cb.message.edit_text(cb.message.text + "\n\nНовый вариант не создан.")
        return
    prefix = cb.message.text.split("Предложенный комментарий:", 1)[0]
    await cb.message.edit_text(
        prefix
        + "Предложенный комментарий:\n"
        + generated["comment"]
        + f"\n\nСтратегия: {generated['strategy']}",
        reply_markup=approval_keyboard(comment_id),
    )


@router.callback_query(F.data.startswith("nc:variant:"))
async def cb_variant(cb: CallbackQuery):
    await _regenerate(cb, int(cb.data.rsplit(":", 1)[1]))


@router.callback_query(F.data.startswith("nc:strategy:"))
async def cb_strategy(cb: CallbackQuery):
    comment_id = int(cb.data.rsplit(":", 1)[1])
    _, account = await _require_selected(cb)
    if account is None:
        return
    buttons = [
        [InlineKeyboardButton(
            text=strategy.replace("_", " "),
            callback_data=f"nc:use:{comment_id}:{strategy}",
        )]
        for strategy in neuro.COMMENT_STRATEGIES
    ]
    await cb.message.answer(
        "Выберите стратегию:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("nc:use:"))
async def cb_use_strategy(cb: CallbackQuery):
    _, _, comment_value, strategy = cb.data.split(":", 3)
    await _regenerate(cb, int(comment_value), strategy)


@router.callback_query(F.data.startswith("nc:no:"))
async def cb_reject(cb: CallbackQuery):
    comment_id = int(cb.data.rsplit(":", 1)[1])
    user_id, account = await _require_selected(cb)
    if account is None:
        return
    async with Session() as session:
        changed = await neuro.reject_comment(
            session,
            user_id=user_id,
            account_id=account.id,
            comment_id=comment_id,
        )
        await session.commit()
    if changed:
        await cb.message.edit_text(cb.message.text + "\n\nОтклонён")
        await cb.answer()
    else:
        await cb.answer("Уже обработан", show_alert=True)


@router.callback_query(F.data.startswith("nc:hide:"))
async def cb_hide_author(cb: CallbackQuery):
    comment_id = int(cb.data.rsplit(":", 1)[1])
    user_id, account = await _require_selected(cb)
    if account is None:
        return
    async with Session() as session:
        changed = await neuro.exclude_comment_author(
            session,
            user_id=user_id,
            account_id=account.id,
            comment_id=comment_id,
        )
        await session.commit()
    if changed:
        await cb.message.edit_text(cb.message.text + "\n\nАвтор исключён")
        await cb.answer()
    else:
        await cb.answer("Комментарий не принадлежит выбранному аккаунту", show_alert=True)


@router.callback_query(F.data.startswith("nc:follow:"))
async def cb_follow(cb: CallbackQuery):
    comment_id = int(cb.data.rsplit(":", 1)[1])
    user_id, account = await _require_selected(cb)
    if account is None:
        return
    await cb.answer("Готовлю ответ...")
    async with Session() as session:
        follow_up = await neuro.generate_follow_up(
            session,
            user_id=user_id,
            account_id=account.id,
            comment_id=comment_id,
        )
        await session.commit()
    if not follow_up:
        await cb.message.answer("Ответ уже обработан или не удалось создать вариант.")
        return
    await cb.message.answer(
        f"Предложенный ответ:\n{follow_up}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="Опубликовать", callback_data=f"nc:fok:{comment_id}"
            ),
            InlineKeyboardButton(
                text="Пропустить", callback_data=f"nc:fno:{comment_id}"
            ),
        ]]),
    )


@router.callback_query(F.data.startswith("nc:fok:"))
async def cb_follow_publish(cb: CallbackQuery):
    comment_id = int(cb.data.rsplit(":", 1)[1])
    user_id, account = await _require_selected(cb)
    if account is None:
        return
    async with Session() as session:
        claim = await neuro.claim_follow_up_publish(
            session,
            user_id=user_id,
            account_id=account.id,
            comment_id=comment_id,
        )
        await session.commit()
        if not claim:
            await cb.answer("Уже обработан или достигнут лимит", show_alert=True)
            return
        result = await neuro.publish_follow_up(session, claim)
    await cb.message.edit_text(cb.message.text + f"\n\nFollow-up: {result}")
    await cb.answer()


@router.callback_query(F.data.startswith("nc:fno:"))
async def cb_follow_reject(cb: CallbackQuery):
    comment_id = int(cb.data.rsplit(":", 1)[1])
    user_id, account = await _require_selected(cb)
    if account is None:
        return
    async with Session() as session:
        result = await session.execute(text("""
            UPDATE neuro_comments SET follow_up_status = 'rejected'
            WHERE id = :comment_id AND user_id = :user_id
              AND threads_account_id = :account_id
              AND follow_up_status = 'pending' AND follow_up_count = 0
        """), {
            "comment_id": comment_id, "user_id": user_id,
            "account_id": account.id,
        })
        await session.commit()
    await cb.message.edit_text(cb.message.text + "\n\nFollow-up отклонён")
    await cb.answer()
