"""Shared scheduling, status, history, and recovery for auto-posting."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import credits
from app.core.llm import LLMError
from app.core.threads_api import ThreadsAPIError
from app.schemas.autopost import (
    AutopostAccount,
    AutopostErrorCode,
    AutopostRun,
    AutopostSettings,
    AutopostStatus,
)

DEFAULT_TIMEZONE = "Europe/Moscow"
DEFAULT_SLOTS = (time(9), time(12), time(15), time(18), time(21))
SCHEDULE_LEAD_MINUTES = 10
MISFIRE_GRACE_MINUTES = 10
STALE_GENERATION_MINUTES = 30
STALE_PUBLISH_MINUTES = 15

SAFE_ERROR_MESSAGES: dict[AutopostErrorCode, str] = {
    "AUTH_EXPIRED": "Истекла авторизация Threads",
    "PERMISSION_DENIED": "Недостаточно разрешений Threads",
    "THREADS_TEMPORARY_ERROR": "Threads API временно недоступен",
    "INSUFFICIENT_CREDITS": "Недостаточно кредитов",
    "GENERATION_FAILED": "Ошибка генерации поста",
    "QUALITY_FAILED": "Пост не прошёл проверку качества",
    "UNKNOWN_ERROR": "Неизвестная ошибка автопостинга",
}

_ACCOUNT_SETTINGS_SQL = text("""
    SELECT
      ta.id AS account_id,
      ta.username,
      ta.expires_at,
      coalesce(ac.active, false) AS active,
      coalesce(ac.posts_per_day, 1) AS posts_per_day,
      coalesce(ac.slots, '') AS slots,
      coalesce(ac.days, 'all') AS days,
      coalesce(ac.timezone, :default_timezone) AS timezone
    FROM threads_accounts ta
    LEFT JOIN autocontent_settings ac ON ac.user_id = ta.user_id
    WHERE ta.id = :account_id
      AND ta.user_id = :uid
""")

_LIST_ACCOUNTS_SQL = text("""
    SELECT id, username, expires_at
    FROM threads_accounts
    WHERE user_id = :uid
    ORDER BY created_at DESC, id DESC
""")

_RUN_STATUS_SQL = text("""
    SELECT
      last_run.started_at AS last_run_at,
      last_run.status AS last_run_status,
      last_run.error_code AS safe_error_code,
      last_run.safe_error_message,
      last_success.finished_at AS last_success_at,
      last_success.threads_post_id AS last_threads_post_id,
      next_run.scheduled_at AS next_run_at
    FROM (SELECT 1) seed
    LEFT JOIN LATERAL (
      SELECT started_at, status, error_code, safe_error_message
      FROM autopost_runs
      WHERE user_id = :uid
        AND threads_account_id = :account_id
      ORDER BY started_at DESC, id DESC
      LIMIT 1
    ) last_run ON true
    LEFT JOIN LATERAL (
      SELECT finished_at, threads_post_id
      FROM autopost_runs
      WHERE user_id = :uid
        AND threads_account_id = :account_id
        AND status = 'success'
      ORDER BY finished_at DESC NULLS LAST, id DESC
      LIMIT 1
    ) last_success ON true
    LEFT JOIN LATERAL (
      SELECT coalesce(post.run_at, run.scheduled_at) AS scheduled_at
      FROM autopost_runs run
      LEFT JOIN scheduled_posts post
        ON post.id = run.scheduled_post_id
      WHERE run.user_id = :uid
        AND run.threads_account_id = :account_id
        AND run.status = 'pending'
        AND coalesce(post.run_at, run.scheduled_at) >= :now
      ORDER BY coalesce(post.run_at, run.scheduled_at), run.id
      LIMIT 1
    ) next_run ON true
""")

_HISTORY_SQL = text("""
    SELECT id, user_id, threads_account_id, scheduled_post_id,
           scheduled_at, started_at, finished_at, status,
           threads_post_id, error_code, safe_error_message
    FROM autopost_runs
    WHERE user_id = :uid
      AND threads_account_id = :account_id
    ORDER BY started_at DESC, id DESC
    LIMIT :limit
