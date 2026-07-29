"""
Кнопки авто-контента (внутри Автопилота): вкл/выкл, сколько постов в день.
Когда включено — worker сам пишет и ставит посты в очередь по нише и голосу.
"""
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from sqlalchemy import text

from app.core.db import Session

log = logging.getLogger("autocontent_ui")
router = Router()


class AcCap(StatesGroup):
    value = State()


async def _uid(tg_id: int) -> int | None:
    async with Session() as s:
        row = (await s.execute(text(
            "SELECT id FROM users WHERE telegram_id = :tg"
        ), {"tg": tg_id})).first()
    return row[0] if row else None


async def _settings(uid: int):
    async with Session() as s:
        await s.execute(text("""
            INSERT INTO autocontent_settings (user_id) VALUES (:uid)
            ON CONFLICT (user_id) DO NOTHING
        """), {"uid": uid})
        row = (await s.execute(text("""
            SELECT active, posts_per_day FROM autocontent_settings
            WHERE user_id = :uid
        """), {"uid": uid})).first()
        await s.commit()
    return row


def _kb(active: bool, per_day: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔴 Выключить автопостинг" if active else "🟢 Включить автопостинг",
            callback_data="ac:toggle")],
        [InlineKeyboardButton(text=f"🎚 Постов в день: {per_day}",
                              callback_data="ac:cap")],
        [InlineKeyboardButton(text="📝 Темы постов", callback_data="ac:topics")],
        [InlineKeyboardButton(text="🎯 Цель", callback_data="ac:goal")],
        [InlineKeyboardButton(text="🕐 Расписание", callback_data="ac:sched")],
        [InlineKeyboardButton(text="⬅️ Назад в Автопилот", callback_data="ap:menu")],
        [InlineKeyboardButton(text="🏠 Главная", callback_data="home")],
    ])


@router.callback_query(F.data == "ac:menu")
async def cb_menu(cb: CallbackQuery):
    uid = await _uid(cb.from_user.id)
    async with Session() as s:
        ready = (await s.execute(text("""
            SELECT
              exists(SELECT 1 FROM voice_profiles WHERE user_id=:uid),
              exists(SELECT 1 FROM user_niches WHERE user_id=:uid),
              exists(SELECT 1 FROM threads_accounts
                     WHERE user_id=:uid AND expires_at>now())
        """), {"uid": uid})).first()
    has_voice, has_niche, has_threads = ready
    missing = []
    if not has_voice:
        missing.append("голос (Сценарист → Мой голос)")
    if not has_niche:
        missing.append("ниша (Радар → Моя ниша)")
    if not has_threads:
        missing.append("Threads (Подключить)")
    if missing:
        await cb.message.answer("Для автопостинга нужно: " + ", ".join(missing))
        await cb.answer()
        return

    active, per_day = await _settings(uid)
    await cb.message.answer(
        f"🤖 Автопостинг {'работает 🟢' if active else 'выключен ⚪'}\n\n"
        "Бот сам пишет посты твоим голосом по твоей нише и ставит их "
        "в очередь на публикацию — по расписанию, без тебя.\n"
        f"Сейчас: {per_day} постов в день.\n\n"
        "Каждый пост списывает кредиты как обычная генерация.",
        reply_markup=_kb(active, per_day),
    )
    await cb.answer()


@router.callback_query(F.data == "ac:toggle")
async def cb_toggle(cb: CallbackQuery):
    uid = await _uid(cb.from_user.id)
    async with Session() as s:
        row = (await s.execute(text("""
            UPDATE autocontent_settings SET active = NOT active
            WHERE user_id = :uid RETURNING active, posts_per_day
        """), {"uid": uid})).first()
        await s.commit()
    active, per_day = row
    await cb.message.edit_reply_markup(reply_markup=_kb(active, per_day))
    await cb.answer("Автопостинг включён" if active else "Выключен")


@router.callback_query(F.data == "ac:cap")
async def cb_cap(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AcCap.value)
    await cb.message.answer("Сколько постов в день писать автоматом? (1-5)")
    await cb.answer()


@router.message(AcCap.value)
async def cap_value(msg: Message, state: FSMContext):
    try:
        n = max(1, min(5, int((msg.text or "").strip())))
    except ValueError:
        await msg.answer("Числом, 1-5")
        return
    await state.clear()
    uid = await _uid(msg.from_user.id)
    async with Session() as s:
        await s.execute(text("""
            UPDATE autocontent_settings SET posts_per_day = :n WHERE user_id = :uid
        """), {"n": n, "uid": uid})
        await s.commit()
    active, per_day = await _settings(uid)
    await msg.answer(f"Готово: {per_day} постов в день.",
                     reply_markup=_kb(active, per_day))


class AcTopics(StatesGroup):
    value = State()


@router.callback_query(F.data == "ac:topics")
async def cb_topics(cb: CallbackQuery, state: FSMContext):
    uid = await _uid(cb.from_user.id)
    async with Session() as s:
        row = (await s.execute(text(
            "SELECT topics FROM autocontent_settings WHERE user_id=:uid"
        ), {"uid": uid})).first()
    cur = (row[0] if row else "") or ""
    shown = cur if cur.strip() else "— (пусто, беру темы из ниши)"
    await state.set_state(AcTopics.value)
    await cb.message.answer(
        "📝 Темы для автопостинга. Пришли список — каждая тема с новой строки.\n"
        "Автопилот будет писать по ним по кругу.\n\n"
        f"Сейчас:\n{shown}\n\n"
        "Пришли новый список, или «-» чтобы очистить.")
    await cb.answer()


