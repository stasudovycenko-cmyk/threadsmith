"""
Основные хендлеры: /start (регистрация + рефералка), баланс,
подключение Threads, тарифы и оплата.
"""
from aiogram import F, Router
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from sqlalchemy import text

from app.core.accounts import ThreadsAccountService
from app.core.config import PLANS
from app.core.db import Session
from app.core.robokassa import payment_link
from app.core.threads_api import auth_link
from app.bot.handlers.nav import NAV_KB
from app.bot.handlers.dashboard import show_dashboard
from app.bot.ux import dashboard_keyboard, navigation_row

router = Router()


def main_kb() -> InlineKeyboardMarkup:
    return dashboard_keyboard("advanced")


@router.message(CommandStart())
async def cmd_start(msg: Message, command: CommandObject):
    async with Session() as s:
        # рефералка: /start ref_123 -> referred_by = 123 (telegram_id пригласившего)
        ref = None
        if command.args and command.args.startswith("ref_"):
            try:
                ref_tg = int(command.args[4:])
                row = (await s.execute(text(
                    "SELECT id FROM users WHERE telegram_id = :tg"
                ), {"tg": ref_tg})).first()
                ref = row[0] if row else None
            except ValueError:
                pass

        # идемпотентная регистрация + разовый free-бонус новичку
        row = (await s.execute(text("""
            INSERT INTO users (telegram_id, referred_by)
            VALUES (:tg, :ref)
            ON CONFLICT (telegram_id) DO NOTHING
            RETURNING id
        """), {"tg": msg.from_user.id, "ref": ref})).first()

        if row:  # новый юзер
            uid = row[0]
            free = PLANS["free"]["credits"]
            await s.execute(text("""
                UPDATE users SET credits_balance = :c WHERE id = :uid
            """), {"c": free, "uid": uid})
            await s.execute(text("""
                INSERT INTO credits_ledger (user_id, delta, reason)
                VALUES (:uid, :c, 'free_signup')
            """), {"uid": uid, "c": free})
            await s.execute(text("""
                INSERT INTO subscriptions (user_id, plan) VALUES (:uid, 'free')
                ON CONFLICT (user_id) DO NOTHING
            """), {"uid": uid})
        await s.commit()

    await msg.answer(
        "ThreadFlow готов. Главная навигация всегда доступна снизу.",
        reply_markup=NAV_KB,
    )
    await show_dashboard(msg, msg.from_user.id)


@router.callback_query(F.data == "balance")
async def cb_balance(cb: CallbackQuery):
    async with Session() as s:
        row = (await s.execute(text("""
            SELECT u.credits_balance, sub.plan
            FROM users u LEFT JOIN subscriptions sub ON sub.user_id = u.id
            WHERE u.telegram_id = :tg
        """), {"tg": cb.from_user.id})).first()
    if row is None:
        await cb.answer("Сначала нажмите /start", show_alert=True)
        return
    bal, plan = row[0], row[1]
    await cb.message.answer(
        f"Тариф: {PLANS.get(plan, PLANS['free'])['title']}\n"
        f"Кредитов: {bal}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            navigation_row("ux:settings")
        ]),
    )
    await cb.answer()


@router.callback_query(F.data == "connect")
async def cb_connect(cb: CallbackQuery):
    async with Session() as s:
        service = ThreadsAccountService(s)
        user_id = await service.user_id_for_telegram(cb.from_user.id)
        if user_id is None:
            await cb.answer("Сначала нажмите /start", show_alert=True)
            return
        oauth = await service.create_oauth_state(user_id, action="connect")
        await s.commit()

    await cb.message.answer(
        "Подключение Threads-аккаунта. Войдите в нужный аккаунт и "
        "подтвердите доступ:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Подключить", url=auth_link(oauth.state))
        ]]),
    )
    await cb.answer()


@router.callback_query(F.data == "plans")
async def cb_plans(cb: CallbackQuery):
    kb = [
        [InlineKeyboardButton(
            text=f"{p['title']} - {p['price']}₽/мес · {p['credits']} кредитов",
            callback_data=f"buy:{code}",
        )]
        for code, p in PLANS.items() if p["price"] > 0
    ]
    kb.append(navigation_row("ux:settings"))
    await cb.message.answer(
        "Тарифы. Кредиты зачисляются сразу после оплаты:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(cb: CallbackQuery):
    plan_code = cb.data.split(":", 1)[1]
    plan = PLANS.get(plan_code)
    if not plan or plan["price"] == 0:
        await cb.answer("Нет такого тарифа", show_alert=True)
        return

    async with Session() as s:
        row = (await s.execute(text(
            "SELECT id FROM users WHERE telegram_id = :tg"
        ), {"tg": cb.from_user.id})).first()
        if not row:
            await cb.answer("Сначала нажмите /start", show_alert=True)
            return
        uid = row[0]
        inv = (await s.execute(text("""
            INSERT INTO payments (user_id, plan, amount, provider)
            VALUES (:uid, :plan, :amount, 'robokassa')
            RETURNING inv_id
        """), {"uid": uid, "plan": plan_code, "amount": plan["price"]})).first()
        await s.commit()

    link = payment_link(
        inv_id=inv[0], amount=float(plan["price"]),
        description=f"Тариф {plan['title']}, 1 месяц",
        user_id=uid, plan=plan_code,
    )
    await cb.message.answer(
        f"Оплата тарифа {plan['title']} - {plan['price']}₽.\n"
        "После оплаты кредиты зачислятся автоматически, а бот пришлёт "
        "подтверждение:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Оплатить", url=link)],
            navigation_row("plans"),
        ]),
    )
    await cb.answer()
