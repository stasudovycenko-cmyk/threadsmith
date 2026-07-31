"""Transparent account-scoped Telegram UI for automatic posting."""

import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import text

from app.core.autopost_status import (
    AutopostStatusService,
    DEFAULT_TIMEZONE,
    parse_slots,
    render_history,
    render_status,
    serialize_slots,
)
from app.core.db import Session

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


async def _ensure_settings(uid: int) -> None:
    async with Session() as session:
        await session.execute(
            text("""
                INSERT INTO autocontent_settings (user_id)
                VALUES (:uid)
                ON CONFLICT (user_id) DO NOTHING
            """),
            {"uid": uid},
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


async def _load_menu(uid: int, account_id: int | None = None):
    await _ensure_settings(uid)
    async with Session() as session:
        service = AutopostStatusService(session)
        accounts = await service.list_accounts(uid)
        if not accounts:
            return None, []
        selected_id = account_id or accounts[0].id
        status = await service.get_status(uid, selected_id)
        return status, accounts


def _account_callback_id(data: str) -> int | None:
    try:
        return int(data.rsplit(":", 1)[1])
    except (AttributeError, TypeError, ValueError):
        return None


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
                else "▶️ Включить автопостинг"
            ),
            callback_data=f"ac:toggle:{account_id}",
        )],
        [InlineKeyboardButton(
            text=f"🎚 Постов в день: {status.settings.posts_per_day}",
            callback_data=f"ac:cap:{account_id}",
        )],
        [InlineKeyboardButton(
            text="🕐 Расписание",
            callback_data=f"ac:sched:{account_id}",
        )],
        [InlineKeyboardButton(
            text="📝 Темы постов",
            callback_data=f"ac:topics:{account_id}",
        )],
        [InlineKeyboardButton(
            text="🎯 Цель",
            callback_data=f"ac:goal:{account_id}",
        )],
        [InlineKeyboardButton(
            text="📋 История",
            callback_data=f"ac:history:{account_id}",
        )],
        [InlineKeyboardButton(
            text="🔄 Обновить статус",
            callback_data=f"ac:refresh:{account_id}",
        )],
        [InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data="ap:menu",
        )],
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
    has_voice, has_niche = await _readiness(uid)
    missing = []
    if not has_voice:
        missing.append("голос (Сценарист → Мой голос)")
    if not has_niche:
        missing.append("ниша (Радар → Моя ниша)")
    if missing:
        await cb.message.answer(
            "Для автопостинга нужно: " + ", ".join(missing)
        )
        await cb.answer()
        return

    status, accounts = await _load_menu(uid, account_id)
    if status is None:
        await cb.message.answer(
            "Сначала подключи Threads: /start → Подключить Threads"
        )
        await cb.answer()
        return
    message_text = render_status(status)
    keyboard = _menu_kb(status, accounts)
    try:
        if edit:
            await cb.message.edit_text(message_text, reply_markup=keyboard)
        else:
            await cb.message.answer(message_text, reply_markup=keyboard)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).casefold():
            raise
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


@router.callback_query(F.data == "ac:toggle")
@router.callback_query(F.data.startswith("ac:toggle:"))
async def cb_toggle(cb: CallbackQuery):
    uid = await _uid(cb.from_user.id)
    account_id = _account_callback_id(cb.data)
    async with Session() as session:
        row = (
            await session.execute(
                text("""
                UPDATE autocontent_settings
                SET active = NOT active
                WHERE user_id = :uid
                RETURNING active
            """),
                {"uid": uid},
            )
        ).first()
        if row and not row[0]:
            await AutopostStatusService(
                session
            ).skip_pending_for_user(uid)
        await session.commit()
    await _show_menu(cb, account_id=account_id, edit=True)


@router.callback_query(F.data == "ac:cap")
@router.callback_query(F.data.startswith("ac:cap:"))
async def cb_cap(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AcCap.value)
    await state.update_data(
        account_id=_account_callback_id(cb.data),
    )
    await cb.message.answer(
        "Сколько постов в день писать автоматом? (1-5)"
    )
    await cb.answer()


@router.message(AcCap.value)
async def cap_value(msg: Message, state: FSMContext):
    try:
        posts_per_day = max(1, min(5, int((msg.text or "").strip())))
    except ValueError:
        await msg.answer("Числом, 1-5")
        return
    data = await state.get_data()
    await state.clear()
    uid = await _uid(msg.from_user.id)
    async with Session() as session:
        await session.execute(
            text("""
                UPDATE autocontent_settings
                SET posts_per_day = :posts_per_day
                WHERE user_id = :uid
            """),
            {"posts_per_day": posts_per_day, "uid": uid},
        )
        await session.commit()
    await _answer_status(msg, uid, data["account_id"])


