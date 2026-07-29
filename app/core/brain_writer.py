"""Append-only Brain events and idempotent canonical-data backfill."""

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.brain_repo import BrainRepo
from app.core.brain_safety import assert_safe_payload
from app.schemas.social_brain import BrainRecord, BrainSection

MAX_EVENT_PAYLOAD_CHARS = 16_000
_BACKFILL_META_KEY = "_backfill"
_INSIGHT_METRICS = (
    "views",
    "likes",
    "replies",
    "reposts",
    "quotes",
    "shares",
)

_INSERT_EVENT_SQL = text("""
    INSERT INTO brain_events (
        brain_id,
        type,
        payload,
        source_type,
        source_id,
        event_key,
        occurred_at,
        created_at
    )
    VALUES (
        :brain_id,
        :event_type,
        CAST(:payload AS jsonb),
        :source_type,
        :source_id,
        :event_key,
        :occurred_at,
        now()
    )
    ON CONFLICT (brain_id, event_key)
      WHERE event_key IS NOT NULL
    DO NOTHING
    RETURNING id
""")

_BACKFILL_CONFIG_SQL = text("""
    SELECT
        (
            SELECT count(*)
            FROM threads_accounts owned
            WHERE owned.user_id = account.user_id
        ) AS account_count,
        vp.profile_json AS voice_profile,
        vp.sample_posts AS voice_samples,
        vp.updated_at AS voice_updated_at,
        un.niche,
        un.keywords,
        un.created_at AS niche_created_at,
        ac.active AS autocontent_active,
        ac.posts_per_day,
        ac.user_id AS autocontent_user_id,
        ac.goal AS autocontent_goal,
        ac.created_at AS autocontent_created_at
    FROM threads_accounts account
    LEFT JOIN voice_profiles vp
      ON vp.user_id = account.user_id
    LEFT JOIN user_niches un
      ON un.user_id = account.user_id
    LEFT JOIN autocontent_settings ac
      ON ac.user_id = account.user_id
    WHERE account.id = :account_id
      AND account.user_id = :uid
""")

_BACKFILL_PERFORMANCE_SQL = text("""
    WITH recent_posts AS (
        SELECT status, threads_post_id
        FROM scheduled_posts
        WHERE user_id = :uid
          AND threads_account_id = :account_id
          AND run_at >= now() - interval '30 days'
    ),
    latest_insights AS (
        SELECT recent.threads_post_id, snapshot.metrics_json
        FROM recent_posts recent
        JOIN LATERAL (
            SELECT metrics_json
            FROM insights_snapshots
            WHERE threads_post_id = recent.threads_post_id
            ORDER BY snapshot_date DESC
            LIMIT 1
        ) snapshot ON true
        WHERE recent.threads_post_id IS NOT NULL
    )
    SELECT
        count(*) AS total_posts,
        count(*) FILTER (WHERE recent.status = 'done')
            AS published_posts,
        count(*) FILTER (WHERE recent.status = 'failed')
            AS failed_posts,
        count(insights.threads_post_id) AS insight_posts,
        coalesce(sum(
            CASE WHEN coalesce(
                insights.metrics_json->>'views', ''
            ) ~ '^[0-9]+$'
            THEN (insights.metrics_json->>'views')::bigint
            ELSE 0 END
        ), 0)::bigint AS views,
        coalesce(sum(
            CASE WHEN coalesce(
                insights.metrics_json->>'likes', ''
            ) ~ '^[0-9]+$'
            THEN (insights.metrics_json->>'likes')::bigint
            ELSE 0 END
        ), 0)::bigint AS likes,
        coalesce(sum(
            CASE WHEN coalesce(
                insights.metrics_json->>'replies', ''
            ) ~ '^[0-9]+$'
            THEN (insights.metrics_json->>'replies')::bigint
            ELSE 0 END
        ), 0)::bigint AS replies,
        coalesce(sum(
            CASE WHEN coalesce(
                insights.metrics_json->>'reposts', ''
            ) ~ '^[0-9]+$'
            THEN (insights.metrics_json->>'reposts')::bigint
            ELSE 0 END
        ), 0)::bigint AS reposts,
        coalesce(sum(
            CASE WHEN coalesce(
                insights.metrics_json->>'quotes', ''
            ) ~ '^[0-9]+$'
            THEN (insights.metrics_json->>'quotes')::bigint
            ELSE 0 END
        ), 0)::bigint AS quotes,
        coalesce(sum(
            CASE WHEN coalesce(
                insights.metrics_json->>'shares', ''
            ) ~ '^[0-9]+$'
            THEN (insights.metrics_json->>'shares')::bigint
            ELSE 0 END
        ), 0)::bigint AS shares
    FROM recent_posts recent
    LEFT JOIN latest_insights insights
      ON insights.threads_post_id = recent.threads_post_id
""")


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return value


