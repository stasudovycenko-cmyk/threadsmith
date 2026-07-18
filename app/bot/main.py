import asyncio
import logging

from aiogram import Bot, Dispatcher

from app.bot.handlers.autopilot import router as autopilot_router
from app.bot.handlers.neuro import router as neuro_router
from app.bot.handlers.radar import router as radar_router
from app.bot.handlers.scenarist import router as scenarist_router
from app.bot.handlers.start import router as start_router
from app.core.config import settings

logging.basicConfig(level=logging.INFO)


async def main():
    bot = Bot(settings.BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(start_router)
    dp.include_router(scenarist_router)
    dp.include_router(autopilot_router)
    dp.include_router(radar_router)
    dp.include_router(neuro_router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
