"""
Модуль 4 в боте: вкл/выкл, режим, кэп, модерация комментов, статистика.

Кредиты: списываем при публикации коммента (approve: в момент ✅,
auto: в момент постинга через отдельный учёт в воркере не делаем -
на MVP считаем posted-комменты бесплатной генерацией, деньги в тарифном
кэпе. Проще: нейрокомментинг доступен только на платных тарифах.
"""
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from sqlalchemy import text

from app.core.crypto import decrypt_token
from app.core.db import Session
from app.core.threads_api import create_container, publish_container

log = logging.getLogger("neuro_bot")
router = Router()


class Cap(StatesGroup):
    value = State()


def neuro_kb(active: bool, mode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔴 Выключить" if active else "🟢 Включить",
            callback_data="nc:toggle")],
        [InlineKeyboardButton(
            text=f"Режим: {'премодерация ✋' if mode == 'approve' else 'авто 🤖'}",
            callback_data="nc:mode")],
        [InlineKeyboardButton(text="🎚 Кэп в день", callback_data="nc:cap"),
         InlineKeyboardButton(text="📊 Статистика", callback_data="nc:stats")],
        [InlineKeyboardButton(text="🏠 Главная", callback_data="home")],
    ])


async def _uid(tg_id: int) -> int | None:
    async with Session() as s:
        row = (await s.execute(text(
            "SELECT id FROM users WHERE telegram_id = :tg"
        ), {"tg": tg_id})).first()
    return row[0] if row else None


async def _settings(uid: int) -> tuple:
    async with Session() as s:
        await s.execute(text("""
            INSERT INTO neuro_settings (user_id) VALUES (:uid)
            ON CONFLICT (user_id) DO NOTHING
        """), {"uid": uid})
        row = (await s.execute(text("""
            SELECT active, mode, daily_cap FROM neuro_settings
            WHERE user_id = :uid
        """), {"uid": uid})).first()
        await s.commit()
    return row


@router.callback_query(F.data == "nc:menu")
async def cb_menu(cb: CallbackQuery):
    uid = await _uid(cb.from_user.id)
    async with Session() as s:
        ready = (await s.execute(text("""
            SELECT
              exists(SELECT 1 FROM voice_profiles WHERE user_id = :uid),
              exists(SELECT 1 FROM user_niches WHERE user_id = :uid),
              exists(SELECT 1 FROM threads_accounts
                     WHERE user_id = :uid AND expires_at > now()),
              coalesce((SELECT plan FROM subscriptions WHERE user_id = :uid), 'free')
        """), {"uid": uid})).first()
    has_voice, has_niche, has_acc, plan = ready

    if plan == 'free':
        await cb.message.answer(
            "Нейрокомментинг - фича платных тарифов: бот сам находит "
            "свежие посты твоей ниши и комментит их твоим голосом. "
            "Профиль растёт, пока ты спишь.\n\n/start -> Тарифы"
        )
        await cb.answer()
        return
    missing = []
    if not has_voice:
        missing.append("голос (Сценарист -> 🎙 Мой голос)")
    if not has_niche:
        missing.append("ниша (Радар -> 🎯 Моя ниша)")
    if not has_acc:
        missing.append("Threads-аккаунт (/start -> Подключить)")
    if missing:
        await cb.message.answer("Для нейрокомментинга нужно: " + ", ".join(missing))
        await cb.answer()
        return

    active, mode, cap = await _settings(uid)
    await cb.message.answer(
        f"Нейрокомментинг {'работает 🟢' if active else 'выключен ⚪'}\n"
        f"Кэп: {cap} комментов/день\n\n"
        "Бот находит свежие залётные посты ниши, пишет ценностный коммент "
        "твоим голосом. Без ссылок и рекламы - только комменты, после "
        "которых хочется зайти в профиль.\n\n"
        "Разброс по времени, максимум 1 коммент автору в день - "
        "чтобы аккаунт не улетел в теневой бан.",
        reply_markup=neuro_kb(active, mode),
    )
    await cb.answer()


@router.callback_query(F.data == "nc:toggle")
async def cb_toggle(cb: CallbackQuery):
    uid = await _uid(cb.from_user.id)
    async with Session() as s:
        row = (await s.execute(text("""
            UPDATE neuro_settings SET active = NOT active
            WHERE user_id = :uid RETURNING active, mode
        """), {"uid": uid})).first()
        await s.commit()
    active, mode = row
    await cb.message.edit_reply_markup(reply_markup=neuro_kb(active, mode))
    await cb.answer("Погнали" if active else "Стоп")


