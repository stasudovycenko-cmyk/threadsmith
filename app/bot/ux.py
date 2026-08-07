"""Shared Telegram UX formatting, navigation, errors, and callback guard."""

import asyncio
import html
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Literal

from aiogram import BaseMiddleware
from aiogram.enums import ParseMode
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


def escape_html(value: Any) -> str:
    return html.escape(str(value), quote=False)


def heading(icon: str, title: Any) -> str:
    return f"{icon} <b>{escape_html(title)}</b>"


def label_value(label: Any, value: Any) -> str:
    return f"{escape_html(label)}: <b>{escape_html(value)}</b>"


def escape_truncated(value: Any, max_escaped_chars: int) -> tuple[str, bool]:
    raw = str(value)
    escaped = escape_html(raw)
    if len(escaped) <= max_escaped_chars:
        return escaped, False
    low, high = 0, len(raw)
    while low < high:
        middle = (low + high + 1) // 2
        if len(escape_html(raw[:middle])) <= max_escaped_chars:
            low = middle
        else:
            high = middle - 1
    return escape_html(raw[:low]).rstrip(), True


def format_local_time(
    value: datetime | None,
    timezone_name: str,
    *,
    now: datetime | None = None,
) -> str:
    if value is None:
        return "ещё не запланирован"
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    zone = resolve_timezone(timezone_name)
    local_value = aware.astimezone(zone)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_now = current.astimezone(zone)
    if local_value.date() == local_now.date():
        return f"Сегодня, {local_value:%H:%M}"
    if local_value.date() == local_now.date() + timedelta(days=1):
        return f"Завтра, {local_value:%H:%M}"
    return local_value.strftime("%d.%m, %H:%M")


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
            InlineKeyboardButton(text="🤖 Автопилот", callback_data="ac:menu"),
            InlineKeyboardButton(text="✍️ Создать пост", callback_data="sc:menu"),
        ],
        [InlineKeyboardButton(text="📊 Аналитика", callback_data="an:menu")],
    ]
    if mode == "advanced":
        rows.append([
            InlineKeyboardButton(text="🔎 Radar", callback_data="rd:menu"),
            InlineKeyboardButton(text="💬 Neuro", callback_data="nc:menu"),
        ])
    rows.extend([
        [
            InlineKeyboardButton(text="👤 Аккаунты", callback_data="cab:accounts"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="ux:settings"),
        ],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="help:menu")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_keyboard(
    mode: InterfaceMode,
    account_id: int | None = None,
) -> InlineKeyboardMarkup:
    other = "advanced" if mode == "simple" else "simple"
    other_label = "Продвинутый" if other == "advanced" else "Простой"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🎙 Стиль общения", callback_data="ux:style"
        )],
        [
            InlineKeyboardButton(text="🎯 Темы", callback_data="ux:topics"),
            InlineKeyboardButton(
                text="🔑 Ключевые слова", callback_data="ux:keywords"
            ),
        ],
        [
            InlineKeyboardButton(
                text="🗓 Расписание",
                callback_data=(
                    f"ac:sched:{account_id}" if account_id else "ac:sched"
                ),
            ),
            InlineKeyboardButton(
                text="🤖 Автопилот", callback_data="ac:menu"
            ),
        ],
        [
            InlineKeyboardButton(
                text="🔔 Уведомления", callback_data="ux:notifications"
            ),
            InlineKeyboardButton(
                text="🌍 Часовой пояс",
                callback_data=(
                    f"ac:timezone:{account_id}"
                    if account_id else "ac:settings"
                ),
            ),
        ],
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
    visible_blocks = [
        data.autopilot,
        data.analytics,
        data.balance,
        data.intelligence,
    ]
    if data.interface_mode == "advanced":
        visible_blocks.extend([data.radar, data.neuro])
    warnings = sum(not block.available for block in visible_blocks)
    if data.connection_status != "connected":
        status = "Нужно переподключить Threads"
    elif warnings:
        status = "Часть данных временно недоступна. Данные обновляются"
    else:
        status = "Работает"
    auto = data.autopilot
    lines = [
        heading("🏠", "Главная"),
        "",
        f"<b>Аккаунт: @{escape_html(data.username)}</b>",
        f"Система: <b>{status}</b>",
        "",
        heading("🤖", "Автопилот"),
    ]
    if auto.available:
        lines.extend([
            f"Статус: <b>{'Работает' if auto.enabled else 'Приостановлен'}</b>",
            f"Сегодня: {auto.posts_today or 0} из {auto.daily_limit or 0} постов",
            f"📚 В очереди: <b>{auto.queue_size or 0} постов</b>",
            "🕐 Следующий пост: <b>"
            + escape_html(format_local_time(auto.next_post_at, auto.timezone))
            + "</b>",
        ])
    else:
        lines.append(auto.warning or "Статус временно недоступен.")
    decision = data.intelligence
    lines.extend(["", heading("💡", "Что рекомендует Автопилот")])
    if decision.available:
        icons = {
            "healthy": "🟢",
            "attention": "⚠️",
            "blocked": "❌",
            "waiting": "🟡",
            "insufficient_data": "📊",
        }
        lines.extend([
            f"{icons.get(decision.status or '', '🟡')} "
            f"{escape_html(decision.human_message or 'Состояние аккаунта изменилось.')}",
            f"Состояние аккаунта: <b>{decision.health_score or 0}/100</b>",
        ])
    else:
        lines.append(decision.warning or "Рекомендация пока рассчитывается.")
    lines.extend(["", heading("📊", "За 7 дней")])
    analytics_posts = data.analytics.posts_7d
    analytics_views = data.analytics.views_7d
    analytics_er = data.analytics.avg_er_7d
    if analytics_posts is None:
        analytics_posts = data.analytics.posts_30d
        analytics_views = data.analytics.views_30d
        analytics_er = data.analytics.avg_er
    if data.analytics.available and analytics_posts:
        lines.extend([
            f"Постов: <b>{format_number(analytics_posts)}</b>",
            f"Просмотров: <b>{format_number(analytics_views).capitalize()}</b>",
            f"Вовлечённость: <b>{format_percent(analytics_er).capitalize()}</b>",
        ])
        if data.analytics.brain_score is not None:
            lines.append(f"Brain Score: {data.analytics.brain_score:.0f}")
    elif data.analytics.available:
        lines.append("Пока недостаточно статистики.")
        lines.append("Для накопления данных продолжайте публикации.")
    else:
        lines.append(data.analytics.warning or "Статус временно недоступен.")
    if data.interface_mode == "advanced" and data.radar.available:
        lines.extend([
            "",
            f"Radar · Подходящих постов: {data.radar.ready_count or 0}",
        ])
    if data.balance.available:
        lines.extend([
            "",
            f"Баланс: <b>{format_number(data.balance.credits)} кредитов</b>",
        ])
    unavailable = [
        block.warning
        for block in visible_blocks
        if not block.available and block.warning
    ]
    if unavailable:
        lines.extend(["", "Данные обновляются:"])
        lines.extend(f"• {escape_html(item)}" for item in unavailable)
    return "\n".join(lines)