def _json_dict(value: Any) -> dict[str, Any]:
    parsed = _json_value(value, {})
    return parsed if isinstance(parsed, dict) else {}


def _string_list(value: Any) -> list[str]:
    parsed = _json_value(value, value)
    if not isinstance(parsed, (list, tuple)):
        return []
    return [
        item
        for raw in parsed
        if (item := str(raw).strip())
    ]


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _fingerprint(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(
        serialized.encode("utf-8")
    ).hexdigest()[:24]


def _merge_missing(
    current: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    result = dict(current)
    for key, value in incoming.items():
        if key not in result:
            result[key] = value
        elif isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge_missing(result[key], value)
    return result


def _event_time(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.now(timezone.utc)


class BrainWriter:
    """The only application boundary that appends `brain_events` rows."""

    def __init__(
        self,
        session: AsyncSession,
        repo: BrainRepo | None = None,
    ):
        self.session = session
        self.repo = repo or BrainRepo(session)

    async def record_event(
        self,
        brain_id: int,
        event_type: str,
        *,
        payload: dict[str, Any] | None = None,
        source_type: str | None = None,
        source_id: str | int | None = None,
        event_key: str | None = None,
        occurred_at: datetime | None = None,
    ) -> int | None:
        event_type = event_type.strip()
        if not event_type:
            raise ValueError("Brain event type cannot be empty")
        if event_key is not None and not event_key.strip():
            raise ValueError("Brain event_key cannot be empty")

        payload = payload or {}
        assert_safe_payload(payload)
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(serialized) > MAX_EVENT_PAYLOAD_CHARS:
            raise ValueError("Brain event payload is too large")

        result = await self.session.execute(
            _INSERT_EVENT_SQL,
            {
                "brain_id": brain_id,
                "event_type": event_type,
                "payload": serialized,
                "source_type": (
                    source_type.strip()
                    if source_type is not None
                    else None
                ),
                "source_id": (
                    str(source_id)
                    if source_id is not None
                    else None
                ),
                "event_key": (
                    event_key.strip()
                    if event_key is not None
                    else None
                ),
                "occurred_at": (
                    occurred_at or datetime.now(timezone.utc)
                ),
            },
        )
        row = result.first()
        return row[0] if row else None

    async def record_post_published(
        self,
        user_id: int,
        account_id: int,
        *,
        scheduled_post_id: int,
        threads_post_id: str,
        occurred_at: datetime | None = None,
    ) -> int | None:
        brain = await self.repo.get_or_create(user_id, account_id)
        return await self.record_event(
            brain.id,
            "post_published",
            payload={"threads_post_id": threads_post_id},
            source_type="scheduled_post",
            source_id=scheduled_post_id,
            event_key=(
                f"post_published:scheduled_post:{scheduled_post_id}"
            ),
            occurred_at=occurred_at,
        )

    async def record_insights_snapshot(
        self,
        user_id: int,
        account_id: int,
        *,
        threads_post_id: str,
        snapshot_date: date,
        metrics: dict[str, Any],
        occurred_at: datetime | None = None,
    ) -> int | None:
        brain = await self.repo.get_or_create(user_id, account_id)
        safe_metrics = {
            key: _as_int(metrics.get(key))
            for key in _INSIGHT_METRICS
            if key in metrics
        }
        source_id = f"{threads_post_id}:{snapshot_date.isoformat()}"
        return await self.record_event(
            brain.id,
            "insights_snapshot",
            payload={
                "threads_post_id": threads_post_id,
                "snapshot_date": snapshot_date.isoformat(),
                "metrics": safe_metrics,
            },
            source_type="insights_snapshot",
            source_id=source_id,
            event_key=f"insights_snapshot:{source_id}",
            occurred_at=occurred_at,
        )

    async def _load_backfill_sources(
        self,
        user_id: int,
        account_id: int,
    ) -> dict[str, Any]:
        params = {"uid": user_id, "account_id": account_id}
        config_result = await self.session.execute(
            _BACKFILL_CONFIG_SQL,
            params,
        )
        config = config_result.mappings().first()
        performance_result = await self.session.execute(
            _BACKFILL_PERFORMANCE_SQL,
            params,
        )
        performance = performance_result.mappings().first() or {}
        return {
            "config": dict(config) if config is not None else {},
            "performance": dict(performance),
        }

    async def _sync_section(
        self,
        brain: BrainRecord,
        section: BrainSection,
        source_name: str,
        incoming: dict[str, Any],
        source_fingerprint: str,
    ) -> BrainRecord:
        current = dict(getattr(brain, section))
        metadata = current.get(_BACKFILL_META_KEY)
        if not isinstance(metadata, dict):
            metadata = {}
        else:
            metadata = dict(metadata)
        marker = metadata.get(source_name)
        if (
            isinstance(marker, dict)
            and marker.get("source_fingerprint")
            == source_fingerprint
        ):
            return brain

        target_before = {
            key: current[key]
            for key in incoming
            if key in current
        }
        managed = not target_before
        if isinstance(marker, dict) and marker.get("managed") is True:
            managed = (
                marker.get("value_fingerprint")
                == _fingerprint(target_before)
            )

        if managed:
            merged = dict(current)
            merged.update(incoming)
        else:
            merged = _merge_missing(current, incoming)

        target_after = {
            key: merged[key]
            for key in incoming
            if key in merged
        }
        new_marker: dict[str, Any] = {
            "source_fingerprint": source_fingerprint,
            "managed": managed,
        }
        if managed:
            new_marker["value_fingerprint"] = _fingerprint(
                target_after
            )
        metadata[source_name] = new_marker
        merged[_BACKFILL_META_KEY] = metadata
        if merged == current:
            return brain
        return await self.repo.update_section(
            brain.id,
            section,
            merged,
            user_id=brain.user_id,
            account_id=brain.threads_account_id,
        )

    async def apply_backfill(
        self,
        user_id: int,
        account_id: int,
    ) -> BrainRecord:
        brain = await self.repo.get_or_create(user_id, account_id)
        sources = await self._load_backfill_sources(
            user_id,
            account_id,
        )
        config = sources["config"]
        performance = sources["performance"]

        # Existing user-level settings cannot be assigned safely when a
        # user has multiple Threads accounts.
        if _as_int(config.get("account_count")) == 1:
            profile = _json_dict(config.get("voice_profile"))
            samples = [
                sample[:500]
                for sample in _string_list(
                    config.get("voice_samples")
                )[-3:]
            ]
            if profile or samples:
                dna: dict[str, Any] = {}
                if profile:
                    dna["voice"] = profile
                if samples:
                    dna["recent_examples"] = samples
                fingerprint = _fingerprint(dna)
                brain = await self._sync_section(
                    brain,
                    "dna",
                    "voice_profiles",
                    dna,
                    fingerprint,
                )
                await self.record_event(
                    brain.id,
                    "backfill_applied",
                    payload={
                        "section": "dna",
                        "fields": sorted(dna),
                    },
                    source_type="voice_profiles",
                    source_id=user_id,
                    event_key=f"backfill:voice_profiles:{fingerprint}",
                    occurred_at=_event_time(
                        config.get("voice_updated_at")
                    ),
                )

            niche = config.get("niche")
            keywords = _string_list(config.get("keywords"))
            if niche or keywords:
                audience = {
                    "niche": str(niche).strip() if niche else None,
                    "keywords": keywords,
                }
                audience = {
                    key: value
                    for key, value in audience.items()
                    if value not in (None, "", [])
                }
                fingerprint = _fingerprint(audience)
                brain = await self._sync_section(
                    brain,
                    "audience",
                    "user_niches",
                    audience,
                    fingerprint,
                )
                await self.record_event(
                    brain.id,
                    "backfill_applied",
                    payload={
                        "section": "audience",
                        "fields": sorted(audience),
                    },
                    source_type="user_niches",
                    source_id=user_id,
                    event_key=f"backfill:user_niches:{fingerprint}",
                    occurred_at=_event_time(
                        config.get("niche_created_at")
                    ),
                )

            autocontent_active = config.get("autocontent_active")
            posts_per_day = config.get("posts_per_day")
            if config.get("autocontent_user_id") is not None:
                goal_value = config.get("autocontent_goal")
                goal = (
                    str(goal_value).strip()
                    if goal_value is not None
                    else ""
                )
                canonical_goal = {
                    "primary": goal or None,
                }
                fingerprint = _fingerprint(canonical_goal)
                brain = await self._sync_section(
                    brain,
                    "goals",
                    "autocontent_goal",
                    canonical_goal,
                    fingerprint,
                )
                await self.record_event(
                    brain.id,
                    "backfill_applied",
                    payload={
                        "section": "goals",
                        "fields": ["primary"],
                    },
                    source_type="autocontent_settings",
                    source_id=user_id,
                    event_key=(
                        f"backfill:autocontent_goal:{fingerprint}"
                    ),
                    occurred_at=_event_time(
                        config.get("autocontent_created_at")
                    ),
                )

            if autocontent_active is not None or posts_per_day is not None:
                autocontent = {
                    "autocontent": {
                        "active": bool(autocontent_active),
                        "posts_per_day": _as_int(posts_per_day),
                    }
                }
                fingerprint = _fingerprint(autocontent)
                brain = await self._sync_section(
                    brain,
                    "constraints",
                    "autocontent_settings",
                    autocontent,
                    fingerprint,
                )
                await self.record_event(
                    brain.id,
                    "backfill_applied",
                    payload={
                        "section": "constraints",
                        "fields": ["autocontent"],
                    },
                    source_type="autocontent_settings",
                    source_id=user_id,
                    event_key=(
                        f"backfill:autocontent_settings:{fingerprint}"
                    ),
                    occurred_at=_event_time(
                        config.get("autocontent_created_at")
                    ),
                )

        total_posts = _as_int(performance.get("total_posts"))
        if total_posts:
            rolling = {
                "window_days": 30,
                "total_posts": total_posts,
                "published_posts": _as_int(
                    performance.get("published_posts")
                ),
                "failed_posts": _as_int(
                    performance.get("failed_posts")
                ),
                "insight_posts": _as_int(
                    performance.get("insight_posts")
                ),
                "metrics": {
                    key: _as_int(performance.get(key))
                    for key in _INSIGHT_METRICS
                },
            }
            incoming_performance = {"rolling_30d": rolling}
            fingerprint = _fingerprint(incoming_performance)
            brain = await self._sync_section(
                brain,
                "performance",
                "scheduled_posts",
                incoming_performance,
                fingerprint,
            )
            await self.record_event(
                brain.id,
                "backfill_applied",
                payload={
                    "section": "performance",
                    "fields": ["rolling_30d"],
                },
                source_type="scheduled_posts",
                source_id=account_id,
                event_key=(
                    f"backfill:scheduled_posts:{account_id}:"
                    f"{fingerprint}"
                ),
            )

        return brain
