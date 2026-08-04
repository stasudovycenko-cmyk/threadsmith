"""Compatibility entry point for the unified Dashboard menu."""

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, Message

from app.bot.handlers.dashboard import show_dashboard
from app.bot.ux import dashboard_keyboard

router = Router()


def main_menu_kb() -> InlineKeyboardMarkup:
    return dashboard_keyboard("advanced")


@router.message(Command("menu"))
async def cmd_menu(msg: Message):
    await show_dashboard(msg, msg.from_user.id)
