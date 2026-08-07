"""Transparent account-scoped Telegram UI for automatic posting."""

import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

from app.core.accounts import ThreadsAccountService
from app.core.autopost_status import (
    AutopostStatusService,
    DEFAULT_TIMEZONE,
    parse_slots,
    render_history,
    render_status,
    serialize_slots,
)
from app.core.db import Session
from app.bot.ux import escape_html, navigation_row, show_ui_screen

log = logging.getLogger("autocontent_ui")
router = Router()


class AcCap(StatesGroup):
    value = State()


class AcTopics(StatesGroup):
    value = State()


class AcSlots(StatesGroup):
    value = State()


class AcTimezone(StatesGroup):
    value = State()


async def _uid(tg_id: int) -> int | None:
    async with Session() as session:
        row = (
            await session.execute(
                text("SELECT id FROM users WHERE telegram_id = :tg"),
                {"tg": tg_id},
            )
        ).first()
    return row[0] if row else None


async def _ensure_settings(uid: int, account_id: int) -> None:
    async with Session() as session:
        await ThreadsAccountService(session).ensure_settings(
            uid,
            account_id,
        )
        await session.commit()


async def _readiness(uid: int) -> tuple[bool, bool]:
    async with Session() as session:
        row = (
            await session.execute(
                text("""
                    SELECT
                      exists(
                        SELECT 1 FROM voice_profiles WHERE user_id = :uid
                      ),
                      exists(
                        SELECT 1 FROM user_niches WHERE user_id = :uid
                      )
                """),
                {"uid": uid},
            )
        ).first()
    return (bool(row[0]), bool(row[1])) if row else (False, False)


async def _overview_details(
    uid: int,
    account_id: int,
) -> tuple[int, int, str]:
    async with Session() as session:
        row = (await session.execute(text("""
            SELECT
              (SELECT count(*) FROM scheduled_posts post
               WHERE post.user_id = setting.user_id
                 AND post.threads_account_id = setting.threads_account_id
                 AND post.status = 'done'
                 AND (post.run_at AT TIME ZONE setting.timezone)::date =
                     (now() AT TIME ZONE setting.timezone)::date),
              (SELECT count(*) FROM scheduled_posts post
               WHERE post.user_id = setting.user_id
                 AND post.threads_account_id = setting.threads_account_id
                 AND post.status IN ('pending', 'publishing')),
              setting.goal
            FROM autocontent_settings setting
            WHERE setting.user_id = :uid
              AND setting.threads_account_id = :account_id
        """), {"uid": uid, "account_id": account_id})).first()
    if row is None:
        return 0, 0, ""
    return int(row[0] or 0), int(row[1] or 0), str(row[2] or "")


async def _load_menu(uid: int, account_id: int | None = None):
    async with Session() as session:
        account_service = ThreadsAccountService(session)
        service = AutopostStatusService(session)
        accounts = await service.list_accounts(uid)
        if not accounts:
            return None, []
        selected = (
            await account_service.select_account(uid, account_id)
            if account_id is not None
            else await account_service.selected_account(uid)
        )
        if selected is None:
            return None, accounts
        selected_id = selected.id
        await account_service.ensure_settings(uid, selected_id)
        status = await service.get_status(uid, selected_id)
        await session.commit()
        return status, accounts


def _account_callback_id(data: str) -> int | None:
    try:
        return int(data.rsplit(":", 1)[1])
    except (AttributeError, TypeError, ValueError):
        return None


async def _resolved_account_id(uid: int, data: str) -> int | None:
    explicit = _account_callback_id(data)
    async with Session() as session:
        accounts = ThreadsAccountService(session)
        if explicit is not None:
            owned = await accounts.select_account(uid, explicit)
            await session.commit()
            return owned.id if owned is not None else None
        selected = await accounts.selected_account(uid)
        await session.commit()
    return selected.id if selected else None


