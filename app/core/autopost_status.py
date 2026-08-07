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
    AutopostQueueClearResult,
    AutopostQueueDay,
    AutopostQueueItem,
    AutopostQueueRebuildResult,
    AutopostQueueSummary,
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
GENERATION_RETRY_COOLDOWN_MINUTES = 60
PLANNING_ACTIVE_DAYS = 4
PLANNING_SEARCH_DAYS = 32
QUEUE_CLEARED_MESSAGE = "Пропущено: очередь очищена"

SAFE_ERROR_MESSAGES: dict[AutopostErrorCode, str] = {
    "AUTH_EXPIRED": "Истекла авторизация Threads",
    "PERMISSION_DENIED": "Недостаточно разрешений Threads",
    "THREADS_TEMPORARY_ERROR": "Threads API временно недоступен",
    "INSUFFICIENT_CREDITS": "Недостаточно кредитов",
    "GENERATION_FAILED": "Ошибка генерации поста",
    "QUALITY_FAILED": "Пост не прошёл проверку качества",
    "UNKNOWN_ERROR": "Не удалось завершить работу Автопилота",
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
      coalesce(ac.timezone, :default_timezone) AS timezone,
      ac.cost_guard_until,
      ac.cost_guard_reason
    FROM threads_accounts ta
    LEFT JOIN autocontent_settings ac
      ON ac.threads_account_id = ta.id
     AND ac.user_id = ta.user_id
    WHERE ta.id = :account_id
      AND ta.user_id = :uid
""")

_LIST_ACCOUNTS_SQL = text("""
    SELECT id, username, expires_at
    FROM threads_accounts
    WHERE user_id = :uid
      AND connection_status = 'connected'
      AND access_token_enc IS NOT NULL
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
    SELECT coalesce(post.run_at, run.scheduled_at) AS scheduled_at
    FROM autopost_runs run
    LEFT JOIN scheduled_posts post ON post.id = run.scheduled_post_id
    WHERE run.user_id = :uid
      AND run.threads_account_id = :account_id
      AND run.status IN ('pending', 'success')
      AND coalesce(post.run_at, run.scheduled_at) >= :window_start
      AND coalesce(post.run_at, run.scheduled_at) < :window_end
      AND (
        post.id IS NULL
        OR post.status IN ('pending', 'publishing', 'done')
      )
      AND (
        run.scheduled_post_id IS NULL
        OR NOT (
          run.scheduled_post_id = ANY(
            CAST(:exclude_post_ids AS bigint[])
          )
        )
      )
    UNION
    SELECT sp.run_at AS scheduled_at
    FROM scheduled_posts sp
    WHERE sp.user_id = :uid
      AND sp.threads_account_id = :account_id
      AND sp.run_at >= :window_start
      AND sp.run_at < :window_end
      AND sp.status IN ('pending', 'publishing', 'done')
      AND sp.content_metadata ->> 'source' = 'autocontent'
      AND NOT (sp.id = ANY(CAST(:exclude_post_ids AS bigint[])))
      AND NOT EXISTS (
        SELECT 1 FROM autopost_runs run
        WHERE run.scheduled_post_id = sp.id
      )
""")

_MANUAL_BLOCKED_SQL = text("""
    SELECT post.run_at
    FROM scheduled_posts post
    WHERE post.user_id = :uid
      AND post.threads_account_id = :account_id
      AND post.run_at >= :window_start
      AND post.run_at < :window_end
      AND post.status IN ('pending', 'publishing')
      AND coalesce(post.content_metadata ->> 'source', '') <> 'autocontent'
      AND NOT EXISTS (
        SELECT 1 FROM autopost_runs run
        WHERE run.scheduled_post_id = post.id
      )
""")

_RETRY_BLOCKED_SQL = text("""
    SELECT run.scheduled_at
    FROM autopost_runs run
    WHERE run.user_id = :uid
      AND run.threads_account_id = :account_id
      AND run.status = 'failed'
      AND run.error_code IN (
        'GENERATION_FAILED', 'QUALITY_FAILED', 'UNKNOWN_ERROR'
      )
      AND run.finished_at > :retry_cutoff
      AND run.scheduled_at >= :window_start
      AND run.scheduled_at < :window_end
""")

_PENDING_AUTO_COUNT_SQL = text("""
    SELECT count(*)
    FROM (
      SELECT post.id
      FROM scheduled_posts post
      WHERE post.user_id = :uid
        AND post.threads_account_id = :account_id
        AND post.status = 'pending'
        AND post.run_at > :now
        AND (
          post.content_metadata ->> 'source' = 'autocontent'
          OR EXISTS (
            SELECT 1 FROM autopost_runs linked
            WHERE linked.scheduled_post_id = post.id
          )
        )
      UNION ALL
      SELECT -run.id
      FROM autopost_runs run
      WHERE run.user_id = :uid
        AND run.threads_account_id = :account_id
        AND run.status = 'pending'
        AND run.scheduled_post_id IS NULL
        AND run.scheduled_at > :now
    ) auto_queue
""")

_AUTO_QUEUE_SQL = text("""
    SELECT post.id, post.text, post.run_at
    FROM scheduled_posts post
    WHERE post.user_id = :uid
      AND post.threads_account_id = :account_id
      AND post.status = 'pending'
      AND post.run_at > :now
      AND (
        post.content_metadata ->> 'source' = 'autocontent'
        OR EXISTS (
          SELECT 1 FROM autopost_runs run
          WHERE run.scheduled_post_id = post.id
        )
      )
    ORDER BY post.run_at, post.id
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
    blocked: Sequence[datetime] = (),
    lead_minutes: int = SCHEDULE_LEAD_MINUTES,
    horizon_days: int = 8,
) -> tuple[datetime, ...]:
    selected = select_planning_slots(
        now=now,
        slots=slots,
        days=days,
        timezone_name=timezone_name,
        posts_per_day=posts_per_day,
        occupied=occupied,
        blocked=blocked,
        lead_minutes=lead_minutes,
        active_days=horizon_days + 1,
        search_calendar_days=horizon_days + 1,
    )
    if not selected:
        return ()
    tz = resolve_timezone(timezone_name)
    first_day = selected[0].astimezone(tz).date()
    return tuple(
        candidate
        for candidate in selected
        if candidate.astimezone(tz).date() == first_day
    )


