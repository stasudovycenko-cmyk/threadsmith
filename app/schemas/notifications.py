"""Typed Telegram publication notification contracts."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

PublicationOutcome = Literal["success", "failed", "unknown"]


class PublicationNotification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduled_post_id: int
    user_id: int
    telegram_id: int
    threads_account_id: int
    username: str
    text: str
    timezone: str = "Europe/Moscow"
    outcome: PublicationOutcome
    published_at: datetime
    threads_post_id: str | None = None
    source: str | None = None
    permalink: str | None = None
    safe_error_message: str | None = None
