"""Account-scoped, period-aware Telegram views for Analytics V2."""

from collections.abc import Mapping
from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.ux import format_number, format_percent, navigation_row
from app.core.accounts import ThreadsAccountService
from app.core.analytics.repository import AnalyticsRepository
from app.core.db import Session

router = Router()
EMPTY = (
    "Пока недостаточно статистики. Данные появятся после первых публикаций "
    "и обновления метрик Threads."
)
WEEKDAYS = (
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье",
)
PERIODS = {"7": 7, "30": 30, "90": 90, "all": None}


def _period_token(days: int | None) -> str:
    return "all" if days is None else str(days)


def _period_label(days: int | None) -> str:
    return "всё время" if days is None else f"{days} дней"


def _days(data: str, default: int | None = 30) -> int | None:
    token = data.rsplit(":", 1)[-1]
    return PERIODS.get(token, default)


def analytics_kb(days: int | None = 30) -> InlineKeyboardMarkup:
    token = _period_token(days)
    period_buttons = []
    for value, label in (("7", "7 дн."), ("30", "30 дн."), ("90", "90 дн."), ("all", "Всё")):
        selected = PERIODS[value] == days
        period_buttons.append(InlineKeyboardButton(
            text=("✓ " if selected else "") + label,
            callback_data=f"an:period:{value}",
        ))
    return InlineKeyboardMarkup(inline_keyboard=[
        period_buttons,
        [
            InlineKeyboardButton(text="🏆 Лучшие посты", callback_data=f"an:top:{token}"),
            InlineKeyboardButton(text="🧩 Темы", callback_data=f"an:dim:topic:{token}"),
        ],
        [
            InlineKeyboardButton(text="🪝 Начала постов", callback_data=f"an:dim:hook_type:{token}"),
            InlineKeyboardButton(text="📣 Призывы", callback_data=f"an:dim:cta_type:{token}"),
        ],
        [
            InlineKeyboardButton(text="🕒 Время", callback_data=f"an:time:{token}"),
            InlineKeyboardButton(text="📈 Рост", callback_data="an:growth"),
        ],
        [InlineKeyboardButton(text="🧠 Brain", callback_data=f"an:brain:{token}")],
        navigation_row("home"),
    ])


def _number(value: Any) -> str:
    return format_number(value)


def _percent(value: Any) -> str:
    return format_percent(value)


def _score(value: Any) -> str:
    return "нет данных" if value is None else f"{float(value):.0f}"


def render_overview(
    username: str,
    row: Mapping[str, Any],
    *,
    days: int | None = 30,
) -> str:
    weekday = row.get("best_weekday")
    weekday_name = (
        WEEKDAYS[int(weekday)]
        if weekday is not None and 0 <= int(weekday) < len(WEEKDAYS)
        else "пока нет данных"
    )
    hour = row.get("best_hour")
    hour_text = f"{int(hour):02d}:00" if hour is not None else "пока нет данных"
    posts = int(row.get("posts_total") or 0)
    if posts == 0:
        return (
            f"📈 Аналитика\n\nАккаунт: @{username}\n"
            f"Период: {_period_label(days)}\n\n{EMPTY}"
        )
    lines = [
        "📈 Аналитика",
        "",
        f"Аккаунт: @{username}",
        f"Период: {_period_label(days)}",
        "",
        f"Постов: {_number(posts)}",
        f"Просмотров: {_number(row.get('views_total'))}",
        f"Реакций: {_number(row.get('likes_total'))}",
        f"Средний ER: {_percent(row.get('avg_er'))}",
        f"Средние просмотры: {_number(row.get('avg_views'))}",
        "",
        "Лучший пост: "
        + str(row.get("best_post_preview") or "пока нет данных"),
        f"Лучшее время: {hour_text}",
        f"Лучший день: {weekday_name}",
        f"Лучшая тема: {row.get('best_topic') or 'пока нет данных'}",
        f"Лучшее начало: {row.get('best_hook') or 'пока нет данных'}",
        f"Лучший призыв: {row.get('best_cta') or 'пока нет данных'}",
        f"Brain Score: {_score(row.get('brain_score'))}",
    ]
    if int(row.get("profile_visits_coverage") or 0) == 0:
        lines.extend([
            "",
            "Threads пока не предоставляет данные о переходах в профиль.",
        ])
    return "\n".join(lines)


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


async def _show_overview(cb: CallbackQuery, days: int | None) -> None:
    user_id, account = await _scope(cb)
    if account is None:
        return
    async with Session() as session:
        row = await AnalyticsRepository(session).period_overview(
            user_id, account.id, days=days
        )
    await cb.message.answer(
        render_overview(account.username or str(account.id), row, days=days),
        reply_markup=analytics_kb(days),
    )
    await cb.answer()


@router.callback_query(F.data.in_({"an:menu", "an:overview"}))
async def cb_menu(cb: CallbackQuery):
    await _show_overview(cb, 30)


@router.callback_query(F.data.startswith("an:period:"))
@router.callback_query(F.data.startswith("an:overview:"))
async def cb_period(cb: CallbackQuery):
    await _show_overview(cb, _days(cb.data))


