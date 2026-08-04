"""Dashboard, interface mode, and deterministic Brain Coach screens."""

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.ux import (
    dashboard_keyboard,
    navigation_row,
    render_dashboard,
    settings_keyboard,
)
from app.core.accounts import ThreadsAccountService
from app.core.brain_coach import BrainCoachService
from app.core.config import PLANS
from app.core.dashboard import DashboardService
from app.core.db import Session
from app.core.ux import UXService
from app.schemas.ux import InterfaceMode

router = Router()


async def show_dashboard(target: Message, telegram_id: int) -> None:
    async with Session() as session:
        accounts = ThreadsAccountService(session)
        user_id = await accounts.user_id_for_telegram(telegram_id)
        if user_id is None:
            await target.answer(
                "Сначала нажмите /start.",
                reply_markup=dashboard_keyboard("simple", has_account=False),
            )
            return
        preferences = await UXService(session).preferences(user_id)
        account = await accounts.selected_account(user_id)
        if account is None:
            await session.commit()
            await target.answer(
                "🏠 ThreadFlow\n\n"
                "Threads-аккаунт пока не подключён.\n\n"
                "Подключите первый аккаунт, затем настройте его за две минуты.",
                reply_markup=dashboard_keyboard(
                    preferences.interface_mode,
                    has_account=False,
                ),
            )
            return
        dashboard = await DashboardService(session).load(
            user_id,
            account,
            mode=preferences.interface_mode,
        )
        await session.commit()
    await target.answer(
        render_dashboard(dashboard),
        reply_markup=dashboard_keyboard(preferences.interface_mode),
    )


@router.callback_query(F.data == "home")
async def cb_home(cb: CallbackQuery):
    await show_dashboard(cb.message, cb.from_user.id)
    await cb.answer()


@router.callback_query(F.data == "ux:settings")
async def cb_settings(cb: CallbackQuery):
    async with Session() as session:
        accounts = ThreadsAccountService(session)
        user_id = await accounts.user_id_for_telegram(cb.from_user.id)
        if user_id is None:
            await cb.answer("Сначала нажмите /start", show_alert=True)
            return
        preferences = await UXService(session).preferences(user_id)
        account = await accounts.selected_account(user_id)
        await session.commit()
    account_text = f"@{account.username or account.id}" if account else "не выбран"
    mode_text = "Простой" if preferences.interface_mode == "simple" else "Продвинутый"
    await cb.message.answer(
        "⚙️ Настройки\n\n"
        f"Аккаунт: {account_text}\n"
        f"Режим интерфейса: {mode_text}\n\n"
        "Режим меняет только видимость разделов. Тариф, расписание и "
        "автоматические действия не меняются.",
        reply_markup=settings_keyboard(preferences.interface_mode),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("ux:set_mode:"))
async def cb_set_mode(cb: CallbackQuery):
    mode = cb.data.rsplit(":", 1)[-1]
    if mode not in {"simple", "advanced"}:
        await cb.answer("Неизвестный режим", show_alert=True)
        return
    async with Session() as session:
        service = ThreadsAccountService(session)
        user_id = await service.user_id_for_telegram(cb.from_user.id)
        if user_id is None:
            await cb.answer("Сначала нажмите /start", show_alert=True)
            return
        preferences = await UXService(session).set_mode(
            user_id,
            mode,
        )
        await session.commit()
    label = "Простой" if preferences.interface_mode == "simple" else "Продвинутый"
    await cb.message.answer(
        f"Режим изменён: {label}.\n\n"
        "Автоматические действия и тариф не изменились.",
        reply_markup=settings_keyboard(preferences.interface_mode),
    )
    await cb.answer("Сохранено")


@router.callback_query(F.data == "coach:menu")
async def cb_coach(cb: CallbackQuery):
    async with Session() as session:
        accounts = ThreadsAccountService(session)
        user_id = await accounts.user_id_for_telegram(cb.from_user.id)
        account = (
            await accounts.selected_account(user_id)
            if user_id is not None
            else None
        )
        if account is None:
            await cb.answer("Подключите Threads-аккаунт", show_alert=True)
            return
        recommendations = await BrainCoachService(session).recommendations(
            user_id,
            account.id,
        )
        await session.commit()
    lines = [
        "🧠 Рекомендации",
        "",
        f"Аккаунт: @{account.username or account.id}",
        "",
    ]
    for item in recommendations:
        lines.extend([item.title, item.detail, ""])
    await cb.message.answer(
        "\n".join(lines).rstrip(),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            navigation_row("ux:settings")
        ]),
    )
    await cb.answer()
