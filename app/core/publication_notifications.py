"""Durable at-most-once Telegram notifications for scheduled posts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.notifications import PublicationNotification, PublicationOutcome


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _source_label(metadata: Mapping[str, Any]) -> str | None:
    source = str(metadata.get("source") or "").casefold()
    if source == "autocontent":
        return "Автопилот"
    if source in {"manual", "scenarist"}:
        return "Вручную"
    if source in {"republish", "repost"}:
        return "Повторная публикация"
    return None


class PublicationNotificationService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def claim(
        self,
        scheduled_post_id: int,
        outcome: PublicationOutcome,
    ) -> PublicationNotification | None:
        row = (
            await self.session.execute(text("""
                UPDATE scheduled_posts post
                SET publication_notification_claimed_at = now()
                FROM users owner,
                     threads_accounts account,
                     autocontent_settings setting
                WHERE post.id = :post_id
                  AND post.user_id = owner.id
                  AND account.id = post.threads_account_id
                  AND account.user_id = post.user_id
                  AND setting.threads_account_id = post.threads_account_id
                  AND setting.user_id = post.user_id
                  AND post.publication_notification_claimed_at IS NULL
                  AND (
                    (:outcome = 'success' AND post.status = 'done'
                      AND setting.publish_notifications_enabled)
                    OR (:outcome = 'failed' AND post.status = 'failed')
                    OR (:outcome = 'unknown' AND post.status = 'failed'
                      AND post.error LIKE 'UNKNOWN_ERROR:%')
                  )
                RETURNING
                  post.id AS scheduled_post_id,
                  post.user_id,
                  owner.telegram_id,
                  post.threads_account_id,
                  coalesce(account.username, account.id::text) AS username,
                  post.text,
                  setting.timezone,
                  post.run_at,
                  post.threads_post_id,
                  post.content_metadata,
                  (SELECT run.finished_at FROM autopost_runs run
                   WHERE run.scheduled_post_id = post.id
                   ORDER BY run.started_at DESC, run.id DESC LIMIT 1)
                    AS finished_at,
                  (SELECT run.safe_error_message FROM autopost_runs run
                   WHERE run.scheduled_post_id = post.id
                   ORDER BY run.started_at DESC, run.id DESC LIMIT 1)
                    AS safe_error_message
            """), {"post_id": scheduled_post_id, "outcome": outcome})
        ).mappings().first()
        if row is None:
            return None
        data = dict(row)
        metadata = _json_object(data.pop("content_metadata", None))
        finished_at = data.pop("finished_at", None)
        run_at = data.pop("run_at")
        published_at = finished_at or run_at
        return PublicationNotification(
            **data,
            outcome=outcome,
            published_at=published_at,
            source=_source_label(metadata),
            # Current publication response/storage has no reliable permalink.
            permalink=None,
        )

    async def recovered_unknown_post_ids(self, *, limit: int = 50) -> list[int]:
        rows = (
            await self.session.execute(text("""
                SELECT post.id
                FROM scheduled_posts post
                JOIN autopost_runs run ON run.scheduled_post_id = post.id
                WHERE post.status = 'failed'
                  AND post.error = 'UNKNOWN_ERROR: interrupted worker'
                  AND post.publication_notification_claimed_at IS NULL
                  AND run.safe_error_message LIKE
                    'Состояние публикации не подтверждено%'
                ORDER BY run.finished_at, post.id
                LIMIT :limit
            """), {"limit": limit})
        ).all()
        return [int(row[0]) for row in rows]
