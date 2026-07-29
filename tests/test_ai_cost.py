import asyncio
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.core import ai_cost
from app.core.ai_cost import (
    AICostEngine,
    AICostGuardError,
    AIUsageContext,
    AIUsageSummaryService,
    AnomalyDetected,
    BudgetExceeded,
    BudgetSnapshot,
    KillSwitchDisabled,
    PostgresUsageStore,
    ReservationCandidate,
    ResolvedLimits,
    TokenUsage,
    UsageReservation,
    calculate_cost_usd,
    evaluate_budget,
)

MODEL = "claude-sonnet-4-6"


def candidate(
    *,
    cost="0.60",
    user_id=1,
    account_id=10,
    feature="generate_post",
    run_id=None,
):
    request_id = uuid.uuid4()
    return ReservationCandidate(
        event_key=f"{request_id}:1",
        request_id=request_id,
        feature=feature,
        model=MODEL,
        attempt=1,
        user_id=user_id,
        threads_account_id=account_id,
        run_id=run_id,
        reserved_cost_usd=Decimal(cost),
    )


def limits(**overrides):
    values = {
        "user_daily_usd": Decimal("1.00"),
        "account_daily_usd": Decimal("1.00"),
        "feature_daily_usd": Decimal("1.00"),
        "run_usd": None,
        "run_calls": None,
        "account_feature_daily_calls": None,
        "feature_hourly_calls": 100,
    }
    values.update(overrides)
    return ResolvedLimits(**values)


class MemoryUsageStore:
    def __init__(self):
        self.events = []
        self.lock = asyncio.Lock()
        self.recent_hour_calls = ()

    @staticmethod
    def _effective_cost(event):
        if event["status"] == "reserved":
            return event["reservation"].reserved_cost_usd
        return event["cost_usd"]

    async def reserve(self, item, resolved_limits):
        async with self.lock:
            if any(
                event["reservation"].event_key == item.event_key
                for event in self.events
            ):
                raise ai_cost.DuplicateUsageEvent(
                    "duplicate_event_key",
                    scope=f"event:{item.event_key}",
                )
            snapshot = BudgetSnapshot(
                user_spend=sum(
                    (
                        self._effective_cost(event)
                        for event in self.events
                        if event["reservation"].user_id == item.user_id
                    ),
                    Decimal("0"),
                ),
                account_spend=sum(
                    (
                        self._effective_cost(event)
                        for event in self.events
                        if event["reservation"].threads_account_id
                        == item.threads_account_id
                    ),
                    Decimal("0"),
                ),
                feature_spend=sum(
                    (
                        self._effective_cost(event)
                        for event in self.events
                        if event["reservation"].feature == item.feature
                    ),
                    Decimal("0"),
                ),
                run_spend=sum(
                    (
                        self._effective_cost(event)
                        for event in self.events
                        if event["reservation"].run_id == item.run_id
                    ),
                    Decimal("0"),
                )
                if item.run_id
                else Decimal("0"),
                run_calls=sum(
                    event["reservation"].run_id == item.run_id
                    for event in self.events
                )
                if item.run_id
                else 0,
                account_feature_calls=sum(
                    event["reservation"].threads_account_id
                    == item.threads_account_id
                    and event["reservation"].feature == item.feature
                    for event in self.events
                ),
                current_hour_calls=sum(
                    event["reservation"].feature == item.feature
                    for event in self.events
                ),
                recent_hour_calls=self.recent_hour_calls,
            )
            evaluate_budget(item, snapshot, resolved_limits)
            reservation = UsageReservation(**item.__dict__)
            self.events.append({
                "reservation": reservation,
                "status": "reserved",
                "usage": TokenUsage(),
                "cost_usd": Decimal("0"),
                "latency_ms": None,
                "failure_type": None,
                "created_at": datetime.now(timezone.utc),
            })
            return reservation

    async def complete(
        self,
        reservation,
        *,
        usage,
        cost_usd,
        status,
        latency_ms,
        failure_type,
    ):
        async with self.lock:
            event = next(
                item for item in self.events
                if item["reservation"].event_key == reservation.event_key
            )
            assert event["status"] == "reserved"
            event.update(
                status=status,
                usage=usage,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                failure_type=failure_type,
            )

    async def summary(
        self,
        *,
        start,
        end,
        user_id=None,
        threads_account_id=None,
        feature=None,
        model=None,
        group_by=None,
    ):
        rows = [
            event for event in self.events
            if start <= event["created_at"] < end
            and event["status"] in {"success", "failure"}
            and (
                user_id is None
                or event["reservation"].user_id == user_id
            )
            and (
                threads_account_id is None
                or event["reservation"].threads_account_id
                == threads_account_id
            )
            and (
                feature is None
                or event["reservation"].feature == feature
            )
            and (
                model is None
                or event["reservation"].model == model
            )
        ]
        field = {
            "user": "user_id",
            "account": "threads_account_id",
            "feature": "feature",
            "model": "model",
        }.get(group_by)
        grouped = defaultdict(list)
        if field:
            for event in rows:
                grouped[getattr(event["reservation"], field)].append(event)
        else:
            grouped[None] = rows
        result = []
        for dimension, events in grouped.items():
            result.append({
                "dimension": dimension,
                "calls": len(events),
                "successful_calls": sum(
                    event["status"] == "success" for event in events
                ),
                "failed_calls": sum(
                    event["status"] == "failure" for event in events
                ),
                "input_tokens": sum(
                    event["usage"].input_tokens for event in events
                ),
                "output_tokens": sum(
                    event["usage"].output_tokens for event in events
                ),
                "cache_read_tokens": sum(
                    event["usage"].cache_read_tokens for event in events
                ),
                "cache_creation_tokens": sum(
                    event["usage"].cache_creation_tokens for event in events
                ),
                "cost_usd": sum(
                    (event["cost_usd"] for event in events),
                    Decimal("0"),
                ),
                "repair_calls": sum(
                    event["reservation"].attempt > 1 for event in events
                ),
            })
        return result