""")

_OCCUPIED_SQL = text("""
    SELECT scheduled_at
    FROM autopost_runs
    WHERE user_id = :uid
      AND threads_account_id = :account_id
      AND scheduled_at >= :window_start
      AND scheduled_at < :window_end
    UNION
    SELECT sp.run_at AS scheduled_at
    FROM scheduled_posts sp
    WHERE sp.user_id = :uid
      AND sp.threads_account_id = :account_id
      AND sp.run_at >= :window_start
      AND sp.run_at < :window_end
      AND sp.status IN ('pending', 'publishing', 'done', 'failed')
      AND sp.content_metadata ->> 'source' = 'autocontent'
      AND NOT EXISTS (
        SELECT 1 FROM autopost_runs run
        WHERE run.scheduled_post_id = sp.id
      )
""")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def resolve_timezone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def parse_slots(
    raw: str | None,
    *,
    default_if_empty: bool = True,
) -> tuple[time, ...]:
    if not raw or not raw.strip():
        return DEFAULT_SLOTS if default_if_empty else ()
    parsed = set()
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        parts = candidate.split(":", 1)
        if not all(part.isdigit() for part in parts):
            continue
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) == 2 else 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            parsed.add(time(hour, minute))
    return tuple(sorted(parsed))


def serialize_slots(slots: Sequence[time]) -> str:
    return ",".join(
        f"{slot.hour:02d}:{slot.minute:02d}"
        for slot in sorted(set(slots))
    )


def schedule_window(
    now: datetime,
    timezone_name: str,
    *,
    days: int = 9,
) -> tuple[datetime, datetime]:
    tz = resolve_timezone(timezone_name)
    local_now = ensure_aware(now).astimezone(tz)
    local_start = datetime.combine(
        local_now.date(),
        time.min,
        tzinfo=tz,
    )
    return (
        local_start.astimezone(timezone.utc),
        (local_start + timedelta(days=days)).astimezone(timezone.utc),
    )


def select_next_slots(
    *,
    now: datetime,
    slots: Sequence[time],
    days: str,
    timezone_name: str,
    posts_per_day: int,
    occupied: Sequence[datetime] = (),
    lead_minutes: int = SCHEDULE_LEAD_MINUTES,
    horizon_days: int = 8,
) -> tuple[datetime, ...]:
    if not slots or posts_per_day <= 0:
        return ()
    tz = resolve_timezone(timezone_name)
    local_now = ensure_aware(now).astimezone(tz)
    earliest = local_now + timedelta(minutes=max(0, lead_minutes))
    occupied_local = [
        ensure_aware(item).astimezone(tz).replace(second=0, microsecond=0)
        for item in occupied
    ]
    occupied_counts = Counter(item.date() for item in occupied_local)
    occupied_minutes = set(occupied_local)

    for offset in range(horizon_days + 1):
        candidate_date = local_now.date() + timedelta(days=offset)
        if days == "weekdays" and candidate_date.weekday() >= 5:
            continue
        remaining = posts_per_day - occupied_counts[candidate_date]
        if remaining <= 0:
            continue
        selected = []
        for slot in sorted(set(slots)):
            candidate = datetime.combine(
                candidate_date,
                slot,
                tzinfo=tz,
            ).replace(second=0, microsecond=0)
            if candidate < earliest or candidate in occupied_minutes:
                continue
            selected.append(candidate.astimezone(timezone.utc))
            if len(selected) >= remaining:
                break
        if selected:
            return tuple(selected)
    return ()


def normalize_error(
    error: Exception,
    *,
    stage: str,
) -> tuple[AutopostErrorCode, str]:
    if isinstance(error, credits.NotEnoughCredits):
        code: AutopostErrorCode = "INSUFFICIENT_CREDITS"
    elif error.__class__.__name__ == "ContentQualityError":
        code = "QUALITY_FAILED"
    elif isinstance(error, LLMError) or stage == "generation":
        code = "GENERATION_FAILED"
    elif isinstance(error, httpx.HTTPError):
        code = "THREADS_TEMPORARY_ERROR"
    elif isinstance(error, ThreadsAPIError):
        status_code = getattr(error, "status_code", None)
        detail = str(error).casefold()
        if status_code == 401:
            code = "AUTH_EXPIRED"
        elif status_code == 403:
            code = "PERMISSION_DENIED"
        elif status_code is not None and status_code >= 500:
            code = "THREADS_TEMPORARY_ERROR"
        elif any(
            marker in detail
            for marker in (
                "oauth",
                "token",
                "session has expired",
                '"code":190',
                '"code": 190',
            )
        ):
            code = "AUTH_EXPIRED"
        elif "permission" in detail:
            code = "PERMISSION_DENIED"
        else:
            code = "THREADS_TEMPORARY_ERROR"
    else:
        code = "UNKNOWN_ERROR"
    return code, SAFE_ERROR_MESSAGES[code]


def _mapping(row: Any) -> Mapping[str, Any]:
    if row is None:
        return {}
    if isinstance(row, Mapping):
        return row
    mapping = getattr(row, "_mapping", None)
    return mapping if isinstance(mapping, Mapping) else {}


def _run_from_row(row: Any) -> AutopostRun:
    return AutopostRun.model_validate(dict(_mapping(row)))


class AutopostStatusService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_accounts(self, user_id: int) -> list[AutopostAccount]:
        rows = (
            await self.session.execute(
                _LIST_ACCOUNTS_SQL,
                {"uid": user_id},
            )
        ).mappings().all()
        return [
            AutopostAccount(
                id=row["id"],
                username=row.get("username"),
                expires_at=row["expires_at"],
            )
            for row in rows
        ]

    async def get_status(
        self,
        user_id: int,
        account_id: int,
        *,
        now: datetime | None = None,
    ) -> AutopostStatus | None:
        current = ensure_aware(now or utc_now())
        account_row = (
            await self.session.execute(
                _ACCOUNT_SETTINGS_SQL,
                {
                    "uid": user_id,
                    "account_id": account_id,
                    "default_timezone": DEFAULT_TIMEZONE,
                },
            )
        ).mappings().first()
        if account_row is None:
            return None
        slots = parse_slots(account_row.get("slots"))
        timezone_name = resolve_timezone(
            account_row.get("timezone")
        ).key
        settings = AutopostSettings(
            enabled=bool(account_row.get("active")),
            posts_per_day=max(
                0,
                min(5, int(account_row.get("posts_per_day") or 1)),
            ),
            slots=slots,
            days=(
                "weekdays"
                if account_row.get("days") == "weekdays"
                else "all"
            ),
            timezone=timezone_name,
        )
        run_row = (
            await self.session.execute(
                _RUN_STATUS_SQL,
                {
                    "uid": user_id,
                    "account_id": account_id,
                    "now": current,
                },
            )
        ).mappings().first()
        run_data = dict(run_row or {})
        next_run_at = run_data.get("next_run_at")
        if settings.enabled and next_run_at is None:
            window_start, window_end = schedule_window(
                current,
                settings.timezone,
            )
            occupied_rows = (
                await self.session.execute(
                    _OCCUPIED_SQL,
                    {
                        "uid": user_id,
                        "account_id": account_id,
                        "window_start": window_start,
                        "window_end": window_end,
                    },
                )
            ).all()
            occupied = [row[0] for row in occupied_rows if row[0]]
            candidates = select_next_slots(
                now=current,
                slots=settings.slots,
                days=settings.days,
                timezone_name=settings.timezone,
                posts_per_day=settings.posts_per_day,
                occupied=occupied,
            )
            next_run_at = candidates[0] if candidates else None

        error_code = run_data.get("safe_error_code")
        error_message = run_data.get("safe_error_message")
        expires_at = ensure_aware(account_row["expires_at"])
        if settings.enabled and expires_at <= current:
            error_code = "AUTH_EXPIRED"
            error_message = SAFE_ERROR_MESSAGES["AUTH_EXPIRED"]
            next_run_at = None

        return AutopostStatus(
            account=AutopostAccount(
                id=account_row["account_id"],
                username=account_row.get("username"),
                expires_at=account_row["expires_at"],
            ),
            settings=settings,
            next_run_at=next_run_at if settings.enabled else None,
            last_run_at=run_data.get("last_run_at"),
            last_run_status=run_data.get("last_run_status"),
            last_success_at=run_data.get("last_success_at"),
            last_threads_post_id=run_data.get("last_threads_post_id"),
            safe_error_code=error_code,
            safe_error_message=error_message,
        )

    async def history(
        self,
        user_id: int,
        account_id: int,
        *,
        limit: int = 10,
    ) -> list[AutopostRun]:
        rows = (
            await self.session.execute(
                _HISTORY_SQL,
                {
                    "uid": user_id,
                    "account_id": account_id,
                    "limit": max(1, min(10, limit)),
                },
            )
        ).mappings().all()
        return [_run_from_row(row) for row in rows]

    async def occupied_slots(
        self,
        user_id: int,
        account_id: int,
        *,
        now: datetime,
        timezone_name: str,
    ) -> list[datetime]:
        window_start, window_end = schedule_window(now, timezone_name)
        rows = (
            await self.session.execute(
                _OCCUPIED_SQL,
                {
                    "uid": user_id,
                    "account_id": account_id,
                    "window_start": window_start,
                    "window_end": window_end,
                },
            )
        ).all()
        return [row[0] for row in rows if row[0]]

    async def reserve_run(
        self,
        user_id: int,
        account_id: int,
        scheduled_at: datetime,
    ) -> int | None:
        row = (
            await self.session.execute(
                text("""
                    INSERT INTO autopost_runs (
                        user_id, threads_account_id, scheduled_at,
                        started_at, status
                    )
                    SELECT
                        :uid, :account_id, :scheduled_at, now(), 'pending'
                    WHERE EXISTS (
                        SELECT 1 FROM autocontent_settings
                        WHERE user_id = :uid
                          AND active
                    )
                    ON CONFLICT (threads_account_id, scheduled_at)
                    DO NOTHING
                    RETURNING id
                """),
                {
                    "uid": user_id,
                    "account_id": account_id,
                    "scheduled_at": ensure_aware(scheduled_at),
                },
            )
        ).first()
        return row[0] if row else None

    async def attach_post(self, run_id: int, post_id: int) -> None:
        await self.session.execute(
            text("""
                UPDATE autopost_runs
                SET scheduled_post_id = :post_id
                WHERE id = :run_id
                  AND status = 'pending'
                  AND scheduled_post_id IS NULL
            """),
            {"run_id": run_id, "post_id": post_id},
        )

    async def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        threads_post_id: str | None = None,
        error_code: AutopostErrorCode | None = None,
        safe_error_message: str | None = None,
    ) -> None:
        await self.session.execute(
            text("""
                UPDATE autopost_runs
                SET status = :status,
                    finished_at = now(),
                    threads_post_id = :threads_post_id,
                    error_code = :error_code,
                    safe_error_message = :safe_error_message
                WHERE id = :run_id
                  AND status = 'pending'
            """),
            {
                "run_id": run_id,
                "status": status,
                "threads_post_id": threads_post_id,
                "error_code": error_code,
                "safe_error_message": safe_error_message,
            },
        )

    async def finish_for_post(
        self,
        post_id: int,
        *,
        status: str,
        threads_post_id: str | None = None,
        error_code: AutopostErrorCode | None = None,
        safe_error_message: str | None = None,
    ) -> None:
        await self.session.execute(
            text("""
                UPDATE autopost_runs
                SET status = :status,
                    finished_at = now(),
                    threads_post_id = :threads_post_id,
                    error_code = :error_code,
                    safe_error_message = :safe_error_message
                WHERE scheduled_post_id = :post_id
                  AND status = 'pending'
            """),
            {
                "post_id": post_id,
                "status": status,
                "threads_post_id": threads_post_id,
                "error_code": error_code,
                "safe_error_message": safe_error_message,
            },
        )

    async def skip_pending_for_user(self, user_id: int) -> int:
        rows = (
            await self.session.execute(
                text("""
                    WITH cancellable_posts AS (
                      SELECT post.id
                      FROM scheduled_posts post
                      WHERE post.user_id = :uid
                        AND post.status = 'pending'
                        AND (
                          post.content_metadata ->> 'source' = 'autocontent'
                          OR EXISTS (
                            SELECT 1 FROM autopost_runs linked
                            WHERE linked.scheduled_post_id = post.id
                              AND linked.status = 'pending'
                          )
                        )
                      FOR UPDATE SKIP LOCKED
                    )
                    UPDATE autopost_runs run
                    SET status = 'skipped',
                        finished_at = now(),
                        error_code = NULL,
                        safe_error_message = :message
                    WHERE run.user_id = :uid
                      AND run.status = 'pending'
                      AND (
                        run.scheduled_post_id IS NULL
                        OR run.scheduled_post_id IN (
                          SELECT id FROM cancellable_posts
                        )
                      )
                    RETURNING run.scheduled_post_id
                """),
                {
                    "uid": user_id,
                    "message": "Пропущено: автопостинг выключен",
                },
            )
        ).all()
        post_ids = [row[0] for row in rows if row[0]]
        await self.session.execute(
            text("""
                UPDATE scheduled_posts
                SET status = 'failed',
                    error = 'Autopost disabled'
                WHERE user_id = :uid
                  AND status = 'pending'
                  AND (
                    id = ANY(CAST(:post_ids AS bigint[]))
                    OR content_metadata ->> 'source' = 'autocontent'
                  )
            """),
            {"uid": user_id, "post_ids": post_ids},
        )
        return len(rows)


async def recover_autopost_state(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    current = ensure_aware(now or utc_now())
    publish_cutoff = current - timedelta(
        minutes=STALE_PUBLISH_MINUTES
    )
    projected = (
        await session.execute(
            text("""
                INSERT INTO autopost_runs (
                    user_id, threads_account_id, scheduled_post_id,
                    scheduled_at, started_at, status
                )
                SELECT
                    post.user_id,
                    post.threads_account_id,
                    post.id,
                    post.run_at,
                    :now,
                    'pending'
                FROM scheduled_posts post
                WHERE post.status IN ('pending', 'publishing')
                  AND post.content_metadata ->> 'source' = 'autocontent'
                  AND NOT EXISTS (
                    SELECT 1 FROM autopost_runs run
                    WHERE run.scheduled_post_id = post.id
                  )
                ON CONFLICT DO NOTHING
                RETURNING id
            """),
            {"now": current},
        )
    ).all()
    interrupted = (
        await session.execute(
            text("""
                WITH stale_posts AS (
                  SELECT post.id
                  FROM scheduled_posts post
                  WHERE post.status = 'publishing'
                    AND (
                      post.publish_started_at IS NULL
                      OR post.publish_started_at < :publish_cutoff
                    )
                  FOR UPDATE SKIP LOCKED
                )
                UPDATE autopost_runs run
                SET status = 'failed',
                    finished_at = :now,
                    error_code = 'UNKNOWN_ERROR',
                    safe_error_message = :message
                WHERE run.status = 'pending'
                  AND run.scheduled_post_id IN (
                    SELECT id FROM stale_posts
                  )
                RETURNING run.scheduled_post_id
            """),
            {
                "now": current,
                "publish_cutoff": publish_cutoff,
                "message": (
                    "Состояние публикации не подтверждено после "
                    "перезапуска worker"
                ),
            },
        )
    ).all()
    interrupted_ids = [row[0] for row in interrupted if row[0]]
    if interrupted_ids:
        await session.execute(
            text("""
                UPDATE scheduled_posts
                SET status = 'failed',
                    error = 'UNKNOWN_ERROR: interrupted worker'
                WHERE id = ANY(CAST(:post_ids AS bigint[]))
                  AND status = 'publishing'
            """),
            {"post_ids": interrupted_ids},
        )

    cutoff = current - timedelta(minutes=MISFIRE_GRACE_MINUTES)
    missed = (
        await session.execute(
            text("""
                WITH missed_posts AS (
                  SELECT post.id
                  FROM scheduled_posts post
                  JOIN autopost_runs candidate
                    ON candidate.scheduled_post_id = post.id
                  WHERE post.status = 'pending'
                    AND candidate.status = 'pending'
                    AND candidate.scheduled_at < :cutoff
                  FOR UPDATE OF post SKIP LOCKED
                )
                UPDATE autopost_runs run
                SET status = 'skipped',
                    finished_at = :now,
                    error_code = 'UNKNOWN_ERROR',
                    safe_error_message = :message
                WHERE run.status = 'pending'
                  AND run.scheduled_post_id IN (
                    SELECT id FROM missed_posts
                  )
                RETURNING run.scheduled_post_id
            """),
            {
                "now": current,
                "cutoff": cutoff,
                "message": "Пропущено после перезапуска worker",
            },
        )
    ).all()
    missed_ids = [row[0] for row in missed if row[0]]
    if missed_ids:
        await session.execute(
            text("""
                UPDATE scheduled_posts
                SET status = 'failed',
                    error = 'UNKNOWN_ERROR: missed after worker restart'
                WHERE id = ANY(CAST(:post_ids AS bigint[]))
                  AND status = 'pending'
            """),
            {"post_ids": missed_ids},
        )

    stale_cutoff = current - timedelta(
        minutes=STALE_GENERATION_MINUTES
    )
    orphaned = (
        await session.execute(
            text("""
                UPDATE autopost_runs
                SET status = 'failed',
                    finished_at = :now,
                    error_code = 'GENERATION_FAILED',
                    safe_error_message = :message
                WHERE status = 'pending'
                  AND scheduled_post_id IS NULL
                  AND started_at < :stale_cutoff
                RETURNING id
            """),
            {
                "now": current,
                "stale_cutoff": stale_cutoff,
                "message": SAFE_ERROR_MESSAGES["GENERATION_FAILED"],
            },
        )
    ).all()
    result = {
        "projected": len(projected),
        "interrupted": len(interrupted_ids),
        "misfired": len(missed_ids),
        "orphaned": len(orphaned),
    }
    return result


def format_local_datetime(
    value: datetime,
    timezone_name: str,
    *,
    now: datetime | None = None,
) -> str:
    tz = resolve_timezone(timezone_name)
    local_value = ensure_aware(value).astimezone(tz)
    local_now = ensure_aware(now or utc_now()).astimezone(tz)
    if local_value.date() == local_now.date():
        prefix = "Сегодня"
    elif local_value.date() == local_now.date() + timedelta(days=1):
        prefix = "Завтра"
    else:
        prefix = local_value.strftime("%d.%m")
    return f"{prefix}, {local_value:%H:%M}"


def format_relative(
    value: datetime,
    *,
    now: datetime | None = None,
) -> str:
    seconds = max(
        0,
        int(
            (
                ensure_aware(value)
                - ensure_aware(now or utc_now())
            ).total_seconds()
        ),
    )
    minutes = seconds // 60
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"через {hours} ч {minutes} мин"
    return f"через {minutes} мин"


def render_status(
    status: AutopostStatus,
    *,
    now: datetime | None = None,
) -> str:
    enabled = status.settings.enabled
    lines = [
        "✨ АВТОПОСТИНГ",
        "",
        f"Статус: {'🟢 Работает' if enabled else '⚪ Выключен'}",
        f"Аккаунт: @{status.account.username or status.account.id}",
        f"Постов в день: {status.settings.posts_per_day}",
        f"Расписание: {serialize_slots(status.settings.slots) or 'не задано'}",
        f"Часовой пояс: {status.settings.timezone}",
        "",
        "🕐 Следующий пост:",
    ]
    if enabled and status.next_run_at is not None:
        lines.extend([
            format_local_datetime(
                status.next_run_at,
                status.settings.timezone,
                now=now,
            ),
            format_relative(status.next_run_at, now=now),
        ])
    else:
        lines.append("не запланирован")

    if status.last_success_at is not None:
        lines.extend([
            "",
            "✅ Последняя публикация:",
            format_local_datetime(
                status.last_success_at,
                status.settings.timezone,
                now=now,
            ),
        ])
    if status.last_run_status in {"failed", "skipped"}:
        icon = "❌" if status.last_run_status == "failed" else "⏭"
        lines.extend([
            "",
            f"{icon} Последний запуск:",
            status.safe_error_message or SAFE_ERROR_MESSAGES["UNKNOWN_ERROR"],
        ])
    elif status.last_run_status == "pending":
        lines.extend(["", "⏳ Последний запуск: ожидает публикации"])
    elif status.safe_error_message:
        lines.extend([
            "",
            "❌ Требуется внимание:",
            status.safe_error_message,
        ])
    return "\n".join(lines)


def render_history(
    runs: Sequence[AutopostRun],
    timezone_name: str,
) -> str:
    lines = ["📋 ИСТОРИЯ АВТОПОСТИНГА", ""]
    if not runs:
        return "\n".join(lines + ["Запусков пока нет."])
    icons = {
        "success": "✅",
        "failed": "❌",
        "skipped": "⏭",
        "pending": "⏳",
    }
    default_messages = {
        "success": "Пост опубликован",
        "failed": SAFE_ERROR_MESSAGES["UNKNOWN_ERROR"],
        "skipped": "Запуск пропущен",
        "pending": "Ожидает публикации",
    }
    tz = resolve_timezone(timezone_name)
    for index, run in enumerate(runs[:10], 1):
        when = ensure_aware(run.scheduled_at).astimezone(tz)
        message = (
            run.safe_error_message
            or default_messages[run.status]
        )
        lines.extend([
            f"{index}. {icons[run.status]} {when:%d.%m %H:%M}",
            message,
            "",
        ])
    return "\n".join(lines).rstrip()
