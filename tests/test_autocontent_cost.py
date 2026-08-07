import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.ai_cost import (
    BudgetExceeded,
    BudgetSnapshot,
    ReservationCandidate,
    evaluate_budget,
    limits_for,
)
from app.core.autocontent_cost import (
    AutocontentCostGuard,
    PlannerAccountTelemetry,
    REPAIR_CIRCUIT_COOLDOWN,
    log_planner_account,
)
from app.core.autopost_status import _RETRY_BLOCKED_SQL
from app.core.scenarist import ContentQualityError
from app.worker import autocontent


NOW = datetime(2026, 8, 7, 10, tzinfo=timezone.utc)


class Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows

    def mappings(self):
        return self


class GuardSession:
    def __init__(self, *, state, window):
        self.state = state
        self.window = window
        self.calls = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        values = dict(params or {})
        self.calls.append((sql, values))
        if "SELECT cost_guard_until" in sql:
            return Result([self.state])
        if "WITH recent_generations" in sql:
            return Result([self.window])
        return Result()


def test_repair_rate_circuit_is_account_scoped_and_allows_probe():
    window = {
        "samples": 20,
        "repairs": 16,
        "latest_generation_at": NOW,
    }
    session = GuardSession(
        state={
            "cost_guard_until": None,
            "cost_guard_reason": None,
            "cost_guard_observed_at": None,
        },
        window=window,
    )
    decision = asyncio.run(
        AutocontentCostGuard(session).check(7, 11, now=NOW)
    )
    assert decision.blocked is True
    update = next(
        values for sql, values in session.calls
        if "SET cost_guard_until" in sql
    )
    assert update["user_id"] == 7
    assert update["account_id"] == 11
    assert update["retry_at"] == NOW + REPAIR_CIRCUIT_COOLDOWN

    probe_session = GuardSession(
        state={
            "cost_guard_until": NOW - timedelta(seconds=1),
            "cost_guard_reason": "REPAIR_RATE_HIGH",
            "cost_guard_observed_at": NOW,
        },
        window=window,
    )
    probe = asyncio.run(
        AutocontentCostGuard(probe_session).check(7, 11, now=NOW)
    )
    assert probe.blocked is False
    assert not any(
        "SET cost_guard_until" in sql for sql, _ in probe_session.calls
    )


def test_account_hourly_generation_and_repair_caps():
    for feature, expected_limit in (("autocontent", 10), ("autocontent_repair", 5)):
        limits = limits_for(feature)
        assert limits.account_feature_hourly_calls == expected_limit
        candidate = ReservationCandidate(
            event_key=f"{feature}:1",
            request_id=uuid.uuid4(),
            feature=feature,
            model="claude-sonnet-4-6",
            attempt=1,
            user_id=7,
            threads_account_id=11,
            run_id="slot:1",
            reserved_cost_usd=Decimal("0.01"),
        )
        with pytest.raises(BudgetExceeded) as caught:
            evaluate_budget(
                candidate,
                BudgetSnapshot(
                    account_feature_hour_calls=expected_limit,
                ),
                limits,
            )
        assert caught.value.reason == "account_feature_hourly_calls"


def test_planner_telemetry_contains_counts_but_not_content(caplog):
    caplog.set_level("INFO", logger="autocontent.cost")
    log_planner_account(
        PlannerAccountTelemetry(
            account_id=11,
            deficit_before=1,
            slots_claimed=1,
            generated=1,
            repaired=0,
            failed=0,
            deficit_after=0,
        ),
        planner_run_id="planner-1",
    )
    payload = json.loads(caplog.records[-1].message.split(" ", 1)[1])
    assert payload["deficit_before"] == 1
    assert payload["deficit_after"] == 0
    assert "text" not in payload
    assert "content" not in payload


def test_failed_generation_slot_has_durable_retry_cooldown_query():
    sql = str(_RETRY_BLOCKED_SQL)
    assert "run.status = 'failed'" in sql
    assert "run.finished_at > :retry_cutoff" in sql
    assert "run.threads_account_id = :account_id" in sql


class PlannerState:
    def __init__(self, deficit=1):
        self.deficit = deficit
        self.next_run_id = 50
        self.claim_lock = asyncio.Lock()
        self.scheduled = []
        self.finished = []


class PlannerService:
    state: PlannerState

    def __init__(self, _session):
        pass

    async def planning_deficit(self, *_args, **_kwargs):
        return self.state.deficit

    async def reserve_next_run(self, *_args, **_kwargs):
        async with self.state.claim_lock:
            if self.state.deficit <= 0:
                return None
            self.state.deficit -= 1
            self.state.next_run_id += 1
            return (
                self.state.next_run_id,
                datetime.now(timezone.utc) + timedelta(hours=2),
            )

    async def lock_queue(self, *_args, **_kwargs):
        return None

    async def attach_post(self, run_id, post_id):
        self.state.scheduled.append((run_id, post_id))

    async def finish_run(self, run_id, **kwargs):
        self.state.finished.append((run_id, kwargs))


class PlannerResult(Result):
    pass


class PlannerSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "FROM autocontent_settings WHERE user_id" in sql:
            future = datetime.now(timezone.utc) + timedelta(hours=2)
            return PlannerResult([(
                "topic",
                f"{future.hour:02d}:{future.minute:02d}",
                "all",
                "reach",
                "UTC",
            )])
        if "SELECT count(*) FROM scheduled_posts" in sql:
            return PlannerResult([(0,)])
        if "INSERT INTO scheduled_posts" in sql:
            return PlannerResult([(901,)])
        return PlannerResult()


class EmptyMemory:
    def __init__(self, _session):
        pass

    async def load(self, *_args, **_kwargs):
        return []


