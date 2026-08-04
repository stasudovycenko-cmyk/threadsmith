"""Account-scoped Neurocommenting v2 Telegram UI."""

import logging

from aiogram import F, Router
from aiogram.filters import Command
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
from app.bot.ux import format_local_time, navigation_row, render_error

log = logging.getLogger("neuro_bot")
router = Router()

STATUS_LABELS = {
    "pending": "Ожидает подтверждения",
    "generating": "Готовится",
    "publishing": "Публикуется",
    "posted": "Опубликован",
    "rejected": "Отклонён",
    "skipped": "Пропущен",
    "unknown": "Результат не подтверждён",
    "failed": "Ошибка публикации",
}
STRATEGY_LABELS = {
    "useful_addition": "Полезное дополнение",
    "personal_observation": "Личное наблюдение",
    "clarifying_question": "Уточняющий вопрос",
    "gentle_disagreement": "Мягкое несогласие",
    "short_insight": "Короткий вывод",
    "specific_support": "Конкретная поддержка",
    "mini_story": "Мини-история",
    "professional_opinion": "Профессиональное мнение",
}


def _strategy_label(value: str | None) -> str:
    return STRATEGY_LABELS.get(value or "", "Не указана")


def _publish_result_label(value: str) -> str:
    return {
        "posted": "Опубликовано",
        "unknown": (
            "Результат не подтверждён. Проверьте Threads вручную; "
            "автоматического повтора не будет."
        ),
        "permission_denied": (
            "Недостаточно разрешений Threads. Переподключите аккаунт."
        ),
        "failed": "Публикация не завершена.",
    }.get(value, "Результат не подтверждён. Проверьте Threads вручную.")


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
        navigation_row("nc:menu"),
    ])


def neuro_kb(active: bool, mode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="⏸ Выключить" if active else "▶️ Включить",
            callback_data=f"nc:set_active:{0 if active else 1}",
        )],
        [InlineKeyboardButton(
            text=(
                "Режим: сначала спрашивать"
                if mode == "approve"
                else "Режим: публиковать автоматически"
            ),
            callback_data="nc:mode",
        )],
        [
            InlineKeyboardButton(text="📋 Ждут подтверждения", callback_data="nc:pending"),
            InlineKeyboardButton(text="🕘 История", callback_data="nc:stats"),
        ],
        [
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="nc:settings"),
            InlineKeyboardButton(text="🎭 Стратегии", callback_data="nc:strategies"),
        ],
        [InlineKeyboardButton(text="ℹ️ Как это работает", callback_data="help:neuro")],
        navigation_row("home"),
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
                   setting.auto_follow_up,
                   coalesce(content.timezone, 'Europe/Moscow'),
                   (SELECT coalesce(sum(
                      CASE WHEN comment.status IN (
                        'publishing', 'posted', 'unknown'
                      ) AND (
                        coalesce(
                          comment.posted_at,
                          comment.publish_claimed_at,
                          comment.created_at
                        ) AT TIME ZONE coalesce(
                          content.timezone, 'Europe/Moscow'
                        )
                      )::date = (
                        now() AT TIME ZONE coalesce(
                          content.timezone, 'Europe/Moscow'
                        )
                      )::date THEN 1 ELSE 0 END
                      + CASE WHEN comment.follow_up_status IN (
                        'publishing', 'posted', 'unknown'
                      ) AND (
                        coalesce(
                          comment.follow_up_claimed_at,
                          comment.replied_at,
                          comment.created_at
                        ) AT TIME ZONE coalesce(
                          content.timezone, 'Europe/Moscow'
                        )
                      )::date = (
                        now() AT TIME ZONE coalesce(
                          content.timezone, 'Europe/Moscow'
                        )
                      )::date THEN 1 ELSE 0 END
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
                   ,
                   (SELECT count(*) FROM neuro_comments pending
                    WHERE pending.user_id = :user_id
                      AND pending.threads_account_id = :account_id
                      AND pending.status = 'pending')
            FROM neuro_settings setting
            LEFT JOIN autocontent_settings content
              ON content.user_id = setting.user_id
             AND content.threads_account_id = setting.threads_account_id
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
        missing.append("профиль голоса")
    if not has_keywords:
        missing.append("ключевые слова Radar")
    if missing:
        await cb.message.answer("Сначала настройте: " + ", ".join(missing))
        await cb.answer()
        return
    (
        active, mode, cap, score, interval, auto_follow_up,
        timezone_name, used, next_at, pending_count,
    ) = await _settings(
        user_id, account.id
    )
    mode_warning = (
        "\n\n⚠️ Автоматический режим публикует без ручного подтверждения."
        if mode == "auto"
        else ""
    )
    await cb.message.answer(
        f"🧠 Neuro для @{account.username or account.id}\n\n"
        "Готовит комментарии к найденным постам. В безопасном режиме "
        "сначала показывает текст вам.\n\n"
        f"Статус: {'🟢 Включён' if active else '⚪ Выключен'}\n"
        "Режим: " + (
            "Сначала спрашивать меня" if mode == "approve"
            else "Публиковать автоматически"
        ) + "\n"
        f"Сегодня опубликовано: {used}\n"
        f"Ждут подтверждения: {pending_count}\n"
        f"Осталось по лимиту: {max(0, cap - used)}\n"
        f"Следующий комментарий: {format_local_time(next_at, timezone_name) if next_at else 'можно публиковать'}\n\n"
        f"Минимальная оценка: {score}\n"
        f"Пауза между действиями: {interval} мин.\n"
        f"Ответ после реакции автора: {'включён' if auto_follow_up else 'выключен'}"
        f"{mode_warning}",
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


@router.callback_query(F.data.startswith("nc:set_active:"))
async def cb_set_active(cb: CallbackQuery):
    desired = cb.data.rsplit(":", 1)[-1] == "1"
    user_id, account = await _require_selected(cb)
    if account is None:
        return
    async with Session() as session:
        row = (await session.execute(text("""
            UPDATE neuro_settings
            SET active = :active
            WHERE user_id = :user_id
              AND threads_account_id = :account_id
              AND active IS DISTINCT FROM :active
            RETURNING active, mode
        """), {
            "user_id": user_id,
            "account_id": account.id,
            "active": desired,
        })).first()
        await session.commit()
    if row is None:
        await cb.answer("Уже выполнено", show_alert=True)
        return
    await cb.message.answer(
        f"Neuro {'включён' if desired else 'выключен'} для "
        f"@{account.username or account.id}.",
        reply_markup=neuro_kb(row[0], row[1]),
    )
    await cb.answer("Сохранено")


@router.callback_query(F.data == "nc:mode")
async def cb_mode(cb: CallbackQuery):
    user_id, account = await _require_selected(cb)
    if account is None:
        return
    async with Session() as session:
        row = (await session.execute(text("""
            SELECT active, mode FROM neuro_settings
            WHERE user_id = :user_id AND threads_account_id = :account_id
        """), {"user_id": user_id, "account_id": account.id})).first()
    if not row:
        await cb.answer("Настройки не найдены", show_alert=True)
        return
    if row[1] == "approve":
        await cb.message.answer(
            f"⚠️ Автоматический режим для @{account.username or account.id}\n\n"
            "Комментарии будут публиковаться без ручного подтверждения.\n\n"
            "Продолжат действовать лимит в день, минимальная оценка и пауза "
            "между действиями.\n\n"
            "Рекомендуем сначала проверить качество в режиме «Сначала спрашивать».",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="Подтверждаю, публиковать автоматически",
                    callback_data="nc:set_mode:auto",
                )],
                navigation_row("nc:menu"),
            ]),
        )
        await cb.answer()
        return
    await _set_mode(cb, user_id, account.id, "approve")