async def reserve_and_complete(
    engine,
    *,
    context=None,
    feature="generate_post",
    attempt=1,
    status="success",
    usage=None,
    request_id=None,
):
    reservation = await engine.reserve_call(
        feature=feature,
        model=MODEL,
        max_tokens=20,
        prompt_chars=20,
        attempt=attempt,
        context=context,
        request_id=request_id,
    )
    await engine.complete_call(
        reservation,
        usage=usage or TokenUsage(input_tokens=10, output_tokens=5),
        status=status,
        latency_ms=12,
        failure_type="ValidationError" if status == "failure" else None,
    )
    return reservation


def test_cost_calculation_sonnet():
    assert calculate_cost_usd(
        MODEL,
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    ) == Decimal("18.0000000000")


def test_cache_token_calculation():
    assert calculate_cost_usd(
        MODEL,
        cache_read_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
    ) == Decimal("4.0500000000")


def test_successful_and_failed_usage_events(monkeypatch):
    monkeypatch.setattr(ai_cost.settings, "AI_USER_DAILY_USD_LIMIT", 10)
    monkeypatch.setattr(ai_cost.settings, "AI_ACCOUNT_DAILY_USD_LIMIT", 10)
    store = MemoryUsageStore()
    engine = AICostEngine(store)
    context = AIUsageContext(user_id=1, threads_account_id=10)

    asyncio.run(reserve_and_complete(engine, context=context))
    asyncio.run(
        reserve_and_complete(engine, context=context, status="failure")
    )

    assert [event["status"] for event in store.events] == [
        "success",
        "failure",
    ]
    assert all(event["cost_usd"] > 0 for event in store.events)


@pytest.mark.parametrize(
    ("snapshot", "expected_reason"),
    [
        (BudgetSnapshot(user_spend=Decimal("0.50")), "user_daily_cost"),
        (
            BudgetSnapshot(account_spend=Decimal("0.50")),
            "account_daily_cost",
        ),
        (
            BudgetSnapshot(feature_spend=Decimal("0.50")),
            "feature_daily_cost",
        ),
    ],
)
def test_user_account_and_feature_budgets(snapshot, expected_reason):
    with pytest.raises(BudgetExceeded) as caught:
        evaluate_budget(candidate(), snapshot, limits())
    assert caught.value.reason == expected_reason


def test_run_and_neuro_account_call_limits():
    neuro_candidate = candidate(
        cost="0.06",
        feature="neuro_comment",
        run_id="neuro-run",
    )
    with pytest.raises(BudgetExceeded) as run_error:
        evaluate_budget(
            neuro_candidate,
            BudgetSnapshot(run_spend=Decimal("0.10")),
            limits(run_usd=Decimal("0.15"), run_calls=5),
        )
    assert run_error.value.reason == "run_cost"

    with pytest.raises(BudgetExceeded) as account_error:
        evaluate_budget(
            neuro_candidate,
            BudgetSnapshot(account_feature_calls=30),
            limits(account_feature_daily_calls=30),
        )
    assert account_error.value.reason == "account_feature_daily_calls"