def is_active_day(value: date, days: str) -> bool:
    return not (days == "weekdays" and value.weekday() >= 5)


def select_planning_slots(
    *,
    now: datetime,
    slots: Sequence[time],
    days: str,
    timezone_name: str,
    posts_per_day: int,
    occupied: Sequence[datetime] = (),
    blocked: Sequence[datetime] = (),
    lead_minutes: int = SCHEDULE_LEAD_MINUTES,
    active_days: int = PLANNING_ACTIVE_DAYS,
    required_slots: int | None = None,
    search_calendar_days: int = PLANNING_SEARCH_DAYS,
) -> tuple[datetime, ...]:
    """Return every free slot in deterministic local-day order."""
    if (
        not slots
        or posts_per_day <= 0
        or active_days <= 0
        or required_slots == 0
    ):
        return ()
    tz = resolve_timezone(timezone_name)
    local_now = ensure_aware(now).astimezone(tz)
    earliest = (
        local_now + timedelta(minutes=max(0, lead_minutes))
    ).replace(second=0, microsecond=0)
    occupied_local = [
        ensure_aware(item).astimezone(tz).replace(second=0, microsecond=0)
        for item in occupied
    ]
    blocked_local = {
        ensure_aware(item).astimezone(tz).replace(second=0, microsecond=0)
        for item in blocked
    }
    occupied_counts = Counter(item.date() for item in occupied_local)
    occupied_minutes = set(occupied_local)
    selected: list[datetime] = []
    active_seen = 0

    for offset in range(max(1, search_calendar_days)):
        candidate_date = local_now.date() + timedelta(days=offset)
        if not is_active_day(candidate_date, days):
            continue
        active_seen += 1
        remaining = max(
            0,
            posts_per_day - occupied_counts[candidate_date],
        )
        for slot in sorted(set(slots)):
            if remaining <= 0:
                break
            candidate = datetime.combine(
                candidate_date,
                slot,
                tzinfo=tz,
            ).replace(second=0, microsecond=0)
            if (
                candidate < earliest
                or candidate in occupied_minutes
                or candidate in blocked_local
            ):
                continue
            selected.append(candidate.astimezone(timezone.utc))
            remaining -= 1
            if required_slots is not None and len(selected) >= required_slots:
                return tuple(selected)
        if required_slots is None and active_seen >= active_days:
            break
    return tuple(selected)