@router.callback_query(F.data == "ac:topics")
@router.callback_query(F.data.startswith("ac:topics:"))
async def cb_topics(cb: CallbackQuery, state: FSMContext):
    uid = await _uid(cb.from_user.id)
    account_id = _account_callback_id(cb.data)
    async with Session() as session:
        row = (
            await session.execute(
                text("""
                    SELECT topics
                    FROM autocontent_settings
                    WHERE user_id = :uid
                """),
                {"uid": uid},
            )
        ).first()
    current = (row[0] if row else "") or ""
    shown = current if current.strip() else "— (беру темы из ниши)"
    await state.set_state(AcTopics.value)
    await state.update_data(account_id=account_id)
    await cb.message.answer(
        "📝 Темы для автопостинга. Пришли список, каждая тема "
        "с новой строки.\n\n"
        f"Сейчас:\n{shown}\n\n"
        "Пришли новый список или «-», чтобы очистить."
    )
    await cb.answer()


@router.message(AcTopics.value)
async def topics_value(msg: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    raw = (msg.text or "").strip()
    topics = "" if raw == "-" else raw
    uid = await _uid(msg.from_user.id)
    async with Session() as session:
        await session.execute(
            text("""
                UPDATE autocontent_settings
                SET topics = :topics
                WHERE user_id = :uid
            """),
            {"topics": topics, "uid": uid},
        )
        await session.commit()
    await _answer_status(msg, uid, data["account_id"])


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
        "🕐 Расписание автопостинга\n\n"
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
    await state.set_state(AcSlots.value)
    await state.update_data(
        account_id=_account_callback_id(cb.data),
    )
    await cb.message.answer(
        "Пришли время через запятую. Пример: 09:00,14:30,19:00"
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
    data = await state.get_data()
    await state.clear()
    uid = await _uid(msg.from_user.id)
    serialized = serialize_slots(slots)
    async with Session() as session:
        await session.execute(
            text("""
                UPDATE autocontent_settings
                SET slots = :slots
                WHERE user_id = :uid
            """),
            {"slots": serialized, "uid": uid},
        )
        await session.commit()
    await _answer_status(msg, uid, data["account_id"])


@router.callback_query(F.data == "ac:days")
@router.callback_query(F.data.startswith("ac:days:"))
async def cb_days(cb: CallbackQuery):
    uid = await _uid(cb.from_user.id)
    account_id = _account_callback_id(cb.data)
    async with Session() as session:
        row = (
            await session.execute(
                text("""
                    SELECT days
                    FROM autocontent_settings
                    WHERE user_id = :uid
                """),
                {"uid": uid},
            )
        ).first()
        current = (row[0] if row else "") or "all"
        next_value = "weekdays" if current == "all" else "all"
        await session.execute(
            text("""
                UPDATE autocontent_settings
                SET days = :days
                WHERE user_id = :uid
            """),
            {"days": next_value, "uid": uid},
        )
        await session.commit()
    await _show_schedule(cb, account_id)


@router.callback_query(F.data.startswith("ac:timezone:"))
async def cb_timezone(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AcTimezone.value)
    await state.update_data(
        account_id=_account_callback_id(cb.data),
    )
    await cb.message.answer(
        "Пришли часовой пояс IANA. Например: Europe/Moscow, "
        "Europe/Berlin или Asia/Almaty"
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
    data = await state.get_data()
    await state.clear()
    uid = await _uid(msg.from_user.id)
    async with Session() as session:
        await session.execute(
            text("""
                UPDATE autocontent_settings
                SET timezone = :timezone
                WHERE user_id = :uid
            """),
            {"timezone": timezone_name, "uid": uid},
        )
        await session.commit()
    await _answer_status(msg, uid, data["account_id"])


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
                """),
                {"uid": uid},
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
        "🎯 Цель автопостинга\n\n"
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
        account_id = None
        goal = parts[2] if len(parts) == 3 else ""
    uid = await _uid(cb.from_user.id)
    async with Session() as session:
        await session.execute(
            text("""
                UPDATE autocontent_settings
                SET goal = :goal
                WHERE user_id = :uid
            """),
            {"goal": goal, "uid": uid},
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
