"""
Модуль 3 в боте: поставить пост в календарь, глянуть очередь,
настроить правила автоответов.

Время: юзер вводит "ДД.ММ ЧЧ:ММ" по Москве. На MVP таймзона захардкожена
МСК - ЦА русскоязычная. Поле tz у юзера - в бэклог.
"""
import logging
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from sqlalchemy import text

from app.core.db import Session

log = logging.getLogger("autopilot_bot")
router = Router()

MSK = timezone(timedelta(hours=3))


class Schedule(StatesGroup):
    body = State()
    link = State()
    when = State()


class Rule(StatesGroup):
    keyword = State()
    response = State()


def ap_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Запланировать пост", callback_data="ap:new")],
        [InlineKeyboardButton(text="📋 Очередь", callback_data="ap:queue"),
         InlineKeyboardButton(text="🤖 Автоответы", callback_data="ap:rules")],
        [InlineKeyboardButton(text="✨ Автопостинг", callback_data="ac:menu")],
        [InlineKeyboardButton(text="🏠 Главная", callback_data="home")],
    ])


async def _uid_and_acc(tg_id: int):
    async with Session() as s:
        row = (await s.execute(text("""
            SELECT u.id, ta.id FROM users u
            LEFT JOIN threads_accounts ta ON ta.user_id = u.id
            WHERE u.telegram_id = :tg
            ORDER BY ta.created_at DESC LIMIT 1
        """), {"tg": tg_id})).first()
    return (row[0], row[1]) if row else (None, None)


@router.callback_query(F.data == "ap:menu")
async def cb_menu(cb: CallbackQuery):
    await cb.message.answer("Автопилот:", reply_markup=ap_kb())
    await cb.answer()


# ---------- планирование поста ----------

@router.callback_query(F.data == "ap:new")
async def cb_new(cb: CallbackQuery, state: FSMContext):
    uid, acc = await _uid_and_acc(cb.from_user.id)
    if not acc:
        await cb.message.answer("Сначала подключи Threads: /start -> Подключить Threads")
        await cb.answer()
        return
    await state.set_state(Schedule.body)
    await cb.message.answer("Текст поста (до 500 символов):")
    await cb.answer()


@router.message(Schedule.body)
async def sch_body(msg: Message, state: FSMContext):
    body = (msg.text or "").strip()
    if len(body) > 500:
        await msg.answer(f"Длина {len(body)}, лимит Threads 500. Режь.")
        return
    await state.update_data(body=body)
    await state.set_state(Schedule.link)
    await msg.answer(
        "Ссылка? Уйдёт первым комментом с UTM, чтобы не резать охват.\n"
        "Нет ссылки - шли «-»"
    )


@router.message(Schedule.link)
async def sch_link(msg: Message, state: FSMContext):
    raw = (msg.text or "").strip()
    link = None if raw == "-" else raw
    if link and not link.startswith("http"):
        await msg.answer("Ссылка должна начинаться с http. Или «-» если без неё.")
        return
    await state.update_data(link=link)
    await state.set_state(Schedule.when)
    await msg.answer("Когда постить? Формат: ДД.ММ ЧЧ:ММ (по Москве). Или «сейчас»")


@router.message(Schedule.when)
async def sch_when(msg: Message, state: FSMContext):
    raw = (msg.text or "").strip().lower()
    now = datetime.now(MSK)
    if raw in ("сейчас", "now"):
        run_at = now
    else:
        try:
            dt = datetime.strptime(raw, "%d.%m %H:%M").replace(year=now.year, tzinfo=MSK)
            if dt < now - timedelta(minutes=5):
                dt = dt.replace(year=now.year + 1)  # 05.01 в декабре = январь следующего
            run_at = dt
        except ValueError:
            await msg.answer("Не понял. Формат: 15.07 09:30. Или «сейчас»")
            return

    data = await state.get_data()
    await state.clear()
    uid, acc = await _uid_and_acc(msg.from_user.id)

    async with Session() as s:
        await s.execute(text("""
            INSERT INTO scheduled_posts (user_id, threads_account_id, text, link, run_at)
            VALUES (:uid, :acc, :body, :link, :run)
        """), {"uid": uid, "acc": acc, "body": data["body"],
               "link": data.get("link"), "run": run_at})
        await s.commit()

    when_str = "прямо сейчас (в ближайшую минуту)" if raw in ("сейчас", "now") \
        else run_at.strftime("%d.%m %H:%M мск")
    await msg.answer(f"В очереди. Публикация: {when_str}", reply_markup=ap_kb())


# ---------- очередь ----------

