"""Dashboard, interface mode, and deterministic Brain Coach screens."""

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.ux import (
    dashboard_keyboard,
    escape_html,
    navigation_row,
    render_dashboard,
    settings_keyboard,
    show_ui_screen,
)
from app.core.accounts import ThreadsAccountService
from app.core.brain_coach import BrainCoachService
from app.core.config import PLANS
from app.core.dashboard import DashboardService
from app.core.db import Session
from app.core.ux import UXService
from app.schemas.ux import InterfaceMode

router = Router()


async def show_dashboard(
    target: Message,
    telegram_id: int,
    *,
    prefer_edit: bool = True,
) -> None:
    async with Session() as session:
        accounts = ThreadsAccountService(session)
        user_id = await accounts.user_id_for_telegram(telegram_id)
        if user_id is None:
            await show_ui_screen(
                target,
                "🏠 <b>Главная</b>\n\nСначала нажмите /start.",
                reply_markup=dashboard_keyboard("simple", has_account=False),
                prefer_edit=prefer_edit,
            )
            return
        preferences = await UXService(session).preferences(user_id)
        account = await accounts.selected_account(user_id)
        if account is None:
            await session.commit()
            await show_ui_screen(
                target,
                "🏠 <b>Главная</b>\n\n"
                "Threads-аккаунт пока не подключён.\n\n"
                "Подключите первый аккаунт, затем настройте его за две минуты.",
                reply_markup=dashboard_keyboard(
                    preferences.interface_mode,
                    has_account=False,
                ),
                prefer_edit=prefer_edit,
            )
            return
        dashboard = await DashboardService(session).load(
            user_id,
            account,
            mode=preferences.interface_mode,
        )
        await session.commit()
    await show_ui_screen(
        target,
        render_dashboard(dashboard),
        reply_markup=dashboard_keyboard(preferences.interface_mode),
        prefer_edit=prefer_edit,
    )


@router.callback_query(F.data == "home")
async def cb_home(cb: CallbackQuery):
    await show_dashboard(cb.message, cb.from_user.id, prefer_edit=True)
    await cb.answer()


@router.callback_query(F.data == "ux:settings")
async def cb_settings(cb: CallbackQuery):
    await show_settings(cb.message, cb.from_user.id)
    await cb.answer()


async def show_settings(target: Message, telegram_id: int) -> None:
    async with Session() as session:
        accounts = ThreadsAccountService(session)
        user_id = await accounts.user_id_for_telegram(telegram_id)
        if user_id is None:
            await show_ui_screen(
                target,
                "⚙️ <b>Настройки аккаунта</b>\n\nСначала нажмите /start.",
                reply_markup=dashboard_keyboard("simple", has_account=False),
            )
            return
        preferences = await UXService(session).preferences(user_id)
        account = await accounts.selected_account(user_id)
        await session.commit()
    account_text = (
        f"@{escape_html(account.username or account.id)}"
        if account else "не выбран"
    )
    mode_text = "Простой" if preferences.interface_mode == "simple" else "Продвинутый"
    await show_ui_screen(
        target,
        "⚙️ <b>Настройки аккаунта</b>\n\n"
        f"<b>Аккаунт: {account_text}</b>\n"
        f"Режим интерфейса: <b>{mode_text}</b>\n\n"
        "Режим меняет только видимость разделов. Тариф, расписание и "
        "автоматические действия не меняются.",
        reply_markup=settings_keyboard(
            preferences.interface_mode,
            account.id if account else None,
        ),
    )


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
        account = await service.selected_account(user_id)
        await session.commit()
    label = "Простой" if preferences.interface_mode == "simple" else "Продвинутый"
    await show_ui_screen(
        cb.message,
        f"⚙️ <b>Настройки аккаунта</b>\n\n"
        f"Режим интерфейса: <b>{label}</b>\n\n"
        "Автоматические действия и тариф не изменились.",
        reply_markup=settings_keyboard(
            preferences.interface_mode,
            account.id if account else None,
        ),
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
