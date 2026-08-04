import asyncio
import inspect
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.bot.handlers.autopilot_intelligence import render_explanation
from app.bot.ux import render_dashboard
from app.core.autopilot_intelligence.context import (
    DecisionContextBuilder,
    DecisionOwnershipError,
    queue_health,
)
from app.core.autopilot_intelligence.engine import AutopilotIntelligenceEngine
from app.core.autopilot_intelligence.history import DecisionHistory
from app.core.autopilot_intelligence.models import (
    ActionType,
    AnalyticsSummary,
    BrainSummary,
    DecisionContext,
    DecisionRun,
    DecisionStatus,
    HealthBreakdown,
    LastDecisionSummary,
    NeuroSummary,
    QueueHealth,
    RadarSummary,
    RuleKind,
    RuleResult,
    SubscriptionSummary,
)
from app.core.autopilot_intelligence.optimizer import (
    DecisionOptimizer,
    HEALTH_WEIGHTS,
    calculate_health,
)
from app.core.autopilot_intelligence.repository import (
    DecisionRepository,
    decision_bucket,
)
from app.core.autopilot_intelligence.rules import (
    AnalyticsRule,
    BrainRule,
    CreditsRule,
    HealthRule,
    NeuroRule,
    PermissionRule,
    PublishingRule,
    QueueRule,
    RadarRule,
    RecoveryRule,
    SafetyRule,
    ScheduleRule,
)
from app.core.dashboard import DashboardService
from app.schemas.ux import (
    DashboardAnalytics,
    DashboardAutopilot,
    DashboardBalance,
    DashboardData,
    DashboardIntelligence,
)
from app.worker import autopilot_intelligence_jobs, main as worker_main

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 4, 10, 30, tzinfo=timezone.utc)


def context(**changes):
    values = {
        "user_id": 7,
        "threads_account_id": 11,
        "connection_status": "connected",
        "has_access_token": True,
        "token_expires_at": NOW + timedelta(days=30),
        "queue_size": 6,
        "scheduled_today": 2,
        "published_today": 1,
        "failed_today": 0,
        "analytics_summary": AnalyticsSummary(
            available=True,
            posts_total=10,
            average_views=500,
            engagement_rate=0.04,
            brain_score=70,
            best_topic="продукт",
            best_hour=18,
            updated_at=NOW,
        ),
        "brain_summary": BrainSummary(
            available=True,
            version=3,
            primary_goal="reach",
            performance_posts=10,
            updated_at=NOW,
        ),
        "radar_summary": RadarSummary(available=True, active=False),
        "neuro_summary": NeuroSummary(available=True, active=False),
        "credits_balance": 20,
        "subscription": SubscriptionSummary(plan="pro", status="active"),
        "timezone": "UTC",
        "goal": "reach",
        "topics": ("продукт",),
        "posts_per_day": 2,
        "planner_enabled": True,
        "publisher_enabled": True,
        "analytics_available": True,
        "last_publish": NOW - timedelta(hours=2),
        "last_generation": NOW - timedelta(hours=3),
        "queue_health": QueueHealth.HEALTHY,
        "autopilot_active": True,
        "current_time": NOW,
    }
    values.update(changes)
    return DecisionContext(**values)


def codes(rule, value):
    return {item.reason_code for item in rule.evaluate(value)}


def test_health_weights_are_open_and_total_one_hundred():
    assert HEALTH_WEIGHTS == {
        "token": 20,
        "credits": 15,
        "queue": 20,
        "analytics": 15,
        "radar": 10,
        "neuro": 10,
        "publishing": 10,
    }
    breakdown = calculate_health(context())
    assert breakdown.total == 100
    assert breakdown.model_dump() == {
        "token": 20,
        "credits": 15,
        "queue": 20,
        "analytics": 15,
        "radar": 10,
        "neuro": 10,
        "publishing": 10,
        "total": 100,
    }


def test_health_breakdown_rejects_an_incorrect_total():
    with pytest.raises(ValidationError):
        HealthBreakdown(
            token=20, credits=15, queue=20, analytics=15,
            radar=10, neuro=10, publishing=10, total=99,
        )