@router.callback_query(F.data == "ap:queue")
async def cb_queue(cb: CallbackQuery):
    uid, _ = await _uid_and_acc(cb.from_user.id)
    async with Session() as s:
        rows = (await s.execute(text("""
            SELECT id, text, run_at, status FROM scheduled_posts
            WHERE user_id = :uid AND status IN ('pending', 'publishing')
            ORDER BY run_at LIMIT 10
        """), {"uid": uid})).all()
    if not rows:
        await cb.message.answer("Очередь пустая.")
        await cb.answer()
        return
    kb = []
    lines = []
    for n, (pid, body, run_at, status) in enumerate(rows, 1):
        preview = " ".join(body.split())[:30]
        when = run_at.astimezone(MSK).strftime("%d.%m %H:%M")
        kb.append([InlineKeyboardButton(text=when + " · " + preview,
                                        callback_data=f"ap:view:{pid}")])
    await cb.message.answer(
        "Очередь на публикацию (мск). Жми пост, чтобы открыть:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("ap:del:"))
async def cb_del(cb: CallbackQuery):
    pid = int(cb.data.rsplit(":", 1)[1])
    uid, _ = await _uid_and_acc(cb.from_user.id)
    async with Session() as s:
        # снимаем только pending - publishing уже в полёте, его не трогаем
        row = (await s.execute(text("""
            DELETE FROM scheduled_posts
            WHERE id = :pid AND user_id = :uid AND status = 'pending'
            RETURNING id
        """), {"pid": pid, "uid": uid})).first()
        await s.commit()
    await cb.answer("Снял" if row else "Уже публикуется, поздно", show_alert=not row)


# ---------- правила автоответов ----------

@router.callback_query(F.data == "ap:rules")
async def cb_rules(cb: CallbackQuery):
    uid, _ = await _uid_and_acc(cb.from_user.id)
    async with Session() as s:
        rules = (await s.execute(text("""
            SELECT id, keyword, active FROM reply_rules WHERE user_id = :uid
            ORDER BY id
        """), {"uid": uid})).all()
    kb = [[InlineKeyboardButton(text="➕ Новое правило", callback_data="ap:rule_new")]]
    for rid, kw, active in rules:
        mark = "🟢" if active else "⚪"
        kb.append([
            InlineKeyboardButton(text=f"{mark} «{kw}»", callback_data=f"ap:rule_t:{rid}"),
            InlineKeyboardButton(text="🗑", callback_data=f"ap:rule_d:{rid}"),
        ])
    await cb.message.answer(
        "Автоответы: слово в комменте -> ответ в ветке.\n"
        "(в личку Threads API не умеет - DM у них нет)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )
    await cb.answer()


@router.callback_query(F.data == "ap:rule_new")
async def cb_rule_new(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Rule.keyword)
    await cb.message.answer("Кодовое слово (например: ГАЙД):")
    await cb.answer()


@router.message(Rule.keyword)
async def rule_kw(msg: Message, state: FSMContext):
    await state.update_data(keyword=(msg.text or "").strip())
    await state.set_state(Rule.response)
    await msg.answer("Текст ответа (можно со ссылкой):")


@router.message(Rule.response)
async def rule_resp(msg: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    uid, _ = await _uid_and_acc(msg.from_user.id)
    async with Session() as s:
        await s.execute(text("""
            INSERT INTO reply_rules (user_id, keyword, response_text)
            VALUES (:uid, :kw, :resp)
        """), {"uid": uid, "kw": data["keyword"], "resp": msg.text or ""})
        await s.commit()
    await msg.answer(
        f"Правило живое: «{data['keyword']}» в комменте -> автоответ в ветке.",
        reply_markup=ap_kb(),
    )


@router.callback_query(F.data.startswith("ap:rule_t:"))
async def cb_rule_toggle(cb: CallbackQuery):
    rid = int(cb.data.rsplit(":", 1)[1])
    uid, _ = await _uid_and_acc(cb.from_user.id)
    async with Session() as s:
        await s.execute(text("""
            UPDATE reply_rules SET active = NOT active
            WHERE id = :rid AND user_id = :uid
        """), {"rid": rid, "uid": uid})
        await s.commit()
    await cb.answer("Переключил")


@router.callback_query(F.data.startswith("ap:rule_d:"))
async def cb_rule_del(cb: CallbackQuery):
    rid = int(cb.data.rsplit(":", 1)[1])
    uid, _ = await _uid_and_acc(cb.from_user.id)
    async with Session() as s:
        await s.execute(text(
            "DELETE FROM reply_rules WHERE id = :rid AND user_id = :uid"
        ), {"rid": rid, "uid": uid})
        await s.commit()
    await cb.answer("Удалил")


@router.callback_query(F.data.startswith("ap:view:"))
async def cb_view(cb: CallbackQuery):
    pid = int(cb.data.rsplit(":", 1)[1])
    uid, _ = await _uid_and_acc(cb.from_user.id)
    async with Session() as s:
        row = (await s.execute(text(
            "SELECT text, run_at, status FROM scheduled_posts "
            "WHERE id = :pid AND user_id = :uid"), {"pid": pid, "uid": uid})).first()
    if not row:
        await cb.answer("Не нашёл", show_alert=True)
        return
    body, run_at, status = row
    when = run_at.astimezone(MSK).strftime("%d.%m %H:%M мск")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Опубликовать сейчас", callback_data=f"ap:now:{pid}")],
        [InlineKeyboardButton(text="❌ Снять", callback_data=f"ap:del:{pid}")],
        [InlineKeyboardButton(text="📋 К очереди", callback_data="ap:queue")],
    ])
    head = "Пост на " + when + " · " + status + " · " + str(len(body)) + " симв."
    await cb.message.answer(head + chr(10) + chr(10) + body, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("ap:now:"))
async def cb_now(cb: CallbackQuery):
    pid = int(cb.data.rsplit(":", 1)[1])
    uid, _ = await _uid_and_acc(cb.from_user.id)
    async with Session() as s:
        row = (await s.execute(text(
            "UPDATE scheduled_posts SET run_at = now() - interval '2 minutes' "
            "WHERE id = :pid AND user_id = :uid AND status = 'pending' RETURNING id"),
            {"pid": pid, "uid": uid})).first()
        await s.commit()
    await cb.answer("Уйдёт в ближайшую минуту 🚀" if row else "Уже публикуется",
                    show_alert=True)