async def _set_mode(
    cb: CallbackQuery,
    user_id: int,
    account_id: int,
    mode: str,
) -> None:
    async with Session() as session:
        row = (await session.execute(text("""
            UPDATE neuro_settings
            SET mode = :mode
            WHERE user_id = :user_id AND threads_account_id = :account_id
              AND mode IS DISTINCT FROM :mode
            RETURNING active, mode
        """), {
            "user_id": user_id, "account_id": account_id, "mode": mode,
        })).first()
        await session.commit()
    if row is None:
        await cb.answer("Уже выполнено", show_alert=True)
        return
    await cb.message.answer(
        "Режим изменён: " + (
            "Сначала спрашивать меня"
            if mode == "approve"
            else "Публиковать автоматически"
        ),
        reply_markup=neuro_kb(row[0], row[1]),
    )
    await cb.answer("Сохранено")


@router.callback_query(F.data == "nc:set_mode:auto")
async def cb_set_auto(cb: CallbackQuery):
    user_id, account = await _require_selected(cb)
    if account is None:
        return
    await _set_mode(cb, user_id, account.id, "auto")


@router.callback_query(F.data == "nc:settings")
async def cb_settings(cb: CallbackQuery):
    user_id, account = await _require_selected(cb)
    if account is None:
        return
    settings = await _settings(user_id, account.id)
    _, _, cap, score, interval, auto_follow_up, _, _, _, _ = settings
    await cb.message.answer(
        f"⚙️ Настройки Neuro\n\nАккаунт: @{account.username or account.id}\n\n"
        f"Лимит в день: {cap}\n"
        f"Минимальная оценка: {score}\n"
        f"Пауза между действиями: {interval} мин.\n"
        f"Ответ после реакции автора: {'включён' if auto_follow_up else 'выключен'}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Лимит в день", callback_data="nc:cap")],
            [InlineKeyboardButton(text="Минимальная оценка", callback_data="nc:score")],
            [InlineKeyboardButton(text="Пауза между действиями", callback_data="nc:interval")],
            navigation_row("nc:menu"),
        ]),
    )
    await cb.answer()


