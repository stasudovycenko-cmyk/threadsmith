"""
Админка. Доступ только у telegram_id из ADMIN_IDS (env, через запятую).

Команды:
  /admin              — панель
  /give <tg_id> <N>   — начислить N кредитов юзеру
  /plan <tg_id> <code>— выдать тариф (free/start/pro) + его кредиты
  /user <tg_id>       — инфо о юзере
  /stats              — общая статистика

Всё через уже существующие credits.topup / текстовые запросы к БД,
чтобы не плодить новую логику.
"""
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from sqlalchemy import text

from app.core import credits
from app.core.config import PLANS, settings
from app.core.db import Session

log = logging.getLogger("admin")
router = Router()


def _is_admin(tg_id: int) -> bool:
    ids = [x.strip() for x in (settings.ADMIN_IDS or "").split(",") if x.strip()]
    return str(tg_id) in ids


async def _resolve_uid(tg_id: int) -> int | None:
    async with Session() as s:
        row = (await s.execute(text(
            "SELECT id FROM users WHERE telegram_id = :tg"
        ), {"tg": tg_id})).first()
    return row[0] if row else None


@router.message(Command("admin"))
async def cmd_admin(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    await msg.answer(
        "🛠 Админка\n\n"
        "Команды:\n"
        "/give <tg_id> <кол-во> — начислить кредиты\n"
        "/plan <tg_id> <free|start|pro> — выдать тариф\n"
        "/user <tg_id> — инфо о юзере\n"
        "/stats — статистика",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")
        ]]),
    )


@router.message(Command("give"))
async def cmd_give(msg: Message, command: CommandObject):
    if not _is_admin(msg.from_user.id):
        return
    try:
        parts = (command.args or "").split()
        target_tg, amount = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        await msg.answer("Формат: /give <tg_id> <кол-во>")
        return

    uid = await _resolve_uid(target_tg)
    if not uid:
        await msg.answer(f"Юзер {target_tg} не найден (он должен нажать /start).")
        return

    async with Session() as s:
        bal = await credits.topup(s, uid, amount, f"admin_grant_by_{msg.from_user.id}")
        await s.commit()
    await msg.answer(f"✅ Начислено {amount} кредитов юзеру {target_tg}. Баланс: {bal}")
    try:
        await msg.bot.send_message(target_tg, f"🎁 Тебе начислено {amount} кредитов.")
    except Exception:
        pass


@router.message(Command("plan"))
async def cmd_plan(msg: Message, command: CommandObject):
    if not _is_admin(msg.from_user.id):
        return
    try:
        parts = (command.args or "").split()
        target_tg, plan_code = int(parts[0]), parts[1].lower()
    except (ValueError, IndexError):
        await msg.answer("Формат: /plan <tg_id> <free|start|pro>")
        return
    if plan_code not in PLANS:
        await msg.answer(f"Нет тарифа '{plan_code}'. Доступно: {', '.join(PLANS)}")
        return

    uid = await _resolve_uid(target_tg)
    if not uid:
        await msg.answer(f"Юзер {target_tg} не найден.")
        return

    plan = PLANS[plan_code]
    async with Session() as s:
        await s.execute(text("""
            INSERT INTO subscriptions (user_id, plan, status, renews_at)
            VALUES (:uid, :plan, 'active', now() + interval '1 month')
            ON CONFLICT (user_id) DO UPDATE SET
                plan = :plan, status = 'active',
                renews_at = now() + interval '1 month'
        """), {"uid": uid, "plan": plan_code})
        if plan["credits"] > 0:
            await credits.topup(s, uid, plan["credits"], f"admin_plan_{plan_code}")
        await s.commit()
    await msg.answer(
        f"✅ Юзеру {target_tg} выдан тариф {plan['title']} "
        f"(+{plan['credits']} кредитов)."
    )
    try:
        await msg.bot.send_message(
            target_tg, f"🚀 Тебе выдан доступ: тариф {plan['title']}.")
    except Exception:
        pass


@router.message(Command("user"))
async def cmd_user(msg: Message, command: CommandObject):
    if not _is_admin(msg.from_user.id):
        return
    try:
        target_tg = int((command.args or "").strip())
    except ValueError:
        await msg.answer("Формат: /user <tg_id>")
        return
    async with Session() as s:
        row = (await s.execute(text("""
            SELECT u.id, u.credits_balance,
                   coalesce(sub.plan,'free'),
                   exists(SELECT 1 FROM voice_profiles v WHERE v.user_id=u.id),
                   exists(SELECT 1 FROM threads_accounts t
                          WHERE t.user_id=u.id AND t.expires_at>now())
            FROM users u LEFT JOIN subscriptions sub ON sub.user_id=u.id
            WHERE u.telegram_id = :tg
        """), {"tg": target_tg})).first()
    if not row:
        await msg.answer(f"Юзер {target_tg} не найден.")
        return
    uid, bal, plan, has_voice, has_threads = row
    await msg.answer(
        f"👤 Юзер {target_tg} (id={uid})\n"
        f"Тариф: {plan}\nКредитов: {bal}\n"
        f"Голос обучен: {'да' if has_voice else 'нет'}\n"
        f"Threads подключён: {'да' if has_threads else 'нет'}"
    )


@router.message(Command("stats"))
async def cmd_stats(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    await _send_stats(msg)


@router.callback_query(F.data == "adm:stats")
async def cb_stats(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer()
        return
    await _send_stats(cb.message)
    await cb.answer()


async def _send_stats(msg: Message):
    async with Session() as s:
        row = (await s.execute(text("""
            SELECT
              (SELECT count(*) FROM users),
              (SELECT count(*) FROM users WHERE created_at > now()-interval '1 day'),
              (SELECT count(*) FROM subscriptions WHERE plan<>'free' AND status='active'),
              (SELECT count(*) FROM threads_accounts WHERE expires_at>now()),
              (SELECT count(*) FROM generations WHERE created_at > now()-interval '1 day')
        """))).first()
    total, new24, paying, threads, gen24 = row
    await msg.answer(
        f"📊 Статистика\n\n"
        f"Всего юзеров: {total}\n"
        f"Новых за сутки: {new24}\n"
        f"Платящих: {paying}\n"
        f"Threads подключено: {threads}\n"
        f"Генераций за сутки: {gen24}"
    )
