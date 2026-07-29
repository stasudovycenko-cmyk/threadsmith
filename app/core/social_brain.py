"""Account-aware aggregation and persistence for Social Brain v1."""

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.social_brain import (
    BrainAccountIdentity,
    BrainAudience,
    BrainConstraints,
    BrainContentPreferences,
    BrainFact,
    BrainGoals,
    BrainMemory,
    BrainNiche,
    BrainPerformance,
    BrainStrategy,
    BrainUserIdentity,
    BrainVoice,
    PerformanceMetrics,
    SocialBrainContext,
)


class SocialBrainAccountError(ValueError):
    """The requested Threads account is missing or owned by another user."""


_SENSITIVE_KEY_PARTS = (
    "access_token",
    "refresh_token",
    "bot_token",
    "api_key",
    "secret",
    "password",
    "oauth",
    "telegram_id",
    "credits_balance",
    "payment",
    "prompt",
    "sample_posts",
    "target_text",
    "comment_text",
    "generation_input",
    "generation_output",
)

_BASE_CONTEXT_SQL = text("""
    SELECT
        u.id AS user_id,
        ta.id AS threads_account_id,
        ta.username AS threads_username,
        (
            SELECT count(*)
            FROM threads_accounts owned
            WHERE owned.user_id = u.id
        ) AS account_count,
        vp.profile_json AS voice_profile_json,
        vp.updated_at AS voice_updated_at,
        un.niche,
        un.keywords AS niche_keywords,
        uss.primary_goal,
        uss.secondary_goal,
        uss.strategy_json,
        uss.autonomy_level,
        uss.updated_at AS strategy_updated_at,
        ac.active AS autocontent_active,
        ac.posts_per_day,
        ns.active AS neuro_active,
        ns.mode AS neuro_mode,
        ns.daily_cap AS neuro_daily_cap
    FROM users u
    JOIN threads_accounts ta
      ON ta.user_id = u.id
     AND ta.id = :account_id
    LEFT JOIN voice_profiles vp ON vp.user_id = u.id
    LEFT JOIN user_niches un ON un.user_id = u.id
    LEFT JOIN user_strategy_state uss
      ON uss.user_id = u.id
     AND uss.threads_account_id = ta.id
    LEFT JOIN autocontent_settings ac ON ac.user_id = u.id
    LEFT JOIN neuro_settings ns ON ns.user_id = u.id
    WHERE u.id = :uid
""")

_FACTS_SQL = text("""
    SELECT DISTINCT ON (fact_type, fact_key)
        threads_account_id,
        fact_type,
        fact_key,
        fact_value_json,
        confidence,
        source,
        updated_at
    FROM social_facts
    WHERE user_id = :uid
      AND (
        threads_account_id IS NULL
        OR threads_account_id = :account_id
      )
    ORDER BY
        fact_type,
        fact_key,
        (threads_account_id IS NOT NULL) DESC,
        confidence DESC,
        updated_at DESC
""")

_PERFORMANCE_SQL = text("""
    WITH recent_posts AS (
        SELECT status, threads_post_id
        FROM scheduled_posts
        WHERE user_id = :uid
          AND threads_account_id = :account_id
          AND run_at >= now() - interval '30 days'
    ),
    latest_insights AS (
        SELECT rp.threads_post_id, latest.metrics_json
        FROM recent_posts rp
        JOIN LATERAL (
            SELECT i.metrics_json
            FROM insights_snapshots i
            WHERE i.threads_post_id = rp.threads_post_id
            ORDER BY i.snapshot_date DESC
            LIMIT 1
        ) latest ON true
        WHERE rp.threads_post_id IS NOT NULL
    )
    SELECT
        count(*) AS total_posts_30d,
        count(*) FILTER (WHERE rp.status = 'done')
            AS published_posts_30d,
        count(*) FILTER (WHERE rp.status = 'failed')
            AS failed_posts_30d,
        count(li.threads_post_id) AS insight_posts_30d,
        coalesce(sum(
            CASE
                WHEN coalesce(li.metrics_json->>'views', '')
                    ~ '^[0-9]+$'
                THEN (li.metrics_json->>'views')::bigint
                ELSE 0
            END
        ), 0)::bigint AS views,
        coalesce(sum(
            CASE
                WHEN coalesce(li.metrics_json->>'likes', '')
                    ~ '^[0-9]+$'
                THEN (li.metrics_json->>'likes')::bigint
                ELSE 0
            END
        ), 0)::bigint AS likes,
        coalesce(sum(
            CASE
                WHEN coalesce(li.metrics_json->>'replies', '')
                    ~ '^[0-9]+$'
                THEN (li.metrics_json->>'replies')::bigint
                ELSE 0
            END
        ), 0)::bigint AS replies,
        coalesce(sum(
            CASE
                WHEN coalesce(li.metrics_json->>'reposts', '')
                    ~ '^[0-9]+$'
                THEN (li.metrics_json->>'reposts')::bigint
                ELSE 0
            END
        ), 0)::bigint AS reposts,
        coalesce(sum(
            CASE
                WHEN coalesce(li.metrics_json->>'quotes', '')
                    ~ '^[0-9]+$'
                THEN (li.metrics_json->>'quotes')::bigint
                ELSE 0
            END
        ), 0)::bigint AS quotes,
        coalesce(sum(
            CASE
                WHEN coalesce(li.metrics_json->>'shares', '')
                    ~ '^[0-9]+$'
                THEN (li.metrics_json->>'shares')::bigint
                ELSE 0
            END
        ), 0)::bigint AS shares
    FROM recent_posts rp
    LEFT JOIN latest_insights li
      ON li.threads_post_id = rp.threads_post_id
""")

