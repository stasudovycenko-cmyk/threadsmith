"""Deterministic Anthropic cost accounting and hard budget guardrails."""

from __future__ import annotations

import json
import logging
import math
import statistics
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Protocol

from sqlalchemy import text

from app.core.config import settings
from app.core.db import Session

log = logging.getLogger("ai_cost")

PRICING_VERSION = "anthropic-2026-07-29"
USD_QUANTUM = Decimal("0.0000000001")
TOKENS_PER_MILLION = Decimal("1000000")
AI_BUDGET_ADVISORY_LOCK_ID = 8_104_202_601


@dataclass(frozen=True)
class ModelPricing:
    input_per_million: Decimal
    output_per_million: Decimal
    cache_read_per_million: Decimal
    cache_creation_per_million: Decimal


# Standard global endpoint pricing. Cache creation uses the five-minute rate;
# ThreadFlow does not currently enable prompt caching.
MODEL_PRICING: dict[str, ModelPricing] = {
    "claude-sonnet-4-6": ModelPricing(
        input_per_million=Decimal("3.00"),
        output_per_million=Decimal("15.00"),
        cache_read_per_million=Decimal("0.30"),
        cache_creation_per_million=Decimal("3.75"),
    ),
}


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass(frozen=True)
class AIUsageContext:
    user_id: int | None = None
    threads_account_id: int | None = None
    run_id: str | None = None
    request_id: uuid.UUID | None = None


@dataclass(frozen=True)
class UsageReservation:
    event_key: str
    request_id: uuid.UUID
    feature: str
    model: str
    attempt: int
    user_id: int | None
    threads_account_id: int | None
    run_id: str | None
    reserved_cost_usd: Decimal


@dataclass(frozen=True)
class ReservationCandidate:
    event_key: str
    request_id: uuid.UUID
    feature: str
    model: str
    attempt: int
    user_id: int | None
    threads_account_id: int | None
    run_id: str | None
    reserved_cost_usd: Decimal


@dataclass(frozen=True)
class BudgetSnapshot:
    user_spend: Decimal = Decimal("0")
    account_spend: Decimal = Decimal("0")
    feature_spend: Decimal = Decimal("0")
    run_spend: Decimal = Decimal("0")
    run_calls: int = 0
    account_feature_calls: int = 0
    account_feature_hour_calls: int = 0
    current_hour_calls: int = 0
    recent_hour_calls: tuple[int, ...] = ()


@dataclass(frozen=True)
class ResolvedLimits:
    user_daily_usd: Decimal
    account_daily_usd: Decimal
    feature_daily_usd: Decimal
    run_usd: Decimal | None
    run_calls: int | None
    account_feature_daily_calls: int | None
    feature_hourly_calls: int
    account_feature_hourly_calls: int | None = None


AI_FEATURE_DAILY_USD_LIMITS: dict[str, Decimal] = {
    "voice_profile": Decimal("10.00"),
    "generate_post": Decimal("20.00"),
    "content_generate": Decimal("20.00"),
    "content_repair": Decimal("10.00"),
    "autocontent": Decimal("10.00"),
    "autocontent_repair": Decimal("10.00"),
    "rewrite": Decimal("15.00"),
    "generate_thread": Decimal("15.00"),
    "radar_analysis": Decimal("10.00"),
    "radar_semantic_score": Decimal("10.00"),
    "neuro_comment": Decimal("10.00"),
    "unspecified": Decimal("5.00"),
}
AI_FEATURE_HOURLY_CALL_LIMITS: dict[str, int] = {
    "autocontent": 100,
    "autocontent_repair": 100,
    "neuro_comment": 100,
    "radar_analysis": 100,
    "radar_semantic_score": 100,
}
AI_DEFAULT_FEATURE_HOURLY_CALL_LIMIT = 200
AI_RUN_USD_LIMITS: dict[str, Decimal] = {
    "autocontent": Decimal("0.25"),
    "autocontent_repair": Decimal("0.25"),
    "neuro_comment": Decimal("0.15"),
}
AI_RUN_CALL_LIMITS: dict[str, int] = {
    "autocontent": 10,
    "autocontent_repair": 10,
    "neuro_comment": 5,
}
AI_ACCOUNT_FEATURE_DAILY_CALL_LIMITS: dict[str, int] = {
    "neuro_comment": 30,
}
AI_ACCOUNT_FEATURE_HOURLY_CALL_LIMITS: dict[str, int] = {
    "autocontent": 10,
    "autocontent_repair": 5,
}

