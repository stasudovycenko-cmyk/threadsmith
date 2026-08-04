"""Centralized in-bot help center."""

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.help_content import HELP_TOPICS
from app.bot.ux import navigation_row

router = Router()


def help_keyboard() -> InlineKeyboardMarkup:
    labels = {
        "quick_start": "Быстрый старт",
        "autopilot": "Как работает Автопилот",
        "analytics": "Как читать Аналитику",
        "credits": "Почему списываются кредиты",
        "errors": "Частые ошибки",
    }
    rows = [
        [InlineKeyboardButton(
            text=label,
            callback_data=f"help:{key}",
        )]
        for key, label in labels.items()
    ]
    rows.extend([
        [InlineKeyboardButton(
            text="Radar, Neuro и Brain",
            callback_data="help:advanced",
        )],
        [InlineKeyboardButton(
            text="Аккаунты и данные",
            callback_data="help:data",
        )],
    ])
    rows.append(navigation_row("ux:settings"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _section_keyboard(items: tuple[tuple[str, str], ...]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        text=label,
        callback_data=f"help:{key}",
    )] for key, label in items]
    rows.append(navigation_row("help:menu"))
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "help:menu")
async def cb_help(cb: CallbackQuery):
    await cb.message.answer(
        "❓ Помощь\n\nВыберите тему:",
        reply_markup=help_keyboard(),
    )
    await cb.answer()


@router.callback_query(F.data == "help:advanced")
async def cb_help_advanced(cb: CallbackQuery):
    await cb.message.answer(
        "❓ Radar, Neuro и Brain\n\nВыберите тему:",
        reply_markup=_section_keyboard((
            ("radar", "Как работает Radar"),
            ("neuro", "Как работает Neuro"),
            ("brain", "Что такое Brain"),
            ("safe_mode", "Безопасный режим"),
        )),
    )
    await cb.answer()


@router.callback_query(F.data == "help:data")
async def cb_help_data(cb: CallbackQuery):
    await cb.message.answer(
        "❓ Аккаунты и данные\n\nВыберите тему:",
        reply_markup=_section_keyboard((
            ("disconnect", "Как отключить аккаунт"),
            ("delete_data", "Как удалить данные"),
        )),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("help:"))
async def cb_help_topic(cb: CallbackQuery):
    key = cb.data.rsplit(":", 1)[-1]
    topic = HELP_TOPICS.get(key)
    if topic is None:
        await cb.answer("Раздел не найден", show_alert=True)
        return
    title, body = topic
    await cb.message.answer(
        f"❓ {title}\n\n{body}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            navigation_row("help:menu")
        ]),
    )
    await cb.answer()