class UIScreenManager:
    """Best-effort process-local tracking for replaceable UI messages."""

    def __init__(self):
        self._message_ids: dict[int, int] = {}

    async def show(
        self,
        target: Any,
        text_value: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
        prefer_edit: bool = True,
    ) -> Any:
        chat_id = int(target.chat.id)
        message_id = self._message_ids.get(chat_id)
        from_user = getattr(target, "from_user", None)
        if (
            prefer_edit
            and message_id is None
            and getattr(from_user, "is_bot", False)
        ):
            message_id = getattr(target, "message_id", message_id)
        if prefer_edit and message_id is not None:
            try:
                edited = await target.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text_value,
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.HTML,
                )
                self._message_ids[chat_id] = message_id
                return edited
            except Exception as error:
                if "message is not modified" in str(error).casefold():
                    return target
                try:
                    await target.bot.delete_message(chat_id, message_id)
                except Exception:
                    pass
        sent = await target.answer(
            text_value,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
        )
        sent_id = getattr(sent, "message_id", None)
        if sent_id is not None:
            self._message_ids[chat_id] = int(sent_id)
        return sent


ui_screens = UIScreenManager()


async def show_ui_screen(
    target: Any,
    text_value: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    prefer_edit: bool = True,
) -> Any:
    return await ui_screens.show(
        target,
        text_value,
        reply_markup=reply_markup,
        prefer_edit=prefer_edit,
    )


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
