"""Глубокая ручная настройка поверх обученного профиля голоса."""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from sqlalchemy import text

from app.core.db import Session

router = Router()

CYCLES = {
    "manner":  ["", "дерзко", "экспертно", "дружелюбно", "провокативно"],
    "length":  ["", "короткие панчи", "средние", "лонгриды"],
    "emoji":   ["", "без эмодзи", "умеренно эмодзи", "много эмодзи"],
    "address": ["", "на ты", "на вы"],
    "hashtags": ["", "с хештегами", "без хештегов"],
    "cta": ["", "с призывом в конце", "без призыва"],
}
LABELS = {"manner": "Манера", "length": "Длина",
          "emoji": "Эмодзи", "address": "Обращение", "hashtags": "Хештеги", "cta": "Призыв"}
FIELDS = ["manner", "length", "emoji", "address", "extra", "hashtags", "cta"]


class Extra(StatesGroup):
    value = State()


async def _uid(tg_id):
    async with Session() as s:
        row = (await s.execute(text(
            "SELECT id FROM users WHERE telegram_id=:tg"), {"tg": tg_id})).first()
    return row[0] if row else None


async def _settings(uid):
    async with Session() as s:
        await s.execute(text(
            "INSERT INTO voice_settings (user_id) VALUES (:uid) "
            "ON CONFLICT (user_id) DO NOTHING"), {"uid": uid})
        row = (await s.execute(text(
            "SELECT manner,length,emoji,address,extra,hashtags,cta FROM voice_settings "
            "WHERE user_id=:uid"), {"uid": uid})).first()
        await s.commit()
    return dict(zip(FIELDS, row))


def _kb(st):
    def lbl(k):
        return f"{LABELS[k]}: {st.get(k) or 'не задано'}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=lbl("manner"), callback_data="vs:cyc:manner")],
        [InlineKeyboardButton(text=lbl("length"), callback_data="vs:cyc:length")],
        [InlineKeyboardButton(text=lbl("emoji"), callback_data="vs:cyc:emoji")],
        [InlineKeyboardButton(text=lbl("address"), callback_data="vs:cyc:address")],
        [InlineKeyboardButton(text=lbl("hashtags"), callback_data="vs:cyc:hashtags")],
        [InlineKeyboardButton(text=lbl("cta"), callback_data="vs:cyc:cta")],
        [InlineKeyboardButton(text="✏️ Доп. инструкции", callback_data="vs:extra")],
        [InlineKeyboardButton(text="🏠 Главная", callback_data="home")],
    ])


async def _show(target, uid):
    st = await _settings(uid)
    await target.answer(
        "🎙 Настройка голоса\n\n"
        "Эти параметры уточняют обученный профиль и применяются ко всем "
        "вашим аккаунтам.\n\n"
        f"Доп. инструкции: {st.get('extra') or '—'}\n\n"
        "Нажимайте кнопки, чтобы выбрать значение. «Не задано» означает, "
        "что параметр не влияет на текст.",
        reply_markup=_kb(st))


@router.callback_query(F.data == "vs:menu")
async def cb_menu(cb: CallbackQuery):
    await _show(cb.message, await _uid(cb.from_user.id))
    await cb.answer()


@router.callback_query(F.data.startswith("vs:cyc:"))
async def cb_cyc(cb: CallbackQuery):
    field = cb.data.rsplit(":", 1)[1]
    if field not in CYCLES:
        await cb.answer()
        return
    uid = await _uid(cb.from_user.id)
    st = await _settings(uid)
    cyc = CYCLES[field]
    cur = st.get(field) or ""
    nxt = cyc[(cyc.index(cur) + 1) % len(cyc)] if cur in cyc else cyc[0]
    async with Session() as s:
        await s.execute(text(
            f"UPDATE voice_settings SET {field}=:v, updated_at=now() "
            "WHERE user_id=:uid"), {"v": nxt, "uid": uid})
        await s.commit()
    st[field] = nxt
    await cb.message.edit_reply_markup(reply_markup=_kb(st))
    await cb.answer(nxt or "сброшено")


@router.callback_query(F.data == "vs:extra")
async def cb_extra(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Extra.value)
    await cb.message.answer(
        "Напишите одним сообщением: чего избегать, фирменную подпись и для кого пишем.\n\n"
        "Пример: без канцелярита, подпись «Стас на связи», аудитория — новички в заработке.\n\n"
        "Или «-» чтобы очистить. Для отмены: /cancel.")
    await cb.answer()


@router.message(Extra.value, Command("cancel"))
async def cancel_extra(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Ввод отменён. Настройки не изменены.")


@router.message(Extra.value)
async def extra_value(msg: Message, state: FSMContext):
    await state.clear()
    val = (msg.text or "").strip()
    if val == "-":
        val = ""
    uid = await _uid(msg.from_user.id)
    async with Session() as s:
        await s.execute(text(
            "UPDATE voice_settings SET extra=:v, updated_at=now() "
            "WHERE user_id=:uid"), {"v": val, "uid": uid})
        await s.commit()
    await msg.answer("Сохранено." if val else "Очищено.")
    await _show(msg, uid)
