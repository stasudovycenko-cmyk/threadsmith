"""
Постоянная нижняя reply-клавиатура: Главное меню + Назад.
"""
from aiogram import F, Router
from aiogram.types import (Message, ReplyKeyboardMarkup, KeyboardButton,
                           InlineKeyboardButton, InlineKeyboardMarkup)

router = Router()

NAV_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🏠 Главное меню"),
               KeyboardButton(text="⬅️ Назад")]],
    resize_keyboard=True, is_persistent=True)


def _main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Сценарист", callback_data="sc:menu"),
         InlineKeyboardButton(text="🚀 Автопилот", callback_data="ap:menu")],
        [InlineKeyboardButton(text="🤖 Нейрокомментинг", callback_data="nc:menu")],
        [InlineKeyboardButton(text="📡 Радар", callback_data="rd:menu"),
         InlineKeyboardButton(text="🔗 Подключить Threads", callback_data="connect")],
        [InlineKeyboardButton(text="💳 Тарифы", callback_data="plans"),
         InlineKeyboardButton(text="⚡ Баланс", callback_data="balance")],
        [InlineKeyboardButton(text="📄 Документы", callback_data="docs:menu")],
    ])


@router.message(F.text.in_({"🏠 Главное меню", "⬅️ Назад"}))
async def nav_main(msg: Message):
    await msg.answer("Главное меню:", reply_markup=_main_menu_kb())