async def _state_account(
    msg: Message,
    state: FSMContext,
) -> tuple[int, int] | None:
    data = await state.get_data()
    account_id = data.get("account_id")
    uid = await _uid(msg.from_user.id)
    if uid is None or not isinstance(account_id, int):
        await state.clear()
        await msg.answer("Сессия настройки завершилась. Откройте её заново.")
        return None
    async with Session() as session:
        selected = await ThreadsAccountService(session).selected_account(uid)
        await session.commit()
    if selected is None or selected.id != account_id:
        await state.clear()
        await msg.answer(
            "Активный аккаунт изменился. Откройте настройку заново."
        )
        return None
    return uid, account_id


def _menu_kb(status, accounts) -> InlineKeyboardMarkup:
    account_id = status.account.id
    active = status.settings.enabled
    rows = []
    if len(accounts) > 1:
        rows.extend([
            [
                InlineKeyboardButton(
                    text=(
                        ("✅ " if account.id == account_id else "")
                        + f"@{account.username or account.id}"
                    ),
                    callback_data=f"ac:menu:{account.id}",
                )
            ]
            for account in accounts
        ])
    rows.extend([
        [InlineKeyboardButton(
            text=(
                "⏸ Остановить"
                if active
                else "▶️ Включить Автопилот"
            ),
            callback_data=f"ac:set_active:{account_id}:{0 if active else 1}",
        )],
        [
            InlineKeyboardButton(
                text="📋 Очередь", callback_data=f"ap:queue:{account_id}"
            ),
            InlineKeyboardButton(
                text="🕘 История", callback_data=f"ac:history:{account_id}"
            ),
        ],
        [InlineKeyboardButton(
            text="⚙️ Настройки",
            callback_data=f"ac:settings:{account_id}",
        )],
        [InlineKeyboardButton(
            text="💡 Почему так?",
            callback_data="intel:why",
        )],
        [InlineKeyboardButton(
            text="ℹ️ Как это работает",
            callback_data="help:autopilot",
        )],
        navigation_row("home"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _answer_status(
    message: Message,
    uid: int,
    account_id: int,
) -> None:
    status, accounts = await _load_menu(uid, account_id)
    if status is None:
        await message.answer("Threads-аккаунт не найден.")
        return
    await message.answer(
        render_status(status),
        reply_markup=_menu_kb(status, accounts),
    )


async def _answer_schedule_changed(
    message: Message,
    uid: int,
    account_id: int | None,
) -> None:
    status, _ = await _load_menu(uid, account_id)
    if status is None:
        await message.answer("Threads-аккаунт не найден.")
        return
    selected_id = status.account.id
    await message.answer(
        "Расписание изменено. Перестроить будущую очередь под новые "
        "настройки?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🔄 Перестроить",
                callback_data=f"ap:q_rebuild:{selected_id}",
            )],
            [InlineKeyboardButton(
                text="Оставить как есть",
                callback_data=f"ac:menu:{selected_id}",
            )],
        ]),
    )


async def _show_menu(
    cb: CallbackQuery,
    *,
    account_id: int | None = None,
    edit: bool = False,
) -> None:
    uid = await _uid(cb.from_user.id)
    if uid is None:
        await cb.answer("Пользователь не найден", show_alert=True)
        return
    status, accounts = await _load_menu(uid, account_id)
    if status is None:
        await cb.message.answer(
            "Сначала подключите Threads-аккаунт. После подключения можно "
            "пройти настройку за две минуты."
        )
        await cb.answer()
        return
    has_voice, has_niche = await _readiness(uid)
    posts_today, queued, goal = await _overview_details(
        uid, status.account.id
    )
    missing = []
    if not has_voice:
        missing.append("профиль голоса")
    if not has_niche:
        missing.append("тематика аккаунта")
    message_text = render_status(status)
    message_text += (
        f"\n\nСегодня: {posts_today} из "
        f"{status.settings.posts_per_day} постов"
        f"\nВ очереди: {queued}"
        f"\nЦель: {goal or 'не задана'}"
    )
    if missing:
        message_text += (
            "\n\nПеред включением настройте: "
            + ", ".join(missing)
            + "."
        )
    keyboard = _menu_kb(status, accounts)
    safe_text = escape_html(message_text).replace(
        "✍️ Автопилот",
        "🤖 <b>Автопилот</b>",
        1,
    )
    await show_ui_screen(
        cb.message,
        safe_text,
        reply_markup=keyboard,
        prefer_edit=True,
    )
    await cb.answer()


