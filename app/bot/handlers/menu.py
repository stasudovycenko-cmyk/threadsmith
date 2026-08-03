"""
Навигация. Единое главное меню + универсальная кнопка «🏠 Главная».
Роутер подключается ПОСЛЕ остальных, ловит общий callback 'home'.

В существующих меню (Сценарист, Радар, Автопилот, Нейрокомментинг)
добавь в конце ряд с кнопкой возврата:
    InlineKeyboardButton(text="🏠 Главная", callback_data="home")
"""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

router = Router()


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Сценарист", callback_data="sc:menu")],
        [InlineKeyboardButton(text="📡 Радар", callback_data="rd:menu"),
         InlineKeyboardButton(text="🚀 Автопилот", callback_data="ap:menu")],
        [InlineKeyboardButton(text="🤖 Нейрокомментинг", callback_data="nc:menu")],
        [InlineKeyboardButton(text="🔗 Подключить Threads", callback_data="connect")],
        [InlineKeyboardButton(text="👤 Личный кабинет", callback_data="cab:menu")],
        [InlineKeyboardButton(text="📈 Аналитика", callback_data="an:menu")],
        [InlineKeyboardButton(text="💳 Тарифы", callback_data="plans"),
         InlineKeyboardButton(text="⚡ Баланс", callback_data="balance")],
    ])


async def show_main(target: Message):
    await target.answer(
        "Главное меню. Выбирай раздел:",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "home")
async def cb_home(cb: CallbackQuery):
    await show_main(cb.message)
    await cb.answer()


@router.message(Command("menu"))
async def cmd_menu(msg: Message):
    await show_main(msg)