AI_MAX_REPAIR_PER_REQUEST = 1
AI_MAX_TRANSPORT_RETRIES = 1
AI_RESERVATION_SAFETY_MULTIPLIER = Decimal("1.15")
AI_ANOMALY_MIN_ACTIVE_HOURS = 6
AI_ANOMALY_MIN_CURRENT_CALLS = 10
AI_ANOMALY_MULTIPLIER = Decimal("4")

AUTOCONTENT_MAX_GENERATIONS_PER_USER_RUN = 5
AUTOCONTENT_MAX_GENERATIONS_PER_PLANNER_RUN = 50
AUTOCONTENT_MAX_POSTS_PER_USER_DAY = 5
AUTOCONTENT_MAX_PENDING_POSTS = 20
NEURO_MAX_CANDIDATES_PER_RUN = 5
NEURO_MAX_LLM_CALLS_PER_RUN = 5


class AICostGuardError(Exception):
    """Base class for controlled pre-call cost guard failures."""

    def __init__(
        self,
        reason: str,
        *,
        scope: str,
        current: Decimal | int | None = None,
        limit: Decimal | int | None = None,
        baseline: Decimal | None = None,
    ):
        super().__init__(reason)
        self.reason = reason
        self.scope = scope
        self.current = current
        self.limit = limit
        self.baseline = baseline


class BudgetExceeded(AICostGuardError):
    pass


class KillSwitchDisabled(AICostGuardError):
    pass


class AnomalyDetected(AICostGuardError):
    pass


class DuplicateUsageEvent(AICostGuardError):
    pass


