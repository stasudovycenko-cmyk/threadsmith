"""Telegram HTML rendering for publication events."""

from datetime import datetime, timezone

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.ux import escape_html, escape_truncated
from app.core.autopost_status import ensure_aware, resolve_timezone
from app.schemas.notifications import PublicationNotification

_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def format_publication_time(
    value: datetime,
    timezone_name: str,
    *,
    now: datetime | None = None,
) -> str:
    tz = resolve_timezone(timezone_name)
    local_value = ensure_aware(value).astimezone(tz)
    local_now = ensure_aware(now or datetime.now(timezone.utc)).astimezone(tz)
    if local_value.date() == local_now.date():
        return f"Сегодня, {local_value:%H:%M}"
    return (
        f"{local_value.day} {_MONTHS[local_value.month - 1]}, "
        f"{local_value:%H:%M}"
    )


def render_publication_notification(
    notification: PublicationNotification,
    *,
    now: datetime | None = None,
) -> str:
    account = escape_html(notification.username)
    shown_text, shortened = escape_truncated(notification.text, 2800)
    when = escape_html(format_publication_time(
        notification.published_at,
        notification.timezone,
        now=now,
    ))
    if notification.outcome == "success":
        lines = [
            "✅ <b>Пост опубликован</b>",
            "",
            f"Аккаунт: <b>@{account}</b>",
            f"Время: <b>{when}</b>",
            "",
            "<b>Текст поста</b>",
            "",
            shown_text,
        ]
        if shortened:
            lines.extend(["", "Текст сокращён в уведомлении."])
        if notification.source:
            icon = {
                "Автопилот": "🤖",
                "Вручную": "✍️",
                "Повторная публикация": "🔁",
            }.get(notification.source, "")
            lines.extend([
                "",
                f"{icon} Источник: <b>{escape_html(notification.source)}</b>".strip(),
            ])
        return "\n".join(lines)
    if notification.outcome == "unknown":
        lines = [
            "⚠️ <b>Нужно проверить публикацию</b>",
            "",
            "ThreadFlow отправил пост, но не получил надёжного подтверждения.",
            "Чтобы избежать дубля, <b>мы не будем автоматически отправлять его повторно</b>.",
            "",
            f"Аккаунт: <b>@{account}</b>",
            "",
            "<b>Текст</b>",
            "",
            shown_text,
        ]
        if shortened:
            lines.extend(["", "Текст сокращён в уведомлении."])
        return "\n".join(lines)
    explanation = escape_html(
        notification.safe_error_message
        or "Threads не подтвердил публикацию."
    )
    lines = [
        "❌ <b>Не удалось опубликовать пост</b>",
        "",
        f"Аккаунт: <b>@{account}</b>",
        "",
        "<b>Пост</b>",
        "",
        shown_text,
    ]
    if shortened:
        lines.extend(["", "Текст сокращён в уведомлении."])
    lines.extend([
        "",
        "<b>Что произошло</b>",
        explanation,
        "",
        "<b>Что делать</b>",
        "Проверьте подключение Threads и историю публикаций. Повтор не запланирован.",
    ])
    return "\n".join(lines)


def publication_keyboard(
    notification: PublicationNotification,
) -> InlineKeyboardMarkup | None:
    if notification.permalink:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="🔗 Открыть пост в Threads",
                url=notification.permalink,
            )
        ]])
    if notification.outcome == "unknown":
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="Проверить",
                callback_data=(
                    f"ac:history:{notification.threads_account_id}"
                ),
            ),
            InlineKeyboardButton(
                text="История",
                callback_data=(
                    f"activity:{notification.threads_account_id}:0"
                ),
            ),
        ]])
    return None
