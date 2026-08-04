"""Shared Telegram UX formatting, navigation, errors, and callback guard."""

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal

from aiogram import BaseMiddleware
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    TelegramObject,
)

from app.core.autopost_status import resolve_timezone
from app.schemas.ux import DashboardData, InterfaceMode

ErrorCategory = Literal[
    "threads_temporary",
    "auth_expired",
    "permission_denied",
    "account_disconnected",
    "insufficient_credits",
    "no_data",
    "already_done",
    "publish_unknown",
    "internal_temporary",
]

ERROR_TEXTS: dict[ErrorCategory, tuple[str, str, str]] = {
    "threads_temporary": (
        "Threads временно недоступен.",
        "Ваши сохранённые данные не потеряны.",
        "Попробуйте ещё раз через несколько минут.",
    ),
    "auth_expired": (
        "Авторизация Threads истекла.",
        "Публикации и настройки сохранены.",
        "Переподключите аккаунт.",
    ),
    "permission_denied": (
        "Недостаточно разрешений Threads.",
        "Сохранённые данные не изменились.",
        "Переподключите аккаунт и подтвердите нужные разрешения.",
    ),
    "account_disconnected": (
        "Threads-аккаунт отключён.",
        "История сохранена, новые действия остановлены.",
        "Переподключите аккаунт или выберите другой.",
    ),
    "insufficient_credits": (
        "Недостаточно кредитов.",
        "Операция не выполнена, данные не потеряны.",
        "Пополните баланс или выберите другой тариф.",
    ),
    "no_data": (
        "Пока недостаточно данных.",
        "Ничего не потеряно.",
        "Данные появятся после первых публикаций и обновления статистики.",
    ),
    "already_done": (
        "Действие уже выполнено.",
        "Повторная операция не запускалась.",
        "Обновите экран, чтобы увидеть актуальный статус.",
    ),
    "publish_unknown": (
        "Результат публикации не подтверждён.",
        "Threads мог принять публикацию, поэтому автоповтора не будет.",
        "Проверьте результат в приложении Threads.",
    ),
    "internal_temporary": (
        "Сервис временно не ответил.",
        "Сохранённые данные не потеряны.",
        "Попробуйте ещё раз немного позже.",
    ),
}


def format_number(value: Any) -> str:
    if value is None:
        return "нет данных"
    number = float(value)
    if abs(number) >= 1_000_000:
        compact = f"{number / 1_000_000:.1f}".replace(".", ",")
        return compact.rstrip("0").rstrip(",") + " млн"
    if abs(number) >= 1_000:
        return f"{int(round(number)):,}".replace(",", " ")
    return str(int(number))


def format_percent(value: Any) -> str:
    if value is None:
        return "нет данных"
    compact = f"{float(value) * 100:.1f}".replace(".", ",")
    return compact.rstrip("0").rstrip(",") + "%"


def format_local_time(value: datetime | None, timezone_name: str) -> str:
    if value is None:
        return "ещё не запланирован"
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(resolve_timezone(timezone_name)).strftime("%d.%m %H:%M")


def render_error(category: ErrorCategory) -> str:
    what, safety, action = ERROR_TEXTS[category]
    return f"{what}\n\n{safety}\n\nЧто делать: {action}"


def navigation_row(back_callback: str) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback),
        InlineKeyboardButton(text="🏠 Главная", callback_data="home"),
    ]


def dashboard_keyboard(
    mode: InterfaceMode,
    *,
    has_account: bool = True,
) -> InlineKeyboardMarkup:
    if not has_account:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🔗 Подключить Threads",
                callback_data="cab:connect",
            )],
            [InlineKeyboardButton(
                text="❓ Быстрый старт",
                callback_data="help:quick_start",
            )],
        ])
    rows = [
        [
            InlineKeyboardButton(text="✍️ Автопилот", callback_data="ac:menu"),
            InlineKeyboardButton(text="📈 Аналитика", callback_data="an:menu"),
        ],
    ]
    if mode == "advanced":
        rows.append([
            InlineKeyboardButton(text="🎯 Radar", callback_data="rd:menu"),
            InlineKeyboardButton(text="🧠 Neuro", callback_data="nc:menu"),
        ])
    rows.extend([
        [
            InlineKeyboardButton(text="👤 Аккаунты", callback_data="cab:accounts"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="ux:settings"),
        ],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_keyboard(mode: InterfaceMode) -> InlineKeyboardMarkup:
    other = "advanced" if mode == "simple" else "simple"
    other_label = "Продвинутый" if other == "advanced" else "Простой"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"Режим: {'Простой' if mode == 'simple' else 'Продвинутый'}",
            callback_data=f"ux:set_mode:{other}",
        )],
        [
            InlineKeyboardButton(text="📜 Активность", callback_data="activity:0"),
            InlineKeyboardButton(text="🧠 Рекомендации", callback_data="coach:menu"),
        ],
        [
            InlineKeyboardButton(text="⚡ Баланс", callback_data="balance"),
            InlineKeyboardButton(text="💳 Тариф", callback_data="plans"),
        ],
        [
            InlineKeyboardButton(text="❓ Помощь", callback_data="help:menu"),
            InlineKeyboardButton(text="📄 Документы", callback_data="docs:menu"),
        ],
        [InlineKeyboardButton(text="🏠 Главная", callback_data="home")],
    ])


