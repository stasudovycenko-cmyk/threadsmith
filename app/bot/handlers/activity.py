"""Paginated account-scoped activity feed."""

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.ux import (
    escape_html,
    format_local_time,
    navigation_row,
    show_ui_screen,
)
from app.core.accounts import ThreadsAccountService
from app.core.activity import ActivityFeedService
from app.core.analytics.repository import AnalyticsRepository
from app.core.db import Session

router = Router()
PAGE_SIZE = 8


def _activity_target(data: str) -> tuple[int | None, int]:
    parts = data.split(":")
    try:
        if len(parts) == 3:
            return int(parts[1]), max(0, int(parts[2]))
        return None, max(0, int(parts[-1]))
    except (TypeError, ValueError):
        return None, 0


@router.callback_query(F.data.startswith("activity:"))
async def cb_activity(cb: CallbackQuery):
    account_id, page = _activity_target(cb.data)
    async with Session() as session:
        accounts = ThreadsAccountService(session)
        user_id = await accounts.user_id_for_telegram(cb.from_user.id)
        account = (
            await (
                accounts.get_owned(user_id, account_id)
                if account_id is not None
                else accounts.selected_account(user_id)
            )
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
    prefix = f"activity:{account.id}:" if account_id is not None else "activity:"
    if page > 0:
        paging.append(InlineKeyboardButton(
            text="← Новее", callback_data=f"{prefix}{page - 1}"
        ))
    if len(events) == PAGE_SIZE:
        paging.append(InlineKeyboardButton(
            text="Старее →", callback_data=f"{prefix}{page + 1}"
        ))
    if paging:
        buttons.append(paging)
    buttons.append(navigation_row(
        f"ac:history:{account.id}"
        if account_id is not None else "ux:settings"
    ))
    rendered = escape_html("\n".join(lines)).replace(
        "📜 Активность", "📜 <b>Активность</b>", 1
    )
    await show_ui_screen(
        cb.message,
        rendered,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        prefer_edit=account_id is None,
    )
    await cb.answer()
