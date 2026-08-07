"""Centralized in-bot help center."""

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.help_content import HELP_TOPICS
from app.bot.ux import escape_html, navigation_row, show_ui_screen

router = Router()


def help_keyboard() -> InlineKeyboardMarkup:
    labels = {
        "about": "Что такое ThreadFlow?",
        "quick_start": "Быстрый старт",
        "connect": "Как подключить Threads?",
        "style": "Как настроить стиль?",
        "topics": "Как добавить темы?",
        "keywords": "Как настроить ключевые слова?",
        "autopilot": "Как работает Автопилот",
        "queue": "Как работает очередь?",
        "analytics": "Как читать Аналитику",
        "errors": "Почему пост не опубликован?",
        "stop": "Как остановить Автопилот?",
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
            text="Radar и Neuro",
            callback_data="help:advanced",
        )],
        [InlineKeyboardButton(
            text="Аккаунты и данные",
            callback_data="help:data",
        )],
    ])
    rows.append(navigation_row("home"))
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
    await show_ui_screen(
        cb.message,
        "❓ <b>Помощь</b>\n\nВыберите тему:",
        reply_markup=help_keyboard(),
    )
    await cb.answer()


@router.callback_query(F.data == "help:advanced")
async def cb_help_advanced(cb: CallbackQuery):
    await show_ui_screen(
        cb.message,
        "❓ <b>Radar и Neuro</b>\n\nВыберите тему:",
        reply_markup=_section_keyboard((
            ("radar", "Как работает Radar"),
            ("neuro", "Как работает Neuro"),
            ("safe_mode", "Безопасный режим"),
        )),
    )
    await cb.answer()


@router.callback_query(F.data == "help:data")
async def cb_help_data(cb: CallbackQuery):
    await show_ui_screen(
        cb.message,
        "❓ <b>Аккаунты и данные</b>\n\nВыберите тему:",
        reply_markup=_section_keyboard((
            ("disconnect", "Как отключить аккаунт"),
            ("delete_data", "Как удалить аккаунт"),
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
    await show_ui_screen(
        cb.message,
        f"❓ <b>{escape_html(title)}</b>\n\n{escape_html(body)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            navigation_row("help:menu")
        ]),
    )
    await cb.answer()