@router.callback_query(F.data == "ac:menu")
async def cb_menu(cb: CallbackQuery):
    await _show_menu(cb)


@router.callback_query(F.data.startswith("ac:menu:"))
async def cb_account_menu(cb: CallbackQuery):
    await _show_menu(cb, account_id=_account_callback_id(cb.data))


@router.callback_query(F.data.startswith("ac:refresh:"))
async def cb_refresh(cb: CallbackQuery):
    await _show_menu(
        cb,
        account_id=_account_callback_id(cb.data),
        edit=True,
    )


@router.callback_query(F.data.startswith("ac:settings:"))
async def cb_settings(cb: CallbackQuery):
    uid = await _uid(cb.from_user.id)
    account_id = _account_callback_id(cb.data)
    status, _ = await _load_menu(uid, account_id)
    if status is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    account_id = status.account.id
    await cb.message.answer(
        f"⚙️ Настройки Автопилота\n\n"
        f"Аккаунт: @{status.account.username or account_id}\n\n"
        f"Постов в день: {status.settings.posts_per_day}\n"
        f"Расписание: {serialize_slots(status.settings.slots) or 'не задано'}\n"
        f"Часовой пояс: {status.settings.timezone}\n\n"
        "После изменения расписания можно безопасно перестроить будущую очередь.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="Лимит в день",
                callback_data=f"ac:cap:{account_id}",
            )],
            [InlineKeyboardButton(
                text="🕒 Расписание",
                callback_data=f"ac:sched:{account_id}",
            )],
            [InlineKeyboardButton(
                text="📝 Темы",
                callback_data=f"ac:topics:{account_id}",
            )],
            [InlineKeyboardButton(
                text="🎯 Цель",
                callback_data=f"ac:goal:{account_id}",
            )],
            [InlineKeyboardButton(
                text="🔄 Обновить статус",
                callback_data=f"ac:refresh:{account_id}",
            )],
            navigation_row(f"ac:menu:{account_id}"),
        ]),
    )
    await cb.answer()


@router.callback_query(F.data == "ac:toggle")
@router.callback_query(F.data.startswith("ac:toggle:"))
async def cb_toggle(cb: CallbackQuery):
    uid = await _uid(cb.from_user.id)
    account_id = await _resolved_account_id(uid, cb.data)
    if account_id is None:
        await cb.answer("Threads-аккаунт не подключён", show_alert=True)
        return
    async with Session() as session:
        row = (
            await session.execute(
                text("""
                UPDATE autocontent_settings
                SET active = NOT active
                WHERE user_id = :uid
                  AND threads_account_id = :account_id
                RETURNING active
            """),
                {"uid": uid, "account_id": account_id},
            )
        ).first()
        if row and not row[0]:
            await AutopostStatusService(
                session
            ).skip_pending_for_account(uid, account_id)
        await session.commit()
    await _show_menu(cb, account_id=account_id, edit=True)


@router.callback_query(F.data.startswith("ac:set_active:"))
async def cb_set_active(cb: CallbackQuery):
    try:
        _, _, account_raw, active_raw = cb.data.split(":", 3)
        account_id = int(account_raw)
        desired = active_raw == "1"
    except (TypeError, ValueError):
        await cb.answer("Некорректная команда", show_alert=True)
        return
    uid = await _uid(cb.from_user.id)
    async with Session() as session:
        row = (
            await session.execute(text("""
                UPDATE autocontent_settings
                SET active = :active, updated_at = now()
                WHERE user_id = :uid
                  AND threads_account_id = :account_id
                  AND active IS DISTINCT FROM :active
                RETURNING active
            """), {
                "uid": uid,
                "account_id": account_id,
                "active": desired,
            })
        ).first()
        if row and not desired:
            await AutopostStatusService(session).skip_pending_for_account(
                uid, account_id
            )
        await session.commit()
    if row is None:
        await cb.answer("Уже выполнено", show_alert=True)
        return
    await _show_menu(cb, account_id=account_id, edit=False)