def render_dashboard(data: DashboardData) -> str:
    visible_blocks = [data.autopilot, data.analytics, data.balance]
    if data.interface_mode == "advanced":
        visible_blocks.extend([data.radar, data.neuro])
    warnings = sum(not block.available for block in visible_blocks)
    status = (
        "🟢 Всё работает"
        if data.connection_status == "connected" and warnings == 0
        else "🟡 Часть данных временно недоступна"
    )
    auto = data.autopilot
    lines = [
        "🏠 ThreadFlow",
        "",
        f"Аккаунт: @{data.username}",
        f"Статус: {status}",
        "",
        "✍️ Автопилот",
    ]
    if auto.available:
        lines.extend([
            f"Сегодня: {auto.posts_today or 0} из {auto.daily_limit or 0} постов",
            f"В очереди: {auto.queue_size or 0}",
            "Следующий пост: "
            + format_local_time(auto.next_post_at, auto.timezone),
        ])
    else:
        lines.append(auto.warning or "Статус временно недоступен.")
    if data.interface_mode == "advanced":
        lines.extend(["", "🎯 Radar"])
        if data.radar.available:
            lines.append(
                f"Подходящих постов: {data.radar.ready_count or 0}"
            )
            lines.append(
                "Последний поиск: "
                + (
                    format_local_time(
                        data.radar.last_search_at,
                        auto.timezone,
                    )
                    if data.radar.last_search_at
                    else "ещё не запускался"
                )
            )
        else:
            lines.append(data.radar.warning or "Статус временно недоступен.")
        lines.extend(["", "🧠 Neuro"])
        if data.neuro.available:
            lines.extend([
                f"Сегодня опубликовано: {data.neuro.posted_today or 0}",
                f"Ждут подтверждения: {data.neuro.pending_count or 0}",
            ])
        else:
            lines.append(data.neuro.warning or "Статус временно недоступен.")
    lines.extend(["", "📈 Аналитика"])
    if data.analytics.available and data.analytics.posts_30d:
        lines.extend([
            f"Просмотры за 30 дней: {format_number(data.analytics.views_30d)}",
            f"Средний ER: {format_percent(data.analytics.avg_er)}",
            "Brain Score: "
            + (
                f"{data.analytics.brain_score:.0f}"
                if data.analytics.brain_score is not None
                else "пока недостаточно данных"
            ),
        ])
    elif data.analytics.available:
        lines.append("Пока недостаточно статистики.")
    else:
        lines.append(data.analytics.warning or "Статус временно недоступен.")
    lines.extend([
        "",
        "⚡ Баланс",
    ])
    if data.balance.available:
        lines.extend([
            f"{format_number(data.balance.credits)} кредитов",
            f"Тариф: {data.balance.plan.upper()}",
        ])
    else:
        lines.append(data.balance.warning or "Баланс временно недоступен.")
    lines.append("")
    if not auto.enabled:
        lines.append("Следующий шаг: настройте и включите Автопилот.")
    elif not data.analytics.posts_30d:
        lines.append("Следующий шаг: продолжайте публикации для аналитики.")
    else:
        lines.append("Следующий шаг: откройте Аналитику и рекомендации Brain.")
    return "\n".join(lines)


class CallbackDeduplicator:
    def __init__(self, ttl_seconds: float = 1.5):
        self.ttl_seconds = ttl_seconds
        self._claims: dict[tuple[int, str], float] = {}

    def claim(
        self,
        user_id: int,
        callback_data: str,
        *,
        now: float | None = None,
    ) -> bool:
        current = time.monotonic() if now is None else now
        self._claims = {
            claim: deadline
            for claim, deadline in self._claims.items()
            if deadline > current
        }
        key = (user_id, callback_data)
        if self._claims.get(key, 0) > current:
            return False
        self._claims[key] = current + self.ttl_seconds
        return True


class CallbackDedupMiddleware(BaseMiddleware):
    """Short process-local guard; durable actions remain DB-idempotent."""

    def __init__(self, ttl_seconds: float = 1.5):
        self.guard = CallbackDeduplicator(ttl_seconds)
        self._lock = asyncio.Lock()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, CallbackQuery):
            return await handler(event, data)
        async with self._lock:
            if not self.guard.claim(event.from_user.id, event.data or ""):
                await event.answer("Уже выполняется")
                return None
        return await handler(event, data)
