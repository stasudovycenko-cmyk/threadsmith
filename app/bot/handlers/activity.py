"""Paginated account-scoped activity feed."""

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.ux import format_local_time, navigation_row
from app.core.accounts import ThreadsAccountService
from app.core.activity import ActivityFeedService
from app.core.analytics.repository import AnalyticsRepository
from app.core.db import Session

router = Router()
PAGE_SIZE = 8


@router.callback_query(F.data.startswith("activity:"))
async def cb_activity(cb: CallbackQuery):
    try:
        page = max(0, int(cb.data.rsplit(":", 1)[-1]))
    except (TypeError, ValueError):
        page = 0
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
        events = await ActivityFeedService(session).list_events(
            user_id,
            account.id,
            page=page,
            page_size=PAGE_SIZE,
        )
        timezone_name = await AnalyticsRepository(session).account_timezone(
            user_id, account.id
        )
        await session.commit()
    lines = ["📜 Активность", "", f"Аккаунт: @{account.username or account.id}"]
    if not events:
        lines.extend([
            "",
            "Событий на этой странице пока нет.",
            "Они появятся после публикаций, поиска Radar и обновления аналитики.",
        ])
    for item in events:
        lines.extend([
            "",
            f"{format_local_time(item.occurred_at, timezone_name)} · {item.title}",
        ])
        if item.detail:
            lines.append(item.detail)
    buttons = []
    paging = []
    if page > 0:
        paging.append(InlineKeyboardButton(
            text="← Новее", callback_data=f"activity:{page - 1}"
        ))
    if len(events) == PAGE_SIZE:
        paging.append(InlineKeyboardButton(
            text="Старее →", callback_data=f"activity:{page + 1}"
        ))
    if paging:
        buttons.append(paging)
    buttons.append(navigation_row("ux:settings"))
    await cb.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await cb.answer()