@router.callback_query(F.data == "nc:mode")
async def cb_mode(cb: CallbackQuery):
    uid = await _uid(cb.from_user.id)
    async with Session() as s:
        row = (await s.execute(text("""
            UPDATE neuro_settings
            SET mode = CASE WHEN mode = 'approve' THEN 'auto' ELSE 'approve' END
            WHERE user_id = :uid RETURNING active, mode
        """), {"uid": uid})).first()
        await s.commit()
    active, mode = row
    await cb.message.edit_reply_markup(reply_markup=neuro_kb(active, mode))
    if mode == "auto":
        await cb.message.answer(
            "⚠️ Авто-режим: комменты уходят без твоего просмотра. "
            "Аккаунт твой, риск твой. Премодерация безопаснее."
        )
    await cb.answer()


@router.callback_query(F.data == "nc:cap")
async def cb_cap(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Cap.value)
    await cb.message.answer("Сколько комментов в день? (1-30, рекомендую 10)")
    await cb.answer()


@router.message(Cap.value)
async def cap_value(msg: Message, state: FSMContext):
    try:
        cap = max(1, min(30, int((msg.text or "").strip())))
    except ValueError:
        await msg.answer("Числом, 1-30")
        return
    await state.clear()
    uid = await _uid(msg.from_user.id)
    async with Session() as s:
        await s.execute(text("""
            UPDATE neuro_settings SET daily_cap = :c WHERE user_id = :uid
        """), {"c": cap, "uid": uid})
        await s.commit()
    await msg.answer(f"Кэп: {cap}/день")


@router.callback_query(F.data == "nc:stats")
async def cb_stats(cb: CallbackQuery):
    uid = await _uid(cb.from_user.id)
    async with Session() as s:
        row = (await s.execute(text("""
            SELECT
              count(*) FILTER (WHERE status = 'posted'),
              count(*) FILTER (WHERE status = 'posted'
                               AND created_at::date = current_date),
              count(*) FILTER (WHERE status = 'rejected')
            FROM neuro_comments WHERE user_id = :uid
        """), {"uid": uid})).first()
    total, today, rejected = row
    await cb.message.answer(
        f"Комментов всего: {total}\nСегодня: {today}\nОтклонено тобой: {rejected}"
    )
    await cb.answer()


# ---------- модерация ----------

@router.callback_query(F.data.startswith("nc:ok:"))
async def cb_approve(cb: CallbackQuery):
    nc_id = int(cb.data.rsplit(":", 1)[1])
    uid = await _uid(cb.from_user.id)
    async with Session() as s:
        row = (await s.execute(text("""
            SELECT nc.target_post_id, nc.comment_text,
                   ta.threads_user_id, ta.access_token_enc
            FROM neuro_comments nc
            JOIN threads_accounts ta ON ta.user_id = nc.user_id
                 AND ta.expires_at > now()
            WHERE nc.id = :id AND nc.user_id = :uid AND nc.status = 'pending'
        """), {"id": nc_id, "uid": uid})).first()
    if not row:
        await cb.answer("Уже обработан или протух", show_alert=True)
        return
    post_id, comment, threads_uid, tok_enc = row

    await cb.answer("Постим...")
    try:
        token = decrypt_token(tok_enc)
        cont = await create_container(token, threads_uid, comment,
                                      reply_to_id=post_id)
        await publish_container(token, threads_uid, cont)
        async with Session() as s:
            await s.execute(text("""
                UPDATE neuro_comments SET status='posted', posted_at=now()
                WHERE id = :id
            """), {"id": nc_id})
            await s.commit()
        await cb.message.edit_text(cb.message.text + "\n\n✅ Опубликован")
    except Exception:
        log.exception("approve publish failed nc=%s", nc_id)
        async with Session() as s:
            await s.execute(text(
                "UPDATE neuro_comments SET status='failed' WHERE id = :id"
            ), {"id": nc_id})
            await s.commit()
        await cb.message.edit_text(cb.message.text + "\n\n❌ Публикация упала")


@router.callback_query(F.data.startswith("nc:no:"))
async def cb_reject(cb: CallbackQuery):
    nc_id = int(cb.data.rsplit(":", 1)[1])
    uid = await _uid(cb.from_user.id)
    async with Session() as s:
        await s.execute(text("""
            UPDATE neuro_comments SET status='rejected'
            WHERE id = :id AND user_id = :uid AND status = 'pending'
        """), {"id": nc_id, "uid": uid})
        await s.commit()
    await cb.message.edit_text(cb.message.text + "\n\n🗑 Отклонён")
    await cb.answer()
