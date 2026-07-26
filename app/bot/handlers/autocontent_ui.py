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
