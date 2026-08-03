"""Account-scoped Telegram views for Analytics V2."""

from collections.abc import Mapping
from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.core.accounts import ThreadsAccountService
from app.core.analytics.repository import AnalyticsRepository
from app.core.db import Session

router = Router()
EMPTY = "Недостаточно статистики."
WEEKDAYS = (
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье",
)


def analytics_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обзор", callback_data="an:overview")],
        [InlineKeyboardButton(text="Лучшие посты", callback_data="an:top")],
        [
            InlineKeyboardButton(text="Темы", callback_data="an:dim:topic"),
            InlineKeyboardButton(text="Hook", callback_data="an:dim:hook_type"),
        ],
        [
            InlineKeyboardButton(text="CTA", callback_data="an:dim:cta_type"),
            InlineKeyboardButton(text="Время", callback_data="an:time"),
        ],
        [
            InlineKeyboardButton(text="Brain Score", callback_data="an:brain"),
            InlineKeyboardButton(text="Рост", callback_data="an:growth"),
        ],
        [InlineKeyboardButton(text="Главная", callback_data="home")],
    ])


def _number(value: Any) -> str:
    if value is None:
        return "-"
    number = float(value)
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f} млн"
    if number >= 1_000:
        return f"{number / 1_000:.1f} тыс."
    return str(int(number))


def _percent(value: Any) -> str:
    return "-" if value is None else f"{float(value) * 100:.2f}%"


def _score(value: Any) -> str:
    return "-" if value is None else f"{float(value):.0f}"


def render_overview(username: str, row: Mapping[str, Any]) -> str:
    weekday = row.get("best_weekday")
    weekday_name = (
        WEEKDAYS[int(weekday)]
        if weekday is not None and 0 <= int(weekday) < len(WEEKDAYS)
        else "-"
    )
    hour = row.get("best_hour")
    hour_text = f"{int(hour):02d}:00" if hour is not None else "-"
    return (
        f"Аналитика @{username}\n\n"
        f"Постов: {_number(row.get('posts_total'))}\n"
        f"Просмотров: {_number(row.get('views_total'))}\n"
        f"ER: {_percent(row.get('avg_er'))}\n"
        f"Brain: {_score(row.get('brain_score'))}\n"
        f"Лучшее время: {hour_text}\n"
        f"Лучший день: {weekday_name}\n"
        f"Лучший Hook: {row.get('best_hook') or '-'}\n"
        f"Лучший CTA: {row.get('best_cta') or '-'}\n"
        f"Лучшая тема: {row.get('best_topic') or '-'}"
    )


async def _scope(cb: CallbackQuery):
    async with Session() as session:
        accounts = ThreadsAccountService(session)
        user_id = await accounts.user_id_for_telegram(cb.from_user.id)
        account = (
            await accounts.selected_account(user_id)
            if user_id is not None
            else None
        )
        await session.commit()
    if account is None:
        await cb.answer("Подключите Threads-аккаунт", show_alert=True)
    return user_id, account


@router.callback_query(F.data == "an:menu")
async def cb_menu(cb: CallbackQuery):
    user_id, account = await _scope(cb)
    if account is None:
        return
    async with Session() as session:
        row = await AnalyticsRepository(session).overview(user_id, account.id)
    if row is None:
        message = f"Аналитика @{account.username or account.id}\n\n{EMPTY}"
    else:
        message = render_overview(account.username or str(account.id), row)
    await cb.message.answer(message, reply_markup=analytics_kb())
    await cb.answer()


@router.callback_query(F.data == "an:overview")
async def cb_overview(cb: CallbackQuery):
    await cb_menu(cb)


@router.callback_query(F.data == "an:top")
async def cb_top(cb: CallbackQuery):
    user_id, account = await _scope(cb)
    if account is None:
        return
    async with Session() as session:
        rows = await AnalyticsRepository(session).top_posts(
            user_id, account.id
        )
    if not rows:
        await cb.message.answer(EMPTY, reply_markup=analytics_kb())
    else:
        lines = [f"Лучшие посты @{account.username or account.id}"]
        for index, row in enumerate(rows, 1):
            published = row.get("published_at")
            date_text = published.strftime("%d.%m.%Y") if published else "-"
            lines.append(
                f"{index}. {_number(row.get('current_views'))} просмотров | "
                f"ER {_percent(row.get('engagement_rate'))} | {date_text}\n"
                f"Тема: {row.get('topic') or '-'}"
            )
        await cb.message.answer("\n\n".join(lines), reply_markup=analytics_kb())
    await cb.answer()


