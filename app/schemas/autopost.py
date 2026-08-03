"""Typed account-scoped contracts for Autopilot Status."""

from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AutopostRunStatus = Literal[
    "success",
    "failed",
    "skipped",
    "pending",
]
AutopostErrorCode = Literal[
    "AUTH_EXPIRED",
    "PERMISSION_DENIED",
    "THREADS_TEMPORARY_ERROR",
    "INSUFFICIENT_CREDITS",
    "GENERATION_FAILED",
    "QUALITY_FAILED",
    "UNKNOWN_ERROR",
]
AutopostDays = Literal["all", "weekdays"]


class AutopostModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AutopostSettings(AutopostModel):
    enabled: bool = False
    posts_per_day: int = Field(default=1, ge=0, le=5)
    slots: tuple[time, ...] = ()
    days: AutopostDays = "all"
    timezone: str = "Europe/Moscow"


class AutopostAccount(AutopostModel):
    id: int
    username: str | None = None
    expires_at: datetime


class AutopostRun(AutopostModel):
    id: int
    user_id: int
    threads_account_id: int
    scheduled_post_id: int | None = None
    scheduled_at: datetime
    started_at: datetime
    finished_at: datetime | None = None
    status: AutopostRunStatus
    threads_post_id: str | None = None
    error_code: AutopostErrorCode | None = None
    safe_error_message: str | None = None


class AutopostStatus(AutopostModel):
    account: AutopostAccount
    settings: AutopostSettings
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_run_status: AutopostRunStatus | None = None
    last_success_at: datetime | None = None
    last_threads_post_id: str | None = None
    safe_error_code: AutopostErrorCode | None = None
    safe_error_message: str | None = None


class AutopostQueueItem(AutopostModel):
    id: int
    text: str
    run_at: datetime


class AutopostQueueDay(AutopostModel):
    day: date
    queued: int = Field(ge=0)
    capacity: int = Field(ge=0)


class AutopostQueueSummary(AutopostModel):
    account: AutopostAccount
    settings: AutopostSettings
    posts: tuple[AutopostQueueItem, ...] = ()
    days: tuple[AutopostQueueDay, ...] = ()


class AutopostQueueRebuildResult(AutopostModel):
    moved_posts: int = Field(ge=0)
    first_post_at: datetime | None = None
    filled_days: int = Field(default=0, ge=0)
    posts_per_day: int = Field(ge=0, le=5)
    today_has_no_slots: bool = False


class AutopostQueueClearResult(AutopostModel):
    deleted_posts: int = Field(ge=0)
    autoposting_disabled: bool = False