@router.callback_query(F.data == "an:top")
@router.callback_query(F.data.startswith("an:top:"))
async def cb_top(cb: CallbackQuery):
    days = _days(cb.data)
    user_id, account = await _scope(cb)
    if account is None:
        return
    async with Session() as session:
        rows = await AnalyticsRepository(session).top_posts(
            user_id, account.id, days=days
        )
    if not rows:
        message = EMPTY
    else:
        lines = [
            "🏆 Лучшие посты",
            f"Аккаунт: @{account.username or account.id}",
            f"Период: {_period_label(days)}",
        ]
        for index, row in enumerate(rows, 1):
            published = row.get("published_at")
            date_text = published.strftime("%d.%m.%Y") if published else "нет даты"
            lines.extend([
                "",
                f"{index}. {_number(row.get('current_views'))} просмотров",
                f"ER: {_percent(row.get('engagement_rate'))} · {date_text}",
                f"Тема: {row.get('topic') or 'не определена'}",
            ])
        message = "\n".join(lines)
    await cb.message.answer(message, reply_markup=analytics_kb(days))
    await cb.answer()


def render_dimension(title: str, rows: list[Mapping[str, Any]]) -> str:
    if not rows:
        return EMPTY
    lines = [title]
    for row in rows:
        lines.extend([
            "",
            str(row.get("dimension_key")),
            f"Постов: {row.get('posts_count')}",
            f"Просмотры: {_number(row.get('avg_views'))}",
            f"ER: {_percent(row.get('avg_er'))}",
            f"Brain: {_score(row.get('avg_brain_score'))}",
        ])
        if row.get("avg_ctr") is not None:
            lines.append(f"CTR: {_percent(row.get('avg_ctr'))}")
    return "\n".join(lines)


@router.callback_query(F.data.startswith("an:dim:"))
async def cb_dimension(cb: CallbackQuery):
    parts = cb.data.split(":")
    dimension = parts[2] if len(parts) > 2 else ""
    days = PERIODS.get(parts[3], 30) if len(parts) > 3 else 30
    titles = {
        "topic": "🧩 Темы",
        "hook_type": "🪝 Начала постов",
        "cta_type": "📣 Призывы",
    }
    if dimension not in titles:
        await cb.answer("Неизвестный отчёт", show_alert=True)
        return
    user_id, account = await _scope(cb)
    if account is None:
        return
    async with Session() as session:
        rows = await AnalyticsRepository(session).dimension_stats(
            user_id, account.id, dimension, days=days
        )
    message = (
        f"{titles[dimension]}\n"
        f"Аккаунт: @{account.username or account.id}\n"
        f"Период: {_period_label(days)}\n\n"
        + render_dimension("Результаты", rows)
    )
    await cb.message.answer(message, reply_markup=analytics_kb(days))
    await cb.answer()


@router.callback_query(F.data == "an:time")
@router.callback_query(F.data.startswith("an:time:"))
async def cb_time(cb: CallbackQuery):
    days = _days(cb.data)
    user_id, account = await _scope(cb)
    if account is None:
        return
    async with Session() as session:
        repository = AnalyticsRepository(session)
        hours = await repository.dimension_stats(
            user_id, account.id, "publish_hour", days=days
        )
        weekdays = await repository.dimension_stats(
            user_id, account.id, "weekday", days=days
        )
    lines = [
        "🕒 Время публикации",
        f"Аккаунт: @{account.username or account.id}",
        f"Период: {_period_label(days)}",
    ]
    for row in hours:
        lines.extend([
            "",
            f"{int(row['dimension_key']):02d}:00",
            f"Просмотры: {_number(row.get('avg_views'))} · ER: {_percent(row.get('avg_er'))}",
        ])
    if weekdays:
        lines.extend(["", "Дни недели"])
    for row in weekdays:
        index = int(row["dimension_key"])
        name = WEEKDAYS[index] if 0 <= index < len(WEEKDAYS) else str(index)
        lines.append(
            f"{name}: {_number(row.get('avg_views'))} · {_percent(row.get('avg_er'))}"
        )
    message = "\n".join(lines) if hours or weekdays else EMPTY
    await cb.message.answer(message, reply_markup=analytics_kb(days))
    await cb.answer()


@router.callback_query(F.data == "an:brain")
@router.callback_query(F.data.startswith("an:brain:"))
async def cb_brain(cb: CallbackQuery):
    days = _days(cb.data)
    user_id, account = await _scope(cb)
    if account is None:
        return
    async with Session() as session:
        overview = await AnalyticsRepository(session).period_overview(
            user_id, account.id, days=days
        )
    message = (
        "🧠 Brain Score\n\n"
        f"Аккаунт: @{account.username or account.id}\n"
        f"Период: {_period_label(days)}\n"
        f"Оценка: {_score(overview.get('brain_score'))}\n\n"
        "Brain объединяет качество темы, начала, призыва и реакций. "
        "При небольшой выборке оценка может отсутствовать."
    )
    await cb.message.answer(message, reply_markup=analytics_kb(days))
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
        lines = ["📈 История роста", f"Аккаунт: @{account.username or account.id}"]
        for row in rows:
            lines.extend([
                "",
                f"Пост {row['threads_post_id']}",
                f"30 минут: {_number(row.get('30m'))}",
                f"2 часа: {_number(row.get('2h'))}",
                f"Сутки: {_number(row.get('24h'))}",
                f"Неделя: {_number(row.get('7d'))}",
            ])
        message = "\n".join(lines)
    await cb.message.answer(message, reply_markup=analytics_kb(30))
    await cb.answer()