@router.callback_query(F.data == "nc:strategies")
async def cb_strategies(cb: CallbackQuery):
    _, account = await _require_selected(cb)
    if account is None:
        return
    lines = [
        "🎭 Стратегии Neuro",
        "",
        f"Аккаунт: @{account.username or account.id}",
        "",
        "Neuro чередует безопасные способы ответа, чтобы комментарии "
        "не повторялись. Для готового комментария стратегию можно сменить "
        "перед публикацией.",
        "",
    ]
    lines.extend(f"• {label}" for label in STRATEGY_LABELS.values())
    await cb.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            navigation_row("nc:menu")
        ]),
    )
    await cb.answer()


@router.callback_query(F.data == "nc:pending")
async def cb_pending(cb: CallbackQuery):
    user_id, account = await _require_selected(cb)
    if account is None:
        return
    async with Session() as session:
        rows = (await session.execute(text("""
            SELECT id, target_author, comment_text, score, strategy
            FROM neuro_comments
            WHERE user_id = :user_id
              AND threads_account_id = :account_id
              AND status = 'pending'
            ORDER BY score DESC NULLS LAST, created_at
            LIMIT 10
        """), {"user_id": user_id, "account_id": account.id})).all()
    if not rows:
        await cb.message.answer(
            f"📋 Ждут подтверждения\n\nАккаунт: @{account.username or account.id}\n\n"
            "Сейчас очередь пуста. Новые варианты появятся после поиска Radar.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                navigation_row("nc:menu")
            ]),
        )
    else:
        await cb.message.answer(
            f"📋 Ждут подтверждения\n\nАккаунт: @{account.username or account.id}\n"
            f"Вариантов: {len(rows)}"
        )
        for comment_id, author, body, score, strategy in rows:
            await cb.message.answer(
                f"Автор: @{author or 'автор'}\n"
                f"Оценка: {score if score is not None else 'нет данных'}\n\n"
                f"Предложенный комментарий:\n{body}\n\n"
                f"Стратегия: {_strategy_label(strategy)}",
                reply_markup=approval_keyboard(comment_id),
            )
    await cb.answer()


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
    await _begin_setting(
        cb, state, SettingValue.cap,
        "Введите лимит комментариев в день: число от 1 до 30.\nДля отмены: /cancel",
    )


@router.callback_query(F.data == "nc:score")
async def cb_score(cb: CallbackQuery, state: FSMContext):
    await _begin_setting(
        cb, state, SettingValue.score,
        "Введите минимальную оценку: число от 0 до 100.\nДля отмены: /cancel",
    )


@router.callback_query(F.data == "nc:interval")
async def cb_interval(cb: CallbackQuery, state: FSMContext):
    await _begin_setting(
        cb, state, SettingValue.interval,
        "Введите паузу между действиями в минутах: от 5 до 1440.\nДля отмены: /cancel",
    )


@router.message(SettingValue.cap, Command("cancel"))
@router.message(SettingValue.score, Command("cancel"))
@router.message(SettingValue.interval, Command("cancel"))
async def cancel_setting(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Ввод отменён. Настройки не изменены.")


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
    labels = {
        "daily_cap": "Лимит в день",
        "minimum_score": "Минимальная оценка",
        "minimum_interval_minutes": "Пауза между действиями",
    }
    await msg.answer(f"Сохранено. {labels[column]}: {value}")


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
    settings = await _settings(user_id, account.id)
    timezone_name = settings[6] if settings else "Europe/Moscow"
    if not rows:
        await cb.message.answer("История пока пуста.")
    else:
        lines = [f"История @{account.username or account.id}:"]
        for author, status, score, strategy, created_at, error, replied in rows:
            lines.append(
                f"{format_local_time(created_at, timezone_name)} "
                f"@{author or 'автор'}: "
                f"{STATUS_LABELS.get(status, 'Статус обновлён')}, "
                f"оценка {score if score is not None else 'нет данных'}, "
                f"стратегия «{_strategy_label(strategy)}»"
                f"{', есть ошибка публикации' if error else ''}"
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
    await cb.message.edit_text(
        cb.message.text + f"\n\n{_publish_result_label(result)}"
    )


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
        + f"\n\nСтратегия: {_strategy_label(generated['strategy'])}",
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
            text=_strategy_label(strategy),
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
    await cb.message.edit_text(
        cb.message.text + f"\n\nОтвет автору: {_publish_result_label(result)}"
    )
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
    await cb.message.edit_text(cb.message.text + "\n\nОтвет автору отклонён")
    await cb.answer()