@router.callback_query(F.data == "ac:cap")
@router.callback_query(F.data.startswith("ac:cap:"))
async def cb_cap(cb: CallbackQuery, state: FSMContext):
    uid = await _uid(cb.from_user.id)
    account_id = await _resolved_account_id(uid, cb.data)
    if account_id is None:
        await cb.answer("Threads-аккаунт не подключён", show_alert=True)
        return
    await state.set_state(AcCap.value)
    await state.update_data(account_id=account_id)
    await cb.message.answer(
        "Введите количество постов в день: число от 1 до 5.\n"
        "Для отмены: /cancel"
    )
    await cb.answer()


@router.message(AcCap.value, Command("cancel"))
@router.message(AcTopics.value, Command("cancel"))
@router.message(AcSlots.value, Command("cancel"))
@router.message(AcTimezone.value, Command("cancel"))
async def cancel_setting(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Ввод отменён. Настройки не изменены.")


@router.message(AcCap.value)
async def cap_value(msg: Message, state: FSMContext):
    try:
        posts_per_day = max(1, min(5, int((msg.text or "").strip())))
    except ValueError:
        await msg.answer("Числом, 1-5")
        return
    scope = await _state_account(msg, state)
    if scope is None:
        return
    uid, account_id = scope
    await state.clear()
    async with Session() as session:
        await session.execute(
            text("""
                UPDATE autocontent_settings
                SET posts_per_day = :posts_per_day
                WHERE user_id = :uid
                  AND threads_account_id = :account_id
            """),
            {
                "posts_per_day": posts_per_day,
                "uid": uid,
                "account_id": account_id,
            },
        )
        await session.commit()
    await _answer_schedule_changed(
        msg,
        uid,
        account_id,
    )


@router.callback_query(F.data == "ac:topics")
@router.callback_query(F.data.startswith("ac:topics:"))
async def cb_topics(cb: CallbackQuery, state: FSMContext):
    uid = await _uid(cb.from_user.id)
    account_id = await _resolved_account_id(uid, cb.data)
    if account_id is None:
        await cb.answer("Threads-аккаунт не подключён", show_alert=True)
        return
    async with Session() as session:
        row = (
            await session.execute(
                text("""
                    SELECT topics
                    FROM autocontent_settings
                    WHERE user_id = :uid
                      AND threads_account_id = :account_id
                """),
                {"uid": uid, "account_id": account_id},
            )
        ).first()
    current = (row[0] if row else "") or ""
    shown = current if current.strip() else "— (беру темы из ниши)"
    await state.set_state(AcTopics.value)
    await state.update_data(account_id=account_id)
    await cb.message.answer(
        "📝 Темы Автопилота. Отправьте список: каждая тема "
        "с новой строки.\n\n"
        f"Сейчас:\n{shown}\n\n"
        "Отправьте новый список или «-», чтобы очистить.\n"
        "Для отмены: /cancel"
    )
    await cb.answer()


@router.message(AcTopics.value)
async def topics_value(msg: Message, state: FSMContext):
    raw = (msg.text or "").strip()
    topics = "" if raw == "-" else raw
    scope = await _state_account(msg, state)
    if scope is None:
        return
    uid, account_id = scope
    await state.clear()
    async with Session() as session:
        await session.execute(
            text("""
                UPDATE autocontent_settings
                SET topics = :topics
                WHERE user_id = :uid
                  AND threads_account_id = :account_id
            """),
            {
                "topics": topics,
                "uid": uid,
                "account_id": account_id,
            },
        )
        await session.commit()
    await _answer_status(msg, uid, account_id)


async def _show_schedule(
    cb: CallbackQuery,
    account_id: int,
) -> None:
    uid = await _uid(cb.from_user.id)
    status, _ = await _load_menu(uid, account_id)
    if status is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    account_id = status.account.id
    days_label = (
        "будни"
        if status.settings.days == "weekdays"
        else "все дни"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🕐 Задать время",
            callback_data=f"ac:slots:{account_id}",
        )],
        [InlineKeyboardButton(
            text=f"📆 Дни: {days_label}",
            callback_data=f"ac:days:{account_id}",
        )],
        [InlineKeyboardButton(
            text="🌍 Часовой пояс",
            callback_data=f"ac:timezone:{account_id}",
        )],
        [InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=f"ac:menu:{account_id}",
        )],
    ])
    await cb.message.answer(
        "🕐 Расписание Автопилота\n\n"
        f"Время: {serialize_slots(status.settings.slots)}\n"
        f"Дни: {days_label}\n"
        f"Часовой пояс: {status.settings.timezone}",
        reply_markup=keyboard,
    )
    await cb.answer()