_GENERATION_MIX_SQL = text("""
    SELECT type AS generation_type, count(*) AS generation_count
    FROM generations
    WHERE user_id = :uid
      AND created_at >= now() - interval '30 days'
      AND type IS NOT NULL
    GROUP BY type
    ORDER BY type
""")

_NEURO_STATUS_SQL = text("""
    SELECT status, count(*) AS status_count
    FROM neuro_comments
    WHERE user_id = :uid
      AND created_at >= now() - interval '30 days'
      AND status IS NOT NULL
    GROUP BY status
    ORDER BY status
""")


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
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


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _is_sensitive_key(key: Any) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _assert_no_sensitive_fields(
    value: Any,
    path: str = "value",
) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_sensitive_key(key):
                raise ValueError(
                    f"sensitive field is not allowed in Social Brain: "
                    f"{path}.{key}"
                )
            _assert_no_sensitive_fields(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_no_sensitive_fields(item, f"{path}[{index}]")


def _without_sensitive_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_sensitive_fields(item)
            for key, item in value.items()
            if not _is_sensitive_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [_without_sensitive_fields(item) for item in value]
    return value


def _profile_voice(
    profile_value: Any,
    updated_at: Any,
) -> BrainVoice:
    profile = _without_sensitive_fields(_json_dict(profile_value))
    return BrainVoice(
        available=bool(profile),
        lexicon=_string_list(profile.get("lexicon")),
        sentence_length=_optional_text(
            profile.get("sentence_length")
        ),
        punctuation=_optional_text(profile.get("punctuation")),
        tone=_optional_text(profile.get("tone")),
        structure=_optional_text(profile.get("structure")),
        taboo=_string_list(profile.get("taboo")),
        sample_phrases=_string_list(profile.get("sample_phrases")),
        updated_at=updated_at,
    )


def _brain_fact(row: Any) -> BrainFact:
    return BrainFact(
        fact_type=str(row["fact_type"]),
        key=str(row["fact_key"]),
        value=_without_sensitive_fields(
            _json_value(row["fact_value_json"], None)
        ),
        scope=(
            "account"
            if row["threads_account_id"] is not None
            else "global"
        ),
        confidence=float(row["confidence"]),
        source=str(row["source"]),
        updated_at=row["updated_at"],
    )


def _apply_voice_profile_fact(
    voice: BrainVoice,
    facts: list[BrainFact],
) -> None:
    profile_fact = next(
        (
            fact
            for fact in facts
            if fact.key == "profile"
            and isinstance(fact.value, dict)
        ),
        None,
    )
    if profile_fact is None:
        return

    profile = _without_sensitive_fields(profile_fact.value)
    list_fields = ("lexicon", "taboo", "sample_phrases")
    text_fields = (
        "sentence_length",
        "punctuation",
        "tone",
        "structure",
    )
    for field in list_fields:
        if field in profile:
            setattr(voice, field, _string_list(profile[field]))
    for field in text_fields:
        if field in profile:
            setattr(voice, field, _optional_text(profile[field]))
    voice.available = True
    voice.updated_at = profile_fact.updated_at


def _apply_niche_fact(
    name: str | None,
    keywords: list[str],
    facts: list[BrainFact],
) -> tuple[str | None, list[str]]:
    niche_fact = next(
        (fact for fact in facts if fact.key == "niche"),
        None,
    )
    if niche_fact is None:
        return name, keywords
    if isinstance(niche_fact.value, str):
        return _optional_text(niche_fact.value), keywords
    if not isinstance(niche_fact.value, dict):
        return name, keywords

    value = _without_sensitive_fields(niche_fact.value)
    if "name" in value:
        name = _optional_text(value["name"])
    if "keywords" in value:
        keywords = _string_list(value["keywords"])
    return name, keywords


def _stored_generation_mix(
    facts: list[BrainFact],
) -> dict[str, int]:
    fact = next(
        (
            item
            for item in facts
            if item.key == "generation_mix_30d"
            and isinstance(item.value, dict)
        ),
        None,
    )
    if fact is None:
        return {}
    raw_mix = fact.value.get("by_type")
    if not isinstance(raw_mix, dict):
        return {}
    return {
        str(key): _as_int(value)
        for key, value in raw_mix.items()
    }


async def build_brain_context(
    session: AsyncSession,
    user_id: int,
    threads_account_id: int,
) -> SocialBrainContext:
    """Build one owned account context without an LLM call."""
    params = {
        "uid": user_id,
        "account_id": threads_account_id,
    }
    base_result = await session.execute(_BASE_CONTEXT_SQL, params)
    base = base_result.mappings().first()
    if base is None:
        raise SocialBrainAccountError(
            f"Threads account {threads_account_id} is not owned by "
            f"user {user_id}"
        )

    account_count = _as_int(base["account_count"])
    uses_user_defaults = account_count == 1

    facts_result = await session.execute(_FACTS_SQL, params)
    facts = [_brain_fact(row) for row in facts_result.mappings().all()]

    performance_result = await session.execute(
        _PERFORMANCE_SQL,
        params,
    )
    performance_row = performance_result.mappings().first() or {}

    generation_mix: dict[str, int] = {}
    neuro_status: dict[str, int] = {}
    if uses_user_defaults:
        generation_result = await session.execute(
            _GENERATION_MIX_SQL,
            params,
        )
        generation_mix = {
            str(row["generation_type"]): _as_int(
                row["generation_count"]
            )
            for row in generation_result.mappings().all()
        }
        neuro_result = await session.execute(
            _NEURO_STATUS_SQL,
            params,
        )
        neuro_status = {
            str(row["status"]): _as_int(row["status_count"])
            for row in neuro_result.mappings().all()
        }

    voice_facts: list[BrainFact] = []
    audience_facts: list[BrainFact] = []
    content_facts: list[BrainFact] = []
    topic_facts: list[BrainFact] = []
    constraint_facts: list[BrainFact] = []
    performance_facts: list[BrainFact] = []
    memory_facts: list[BrainFact] = []

    fact_targets = {
        "voice": voice_facts,
        "audience": audience_facts,
        "content_pattern": content_facts,
        "topic": topic_facts,
        "constraint": constraint_facts,
        "performance": performance_facts,
    }
    for fact in facts:
        fact_targets.get(fact.fact_type, memory_facts).append(fact)

    voice = _profile_voice(
        (
            base["voice_profile_json"]
            if uses_user_defaults
            else None
        ),
        (
            base["voice_updated_at"]
            if uses_user_defaults
            else None
        ),
    )
    voice.facts = voice_facts
    _apply_voice_profile_fact(voice, voice_facts)

    niche_name = (
        _optional_text(base["niche"])
        if uses_user_defaults
        else None
    )
    niche_keywords = (
        _string_list(base["niche_keywords"])
        if uses_user_defaults
        else []
    )
    niche_name, niche_keywords = _apply_niche_fact(
        niche_name,
        niche_keywords,
        topic_facts,
    )

    if not generation_mix:
        generation_mix = _stored_generation_mix(content_facts)

    insight_posts = _as_int(
        performance_row.get("insight_posts_30d")
    )
    metrics = None
    if insight_posts:
        metrics = PerformanceMetrics(
            views=_as_int(performance_row.get("views")),
            likes=_as_int(performance_row.get("likes")),
            replies=_as_int(performance_row.get("replies")),
            reposts=_as_int(performance_row.get("reposts")),
            quotes=_as_int(performance_row.get("quotes")),
            shares=_as_int(performance_row.get("shares")),
        )

    strategy_values = _without_sensitive_fields(
        _json_dict(base["strategy_json"])
    )
    return SocialBrainContext(
        user=BrainUserIdentity(user_id=user_id),
        account=BrainAccountIdentity(
            threads_account_id=threads_account_id,
            threads_username=_optional_text(
                base["threads_username"]
            ),
            uses_user_defaults=uses_user_defaults,
        ),
        voice=voice,
        niche=BrainNiche(
            name=niche_name,
            keywords=niche_keywords,
            topic_facts=topic_facts,
        ),
        goals=BrainGoals(
            primary=_optional_text(base["primary_goal"]),
            secondary=_optional_text(base["secondary_goal"]),
        ),
        audience=BrainAudience(facts=audience_facts),
        content_preferences=BrainContentPreferences(
            autocontent_active=(
                base["autocontent_active"]
                if uses_user_defaults
                else None
            ),
            posts_per_day=(
                base["posts_per_day"]
                if uses_user_defaults
                else None
            ),
            generation_mix_30d=generation_mix,
            facts=content_facts,
        ),
        constraints=BrainConstraints(
            voice_taboo=voice.taboo,
            neuro_active=(
                base["neuro_active"]
                if uses_user_defaults
                else None
            ),
            neuro_mode=(
                _optional_text(base["neuro_mode"])
                if uses_user_defaults
                else None
            ),
            neuro_daily_cap=(
                base["neuro_daily_cap"]
                if uses_user_defaults
                else None
            ),
            facts=constraint_facts,
        ),
        performance=BrainPerformance(
            generated_30d=sum(generation_mix.values()),
            total_posts_30d=_as_int(
                performance_row.get("total_posts_30d")
            ),
            published_posts_30d=_as_int(
                performance_row.get("published_posts_30d")
            ),
            failed_posts_30d=_as_int(
                performance_row.get("failed_posts_30d")
            ),
            insight_posts_30d=insight_posts,
            metrics_30d=metrics,
            neuro_status_30d=neuro_status,
            facts=performance_facts,
        ),
        strategy=BrainStrategy(
            autonomy_level=_optional_text(base["autonomy_level"]),
            values=strategy_values,
            updated_at=base["strategy_updated_at"],
        ),
        memory=BrainMemory(facts=memory_facts),
    )


async def upsert_social_fact(
    session: AsyncSession,
    user_id: int,
    *,
    threads_account_id: int | None,
    fact_type: str,
    key: str,
    value: Any,
    source: str,
    confidence: float = 1.0,
) -> None:
    """Upsert one global or owned account fact."""
    for metadata in (fact_type, key, source):
        if not str(metadata).strip():
            raise ValueError("Social Brain fact metadata cannot be empty")
        if _is_sensitive_key(metadata):
            raise ValueError(
                "sensitive metadata is not allowed in Social Brain"
            )
    if not 0 <= confidence <= 1:
        raise ValueError("Social Brain confidence must be between 0 and 1")
    _assert_no_sensitive_fields(value)
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    params = {
        "uid": user_id,
        "account_id": threads_account_id,
        "fact_type": fact_type.strip(),
        "fact_key": key.strip(),
        "fact_value": serialized,
        "confidence": confidence,
        "source": source.strip(),
    }

    if threads_account_id is None:
        result = await session.execute(text("""
            INSERT INTO social_facts (
                user_id,
                threads_account_id,
                fact_type,
                fact_key,
                fact_value_json,
                confidence,
                source,
                created_at,
                updated_at
            )
            VALUES (
                :uid,
                NULL,
                :fact_type,
                :fact_key,
                CAST(:fact_value AS jsonb),
                :confidence,
                :source,
                now(),
                now()
            )
            ON CONFLICT (user_id, fact_type, fact_key)
              WHERE threads_account_id IS NULL
            DO UPDATE SET
                fact_value_json = EXCLUDED.fact_value_json,
                confidence = EXCLUDED.confidence,
                source = EXCLUDED.source,
                updated_at = now()
            RETURNING id
        """), params)
    else:
        result = await session.execute(text("""
            INSERT INTO social_facts (
                user_id,
                threads_account_id,
                fact_type,
                fact_key,
                fact_value_json,
                confidence,
                source,
                created_at,
                updated_at
            )
            SELECT
                :uid,
                :account_id,
                :fact_type,
                :fact_key,
                CAST(:fact_value AS jsonb),
                :confidence,
                :source,
                now(),
                now()
            FROM threads_accounts account
            WHERE account.id = :account_id
              AND account.user_id = :uid
            ON CONFLICT (
                user_id,
                threads_account_id,
                fact_type,
                fact_key
            )
              WHERE threads_account_id IS NOT NULL
            DO UPDATE SET
                fact_value_json = EXCLUDED.fact_value_json,
                confidence = EXCLUDED.confidence,
                source = EXCLUDED.source,
                updated_at = now()
            RETURNING id
        """), params)
        if result.first() is None:
            raise SocialBrainAccountError(
                f"Threads account {threads_account_id} is not owned by "
                f"user {user_id}"
            )


async def upsert_strategy_state(
    session: AsyncSession,
    user_id: int,
    threads_account_id: int,
    *,
    primary_goal: str | None = None,
    secondary_goal: str | None = None,
    strategy: dict[str, Any] | None = None,
    autonomy_level: str | None = None,
) -> None:
    """Persist strategy for one owned account."""
    strategy = strategy or {}
    _assert_no_sensitive_fields(strategy, "strategy")
    serialized = json.dumps(
        strategy,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    result = await session.execute(text("""
        INSERT INTO user_strategy_state (
            user_id,
            threads_account_id,
            primary_goal,
            secondary_goal,
            strategy_json,
            autonomy_level,
            created_at,
            updated_at
        )
        SELECT
            :uid,
            :account_id,
            :primary_goal,
            :secondary_goal,
            CAST(:strategy AS jsonb),
            :autonomy_level,
            now(),
            now()
        FROM threads_accounts account
        WHERE account.id = :account_id
          AND account.user_id = :uid
        ON CONFLICT (user_id, threads_account_id) DO UPDATE SET
            primary_goal = EXCLUDED.primary_goal,
            secondary_goal = EXCLUDED.secondary_goal,
            strategy_json = EXCLUDED.strategy_json,
            autonomy_level = EXCLUDED.autonomy_level,
            updated_at = now()
        RETURNING threads_account_id
    """), {
        "uid": user_id,
        "account_id": threads_account_id,
        "primary_goal": primary_goal,
        "secondary_goal": secondary_goal,
        "strategy": serialized,
        "autonomy_level": autonomy_level,
    })
    if result.first() is None:
        raise SocialBrainAccountError(
            f"Threads account {threads_account_id} is not owned by "
            f"user {user_id}"
        )


async def initialize_brain_from_existing_data(
    session: AsyncSession,
    user_id: int,
    threads_account_id: int,
) -> SocialBrainContext:
    """Persist deterministic account summaries and return fresh context."""
    brain = await build_brain_context(
        session,
        user_id,
        threads_account_id,
    )

    mix = brain.content_preferences.generation_mix_30d
    if mix:
        await upsert_social_fact(
            session,
            user_id,
            threads_account_id=threads_account_id,
            fact_type="content_pattern",
            key="generation_mix_30d",
            value={"window_days": 30, "by_type": mix},
            source="initial_deterministic_build",
        )

    performance = brain.performance
    has_performance = any((
        performance.total_posts_30d,
        performance.published_posts_30d,
        performance.failed_posts_30d,
        performance.insight_posts_30d,
        performance.neuro_status_30d,
    ))
    if has_performance:
        await upsert_social_fact(
            session,
            user_id,
            threads_account_id=threads_account_id,
            fact_type="performance",
            key="rolling_30d",
            value={
                "window_days": 30,
                "total_posts": performance.total_posts_30d,
                "published_posts": performance.published_posts_30d,
                "failed_posts": performance.failed_posts_30d,
                "insight_posts": performance.insight_posts_30d,
                "metrics": (
                    performance.metrics_30d.model_dump(mode="json")
                    if performance.metrics_30d
                    else {}
                ),
                "neuro_status": performance.neuro_status_30d,
            },
            source="initial_deterministic_build",
        )

    return await build_brain_context(
        session,
        user_id,
        threads_account_id,
    )
