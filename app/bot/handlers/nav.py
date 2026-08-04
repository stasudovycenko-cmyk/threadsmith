"""
Постоянная нижняя reply-клавиатура: Главное меню + Назад.
"""
from aiogram import F, Router
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

from app.bot.handlers.dashboard import show_dashboard

router = Router()

NAV_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🏠 Главная"),
               KeyboardButton(text="⬅️ Назад")]],
    resize_keyboard=True, is_persistent=True)


@router.message(F.text.in_({"🏠 Главная", "🏠 Главное меню", "⬅️ Назад"}))
async def nav_main(msg: Message):
    await show_dashboard(msg, msg.from_user.id)