@pytest.mark.parametrize(
    ("balance", "expected"),
    [(0, 0), (1, 8), (4, 8), (5, 15), (100, 15)],
)
def test_health_credit_bands(balance, expected):
    assert calculate_health(context(credits_balance=balance)).credits == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (QueueHealth.DISABLED, 20),
        (QueueHealth.RECOVERY_REQUIRED, 0),
        (QueueHealth.EMPTY, 5),
        (QueueHealth.LOW, 12),
        (QueueHealth.FULL, 17),
        (QueueHealth.HEALTHY, 20),
    ],
)
def test_health_queue_bands(state, expected):
    assert calculate_health(context(queue_health=state)).queue == expected


def test_missing_optional_modules_are_neutral_but_missing_analytics_is_visible():
    value = context(
        radar_summary=RadarSummary(),
        neuro_summary=NeuroSummary(),
        analytics_summary=AnalyticsSummary(),
        analytics_available=False,
    )
    score = calculate_health(value)
    assert score.radar == 10
    assert score.neuro == 10
    assert score.analytics == 5
    assert score.total == 90


def test_queue_health_is_deterministic():
    common = {
        "active": True,
        "posts_per_day": 2,
        "failed_today": 0,
        "stuck_publishing": 0,
        "unknown_publications": 0,
    }
    assert queue_health(queue_size=0, **common) == QueueHealth.EMPTY
    assert queue_health(queue_size=2, **common) == QueueHealth.LOW
    assert queue_health(queue_size=4, **common) == QueueHealth.HEALTHY
    assert queue_health(queue_size=15, **common) == QueueHealth.FULL
    assert queue_health(
        queue_size=4, **{**common, "stuck_publishing": 1}
    ) == QueueHealth.RECOVERY_REQUIRED


def test_permission_rule_covers_disconnect_missing_and_expired_token():
    rule = PermissionRule()
    assert "ACCOUNT_DISCONNECTED" in codes(
        rule, context(connection_status="disconnected", publisher_enabled=False)
    )
    assert "NO_TOKEN" in codes(
        rule, context(has_access_token=False, publisher_enabled=False)
    )
    assert "TOKEN_EXPIRED" in codes(
        rule,
        context(
            token_expires_at=NOW - timedelta(seconds=1),
            publisher_enabled=False,
        ),
    )


def test_credits_rule_distinguishes_zero_low_and_enough():
    assert "NO_CREDITS" in codes(CreditsRule(), context(credits_balance=0))
    assert "LOW_CREDITS" in codes(CreditsRule(), context(credits_balance=3))
    assert CreditsRule().evaluate(context(credits_balance=5)) == ()


@pytest.mark.parametrize(
    ("state", "code"),
    [
        (QueueHealth.DISABLED, "AUTOPILOT_DISABLED"),
        (QueueHealth.EMPTY, "QUEUE_EMPTY"),
        (QueueHealth.LOW, "QUEUE_LOW"),
        (QueueHealth.FULL, "QUEUE_FULL"),
        (QueueHealth.HEALTHY, "QUEUE_HEALTHY"),
    ],
)
def test_queue_rule_states(state, code):
    assert code in codes(QueueRule(), context(queue_health=state))


def test_analytics_rule_missing_stale_low_and_good_topic():
    rule = AnalyticsRule()
    assert "ANALYTICS_UNAVAILABLE" in codes(
        rule, context(analytics_summary=AnalyticsSummary())
    )
    assert "ANALYTICS_DELAYED" in codes(
        rule,
        context(analytics_summary=AnalyticsSummary(
            available=True, stale=True, posts_total=10
        )),
    )
    assert "LOW_PERFORMANCE" in codes(
        rule,
        context(analytics_summary=AnalyticsSummary(
            available=True, posts_total=10, brain_score=30
        )),
    )
    assert "LOW_ENGAGEMENT" in codes(
        rule,
        context(analytics_summary=AnalyticsSummary(
            available=True, posts_total=10,
            engagement_rate=0, brain_score=50,
        )),
    )
    assert "GOOD_TOPIC_FOUND" in codes(rule, context())


def test_radar_found_topic_and_permission_failure():
    found = context(radar_summary=RadarSummary(
        available=True,
        active=True,
        ready_count=3,
        best_score=91,
        last_status="success",
    ))
    assert "HOT_TOPIC_FOUND" in codes(RadarRule(), found)
    denied = context(radar_summary=RadarSummary(
        available=True, active=True, last_status="permission_denied"
    ))
    result = RadarRule().evaluate(denied)[0]
    assert result.kind == RuleKind.BLOCKER
    assert result.action == ActionType.RECONNECT_ACCOUNT