def calculate_cost_usd(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> Decimal:
    """Calculate one physical call cost from token counters."""
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        raise ValueError(f"no AI pricing configured for model {model!r}")
    counters = (
        input_tokens,
        output_tokens,
        cache_read_tokens,
        cache_creation_tokens,
    )
    if any(value < 0 for value in counters):
        raise ValueError("token counts must be non-negative")
    cost = (
        Decimal(input_tokens) * pricing.input_per_million
        + Decimal(output_tokens) * pricing.output_per_million
        + Decimal(cache_read_tokens) * pricing.cache_read_per_million
        + Decimal(cache_creation_tokens)
        * pricing.cache_creation_per_million
    ) / TOKENS_PER_MILLION
    return cost.quantize(USD_QUANTUM, rounding=ROUND_HALF_UP)


def estimate_reservation_cost(
    model: str,
    *,
    prompt_chars: int,
    max_output_tokens: int,
) -> Decimal:
    """Conservative local estimate; prompt content is never persisted."""
    estimated_input_tokens = math.ceil(max(prompt_chars, 0) / 2) + 256
    base = calculate_cost_usd(
        model,
        input_tokens=estimated_input_tokens,
        output_tokens=max_output_tokens,
    )
    return (base * AI_RESERVATION_SAFETY_MULTIPLIER).quantize(
        USD_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


def _feature_enabled(feature: str) -> tuple[bool, str]:
    if not settings.AI_ENABLED:
        return False, "global"
    if (
        feature.startswith("autocontent")
        and not settings.AI_AUTOCONTENT_ENABLED
    ):
        return False, "autocontent"
    if feature == "neuro_comment" and not settings.AI_NEURO_ENABLED:
        return False, "neuro"
    if feature.startswith("radar_") and not settings.AI_RADAR_ENABLED:
        return False, "radar"
    return True, ""


def limits_for(feature: str) -> ResolvedLimits:
    return ResolvedLimits(
        user_daily_usd=Decimal(str(settings.AI_USER_DAILY_USD_LIMIT)),
        account_daily_usd=Decimal(
            str(settings.AI_ACCOUNT_DAILY_USD_LIMIT)
        ),
        feature_daily_usd=AI_FEATURE_DAILY_USD_LIMITS.get(
            feature,
            AI_FEATURE_DAILY_USD_LIMITS["unspecified"],
        ),
        run_usd=AI_RUN_USD_LIMITS.get(feature),
        run_calls=AI_RUN_CALL_LIMITS.get(feature),
        account_feature_daily_calls=(
            AI_ACCOUNT_FEATURE_DAILY_CALL_LIMITS.get(feature)
        ),
        feature_hourly_calls=AI_FEATURE_HOURLY_CALL_LIMITS.get(
            feature,
            AI_DEFAULT_FEATURE_HOURLY_CALL_LIMIT,
        ),
        account_feature_hourly_calls=(
            AI_ACCOUNT_FEATURE_HOURLY_CALL_LIMITS.get(feature)
        ),
    )


def evaluate_budget(
    candidate: ReservationCandidate,
    snapshot: BudgetSnapshot,
    limits: ResolvedLimits,
) -> None:
    """Pure budget/anomaly policy shared by DB and unit-test backends."""

    def check_cost(
        current: Decimal,
        limit: Decimal,
        *,
        reason: str,
        scope: str,
    ) -> None:
        prospective = current + candidate.reserved_cost_usd
        if prospective > limit:
            raise BudgetExceeded(
                reason,
                scope=scope,
                current=current,
                limit=limit,
            )

    if candidate.user_id is not None:
        check_cost(
            snapshot.user_spend,
            limits.user_daily_usd,
            reason="user_daily_cost",
            scope=f"user:{candidate.user_id}",
        )
    if candidate.threads_account_id is not None:
        check_cost(
            snapshot.account_spend,
            limits.account_daily_usd,
            reason="account_daily_cost",
            scope=f"account:{candidate.threads_account_id}",
        )
    check_cost(
        snapshot.feature_spend,
        limits.feature_daily_usd,
        reason="feature_daily_cost",
        scope=f"feature:{candidate.feature}",
    )

    if candidate.run_id and limits.run_usd is not None:
        check_cost(
            snapshot.run_spend,
            limits.run_usd,
            reason="run_cost",
            scope=f"run:{candidate.run_id}",
        )
    if (
        candidate.run_id
        and limits.run_calls is not None
        and snapshot.run_calls + 1 > limits.run_calls
    ):
        raise BudgetExceeded(
            "run_calls",
            scope=f"run:{candidate.run_id}",
            current=snapshot.run_calls,
            limit=limits.run_calls,
        )
    if (
        candidate.threads_account_id is not None
        and limits.account_feature_daily_calls is not None
        and snapshot.account_feature_calls + 1
        > limits.account_feature_daily_calls
    ):
        raise BudgetExceeded(
            "account_feature_daily_calls",
            scope=f"account:{candidate.threads_account_id}",
            current=snapshot.account_feature_calls,
            limit=limits.account_feature_daily_calls,
        )

    if (
        candidate.threads_account_id is not None
        and limits.account_feature_hourly_calls is not None
        and snapshot.account_feature_hour_calls + 1
        > limits.account_feature_hourly_calls
    ):
        raise BudgetExceeded(
            "account_feature_hourly_calls",
            scope=f"account:{candidate.threads_account_id}",
            current=snapshot.account_feature_hour_calls,
            limit=limits.account_feature_hourly_calls,
        )

    prospective_calls = snapshot.current_hour_calls + 1
    if prospective_calls > limits.feature_hourly_calls:
        raise AnomalyDetected(
            "feature_hourly_hard_cap",
            scope=f"feature:{candidate.feature}",
            current=prospective_calls,
            limit=limits.feature_hourly_calls,
        )

    active_hours = [
        value for value in snapshot.recent_hour_calls if value > 0
    ]
    if (
        prospective_calls >= AI_ANOMALY_MIN_CURRENT_CALLS
        and len(active_hours) >= AI_ANOMALY_MIN_ACTIVE_HOURS
    ):
        baseline = Decimal(str(statistics.median(active_hours)))
        threshold = max(
            Decimal(AI_ANOMALY_MIN_CURRENT_CALLS),
            baseline * AI_ANOMALY_MULTIPLIER,
        )
        if Decimal(prospective_calls) > threshold:
            raise AnomalyDetected(
                "feature_hourly_baseline",
                scope=f"feature:{candidate.feature}",
                current=prospective_calls,
                limit=threshold,
                baseline=baseline,
            )


class UsageStore(Protocol):
    async def reserve(
        self,
        candidate: ReservationCandidate,
        limits: ResolvedLimits,
    ) -> UsageReservation:
        ...

    async def complete(
        self,
        reservation: UsageReservation,
        *,
        usage: TokenUsage,
        cost_usd: Decimal,
        status: str,
        latency_ms: int,
        failure_type: str | None,
    ) -> None:
        ...

    async def summary(
        self,
        *,
        start: datetime,
        end: datetime,
        user_id: int | None = None,
        threads_account_id: int | None = None,
        feature: str | None = None,
        model: str | None = None,
        group_by: str | None = None,
    ) -> list[dict]:
        ...


def _decimal(value: object) -> Decimal:
    return Decimal(str(value or 0))


class PostgresUsageStore:
    """PostgreSQL-backed atomic reservations.

    A transaction-level advisory lock serializes the small reservation
    critical section across bot/worker/API processes. Anthropic calls happen
    after the transaction commits, so the lock is never held over network I/O.
    """

    def __init__(self, session_factory=Session):
        self._session_factory = session_factory

    async def reserve(
        self,
        candidate: ReservationCandidate,
        limits: ResolvedLimits,
    ) -> UsageReservation:
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text("select pg_advisory_xact_lock(:lock_id)"),
                    {"lock_id": AI_BUDGET_ADVISORY_LOCK_ID},
                )
                existing = (
                    await session.execute(
                        text(
                            "select 1 from ai_usage_events "
                            "where event_key = :event_key"
                        ),
                        {"event_key": candidate.event_key},
                    )
                ).first()
                if existing:
                    raise DuplicateUsageEvent(
                        "duplicate_event_key",
                        scope=f"event:{candidate.event_key}",
                    )

                row = (
                    await session.execute(
                        text(
                            """
                            select
                              coalesce(sum(
                                case when user_id = :user_id then
                                  case when status = 'reserved'
                                    then reserved_cost_usd
                                    else estimated_cost_usd end
                                else 0 end
                              ), 0) as user_spend,
                              coalesce(sum(
                                case when threads_account_id = :account_id then
                                  case when status = 'reserved'
                                    then reserved_cost_usd
                                    else estimated_cost_usd end
                                else 0 end
                              ), 0) as account_spend,
                              coalesce(sum(
                                case when feature = :feature then
                                  case when status = 'reserved'
                                    then reserved_cost_usd
                                    else estimated_cost_usd end
                                else 0 end
                              ), 0) as feature_spend,
                              coalesce(sum(
                                case when run_id = :run_id then
                                  case when status = 'reserved'
                                    then reserved_cost_usd
                                    else estimated_cost_usd end
                                else 0 end
                              ), 0) as run_spend,
                              count(*) filter (
                                where run_id = :run_id
                              ) as run_calls,
                              count(*) filter (
                                where threads_account_id = :account_id
                                  and feature = :feature
                              ) as account_feature_calls,
                              count(*) filter (
                                where threads_account_id = :account_id
                                  and feature = :feature
                                  and created_at >= (
                                    date_trunc(
                                      'hour',
                                      now() at time zone 'UTC'
                                    ) at time zone 'UTC'
                                  )
                              ) as account_feature_hour_calls,
                              count(*) filter (
                                where feature = :feature
                                  and created_at >= (
                                    date_trunc(
                                      'hour',
                                      now() at time zone 'UTC'
                                    ) at time zone 'UTC'
                                  )
                              ) as current_hour_calls
                            from ai_usage_events
                            where created_at >= (
                              date_trunc(
                                'day',
                                now() at time zone 'UTC'
                              ) at time zone 'UTC'
                            )
                            """
                        ),
                        {
                            "user_id": candidate.user_id,
                            "account_id": candidate.threads_account_id,
                            "feature": candidate.feature,
                            "run_id": candidate.run_id,
                        },
                    )
                ).mappings().one()
                recent_hours: tuple[int, ...] = ()
                if (
                    int(row["current_hour_calls"] or 0) + 1
                    >= AI_ANOMALY_MIN_CURRENT_CALLS
                ):
                    history_rows = (
                        await session.execute(
                            text(
                                """
                                select count(*) as calls
                                from ai_usage_events
                                where feature = :feature
                                  and created_at >=
                                      (
                                        date_trunc(
                                          'hour',
                                          now() at time zone 'UTC'
                                        ) at time zone 'UTC'
                                      )
                                      - interval '24 hours'
                                  and created_at < (
                                    date_trunc(
                                      'hour',
                                      now() at time zone 'UTC'
                                    ) at time zone 'UTC'
                                  )
                                group by date_trunc('hour', created_at)
                                order by date_trunc('hour', created_at)
                                """
                            ),
                            {"feature": candidate.feature},
                        )
                    ).all()
                    recent_hours = tuple(int(item[0]) for item in history_rows)

                snapshot = BudgetSnapshot(
                    user_spend=_decimal(row["user_spend"]),
                    account_spend=_decimal(row["account_spend"]),
                    feature_spend=_decimal(row["feature_spend"]),
                    run_spend=_decimal(row["run_spend"]),
                    run_calls=int(row["run_calls"] or 0),
                    account_feature_calls=int(
                        row["account_feature_calls"] or 0
                    ),
                    account_feature_hour_calls=int(
                        row.get("account_feature_hour_calls") or 0
                    ),
                    current_hour_calls=int(row["current_hour_calls"] or 0),
                    recent_hour_calls=recent_hours,
                )
                evaluate_budget(candidate, snapshot, limits)
                await session.execute(
                    text(
                        """
                        insert into ai_usage_events (
                          user_id, threads_account_id, feature, model,
                          input_tokens, output_tokens, cache_read_tokens,
                          cache_creation_tokens, estimated_cost_usd,
                          reserved_cost_usd, pricing_version, attempt, status,
                          request_id, event_key, run_id
                        ) values (
                          :user_id, :account_id, :feature, :model,
                          0, 0, 0, 0, 0,
                          :reserved_cost, :pricing_version, :attempt,
                          'reserved', :request_id, :event_key, :run_id
                        )
                        """
                    ),
                    {
                        "user_id": candidate.user_id,
                        "account_id": candidate.threads_account_id,
                        "feature": candidate.feature,
                        "model": candidate.model,
                        "reserved_cost": candidate.reserved_cost_usd,
                        "pricing_version": PRICING_VERSION,
                        "attempt": candidate.attempt,
                        "request_id": candidate.request_id,
                        "event_key": candidate.event_key,
                        "run_id": candidate.run_id,
                    },
                )
        return UsageReservation(**candidate.__dict__)

    async def complete(
        self,
        reservation: UsageReservation,
        *,
        usage: TokenUsage,
        cost_usd: Decimal,
        status: str,
        latency_ms: int,
        failure_type: str | None,
    ) -> None:
        if status not in {"success", "failure"}:
            raise ValueError(f"invalid usage status {status!r}")
        async with self._session_factory() as session:
            result = await session.execute(
                text(
                    """
                    update ai_usage_events
                    set input_tokens = :input_tokens,
                        output_tokens = :output_tokens,
                        cache_read_tokens = :cache_read_tokens,
                        cache_creation_tokens = :cache_creation_tokens,
                        estimated_cost_usd = :cost,
                        status = :status,
                        latency_ms = :latency_ms,
                        failure_type = :failure_type,
                        completed_at = now()
                    where event_key = :event_key
                      and status = 'reserved'
                    """
                ),
                {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "cache_read_tokens": usage.cache_read_tokens,
                    "cache_creation_tokens": usage.cache_creation_tokens,
                    "cost": cost_usd,
                    "status": status,
                    "latency_ms": latency_ms,
                    "failure_type": failure_type,
                    "event_key": reservation.event_key,
                },
            )
            if result.rowcount != 1:
                await session.rollback()
                raise DuplicateUsageEvent(
                    "usage_event_already_completed",
                    scope=f"event:{reservation.event_key}",
                )
            await session.commit()

    async def summary(
        self,
        *,
        start: datetime,
        end: datetime,
        user_id: int | None = None,
        threads_account_id: int | None = None,
        feature: str | None = None,
        model: str | None = None,
        group_by: str | None = None,
    ) -> list[dict]:
        group_columns = {
            "user": "user_id",
            "account": "threads_account_id",
            "feature": "feature",
            "model": "model",
        }
        if group_by is not None and group_by not in group_columns:
            raise ValueError(f"unsupported summary dimension {group_by!r}")
        dimension = group_columns.get(group_by)
        select_dimension = (
            f"{dimension} as dimension," if dimension else ""
        )
        group_clause = f"group by {dimension}" if dimension else ""
        conditions = [
            "created_at >= :start",
            "created_at < :end",
            "status in ('success', 'failure')",
        ]
        params: dict[str, object] = {"start": start, "end": end}
        filters = {
            "user_id": user_id,
            "threads_account_id": threads_account_id,
            "feature": feature,
            "model": model,
        }
        for column, value in filters.items():
            if value is not None:
                conditions.append(f"{column} = :{column}")
                params[column] = value
        query = f"""
            select
              {select_dimension}
              count(*) as calls,
              count(*) filter (where status = 'success') as successful_calls,
              count(*) filter (where status = 'failure') as failed_calls,
              coalesce(sum(input_tokens), 0) as input_tokens,
              coalesce(sum(output_tokens), 0) as output_tokens,
              coalesce(sum(cache_read_tokens), 0) as cache_read_tokens,
              coalesce(sum(cache_creation_tokens), 0)
                as cache_creation_tokens,
              coalesce(sum(estimated_cost_usd), 0) as cost_usd,
              count(*) filter (where attempt > 1) as repair_calls
            from ai_usage_events
            where {" and ".join(conditions)}
            {group_clause}
            {f"order by {dimension}" if dimension else ""}
        """
        async with self._session_factory() as session:
            rows = (await session.execute(text(query), params)).mappings().all()
        return [dict(row) for row in rows]


def _guard_log_payload(
    error: AICostGuardError,
    *,
    candidate: ReservationCandidate,
) -> dict:
    return {
        "event": (
            "ai_anomaly_stop"
            if isinstance(error, AnomalyDetected)
            else "ai_budget_reject"
        ),
        "reason": error.reason,
        "scope": error.scope,
        "feature": candidate.feature,
        "user_id": candidate.user_id,
        "threads_account_id": candidate.threads_account_id,
        "current": (
            str(error.current) if error.current is not None else None
        ),
        "limit": str(error.limit) if error.limit is not None else None,
        "baseline": (
            str(error.baseline) if error.baseline is not None else None
        ),
    }


class AICostEngine:
    def __init__(self, store: UsageStore | None = None):
        self.store = store or PostgresUsageStore()

    async def reserve_call(
        self,
        *,
        feature: str,
        model: str,
        max_tokens: int,
        prompt_chars: int,
        attempt: int,
        context: AIUsageContext | None = None,
        request_id: uuid.UUID | None = None,
    ) -> UsageReservation:
        enabled, switch_scope = _feature_enabled(feature)
        context = context or AIUsageContext()
        resolved_request_id = (
            request_id or context.request_id or uuid.uuid4()
        )
        candidate = ReservationCandidate(
            event_key=f"{resolved_request_id}:{attempt}",
            request_id=resolved_request_id,
            feature=feature,
            model=model,
            attempt=attempt,
            user_id=context.user_id,
            threads_account_id=context.threads_account_id,
            run_id=context.run_id,
            reserved_cost_usd=estimate_reservation_cost(
                model,
                prompt_chars=prompt_chars,
                max_output_tokens=max_tokens,
            ),
        )
        if not enabled:
            error = KillSwitchDisabled(
                "kill_switch",
                scope=switch_scope,
            )
            self._log_guard(error, candidate)
            raise error
        try:
            return await self.store.reserve(
                candidate,
                limits_for(feature),
            )
        except AICostGuardError as error:
            self._log_guard(error, candidate)
            raise

    async def complete_call(
        self,
        reservation: UsageReservation,
        *,
        usage: TokenUsage,
        status: str,
        latency_ms: int,
        failure_type: str | None = None,
    ) -> Decimal:
        cost = calculate_cost_usd(
            reservation.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
        )
        await self.store.complete(
            reservation,
            usage=usage,
            cost_usd=cost,
            status=status,
            latency_ms=latency_ms,
            failure_type=failure_type,
        )
        return cost

    @staticmethod
    def _log_guard(
        error: AICostGuardError,
        candidate: ReservationCandidate,
    ) -> None:
        log.warning(
            "%s %s",
            (
                "ai_anomaly_stop"
                if isinstance(error, AnomalyDetected)
                else "ai_budget_reject"
            ),
            json.dumps(
                _guard_log_payload(error, candidate=candidate),
                ensure_ascii=True,
                separators=(",", ":"),
            ),
        )


def _normalize_summary(row: dict | None) -> dict:
    row = row or {}
    return {
        "calls": int(row.get("calls") or 0),
        "successful_calls": int(row.get("successful_calls") or 0),
        "failed_calls": int(row.get("failed_calls") or 0),
        "input_tokens": int(row.get("input_tokens") or 0),
        "output_tokens": int(row.get("output_tokens") or 0),
        "cache_read_tokens": int(row.get("cache_read_tokens") or 0),
        "cache_creation_tokens": int(
            row.get("cache_creation_tokens") or 0
        ),
        "cost_usd": str(_decimal(row.get("cost_usd"))),
        "repair_calls": int(row.get("repair_calls") or 0),
    }


class AIUsageSummaryService:
    def __init__(self, store: UsageStore | None = None):
        self.store = store or PostgresUsageStore()

    async def get(
        self,
        *,
        day: date,
        user_id: int | None = None,
        threads_account_id: int | None = None,
        feature: str | None = None,
        model: str | None = None,
    ) -> dict:
        start = datetime.combine(day, datetime_time.min, tzinfo=timezone.utc)
        rows = await self.store.summary(
            start=start,
            end=start + timedelta(days=1),
            user_id=user_id,
            threads_account_id=threads_account_id,
            feature=feature,
            model=model,
        )
        return _normalize_summary(rows[0] if rows else None)

    async def by_dimension(
        self,
        *,
        day: date,
        dimension: str,
    ) -> list[dict]:
        start = datetime.combine(day, datetime_time.min, tzinfo=timezone.utc)
        rows = await self.store.summary(
            start=start,
            end=start + timedelta(days=1),
            group_by=dimension,
        )
        return [
            {
                "dimension": row.get("dimension"),
                **_normalize_summary(row),
            }
            for row in rows
        ]

    async def daily(self, *, day: date) -> dict:
        return {
            "day": day.isoformat(),
            "totals": await self.get(day=day),
            "by_user": await self.by_dimension(day=day, dimension="user"),
            "by_account": await self.by_dimension(
                day=day, dimension="account"
            ),
            "by_feature": await self.by_dimension(
                day=day, dimension="feature"
            ),
            "by_model": await self.by_dimension(
                day=day, dimension="model"
            ),
        }