def render_dimension(title: str, rows: list[Mapping[str, Any]]) -> str:
    if not rows:
        return EMPTY
    lines = [title]
    for row in rows:
        ctr = row.get("avg_ctr")
        suffix = f" | CTR {_percent(ctr)}" if ctr is not None else ""
        lines.append(
            f"{row.get('dimension_key')}: {row.get('posts_count')} пост. | "
            f"{_number(row.get('avg_views'))} views | "
            f"ER {_percent(row.get('avg_er'))} | "
            f"Brain {_score(row.get('avg_brain_score'))}{suffix}"
        )
    return "\n\n".join(lines)


@router.callback_query(F.data.startswith("an:dim:"))
async def cb_dimension(cb: CallbackQuery):
    dimension = cb.data.rsplit(":", 1)[-1]
    titles = {
        "topic": "Темы",
        "hook_type": "Hook",
        "cta_type": "CTA",
    }
    if dimension not in titles:
        await cb.answer("Неизвестный отчёт", show_alert=True)
        return
    user_id, account = await _scope(cb)
    if account is None:
        return
    async with Session() as session:
        rows = await AnalyticsRepository(session).dimension_stats(
            user_id, account.id, dimension
        )
    await cb.message.answer(
        render_dimension(titles[dimension], rows),
        reply_markup=analytics_kb(),
    )
    await cb.answer()


@router.callback_query(F.data == "an:time")
async def cb_time(cb: CallbackQuery):
    user_id, account = await _scope(cb)
    if account is None:
        return
    async with Session() as session:
        repository = AnalyticsRepository(session)
        hours = await repository.dimension_stats(
            user_id, account.id, "publish_hour"
        )
        weekdays = await repository.dimension_stats(
            user_id, account.id, "weekday"
        )
    lines = ["Время публикации"]
    for row in hours:
        lines.append(
            f"{int(row['dimension_key']):02d}:00 | "
            f"{_number(row.get('avg_views'))} views | "
            f"ER {_percent(row.get('avg_er'))}"
        )
    if weekdays:
        lines.append("\nДни недели")
    for row in weekdays:
        index = int(row["dimension_key"])
        name = WEEKDAYS[index] if 0 <= index < len(WEEKDAYS) else str(index)
        lines.append(
            f"{name} | {_number(row.get('avg_views'))} views | "
            f"ER {_percent(row.get('avg_er'))}"
        )
    await cb.message.answer(
        "\n".join(lines) if hours or weekdays else EMPTY,
        reply_markup=analytics_kb(),
    )
    await cb.answer()


@router.callback_query(F.data == "an:brain")
async def cb_brain(cb: CallbackQuery):
    user_id, account = await _scope(cb)
    if account is None:
        return
    async with Session() as session:
        overview = await AnalyticsRepository(session).overview(
            user_id, account.id
        )
    message = (
        f"Brain Score @{account.username or account.id}: "
        f"{_score(overview.get('brain_score'))}"
        if overview
        else EMPTY
    )
    await cb.message.answer(message, reply_markup=analytics_kb())
    await cb.answer()


@router.callback_query(F.data == "an:growth")
async def cb_growth(cb: CallbackQuery):
    user_id, account = await _scope(cb)
    if account is None:
        return
    async with Session() as session:
        rows = await AnalyticsRepository(session).growth_history(
            user_id, account.id
        )
    if not rows:
        message = EMPTY
    else:
        lines = ["История роста"]
        for row in rows:
            lines.append(
                f"Пост {row['threads_post_id']}\n"
                f"30 мин: {_number(row.get('30m'))} | "
                f"2 часа: {_number(row.get('2h'))} | "
                f"сутки: {_number(row.get('24h'))} | "
                f"неделя: {_number(row.get('7d'))}"
            )
        message = "\n\n".join(lines)
    await cb.message.answer(message, reply_markup=analytics_kb())
    await cb.answer()