def test_neuro_ready_unknown_and_limit_states():
    ready = context(neuro_summary=NeuroSummary(
        available=True, active=True, pending_count=2, daily_cap=5
    ))
    assert "NEURO_QUEUE_READY" in codes(NeuroRule(), ready)
    unknown = context(neuro_summary=NeuroSummary(
        available=True, active=True, unknown_count=1, daily_cap=5
    ))
    assert "RECOVERY_REQUIRED" in codes(NeuroRule(), unknown)
    capped = context(neuro_summary=NeuroSummary(
        available=True, active=True, posted_today=5, daily_cap=5
    ))
    assert "NEURO_LIMIT_REACHED" in codes(NeuroRule(), capped)


def test_publishing_and_recovery_are_separate_rules():
    assert "PUBLISH_FAILED" in codes(
        PublishingRule(), context(failed_today=1)
    )
    assert "RECOVERY_REQUIRED" in codes(
        RecoveryRule(), context(stuck_publishing=1)
    )


def test_schedule_brain_and_safety_rules():
    assert "SCHEDULE_NOT_CONFIGURED" in codes(
        ScheduleRule(), context(posts_per_day=0)
    )
    assert "BRAIN_UNAVAILABLE" in codes(
        BrainRule(), context(brain_summary=BrainSummary())
    )
    assert "TOPICS_NOT_CONFIGURED" in codes(
        SafetyRule(), context(topics=())
    )
    assert "SUBSCRIPTION_INACTIVE" in codes(
        SafetyRule(),
        context(subscription=SubscriptionSummary(status="expired")),
    )


def test_health_rule_reports_low_operational_health():
    unhealthy = context(
        has_access_token=False,
        publisher_enabled=False,
        credits_balance=0,
        queue_health=QueueHealth.RECOVERY_REQUIRED,
        stuck_publishing=1,
        analytics_summary=AnalyticsSummary(),
        analytics_available=False,
    )
    assert "SYSTEM_HEALTH_LOW" in codes(HealthRule(), unhealthy)


def test_optimizer_blocker_wins_over_queue_analytics_and_radar():
    value = context(
        has_access_token=False,
        publisher_enabled=False,
        queue_size=0,
        queue_health=QueueHealth.EMPTY,
        radar_summary=RadarSummary(
            available=True, active=True, ready_count=2, best_score=95
        ),
    )
    decision = AutopilotIntelligenceEngine().evaluate(value)
    assert decision.status == DecisionStatus.BLOCKED
    assert decision.recommendation == "NO_TOKEN"
    assert decision.safe_action == ActionType.RECONNECT_ACCOUNT
    assert "QUEUE_EMPTY" in decision.reason_codes
    assert "HOT_TOPIC_FOUND" in decision.reason_codes


def test_optimizer_recovery_wins_over_refill_and_failed_warning():
    value = context(
        queue_health=QueueHealth.RECOVERY_REQUIRED,
        stuck_publishing=1,
        failed_today=1,
    )
    decision = AutopilotIntelligenceEngine().evaluate(value)
    assert decision.recommendation == "RECOVERY_REQUIRED"
    assert decision.safe_action == ActionType.OPEN_RECOVERY
    assert decision.status == DecisionStatus.BLOCKED


def test_optimizer_selects_highest_priority_safe_action_without_blockers():
    values = (
        RuleResult(
            rule_id="queue", kind=RuleKind.ACTION,
            reason_code="QUEUE_LOW", priority=60,
            action=ActionType.OPEN_QUEUE,
        ),
        RuleResult(
            rule_id="radar", kind=RuleKind.RECOMMENDATION,
            reason_code="HOT_TOPIC_FOUND", priority=72,
            action=ActionType.OPEN_RADAR,
        ),
        RuleResult(
            rule_id="analytics", kind=RuleKind.WARNING,
            reason_code="ANALYTICS_DELAYED", priority=90,
        ),
    )
    health = calculate_health(context())
    decision = DecisionOptimizer().optimize(context(), values, health)
    assert decision.recommendation == "HOT_TOPIC_FOUND"
    assert decision.safe_action == ActionType.OPEN_RADAR