@router.callback_query(F.data == "ac:sched")
@router.callback_query(F.data.startswith("ac:sched:"))
async def cb_sched(cb: CallbackQuery):
    await _show_schedule(cb, _account_callback_id(cb.data))


@router.callback_query(F.data == "ac:slots")
@router.callback_query(F.data.startswith("ac:slots:"))
async def cb_slots(cb: CallbackQuery, state: FSMContext):
    uid = await _uid(cb.from_user.id)
    account_id = await _resolved_account_id(uid, cb.data)
    if account_id is None:
        await cb.answer("Threads-аккаунт не подключён", show_alert=True)
        return
    await state.set_state(AcSlots.value)
    await state.update_data(account_id=account_id)
    await cb.message.answer(
        "Отправьте время через запятую. Пример: 09:00,14:30,19:00\n"
        "Для отмены: /cancel"
    )
    await cb.answer()


@router.message(AcSlots.value)
async def slots_value(msg: Message, state: FSMContext):
    slots = parse_slots(
        (msg.text or "").replace(" ", ""),
        default_if_empty=False,
    )
    if not slots:
        await msg.answer("Не понял. Пример: 09:00,14:30,19:00")
        return
    scope = await _state_account(msg, state)
    if scope is None:
        return
    uid, account_id = scope
    await state.clear()
    serialized = serialize_slots(slots)
    async with Session() as session:
        await session.execute(
            text("""
                UPDATE autocontent_settings
                SET slots = :slots
                WHERE user_id = :uid
                  AND threads_account_id = :account_id
            """),
            {
                "slots": serialized,
                "uid": uid,
                "account_id": account_id,
            },
        )
        await session.commit()
    await _answer_schedule_changed(
        msg,
        uid,
        account_id,
    )


@router.callback_query(F.data == "ac:days")
@router.callback_query(F.data.startswith("ac:days:"))
async def cb_days(cb: CallbackQuery):
    uid = await _uid(cb.from_user.id)
    account_id = await _resolved_account_id(uid, cb.data)
    if account_id is None:
        await cb.answer("Threads-аккаунт не подключён", show_alert=True)
        return
    async with Session() as session:
        row = (
            await session.execute(
                text("""
                    SELECT days
                    FROM autocontent_settings
                    WHERE user_id = :uid
                      AND threads_account_id = :account_id
                """),
                {"uid": uid, "account_id": account_id},
            )
        ).first()
        current = (row[0] if row else "") or "all"
        next_value = "weekdays" if current == "all" else "all"
        await session.execute(
            text("""
                UPDATE autocontent_settings
                SET days = :days
                WHERE user_id = :uid
                  AND threads_account_id = :account_id
            """),
            {
                "days": next_value,
                "uid": uid,
                "account_id": account_id,
            },
        )
        await session.commit()
    await _answer_schedule_changed(
        cb.message,
        uid,
        account_id,
    )
    await cb.answer()


@router.callback_query(F.data.startswith("ac:timezone:"))
async def cb_timezone(cb: CallbackQuery, state: FSMContext):
    uid = await _uid(cb.from_user.id)
    account_id = await _resolved_account_id(uid, cb.data)
    if account_id is None:
        await cb.answer("Threads-аккаунт не найден", show_alert=True)
        return
    await state.set_state(AcTimezone.value)
    await state.update_data(account_id=account_id)
    await cb.message.answer(
        "Отправьте часовой пояс IANA. Например: Europe/Moscow, "
        "Europe/Berlin или Asia/Almaty.\nДля отмены: /cancel"
    )
    await cb.answer()


