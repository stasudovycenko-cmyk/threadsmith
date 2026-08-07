import asyncio
import logging

from aiogram import Bot, Dispatcher

from app.bot.handlers.admin import router as admin_router
from app.bot.handlers.activity import router as activity_router
from app.bot.handlers.analytics import router as analytics_router
from app.bot.handlers.nav import router as nav_router
from app.bot.handlers.docs import router as docs_router
from app.bot.handlers.autocontent_ui import router as autocontent_router
from app.bot.handlers.autopilot import router as autopilot_router
from app.bot.handlers.autopilot_intelligence import (
    router as autopilot_intelligence_router,
)
from app.bot.handlers.cabinet import router as cabinet_router
from app.bot.handlers.dashboard import router as dashboard_router
from app.bot.handlers.help import router as help_router
from app.bot.handlers.neuro import router as neuro_router
from app.bot.handlers.onboarding import router as onboarding_router
from app.bot.handlers.radar import router as radar_router
from app.bot.handlers.scenarist import router as scenarist_router
from app.bot.handlers.settings import router as settings_router
from app.bot.handlers.start import router as start_router
from app.bot.handlers.menu import router as menu_router
from app.bot.handlers.voice_settings import router as voice_settings_router
from app.core.config import settings
from app.bot.ux import CallbackDedupMiddleware

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)


async def main():
    bot = Bot(settings.BOT_TOKEN)
    dp = Dispatcher()
    dp.callback_query.outer_middleware(CallbackDedupMiddleware())

    # админка первой - её команды не должны перехватываться другими
    dp.include_router(admin_router)
    dp.include_router(nav_router)
    dp.include_router(docs_router)

    dp.include_router(start_router)
    dp.include_router(dashboard_router)
    dp.include_router(settings_router)
    dp.include_router(onboarding_router)
    dp.include_router(activity_router)
    dp.include_router(help_router)
    dp.include_router(cabinet_router)
    dp.include_router(analytics_router)
    dp.include_router(scenarist_router)
    dp.include_router(autopilot_router)
    dp.include_router(autopilot_intelligence_router)
    dp.include_router(autocontent_router)
    dp.include_router(radar_router)
    dp.include_router(neuro_router)
    dp.include_router(voice_settings_router)

    # Compatibility command router stays last to avoid broad command overlap.
    dp.include_router(menu_router)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