def test_budget_is_checked_before_reservation_is_written():
    store = MemoryUsageStore()
    engine = AICostEngine(store)
    original = ai_cost.AI_FEATURE_DAILY_USD_LIMITS["generate_post"]
    ai_cost.AI_FEATURE_DAILY_USD_LIMITS["generate_post"] = Decimal("0")
    try:
        with pytest.raises(BudgetExceeded):
            asyncio.run(engine.reserve_call(
                feature="generate_post",
                model=MODEL,
                max_tokens=100,
                prompt_chars=10,
                attempt=1,
                context=AIUsageContext(user_id=1, threads_account_id=10),
            ))
    finally:
        ai_cost.AI_FEATURE_DAILY_USD_LIMITS["generate_post"] = original
    assert store.events == []


def test_concurrent_reservations_cannot_both_cross_user_budget(monkeypatch):
    store = MemoryUsageStore()
    engine = AICostEngine(store)
    one_reservation = ai_cost.estimate_reservation_cost(
        MODEL,
        prompt_chars=10,
        max_output_tokens=1000,
    )
    monkeypatch.setattr(
        ai_cost.settings,
        "AI_USER_DAILY_USD_LIMIT",
        one_reservation + Decimal("0.0000000001"),
    )
    monkeypatch.setattr(ai_cost.settings, "AI_ACCOUNT_DAILY_USD_LIMIT", 10)

    async def run():
        context = AIUsageContext(user_id=1)
        return await asyncio.gather(
            engine.reserve_call(
                feature="generate_post",
                model=MODEL,
                max_tokens=1000,
                prompt_chars=10,
                attempt=1,
                context=context,
            ),
            engine.reserve_call(
                feature="generate_post",
                model=MODEL,
                max_tokens=1000,
                prompt_chars=10,
                attempt=1,
                context=context,
            ),
            return_exceptions=True,
        )

    results = asyncio.run(run())
    assert sum(isinstance(item, UsageReservation) for item in results) == 1
    assert sum(isinstance(item, BudgetExceeded) for item in results) == 1


def test_postgres_reservation_locks_before_budget_check_and_insert():
    calls = []

    class Result:
        def __init__(self, row=None):
            self.row = row

        def first(self):
            return self.row

        def mappings(self):
            return self

        def one(self):
            return self.row

    class Transaction:
        async def __aenter__(self):
            calls.append("begin")

        async def __aexit__(self, *_args):
            calls.append("commit")

    class FakePostgresSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def begin(self):
            return Transaction()

        async def execute(self, statement, _params=None):
            sql = str(statement).lower()
            if "pg_advisory_xact_lock" in sql:
                calls.append("lock")
                return Result()
            if "select 1 from ai_usage_events" in sql:
                calls.append("idempotency")
                return Result()
            if "as user_spend" in sql:
                calls.append("budget")
                return Result({
                    "user_spend": 0,
                    "account_spend": 0,
                    "feature_spend": 0,
                    "run_spend": 0,
                    "run_calls": 0,
                    "account_feature_calls": 0,
                    "current_hour_calls": 0,
                })
            if "insert into ai_usage_events" in sql:
                calls.append("insert")
                return Result()
            raise AssertionError(sql)

    store = PostgresUsageStore(lambda: FakePostgresSession())
    result = asyncio.run(store.reserve(
        candidate(cost="0.01"),
        limits(),
    ))

    assert isinstance(result, UsageReservation)
    assert calls == [
        "begin",
        "lock",
        "idempotency",
        "budget",
        "insert",
        "commit",
    ]


@pytest.mark.parametrize(
    ("feature", "setting_name"),
    [
        ("autocontent", "AI_AUTOCONTENT_ENABLED"),
        ("autocontent_repair", "AI_AUTOCONTENT_ENABLED"),
        ("neuro_comment", "AI_NEURO_ENABLED"),
        ("radar_analysis", "AI_RADAR_ENABLED"),
    ],
)
def test_feature_kill_switches(monkeypatch, feature, setting_name):
    store = MemoryUsageStore()
    engine = AICostEngine(store)
    monkeypatch.setattr(ai_cost.settings, setting_name, False)

    with pytest.raises(KillSwitchDisabled):
        asyncio.run(engine.reserve_call(
            feature=feature,
            model=MODEL,
            max_tokens=20,
            prompt_chars=10,
            attempt=1,
        ))
    assert store.events == []