def test_optimizer_selects_highest_priority_blocker():
    values = (
        RuleResult(
            rule_id="credits", kind=RuleKind.BLOCKER,
            reason_code="NO_CREDITS", priority=88,
            action=ActionType.OPEN_BALANCE,
        ),
        RuleResult(
            rule_id="permissions", kind=RuleKind.BLOCKER,
            reason_code="NO_TOKEN", priority=100,
            action=ActionType.RECONNECT_ACCOUNT,
        ),
    )
    decision = DecisionOptimizer().optimize(
        context(), values, calculate_health(context())
    )
    assert decision.recommendation == "NO_TOKEN"
    assert decision.blockers == ("NO_TOKEN", "NO_CREDITS")


def test_same_context_and_decision_have_stable_hashes():
    first = context()
    second = context(
        current_time=NOW + timedelta(minutes=10),
        last_decision=LastDecisionSummary(
            decision_hash="a" * 64,
            status=DecisionStatus.HEALTHY,
            health_score=100,
            created_at=NOW,
        ),
    )
    assert first.context_hash() == second.context_hash()
    first_result = AutopilotIntelligenceEngine().evaluate(first)
    second_result = AutopilotIntelligenceEngine().evaluate(second)
    assert first_result.decision_hash() == second_result.decision_hash()


def test_account_isolation_changes_context_hash_and_decision_scope():
    first = context(user_id=7, threads_account_id=11)
    second = context(user_id=7, threads_account_id=12)
    assert first.context_hash() != second.context_hash()
    first_run = run(first)
    second_run = run(second)
    assert first_run.threads_account_id != second_run.threads_account_id


def test_different_state_produces_different_decision_result():
    healthy = AutopilotIntelligenceEngine().evaluate(context())
    empty = AutopilotIntelligenceEngine().evaluate(context(
        queue_size=0, queue_health=QueueHealth.EMPTY
    ))
    assert healthy.decision_hash() != empty.decision_hash()
    assert empty.next_recommended_action == ActionType.OPEN_QUEUE


def test_decision_bucket_is_stable_for_fifteen_minutes():
    assert decision_bucket(NOW.replace(minute=31)) == NOW.replace(
        minute=30, second=0, microsecond=0
    )
    assert decision_bucket(NOW.replace(minute=44)) == decision_bucket(
        NOW.replace(minute=31)
    )
    assert decision_bucket(NOW.replace(minute=45)) != decision_bucket(
        NOW.replace(minute=44)
    )


def run(value=None, run_id=1):
    value = value or context()
    result = AutopilotIntelligenceEngine().evaluate(value)
    return DecisionRun(
        id=run_id,
        user_id=value.user_id,
        threads_account_id=value.threads_account_id,
        context_hash=value.context_hash(),
        decision_hash=result.decision_hash(),
        result=result,
        created_at=NOW,
    )


class FakeHistoryRepository:
    def __init__(self, values):
        self.values = values
        self.calls = []

    async def latest(self, user_id, account_id):
        self.calls.append(("latest", user_id, account_id))
        return self.values[0] if self.values else None

    async def history(self, user_id, account_id, *, limit, offset):
        self.calls.append(("history", user_id, account_id, limit, offset))
        return self.values[offset:offset + limit]


def test_history_facade_preserves_account_scope_and_pagination():
    repository = FakeHistoryRepository([run(run_id=1), run(run_id=2)])
    history = DecisionHistory(repository)
    assert asyncio.run(history.latest(7, 11)).id == 1
    assert len(asyncio.run(history.page(7, 11, limit=1, offset=1))) == 1
    assert repository.calls == [
        ("latest", 7, 11),
        ("history", 7, 11, 1, 1),
    ]


def test_repository_reads_and_writes_are_account_scoped():
    source = inspect.getsource(DecisionRepository)
    assert "account.user_id = :user_id" in source
    assert "run.user_id = :user_id" in source
    assert "run.threads_account_id = :account_id" in source
    assert "ON CONFLICT" in source
    assert "context_hash" in source


def test_repository_and_dashboard_normalize_json_strings():
    expected = run()
    restored = DecisionRepository._run({
        "id": expected.id,
        "user_id": expected.user_id,
        "threads_account_id": expected.threads_account_id,
        "context_hash": expected.context_hash,
        "decision_hash": expected.decision_hash,
        "result_json": expected.result.model_dump_json(),
        "created_at": expected.created_at,
    })
    assert restored == expected
    block = DashboardService._intelligence({
        "id": expected.id,
        "status": expected.result.status.value,
        "health_score": expected.result.health_score,
        "result_json": expected.result.model_dump_json().encode("utf-8"),
        "created_at": expected.created_at,
    })
    assert block.available is True
    assert block.human_message == expected.result.human_message


