"""
Модуль 1 в боте: задать нишу, топ ниши с разборами, метрики своих постов.

Кредитная логика:
- radar_search (8 кр.) - живой поиск API + библиотека, выдача топа
- razbor (3 кр.) - LLM-разбор конкретного поста по кнопке
- "Мои посты" - бесплатно: свои метрики это retention, деньги на генерации
"""
import json
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from sqlalchemy import text

from app.core import credits, radar
from app.core.accounts import (
    ThreadsAccountService,
    authorization_status,
)
from app.core.ai_cost import AIUsageContext
from app.core.config import CREDIT_COSTS
from app.core.crypto import decrypt_token
from app.core.db import Session
from app.core.llm import LLMError, LLMGuardError

log = logging.getLogger("radar_bot")
router = Router()


class Niche(StatesGroup):
    setup = State()


def radar_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Топ ниши", callback_data="rd:top")],
        [InlineKeyboardButton(text="🎯 Моя ниша", callback_data="rd:niche"),
         InlineKeyboardButton(text="📈 Мои посты", callback_data="rd:my")],
        [InlineKeyboardButton(text="🏠 Главная", callback_data="home")],
    ])


async def _uid_acc(tg_id: int):
    async with Session() as s:
        service = ThreadsAccountService(s)
        user_id = await service.user_id_for_telegram(tg_id)
        account = (
            await service.selected_credentials(user_id)
            if user_id is not None
            else None
        )
        await s.commit()
    if account is None:
        return user_id, None, None
    if authorization_status(account) == "EXPIRED":
        return user_id, account.id, None
    return user_id, account.id, account.access_token_enc


@router.callback_query(F.data == "rd:menu")
async def cb_menu(cb: CallbackQuery):
    await cb.message.answer(
        "Радар. Что залетает в нише - и почему.\n"
        "(метрики чужих постов Threads API не отдаёт - ранжирую по их "
        "TOP-выдаче и свежести)",
        reply_markup=radar_kb(),
    )
    await cb.answer()


# ---------- ниша ----------

@router.callback_query(F.data == "rd:niche")
async def cb_niche(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Niche.setup)
    await cb.message.answer(
        "Ниша и ключевики одной строкой через запятую.\n"
        "Первое слово-фраза = название ниши, остальное = ключи для поиска.\n\n"
        "Пример: трафик, продвижение threads, органика без бюджета"
    )
    await cb.answer()


@router.message(Niche.setup)
async def niche_setup(msg: Message, state: FSMContext):
    await state.clear()
    parts = [p.strip() for p in (msg.text or "").split(",") if p.strip()]
    if not parts:
        await msg.answer("Пусто. Формат: ниша, ключ1, ключ2")
        return
    niche, keywords = parts[0], parts[1:] or [parts[0]]
    uid, _, _ = await _uid_acc(msg.from_user.id)
    async with Session() as s:
        await s.execute(text("""
            INSERT INTO user_niches (user_id, niche, keywords)
            VALUES (:uid, :n, :kw)
            ON CONFLICT (user_id) DO UPDATE SET niche = :n, keywords = :kw
        """), {"uid": uid, "n": niche, "kw": keywords})
        await s.commit()
    await msg.answer(
        f"Ниша: {niche}\nКлючи: {', '.join(keywords)}\n\n"
        "Краулер начнёт копить посты по ней в фоне. «Топ ниши» - живой поиск сразу.",
        reply_markup=radar_kb(),
    )


# ---------- топ ниши ----------

