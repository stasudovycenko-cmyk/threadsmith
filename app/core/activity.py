"""Unified read-only activity feed over existing account-scoped records."""

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.ux import ActivityItem

_ACTIVITY_SQL = text("""
    WITH activity AS (
      SELECT
        CASE post.status
          WHEN 'pending' THEN 'post_scheduled'
          WHEN 'publishing' THEN 'post_publishing'
          WHEN 'done' THEN 'post_published'
          WHEN 'failed' THEN 'publication_failed'
          ELSE 'post_created'
        END AS event_type,
        post.run_at AS occurred_at,
        jsonb_build_object('preview', left(post.text, 80)) AS payload
      FROM scheduled_posts post
      WHERE post.user_id = :user_id
        AND post.threads_account_id = :account_id

      UNION ALL

      SELECT 'radar_search_completed',
             coalesce(run.finished_at, run.started_at),
             jsonb_build_object(
               'status', run.status,
               'found', run.candidates_saved
             )
      FROM radar_search_runs run
      WHERE run.user_id = :user_id
        AND run.threads_account_id = :account_id
        AND run.status <> 'running'

      UNION ALL

      SELECT 'radar_candidate_found', candidate.discovered_at,
             jsonb_build_object('author', candidate.author_username)
      FROM radar_candidates candidate
      WHERE candidate.user_id = :user_id
        AND candidate.threads_account_id = :account_id
        AND candidate.status IN ('ready', 'pending', 'commented')

      UNION ALL

      SELECT
        CASE
          WHEN comment.author_replied THEN 'author_replied'
          WHEN comment.status = 'posted' THEN 'comment_published'
          ELSE 'comment_prepared'
        END,
        coalesce(comment.replied_at, comment.posted_at, comment.created_at),
        jsonb_build_object('author', comment.target_author)
      FROM neuro_comments comment
      WHERE comment.user_id = :user_id
        AND comment.threads_account_id = :account_id
        AND (comment.status IN ('pending', 'posted') OR comment.author_replied)

      UNION ALL

      SELECT event.type, event.occurred_at, event.payload
      FROM brain_events event
      JOIN brains brain ON brain.id = event.brain_id
      WHERE brain.user_id = :user_id
        AND brain.threads_account_id = :account_id
        AND event.type IN (
          'POST_PERFORMANCE_UPDATED', 'feedback_rebuilt',
          'strategy_updated', 'account_reconnected', 'settings_changed'
        )
    )
    SELECT event_type, occurred_at, payload
    FROM activity
    ORDER BY occurred_at DESC, event_type
    LIMIT :row_limit OFFSET :row_offset
""")


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _render(row: Mapping[str, Any]) -> ActivityItem:
    event_type = str(row["event_type"])
    payload = _payload(row.get("payload"))
    titles = {
        "post_created": "Пост создан",
        "post_scheduled": "Пост запланирован",
        "post_publishing": "Пост публикуется",
        "post_published": "Пост опубликован",
        "publication_failed": "Публикация не удалась",
        "radar_search_completed": "Radar завершил поиск",
        "radar_candidate_found": "Найден подходящий пост",
        "comment_prepared": "Комментарий подготовлен",
        "comment_published": "Комментарий опубликован",
        "author_replied": "Автор ответил",
        "POST_PERFORMANCE_UPDATED": "Аналитика обновилась",
        "feedback_rebuilt": "Автопилот обновил рекомендации",
        "strategy_updated": "Автопилот обновил рекомендации",
        "account_reconnected": "Аккаунт переподключён",
        "settings_changed": "Настройки изменены",
    }
    detail = None
    title = titles.get(event_type, "Активность обновлена")
    if payload.get("preview"):
        detail = str(payload["preview"])
    elif payload.get("author"):
        detail = "@" + str(payload["author"]).lstrip("@")
    elif event_type == "radar_search_completed":
        detail = f"Подходящих постов: {int(payload.get('found') or 0)}"
    elif event_type == "POST_PERFORMANCE_UPDATED":
        scores = payload.get("scores")
        brain_score = scores.get("brain_score") if isinstance(scores, dict) else None
        if brain_score is not None and float(brain_score) >= 80:
            title = "Пост достиг сильного результата"
            detail = f"Brain Score: {float(brain_score):.0f}"
    return ActivityItem(
        event_type=event_type,
        occurred_at=row["occurred_at"],
        title=title,
        detail=detail,
    )


class ActivityFeedService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_events(
        self,
        user_id: int,
        account_id: int,
        *,
        page: int = 0,
        page_size: int = 10,
    ) -> list[ActivityItem]:
        page = max(0, page)
        page_size = min(20, max(1, page_size))
        rows = (
            await self.session.execute(_ACTIVITY_SQL, {
                "user_id": user_id,
                "account_id": account_id,
                "row_limit": page_size,
                "row_offset": page * page_size,
            })
        ).mappings().all()
        return [_render(row) for row in rows]