class NoopSession:
    @asynccontextmanager
    async def begin_nested(self):
        yield


class FakeAutopost:
    def __init__(self):
        self.calls = []

    async def get_status(self, user_id, account_id, *, now):
        self.calls.append(("status", user_id, account_id))
        return SimpleNamespace(
            settings=SimpleNamespace(enabled=True, posts_per_day=2),
            last_success_at=NOW - timedelta(hours=1),
            last_run_at=NOW - timedelta(hours=2),
        )

    async def queue_summary(self, user_id, account_id, *, now):
        self.calls.append(("queue", user_id, account_id))
        return SimpleNamespace(posts=(1, 2, 3, 4))


class FakeAnalytics:
    async def overview(self, user_id, account_id):
        return {
            "posts_total": 8,
            "avg_views": 400,
            "avg_er": 0.03,
            "brain_score": 70,
            "best_topic": "продукт",
            "best_hour": 18,
            "updated_at": NOW,
        }


class BrokenAnalytics:
    async def overview(self, user_id, account_id):
        raise RuntimeError("analytics unavailable")


class FakeBrains:
    async def get_by_account(self, user_id, account_id):
        return SimpleNamespace(
            goals={"primary": "engagement"},
            performance={"feedback_v1": {"posts_analyzed": 8}},
            version=2,
            updated_at=NOW,
        )


class Builder(DecisionContextBuilder):
    async def _load_account(self, user_id, account_id):
        return {
            "id": account_id,
            "connection_status": "connected",
            "has_access_token": True,
            "expires_at": NOW + timedelta(days=30),
            "credits_balance": 30,
            "subscription_plan": "pro",
            "subscription_status": "active",
            "planner_enabled": True,
            "posts_per_day": 2,
            "topics": "продукт\nмаркетинг",
            "goal": "reach",
            "timezone": "UTC",
        }

    async def _load_daily(self, user_id, account_id, timezone_name, now):
        return {
            "scheduled_today": 2,
            "published_today": 1,
            "failed_today": 0,
            "stuck_publishing": 0,
            "unknown_publications": 0,
        }

    async def _load_radar(self, user_id, account_id):
        return {
            "keyword_count": 2,
            "last_search_at": NOW,
            "last_status": "success",
            "ready_count": 1,
            "best_score": 85,
        }

    async def _load_neuro(self, user_id, account_id, timezone_name, now):
        return {
            "active": True,
            "mode": "approve",
            "daily_cap": 5,
            "pending_count": 1,
        }

    async def _load_last_decision(self, user_id, account_id):
        return {}


def test_context_builder_reuses_existing_services_and_contains_no_secret():
    autopost = FakeAutopost()
    builder = Builder(
        NoopSession(),
        autopost=autopost,
        analytics=FakeAnalytics(),
        brains=FakeBrains(),
        clock=lambda: NOW,
    )
    value = asyncio.run(builder.build(7, 11))
    assert value.queue_size == 4
    assert value.analytics_summary.posts_total == 8
    assert value.brain_summary.performance_posts == 8
    assert value.radar_summary.ready_count == 1
    assert value.neuro_summary.pending_count == 1
    assert autopost.calls == [("status", 7, 11), ("queue", 7, 11)]
    serialized = value.model_dump_json()
    assert '"has_access_token":true' in serialized
    assert "access_token_enc" not in serialized
    assert "encrypted-token" not in serialized


def test_context_builder_degrades_when_optional_analytics_is_unavailable():
    builder = Builder(
        NoopSession(),
        autopost=FakeAutopost(),
        analytics=BrokenAnalytics(),
        brains=FakeBrains(),
        clock=lambda: NOW,
    )
    value = asyncio.run(builder.build(7, 11))
    assert value.analytics_available is False
    assert value.analytics_summary.available is False
    assert "ANALYTICS_UNAVAILABLE" in codes(AnalyticsRule(), value)


class EmptyMappingsResult:
    def mappings(self):
        return self

    def first(self):
        return None


class EmptyAccountSession(NoopSession):
    async def execute(self, _statement, _params):
        return EmptyMappingsResult()


def test_context_builder_rejects_a_foreign_or_missing_account():
    builder = DecisionContextBuilder(EmptyAccountSession(), clock=lambda: NOW)
    with pytest.raises(DecisionOwnershipError):
        asyncio.run(builder.build(7, 999))