@router.callback_query(F.data == "rd:top")
async def cb_top(cb: CallbackQuery):
    uid, acc_id, tok_enc = await _uid_acc(cb.from_user.id)
    if not acc_id:
        await cb.message.answer("Подключи Threads: /start -> Подключить Threads")
        await cb.answer()
        return
    if tok_enc is None:
        await cb.message.answer(
            "Авторизация Threads истекла. Переподключи аккаунт в личном "
            "кабинете."
        )
        await cb.answer()
        return

    async with Session() as s:
        nrow = (await s.execute(text(
            "SELECT niche, keywords FROM user_niches WHERE user_id = :uid"
        ), {"uid": uid})).first()
    if not nrow:
        await cb.message.answer("Сначала задай нишу - «🎯 Моя ниша»")
        await cb.answer()
        return
    niche, keywords = nrow

    cost = CREDIT_COSTS["radar_search"]
    async with Session() as s:
        try:
            await credits.spend(s, uid, cost, "radar_search")
            await s.commit()
        except credits.NotEnoughCredits:
            await cb.message.answer("Не хватает кредитов. /start -> Тарифы")
            await cb.answer()
            return

    await cb.answer("Ищу...")
    token = decrypt_token(tok_enc)
    try:
        async with Session() as s:
            for kw in keywords[:2]:  # 2 живых запроса, остальное из библиотеки
                await radar.search_and_store(s, token, acc_id, niche, kw)
            await s.commit()
    except Exception:
        log.exception("live search failed uid=%s", uid)
        # библиотека всё равно есть - не возвращаем кредиты, выдача будет

    async with Session() as s:
        posts = await radar.top_posts(s, niche)

    if not posts:
        async with Session() as s:
            await credits.topup(s, uid, cost, "refund_radar_empty")
            await s.commit()
        await cb.message.answer("Пусто по этой нише. Кредиты вернул. Попробуй другие ключи.")
        return

    for pid, author, body, score, permalink in posts:
        preview = body[:300] + ("..." if len(body) > 300 else "")
        await cb.message.answer(
            f"@{author} · 🔥 {score}\n\n{preview}\n\n{permalink or ''}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=f"🔍 Разбор ({CREDIT_COSTS['razbor']} кр.)",
                                     callback_data=f"rd:rz:{pid}")
            ]]),
        )


# ---------- разбор ----------

@router.callback_query(F.data.startswith("rd:rz:"))
async def cb_razbor(cb: CallbackQuery):
    pid = cb.data.rsplit(":", 1)[1]
    uid, acc_id, _ = await _uid_acc(cb.from_user.id)
    cost = CREDIT_COSTS["razbor"]

    async with Session() as s:
        try:
            await credits.spend(s, uid, cost, "razbor")
            await s.commit()
        except credits.NotEnoughCredits:
            await cb.answer("Не хватает кредитов", show_alert=True)
            return

    await cb.answer("Разбираю...")
    try:
        async with Session() as s:
            r = await radar.razbor(
                s,
                pid,
                usage_context=AIUsageContext(
                    user_id=uid,
                    threads_account_id=acc_id,
                ),
            )
            await s.commit()
    except LLMGuardError:
        async with Session() as s:
            await credits.topup(s, uid, cost, "refund_razbor_ai_guard")
            await s.commit()
        await cb.message.answer(
            "AI-разбор временно остановлен защитным лимитом. "
            "Кредиты вернул."
        )
        return
    except (LLMError, ValueError):
        async with Session() as s:
            await credits.topup(s, uid, cost, "refund_razbor")
            await s.commit()
        await cb.message.answer("Разбор упал, кредиты вернул.")
        return

    await cb.message.answer(
        f"Хук [{r.get('hook_type')}]: {r.get('hook')}\n\n"
        f"Структура: {r.get('structure')}\n\n"
        f"Триггер: {r.get('trigger')}\n\n"
        f"Концовка: {r.get('ending')}\n\n"
        f"💡 Как повторить: {r.get('how_to_repeat')}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✍️ Написать по этой механике",
                                 callback_data="sc:gen")
        ]]),
    )


# ---------- мои посты ----------

@router.callback_query(F.data == "rd:my")
async def cb_my(cb: CallbackQuery):
    uid, account_id, _ = await _uid_acc(cb.from_user.id)
    if account_id is None:
        await cb.message.answer("Threads-аккаунт не подключён.")
        await cb.answer()
        return
    async with Session() as s:
        rows = (await s.execute(text("""
            SELECT sp.text, isnap.metrics_json
            FROM scheduled_posts sp
            JOIN LATERAL (
                SELECT metrics_json FROM insights_snapshots i
                WHERE i.threads_post_id = sp.threads_post_id
                ORDER BY snapshot_date DESC LIMIT 1
            ) isnap ON true
            WHERE sp.user_id = :uid
              AND sp.threads_account_id = :account_id
              AND sp.status = 'done'
            ORDER BY sp.run_at DESC LIMIT 5
        """), {"uid": uid, "account_id": account_id})).all()

    if not rows:
        await cb.message.answer(
            "Пока нет данных. Метрики появляются через сутки после "
            "публикации через Автопилот."
        )
        await cb.answer()
        return

    lines = ["Твои последние посты:\n"]
    for body, mj in rows:
        m = mj if isinstance(mj, dict) else json.loads(mj)
        lines.append(
            f"«{body[:50]}...»\n"
            f"👁 {m.get('views', 0)} · ❤️ {m.get('likes', 0)} · "
            f"💬 {m.get('replies', 0)} · 🔁 {m.get('reposts', 0)}\n"
        )
    await cb.message.answer("\n".join(lines))
    await cb.answer()