@router.message(AcTopics.value)
async def topics_value(msg: Message, state: FSMContext):
    await state.clear()
    raw = (msg.text or "").strip()
    val = "" if raw == "-" else raw
    uid = await _uid(msg.from_user.id)
    async with Session() as s:
        await s.execute(text(
            "UPDATE autocontent_settings SET topics=:t WHERE user_id=:uid"
        ), {"t": val, "uid": uid})
        await s.commit()
    n = len([t for t in val.splitlines() if t.strip()])
    active, per_day = await _settings(uid)
    await msg.answer(
        ("Сохранил %d тем." % n) if n else "Очистил — вернул генерацию по нише.",
        reply_markup=_kb(active, per_day))


class AcSlots(StatesGroup):
    value = State()


@router.callback_query(F.data == "ac:sched")
async def cb_sched(cb: CallbackQuery):
    uid = await _uid(cb.from_user.id)
    async with Session() as s:
        row = (await s.execute(text(
            "SELECT slots, days FROM autocontent_settings WHERE user_id=:uid"
        ), {"uid": uid})).first()
    slots = (row[0] if row else "") or "9,12,15,18,21"
    days = (row[1] if row else "") or "all"
    days_lbl = "будни" if days == "weekdays" else "все дни"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕐 Задать часы", callback_data="ac:slots")],
        [InlineKeyboardButton(text=f"📆 Дни: {days_lbl}", callback_data="ac:days")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="ac:menu")],
        [InlineKeyboardButton(text="🏠 Главная", callback_data="home")],
    ])
    await cb.message.answer(
        f"🕐 Расписание автопостинга.\n\nЧасы (МСК): {slots}\nДни: {days_lbl}",
        reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "ac:slots")
async def cb_slots(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AcSlots.value)
    await cb.message.answer("Пришли часы через запятую (МСК, 0-23). Пример: 9,14,19,22")
    await cb.answer()


@router.message(AcSlots.value)
async def slots_value(msg: Message, state: FSMContext):
    await state.clear()
    raw = (msg.text or "").replace(" ", "")
    hours = [int(x) for x in raw.split(",") if x.isdigit() and 0 <= int(x) <= 23]
    if not hours:
        await msg.answer("Не понял. Пример: 9,14,19")
        return
    val = ",".join(str(x) for x in hours)
    uid = await _uid(msg.from_user.id)
    async with Session() as s:
        await s.execute(text("UPDATE autocontent_settings SET slots=:v WHERE user_id=:uid"),
                        {"v": val, "uid": uid})
        await s.commit()
    await msg.answer(f"Часы обновлены: {val}")


@router.callback_query(F.data == "ac:days")
async def cb_days(cb: CallbackQuery):
    uid = await _uid(cb.from_user.id)
    async with Session() as s:
        row = (await s.execute(text(
            "SELECT days FROM autocontent_settings WHERE user_id=:uid"), {"uid": uid})).first()
        cur = (row[0] if row else "") or "all"
        nxt = "weekdays" if cur == "all" else "all"
        await s.execute(text("UPDATE autocontent_settings SET days=:v WHERE user_id=:uid"),
                        {"v": nxt, "uid": uid})
        await s.commit()
    await cb.answer("будни" if nxt == "weekdays" else "все дни")


GOAL_CYCLE = ["", "охваты", "подписчики", "переходы по ссылке", "вовлечение"]


@router.callback_query(F.data == "ac:goal")
async def cb_goal(cb: CallbackQuery):
    uid = await _uid(cb.from_user.id)
    async with Session() as s:
        row = (await s.execute(text(
            "SELECT coalesce(goal,'') FROM autocontent_settings WHERE user_id=:uid"
        ), {"uid": uid})).first()
    cur = row[0] if row else ""
    kb = []
    for g in GOAL_CYCLE[1:]:
        mark = "✅ " if g == cur else ""
        kb.append([InlineKeyboardButton(text=mark + g,
                                        callback_data="ac:setgoal:" + g)])
    kb.append([InlineKeyboardButton(text="Без цели", callback_data="ac:setgoal:")])
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="ac:menu")])
    nl = chr(10)
    await cb.message.answer(
        "🎯 Цель автопостинга — под неё бот меняет подачу." + nl + nl +
        "Сейчас: " + (cur or "не задана"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cb.answer()


@router.callback_query(F.data.startswith("ac:setgoal:"))
async def cb_setgoal(cb: CallbackQuery):
    g = cb.data.split(":", 2)[2]
    uid = await _uid(cb.from_user.id)
    async with Session() as s:
        await s.execute(text(
            "UPDATE autocontent_settings SET goal=:g WHERE user_id=:uid"),
            {"g": g, "uid": uid})
        await s.commit()
    await cb.answer("Цель: " + (g or "убрана"), show_alert=True)