def test_context_source_query_proves_ownership_without_loading_token_value():
    source = inspect.getsource(DecisionContextBuilder._load_account)
    assert "account.user_id = :user_id" in source
    assert "account.id = :account_id" in source
    assert "access_token_enc IS NOT NULL" in source
    assert "account.access_token_enc," not in source


def test_dashboard_and_explanation_are_russian_and_non_technical():
    decision = run()
    rendered = render_explanation(decision)
    assert "Почему Автопилот так решил" in rendered
    assert "Оценка: 100 из 100" in rendered
    assert "Следующий шаг" in rendered
    assert "SYSTEM_HEALTHY" not in rendered
    dashboard = DashboardData(
        user_id=7,
        account_id=11,
        username="creator",
        autopilot=DashboardAutopilot(enabled=True),
        analytics=DashboardAnalytics(),
        balance=DashboardBalance(credits=20, plan="pro"),
        intelligence=DashboardIntelligence(
            available=True,
            status="healthy",
            health_score=100,
            human_message="Всё работает, срочных действий нет.",
        ),
    )
    dashboard_text = render_dashboard(dashboard)
    assert "Что рекомендует Автопилот" in dashboard_text
    assert "Оценка состояния: 100 из 100" in dashboard_text


def test_dashboard_without_decision_has_safe_fallback():
    dashboard = DashboardData(
        user_id=7,
        account_id=11,
        username="creator",
        intelligence=DashboardIntelligence(
            warning="Рекомендация пока рассчитывается."
        ),
    )
    assert "Рекомендация пока рассчитывается" in render_dashboard(dashboard)


class WorkerResult:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class WorkerSession:
    rows = [(7, 11), (8, 12)]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _statement):
        return WorkerResult(self.rows)

    async def commit(self):
        return None

    async def rollback(self):
        return None


def test_worker_evaluates_multiple_accounts_in_isolation(monkeypatch):
    calls = []

    class Service:
        def __init__(self, _session):
            pass

        async def evaluate_account(self, user_id, account_id):
            calls.append((user_id, account_id))
            if account_id == 12:
                raise RuntimeError("isolated failure")

    monkeypatch.setattr(autopilot_intelligence_jobs, "Session", WorkerSession)
    monkeypatch.setattr(
        autopilot_intelligence_jobs,
        "AutopilotIntelligenceService",
        Service,
    )
    totals = asyncio.run(
        autopilot_intelligence_jobs.autopilot_intelligence_job()
    )
    assert calls == [(7, 11), (8, 12)]
    assert totals == {"accounts": 2, "evaluated": 1, "failed": 1}


def test_worker_has_no_llm_network_credit_or_execution_dependency():
    source = inspect.getsource(autopilot_intelligence_jobs)
    for forbidden in (
        "ask_json", "Anthropic", "httpx", "threads_api", "spend_once",
        "publish_one", "generate",
    ):
        assert forbidden not in source


def test_core_is_deterministic_and_has_no_llm_or_credit_spending():
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "app/core/autopilot_intelligence").glob("*.py")
    )
    for forbidden in (
        "ask_json", "Anthropic", "random.", "spend_once",
        "credits.spend", "publish_one", "create_container",
    ):
        assert forbidden not in source


def test_scheduler_runs_intelligence_every_fifteen_minutes(monkeypatch):
    jobs = []

    class Scheduler:
        def __init__(self, *args, **kwargs):
            pass

        def add_job(self, *args, **kwargs):
            jobs.append((args, kwargs))

    monkeypatch.setattr(worker_main, "AsyncIOScheduler", Scheduler)
    worker_main.build_scheduler()
    item = next(
        (args, kwargs) for args, kwargs in jobs
        if args[0] is worker_main.autopilot_intelligence_job
    )
    assert item[0][1] == "interval"
    assert item[1]["minutes"] == 15


def test_migration_contract_and_rollback_are_scoped():
    migration = (
        ROOT / "migrations/014_autopilot_intelligence_v1.sql"
    ).read_text(encoding="utf-8")
    rollback = (
        ROOT / "migrations/rollback/014_autopilot_intelligence_v1.sql"
    ).read_text(encoding="utf-8")
    assert "create table decision_runs" in migration.lower()
    assert "decision_hash" in migration
    assert "health_score" in migration
    assert "decision_runs_context_bucket_unique" in migration
    assert "foreign key (threads_account_id, user_id)" in migration
    assert "drop table if exists decision_runs" in rollback.lower()
    assert "scheduled_posts" not in rollback