def test_global_kill_switch(monkeypatch):
    store = MemoryUsageStore()
    monkeypatch.setattr(ai_cost.settings, "AI_ENABLED", False)
    with pytest.raises(KillSwitchDisabled):
        asyncio.run(AICostEngine(store).reserve_call(
            feature="generate_post",
            model=MODEL,
            max_tokens=20,
            prompt_chars=10,
            attempt=1,
        ))


def test_anomaly_detects_burst_with_history():
    snapshot = BudgetSnapshot(
        current_hour_calls=10,
        recent_hour_calls=(1, 2, 1, 1, 2, 1),
    )
    with pytest.raises(AnomalyDetected) as caught:
        evaluate_budget(
            candidate(cost="0.01"),
            snapshot,
            limits(feature_hourly_calls=100),
        )
    assert caught.value.reason == "feature_hourly_baseline"


def test_anomaly_does_not_fire_with_insufficient_history():
    evaluate_budget(
        candidate(cost="0.01"),
        BudgetSnapshot(
            current_hour_calls=10,
            recent_hour_calls=(1, 1, 1, 1, 1),
        ),
        limits(feature_hourly_calls=100),
    )


def test_daily_feature_summary_and_repairs(monkeypatch):
    monkeypatch.setattr(ai_cost.settings, "AI_USER_DAILY_USD_LIMIT", 10)
    monkeypatch.setattr(ai_cost.settings, "AI_ACCOUNT_DAILY_USD_LIMIT", 10)
    store = MemoryUsageStore()
    engine = AICostEngine(store)
    request_id = uuid.uuid4()
    context = AIUsageContext(user_id=1, threads_account_id=10)
    asyncio.run(reserve_and_complete(
        engine,
        context=context,
        feature="generate_post",
        attempt=1,
        status="failure",
        request_id=request_id,
    ))
    asyncio.run(reserve_and_complete(
        engine,
        context=context,
        feature="generate_post",
        attempt=2,
        request_id=request_id,
    ))
    service = AIUsageSummaryService(store)

    today = datetime.now(timezone.utc).date()
    total = asyncio.run(service.get(day=today))
    by_feature = asyncio.run(
        service.by_dimension(day=today, dimension="feature")
    )

    assert total["calls"] == 2
    assert total["successful_calls"] == 1
    assert total["failed_calls"] == 1
    assert total["repair_calls"] == 1
    assert by_feature[0]["dimension"] == "generate_post"
    assert by_feature[0]["calls"] == 2


def test_account_and_user_usage_are_isolated(monkeypatch):
    monkeypatch.setattr(ai_cost.settings, "AI_USER_DAILY_USD_LIMIT", 10)
    monkeypatch.setattr(ai_cost.settings, "AI_ACCOUNT_DAILY_USD_LIMIT", 10)
    store = MemoryUsageStore()
    engine = AICostEngine(store)
    asyncio.run(reserve_and_complete(
        engine,
        context=AIUsageContext(user_id=1, threads_account_id=10),
    ))
    asyncio.run(reserve_and_complete(
        engine,
        context=AIUsageContext(user_id=2, threads_account_id=20),
    ))
    service = AIUsageSummaryService(store)
    today = datetime.now(timezone.utc).date()

    user_one = asyncio.run(service.get(day=today, user_id=1))
    account_twenty = asyncio.run(
        service.get(day=today, threads_account_id=20)
    )

    assert user_one["calls"] == 1
    assert account_twenty["calls"] == 1


def test_missing_attribution_is_recorded_as_system_scope(monkeypatch):
    monkeypatch.setattr(ai_cost.settings, "AI_USER_DAILY_USD_LIMIT", 10)
    monkeypatch.setattr(ai_cost.settings, "AI_ACCOUNT_DAILY_USD_LIMIT", 10)
    store = MemoryUsageStore()
    asyncio.run(reserve_and_complete(AICostEngine(store)))

    reservation = store.events[0]["reservation"]
    assert reservation.user_id is None
    assert reservation.threads_account_id is None


def test_usage_schema_cannot_store_prompts_or_responses():
    migration = (
        Path(__file__).parents[1]
        / "migrations"
        / "006_ai_cost_engine.sql"
    ).read_text(encoding="utf-8").lower()
    table_body = migration.split("create table", 1)[1].split(");", 1)[0]
    assert "prompt" not in table_body
    assert "response" not in table_body
    assert "api_key" not in table_body
    assert "access_token" not in table_body