def available_day_capacity(
    *,
    now: datetime,
    day: date,
    slots: Sequence[time],
    days: str,
    timezone_name: str,
    posts_per_day: int,
    blocked: Sequence[datetime] = (),
    lead_minutes: int = SCHEDULE_LEAD_MINUTES,
) -> int:
    if not is_active_day(day, days):
        return 0
    tz = resolve_timezone(timezone_name)
    local_now = ensure_aware(now).astimezone(tz)
    earliest = local_now + timedelta(minutes=max(0, lead_minutes))
    blocked_local = {
        ensure_aware(value).astimezone(tz).replace(
            second=0,
            microsecond=0,
        )
        for value in blocked
    }
    available = sum(
        datetime.combine(day, slot, tzinfo=tz) >= earliest
        and datetime.combine(day, slot, tzinfo=tz) not in blocked_local
        for slot in sorted(set(slots))
    )
    return min(max(0, posts_per_day), available)


def queue_summary_dates(local_today: date, days: str) -> tuple[date, ...]:
    tomorrow = local_today + timedelta(days=1)
    next_active = tomorrow + timedelta(days=1)
    while not is_active_day(next_active, days):
        next_active += timedelta(days=1)
    return local_today, tomorrow, next_active


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
                days=PLANNING_SEARCH_DAYS,
            )
            occupied_rows = (
                await self.session.execute(
                    _OCCUPIED_SQL,
                    {
                        "uid": user_id,
                        "account_id": account_id,
                        "window_start": window_start,
                        "window_end": window_end,
                        "exclude_post_ids": [],
                    },
                )
            ).all()
            occupied = [row[0] for row in occupied_rows if row[0]]
            blocked_rows = (
                await self.session.execute(
                    _MANUAL_BLOCKED_SQL,
                    {
                        "uid": user_id,
                        "account_id": account_id,
                        "window_start": window_start,
                        "window_end": window_end,
                    },
                )
            ).all()
            blocked = [row[0] for row in blocked_rows if row[0]]
            retry_blocked = await self.retry_blocked_slots(
                user_id,
                account_id,
                now=current,
                timezone_name=settings.timezone,
            )
            blocked.extend(retry_blocked)
            candidates = select_next_slots(
                now=current,
                slots=settings.slots,
                days=settings.days,
                timezone_name=settings.timezone,
                posts_per_day=settings.posts_per_day,
                occupied=occupied,
                blocked=blocked,
            )
            next_run_at = candidates[0] if candidates else None

        error_code = run_data.get("safe_error_code")
        error_message = run_data.get("safe_error_message")
        expires_at = ensure_aware(account_row["expires_at"])
        if settings.enabled and expires_at <= current:
            error_code = "AUTH_EXPIRED"
            error_message = SAFE_ERROR_MESSAGES["AUTH_EXPIRED"]
            next_run_at = None
        guard_until = account_row.get("cost_guard_until")
        if guard_until is not None:
            guard_until = ensure_aware(guard_until)
        if settings.enabled and guard_until and guard_until > current:
            error_code = "GENERATION_FAILED"
            error_message = (
                "Автогенерация временно приостановлена из-за частых "
                "исправлений. Следующая попытка будет выполнена автоматически."
            )
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

    async def lock_queue(self, user_id: int, account_id: int) -> None:
        await self.session.execute(
            text("""
                SELECT pg_advisory_xact_lock(
                    hashtextextended(:lock_scope, 0)
                )
            """),
            {"lock_scope": f"autopost_queue:{user_id}:{account_id}"},
        )

    async def occupied_slots(
        self,
        user_id: int,
        account_id: int,
        *,
        now: datetime,
        timezone_name: str,
        exclude_post_ids: Sequence[int] = (),
    ) -> list[datetime]:
        window_start, window_end = schedule_window(
            now,
            timezone_name,
            days=PLANNING_SEARCH_DAYS,
        )
        rows = (
            await self.session.execute(
                _OCCUPIED_SQL,
                {
                    "uid": user_id,
                    "account_id": account_id,
                    "window_start": window_start,
                    "window_end": window_end,
                    "exclude_post_ids": list(exclude_post_ids),
                },
            )
        ).all()
        return [row[0] for row in rows if row[0]]

    async def blocked_manual_slots(
        self,
        user_id: int,
        account_id: int,
        *,
        now: datetime,
        timezone_name: str,
    ) -> list[datetime]:
        window_start, window_end = schedule_window(
            now,
            timezone_name,
            days=PLANNING_SEARCH_DAYS,
        )
        rows = (
            await self.session.execute(
                _MANUAL_BLOCKED_SQL,
                {
                    "uid": user_id,
                    "account_id": account_id,
                    "window_start": window_start,
                    "window_end": window_end,
                },
            )
        ).all()
        return [row[0] for row in rows if row[0]]

    async def retry_blocked_slots(
        self,
        user_id: int,
        account_id: int,
        *,
        now: datetime,
        timezone_name: str,
    ) -> list[datetime]:
        current = ensure_aware(now)
        window_start, window_end = schedule_window(
            current,
            timezone_name,
            days=PLANNING_SEARCH_DAYS,
        )
        rows = (
            await self.session.execute(
                _RETRY_BLOCKED_SQL,
                {
                    "uid": user_id,
                    "account_id": account_id,
                    "window_start": window_start,
                    "window_end": window_end,
                    "retry_cutoff": current - timedelta(
                        minutes=GENERATION_RETRY_COOLDOWN_MINUTES
                    ),
                },
            )
        ).all()
        return [row[0] for row in rows if row[0]]

    async def pending_auto_count(
        self,
        user_id: int,
        account_id: int,
        *,
        now: datetime,
    ) -> int:
        row = (
            await self.session.execute(
                _PENDING_AUTO_COUNT_SQL,
                {
                    "uid": user_id,
                    "account_id": account_id,
                    "now": ensure_aware(now),
                },
            )
        ).first()
        return int(row[0] if row else 0)

    async def _planning_candidates_locked(
        self,
        user_id: int,
        account_id: int,
        *,
        settings: AutopostSettings,
        now: datetime,
        pending_cap: int | None,
    ) -> tuple[datetime, ...]:
        pending_count = await self.pending_auto_count(
            user_id,
            account_id,
            now=now,
        )
        if pending_cap is not None and pending_count >= pending_cap:
            return ()
        occupied = await self.occupied_slots(
            user_id,
            account_id,
            now=now,
            timezone_name=settings.timezone,
        )
        blocked = await self.blocked_manual_slots(
            user_id,
            account_id,
            now=now,
            timezone_name=settings.timezone,
        )
        blocked.extend(await self.retry_blocked_slots(
            user_id,
            account_id,
            now=now,
            timezone_name=settings.timezone,
        ))
        candidates = select_planning_slots(
            now=now,
            slots=settings.slots,
            days=settings.days,
            timezone_name=settings.timezone,
            posts_per_day=settings.posts_per_day,
            occupied=occupied,
            blocked=blocked,
        )
        if pending_cap is None:
            return candidates
        return candidates[:max(0, pending_cap - pending_count)]

    async def planning_deficit(
        self,
        user_id: int,
        account_id: int,
        *,
        settings: AutopostSettings,
        now: datetime,
        pending_cap: int | None = None,
    ) -> int:
        await self.lock_queue(user_id, account_id)
        candidates = await self._planning_candidates_locked(
            user_id,
            account_id,
            settings=settings,
            now=ensure_aware(now),
            pending_cap=pending_cap,
        )
        return len(candidates)

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
                        SELECT 1
                        FROM autocontent_settings setting
                        JOIN threads_accounts account
                          ON account.id = setting.threads_account_id
                         AND account.user_id = setting.user_id
                        WHERE setting.user_id = :uid
                          AND setting.threads_account_id = :account_id
                          AND setting.active
                          AND account.connection_status = 'connected'
                          AND account.access_token_enc IS NOT NULL
                          AND account.expires_at > now()
                    )
                    ON CONFLICT DO NOTHING
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

    async def reserve_next_run(
        self,
        user_id: int,
        account_id: int,
        *,
        settings: AutopostSettings,
        now: datetime,
        pending_cap: int | None = None,
    ) -> tuple[int, datetime] | None:
        await self.lock_queue(user_id, account_id)
        candidates = await self._planning_candidates_locked(
            user_id,
            account_id,
            settings=settings,
            now=ensure_aware(now),
            pending_cap=pending_cap,
        )
        for scheduled_at in candidates:
            run_id = await self.reserve_run(
                user_id,
                account_id,
                scheduled_at,
            )
            if run_id is not None:
                return run_id, scheduled_at
        return None

    async def queue_summary(
        self,
        user_id: int,
        account_id: int,
        *,
        now: datetime | None = None,
    ) -> AutopostQueueSummary | None:
        current = ensure_aware(now or utc_now())
        status = await self.get_status(
            user_id,
            account_id,
            now=current,
        )
        if status is None:
            return None
        rows = (
            await self.session.execute(
                _AUTO_QUEUE_SQL,
                {
                    "uid": user_id,
                    "account_id": account_id,
                    "now": current,
                },
            )
        ).mappings().all()
        posts = tuple(
            AutopostQueueItem(
                id=row["id"],
                text=row["text"],
                run_at=row["run_at"],
            )
            for row in rows
        )
        tz = resolve_timezone(status.settings.timezone)
        local_today = current.astimezone(tz).date()
        counts = Counter(
            ensure_aware(post.run_at).astimezone(tz).date()
            for post in posts
        )
        blocked = await self.blocked_manual_slots(
            user_id,
            account_id,
            now=current,
            timezone_name=status.settings.timezone,
        )
        summary_days = queue_summary_dates(
            local_today,
            status.settings.days,
        )
        days = tuple(
            AutopostQueueDay(
                day=day,
                queued=counts[day],
                capacity=available_day_capacity(
                    now=current,
                    day=day,
                    slots=status.settings.slots,
                    days=status.settings.days,
                    timezone_name=status.settings.timezone,
                    posts_per_day=status.settings.posts_per_day,
                    blocked=blocked,
                ),
            )
            for day in summary_days
        )
        return AutopostQueueSummary(
            account=status.account,
            settings=status.settings,
            posts=posts,
            days=days,
        )

    async def rebuild_queue(
        self,
        user_id: int,
        account_id: int,
        *,
        now: datetime | None = None,
    ) -> AutopostQueueRebuildResult:
        current = ensure_aware(now or utc_now())
        status = await self.get_status(
            user_id,
            account_id,
            now=current,
        )
        if status is None:
            raise ValueError("Threads account not found")
        await self.lock_queue(user_id, account_id)
        rows = (
            await self.session.execute(
                text("""
                    SELECT post.id, post.text, post.run_at
                    FROM scheduled_posts post
                    WHERE post.user_id = :uid
                      AND post.threads_account_id = :account_id
                      AND post.status = 'pending'
                      AND post.run_at > :now
                      AND (
                        post.content_metadata ->> 'source' = 'autocontent'
                        OR EXISTS (
                          SELECT 1 FROM autopost_runs run
                          WHERE run.scheduled_post_id = post.id
                        )
                      )
                    ORDER BY post.run_at, post.id
                    FOR UPDATE OF post SKIP LOCKED
                """),
                {
                    "uid": user_id,
                    "account_id": account_id,
                    "now": current,
                },
            )
        ).mappings().all()
        if not rows:
            return AutopostQueueRebuildResult(
                moved_posts=0,
                posts_per_day=status.settings.posts_per_day,
            )
        post_ids = [int(row["id"]) for row in rows]
        occupied = await self.occupied_slots(
            user_id,
            account_id,
            now=current,
            timezone_name=status.settings.timezone,
            exclude_post_ids=post_ids,
        )
        blocked = await self.blocked_manual_slots(
            user_id,
            account_id,
            now=current,
            timezone_name=status.settings.timezone,
        )
        schedule = select_planning_slots(
            now=current,
            slots=status.settings.slots,
            days=status.settings.days,
            timezone_name=status.settings.timezone,
            posts_per_day=status.settings.posts_per_day,
            occupied=occupied,
            blocked=blocked,
            active_days=PLANNING_ACTIVE_DAYS,
            required_slots=len(rows),
            search_calendar_days=max(
                PLANNING_SEARCH_DAYS,
                len(rows) * 3 + 7,
            ),
        )
        if len(schedule) < len(rows):
            raise ValueError("Not enough configured future slots")

        await self.session.execute(
            text("""
                UPDATE autopost_runs
                SET status = 'skipped'
                WHERE scheduled_post_id = ANY(
                    CAST(:post_ids AS bigint[])
                )
                  AND status = 'pending'
            """),
            {"post_ids": post_ids},
        )
        moved = 0
        for row, scheduled_at in zip(rows, schedule):
            post_id = int(row["id"])
            if ensure_aware(row["run_at"]) != scheduled_at:
                moved += 1
            updated = (
                await self.session.execute(
                    text("""
                        UPDATE scheduled_posts
                        SET run_at = :scheduled_at
                        WHERE id = :post_id
                          AND user_id = :uid
                          AND threads_account_id = :account_id
                          AND status = 'pending'
                        RETURNING id
                    """),
                    {
                        "scheduled_at": scheduled_at,
                        "post_id": post_id,
                        "uid": user_id,
                        "account_id": account_id,
                    },
                )
            ).first()
            if updated is None:
                raise RuntimeError("Queue post changed during rebuild")
            run = (
                await self.session.execute(
                    text("""
                        UPDATE autopost_runs
                        SET scheduled_at = :scheduled_at,
                            status = 'pending',
                            finished_at = NULL,
                            error_code = NULL,
                            safe_error_message = NULL
                        WHERE scheduled_post_id = :post_id
                          AND status = 'skipped'
                          AND finished_at IS NULL
                        RETURNING id
                    """),
                    {
                        "scheduled_at": scheduled_at,
                        "post_id": post_id,
                    },
                )
            ).first()
            if run is None:
                await self.session.execute(
                    text("""
                        INSERT INTO autopost_runs (
                            user_id, threads_account_id,
                            scheduled_post_id, scheduled_at,
                            started_at, status
                        ) VALUES (
                            :uid, :account_id, :post_id,
                            :scheduled_at, now(), 'pending'
                        )
                    """),
                    {
                        "uid": user_id,
                        "account_id": account_id,
                        "post_id": post_id,
                        "scheduled_at": scheduled_at,
                    },
                )
        tz = resolve_timezone(status.settings.timezone)
        filled_days = len({
            ensure_aware(value).astimezone(tz).date()
            for value in schedule
        })
        today = current.astimezone(tz).date()
        return AutopostQueueRebuildResult(
            moved_posts=moved,
            first_post_at=schedule[0],
            filled_days=filled_days,
            posts_per_day=status.settings.posts_per_day,
            today_has_no_slots=(
                ensure_aware(schedule[0]).astimezone(tz).date()
                != today
            ),
        )

    async def clear_queue(
        self,
        user_id: int,
        account_id: int,
        *,
        disable_autoposting: bool,
        now: datetime | None = None,
    ) -> AutopostQueueClearResult:
        current = ensure_aware(now or utc_now())
        status = await self.get_status(
            user_id,
            account_id,
            now=current,
        )
        if status is None:
            raise ValueError("Threads account not found")
        await self.lock_queue(user_id, account_id)
        rows = (
            await self.session.execute(
                text("""
                    SELECT post.id
                    FROM scheduled_posts post
                    WHERE post.user_id = :uid
                      AND post.threads_account_id = :account_id
                      AND post.status = 'pending'
                      AND post.run_at > :now
                      AND (
                        post.content_metadata ->> 'source' = 'autocontent'
                        OR EXISTS (
                          SELECT 1 FROM autopost_runs run
                          WHERE run.scheduled_post_id = post.id
                        )
                      )
                    FOR UPDATE OF post SKIP LOCKED
                """),
                {
                    "uid": user_id,
                    "account_id": account_id,
                    "now": current,
                },
            )
        ).all()
        post_ids = [int(row[0]) for row in rows]
        await self.session.execute(
            text("""
                UPDATE autopost_runs
                SET status = 'skipped',
                    finished_at = :now,
                    error_code = NULL,
                    safe_error_message = :message
                WHERE user_id = :uid
                  AND threads_account_id = :account_id
                  AND status = 'pending'
                  AND scheduled_at > :now
                  AND (
                    scheduled_post_id IS NULL
                    OR scheduled_post_id = ANY(
                      CAST(:post_ids AS bigint[])
                    )
                  )
            """),
            {
                "uid": user_id,
                "account_id": account_id,
                "now": current,
                "post_ids": post_ids,
                "message": QUEUE_CLEARED_MESSAGE,
            },
        )
        deleted = []
        if post_ids:
            deleted = (
                await self.session.execute(
                    text("""
                        DELETE FROM scheduled_posts
                        WHERE id = ANY(CAST(:post_ids AS bigint[]))
                          AND user_id = :uid
                          AND threads_account_id = :account_id
                          AND status = 'pending'
                        RETURNING id
                    """),
                    {
                        "post_ids": post_ids,
                        "uid": user_id,
                        "account_id": account_id,
                    },
                )
            ).all()
        if disable_autoposting:
            await self.session.execute(
                text("""
                    UPDATE autocontent_settings
                    SET active = false
                    WHERE user_id = :uid
                      AND threads_account_id = :account_id
                """),
                {"uid": user_id, "account_id": account_id},
            )
        return AutopostQueueClearResult(
            deleted_posts=len(deleted),
            autoposting_disabled=disable_autoposting,
        )

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

    async def skip_pending_for_account(
        self,
        user_id: int,
        account_id: int,
    ) -> int:
        rows = (
            await self.session.execute(
                text("""
                    WITH cancellable_posts AS (
                      SELECT post.id
                      FROM scheduled_posts post
                      WHERE post.user_id = :uid
                        AND post.threads_account_id = :account_id
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
                      AND run.threads_account_id = :account_id
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
                    "account_id": account_id,
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
                  AND threads_account_id = :account_id
                  AND status = 'pending'
                  AND (
                    id = ANY(CAST(:post_ids AS bigint[]))
                    OR content_metadata ->> 'source' = 'autocontent'
                  )
            """),
            {
                "uid": user_id,
                "account_id": account_id,
                "post_ids": post_ids,
            },
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
                    "технического перезапуска"
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
                "message": "Публикация пропущена после технического перезапуска",
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
        "✍️ Автопилот",
        "",
        f"Аккаунт: @{status.account.username or status.account.id}",
        f"Статус: {'🟢 Включён' if enabled else '⚪ Выключен'}",
        "",
        "Создаёт и публикует посты по заданному расписанию.",
        "",
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


def render_queue_summary(summary: AutopostQueueSummary) -> str:
    """Render the account-local automatic queue overview."""
    lines = [
        "📋 Очередь Автопилота",
        "",
        f"Аккаунт: @{summary.account.username or summary.account.id}",
        f"Постов в день: {summary.settings.posts_per_day}",
        f"Часовой пояс: {summary.settings.timezone}",
        f"Будущих постов: {len(summary.posts)}",
    ]
    labels = ("Сегодня", "Завтра", "Следующий активный день")
    for index, day in enumerate(summary.days[:3]):
        label = labels[index]
        if index == 2:
            label = f"{label} ({day.day:%d.%m})"
        if not is_active_day(day.day, summary.settings.days):
            lines.append(f"{label}: неактивный день")
        else:
            lines.append(
                f"{label}: {day.queued} из {day.capacity} "
                "доступных слотов"
            )
    return "\n".join(lines)


def render_rebuild_result(
    result: AutopostQueueRebuildResult,
    timezone_name: str,
    *,
    now: datetime | None = None,
) -> str:
    lines = ["✅ Очередь перестроена", ""]
    if result.first_post_at is None:
        lines.append("Будущих автопостов в очереди нет.")
        return "\n".join(lines)
    current = ensure_aware(now or utc_now())
    tz = resolve_timezone(timezone_name)
    local_first = ensure_aware(result.first_post_at).astimezone(tz)
    local_today = current.astimezone(tz).date()
    if local_first.date() == local_today:
        first_label = f"сегодня в {local_first:%H:%M}"
    elif local_first.date() == local_today + timedelta(days=1):
        first_label = f"завтра в {local_first:%H:%M}"
    else:
        first_label = f"{local_first:%d.%m} в {local_first:%H:%M}"
    lines.extend([
        f"Перенесено постов: {result.moved_posts}",
        f"Первый пост: {first_label}",
        f"Заполнено дней: {result.filled_days}",
        f"Постов в день: {result.posts_per_day}",
    ])
    if result.today_has_no_slots:
        lines.extend([
            "",
            "Сегодня свободных слотов больше нет. Очередь начнётся "
            "со следующего активного дня.",
        ])
    return "\n".join(lines)


def render_clear_result(result: AutopostQueueClearResult) -> str:
    lines = [
        "✅ Очередь очищена",
        "",
        f"Удалено будущих постов: {result.deleted_posts}",
        "Возвращено кредитов: 0",
    ]
    if result.autoposting_disabled:
        lines.extend(["", "Автопилот выключен."])
    else:
        lines.extend([
            "",
            "Автопилот остаётся включён и снова начнёт создавать "
            "новые посты.",
        ])
    return "\n".join(lines)
