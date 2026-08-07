"""
Постоянная нижняя reply-клавиатура: Главное меню + Назад.
"""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

from app.bot.handlers.dashboard import show_dashboard

router = Router()

NAV_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🏠 Главная")]],
    resize_keyboard=True, is_persistent=True)


async def clear_active_flow(msg: Message, state: FSMContext) -> None:
    data = await state.get_data()
    chat_id = data.get("transient_chat_id")
    message_id = data.get("transient_message_id")
    if chat_id is not None and message_id is not None:
        try:
            await msg.bot.delete_message(chat_id, message_id)
        except Exception:
            pass
    await state.clear()


@router.message(Command("menu"))
@router.message(F.text.in_({"🏠 Главная", "🏠 Главное меню"}))
async def nav_main(msg: Message, state: FSMContext):
    await clear_active_flow(msg, state)
    await show_dashboard(msg, msg.from_user.id)