@router.message(AcTimezone.value)
async def timezone_value(msg: Message, state: FSMContext):
    timezone_name = (msg.text or "").strip()
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        await msg.answer(
            f"Неизвестный часовой пояс. Пример: {DEFAULT_TIMEZONE}"
        )
        return
    scope = await _state_account(msg, state)
    if scope is None:
        return
    uid, account_id = scope
    await state.clear()
    async with Session() as session:
        await session.execute(
            text("""
                UPDATE autocontent_settings
                SET timezone = :timezone
                WHERE user_id = :uid
                  AND threads_account_id = :account_id
            """),
            {
                "timezone": timezone_name,
                "uid": uid,
                "account_id": account_id,
            },
        )
        await session.commit()
    await _answer_schedule_changed(
        msg,
        uid,
        account_id,
    )


GOAL_CYCLE = [
    "",
    "охваты",
    "подписчики",
    "переходы по ссылке",
    "вовлечение",
]


@router.callback_query(F.data == "ac:goal")
@router.callback_query(F.data.startswith("ac:goal:"))
async def cb_goal(cb: CallbackQuery):
    uid = await _uid(cb.from_user.id)
    account_id = _account_callback_id(cb.data)
    status, _ = await _load_menu(uid, account_id)
    if status is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    account_id = status.account.id
    async with Session() as session:
        row = (
            await session.execute(
                text("""
                    SELECT coalesce(goal, '')
                    FROM autocontent_settings
                    WHERE user_id = :uid
                      AND threads_account_id = :account_id
                """),
                {"uid": uid, "account_id": account_id},
            )
        ).first()
    current = row[0] if row else ""
    rows = []
    for index, goal in enumerate(GOAL_CYCLE):
        label = goal or "Без цели"
        if goal == current:
            label = "✅ " + label
        rows.append([InlineKeyboardButton(
            text=label,
            callback_data=f"ac:setgoal:{account_id}:{index}",
        )])
    rows.append([InlineKeyboardButton(
        text="⬅️ Назад",
        callback_data=f"ac:menu:{account_id}",
    )])
    await cb.message.answer(
        "🎯 Цель Автопилота\n\n"
        f"Сейчас: {current or 'не задана'}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("ac:setgoal:"))
async def cb_setgoal(cb: CallbackQuery):
    parts = cb.data.split(":", 3)
    if len(parts) == 4:
        account_id = int(parts[2])
        index = max(
            0,
            min(len(GOAL_CYCLE) - 1, int(parts[3])),
        )
        goal = GOAL_CYCLE[index]
    else:
        uid = await _uid(cb.from_user.id)
        account_id = await _resolved_account_id(uid, cb.data)
        goal = parts[2] if len(parts) == 3 else ""
    uid = await _uid(cb.from_user.id)
    if account_id is None:
        await cb.answer("Threads-аккаунт не подключён", show_alert=True)
        return
    async with Session() as session:
        await session.execute(
            text("""
                UPDATE autocontent_settings
                SET goal = :goal
                WHERE user_id = :uid
                  AND threads_account_id = :account_id
            """),
            {
                "goal": goal,
                "uid": uid,
                "account_id": account_id,
            },
        )
        await session.commit()
    await _show_menu(cb, account_id=account_id, edit=False)


@router.callback_query(F.data.startswith("ac:history:"))
async def cb_history(cb: CallbackQuery):
    uid = await _uid(cb.from_user.id)
    account_id = _account_callback_id(cb.data)
    async with Session() as session:
        service = AutopostStatusService(session)
        status = await service.get_status(uid, account_id)
        runs = await service.history(uid, account_id, limit=10)
    if status is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=f"ac:menu:{account_id}",
        )
    ]])
    await cb.message.answer(
        render_history(runs, status.settings.timezone),
        reply_markup=keyboard,
    )
    await cb.answer()