class AllowGuard:
    def __init__(self, _session):
        pass

    async def check(self, *_args, **_kwargs):
        return SimpleNamespace(blocked=False)


def configure_planner(monkeypatch, state, generate):
    PlannerService.state = state

    async def get_voice(*_args, **_kwargs):
        return {"tone": "direct"}

    async def brain(*_args, **_kwargs):
        return None

    async def spend_once(*_args, **_kwargs):
        return True

    async def no_refund(*_args, **_kwargs):
        raise AssertionError("successful generation must not refund")

    monkeypatch.setattr(autocontent, "Session", PlannerSession)
    monkeypatch.setattr(autocontent, "AutopostStatusService", PlannerService)
    monkeypatch.setattr(autocontent, "AutocontentCostGuard", AllowGuard)
    monkeypatch.setattr(autocontent, "ContentMemoryRepo", EmptyMemory)
    monkeypatch.setattr(autocontent.scenarist, "get_voice", get_voice)
    monkeypatch.setattr(
        autocontent.social_brain, "build_account_context", brain
    )
    monkeypatch.setattr(autocontent.scenarist, "generate_post", generate)
    monkeypatch.setattr(autocontent.credits, "spend_once", spend_once)
    monkeypatch.setattr(autocontent.credits, "topup", no_refund)


def generated_output(*, repaired=False):
    return {
        "hooks": [{"type": "insight", "text": "Opening"}],
        "selected_hook": {"text": "Opening"},
        "body": "A concrete body for the scheduled post.",
        "metadata": {
            "source": "autocontent",
            "pipeline_stage": "repair" if repaired else "generate",
            "repair_reasons": ["CTA_MISSING"] if repaired else [],
        },
    }


def test_account8_next_tick_does_not_regenerate_filled_slot(monkeypatch):
    state = PlannerState(deficit=1)
    calls = []

    async def generate(*_args, **_kwargs):
        calls.append(1)
        return generated_output()

    configure_planner(monkeypatch, state, generate)
    first = asyncio.run(autocontent._plan_for_user(
        7, 1, "niche", ["keyword"], 8,
        account_expires_at=NOW + timedelta(days=30),
    ))
    second = asyncio.run(autocontent._plan_for_user(
        7, 1, "niche", ["keyword"], 8,
        account_expires_at=NOW + timedelta(days=30),
    ))
    assert (first, second) == (1, 0)
    assert len(calls) == 1
    assert len(state.scheduled) == 1


def test_repaired_slot_and_concurrent_planners_are_idempotent(monkeypatch):
    state = PlannerState(deficit=1)
    calls = []

    async def generate(*_args, **_kwargs):
        calls.append(1)
        return generated_output(repaired=True)

    configure_planner(monkeypatch, state, generate)

    async def run_both():
        return await asyncio.gather(
            autocontent._plan_for_user(
                7, 1, "niche", ["keyword"], 8,
                account_expires_at=NOW + timedelta(days=30),
            ),
            autocontent._plan_for_user(
                7, 1, "niche", ["keyword"], 8,
                account_expires_at=NOW + timedelta(days=30),
            ),
        )

    assert sorted(asyncio.run(run_both())) == [0, 1]
    assert len(calls) == 1
    assert len(state.scheduled) == 1


def test_exact_deficit_generates_only_required_posts(monkeypatch):
    state = PlannerState(deficit=2)
    calls = []

    async def generate(*_args, **_kwargs):
        calls.append(1)
        return generated_output()

    configure_planner(monkeypatch, state, generate)
    result = asyncio.run(autocontent._plan_for_user(
        7, 1, "niche", ["keyword"], 8,
        account_expires_at=NOW + timedelta(days=30),
        max_generations=5,
    ))
    assert result == 2
    assert len(calls) == 2
    assert len(state.scheduled) == 2


def test_failed_repair_is_not_retried_on_next_tick(monkeypatch):
    state = PlannerState(deficit=1)
    calls = []
    refunds = []

    async def generate(*_args, **_kwargs):
        calls.append(1)
        raise ContentQualityError(
            "quality failed",
            repair_reasons=("missing_engagement_cta",),
        )

    async def refund(*_args, **_kwargs):
        refunds.append(1)
        return 10

    configure_planner(monkeypatch, state, generate)
    monkeypatch.setattr(autocontent.credits, "topup", refund)
    first = asyncio.run(autocontent._plan_for_user(
        7, 1, "niche", ["keyword"], 8,
        account_expires_at=NOW + timedelta(days=30),
    ))
    second = asyncio.run(autocontent._plan_for_user(
        7, 1, "niche", ["keyword"], 8,
        account_expires_at=NOW + timedelta(days=30),
    ))
    assert (first, second) == (1, 0)
    assert len(calls) == 1
    assert len(refunds) == 1
    assert state.finished[0][1]["status"] == "failed"


def test_concurrent_slot_claim_charges_credits_once(monkeypatch):
    state = PlannerState(deficit=1)
    charges = []

    async def generate(*_args, **_kwargs):
        return generated_output()

    async def spend_once(*_args, **_kwargs):
        charges.append(_args[-1])
        return True

    configure_planner(monkeypatch, state, generate)
    monkeypatch.setattr(autocontent.credits, "spend_once", spend_once)

    async def run_both():
        return await asyncio.gather(
            autocontent._plan_for_user(
                7, 1, "niche", ["keyword"], 8,
                account_expires_at=NOW + timedelta(days=30),
            ),
            autocontent._plan_for_user(
                7, 1, "niche", ["keyword"], 8,
                account_expires_at=NOW + timedelta(days=30),
            ),
        )

    asyncio.run(run_both())
    assert len(charges) == 1
