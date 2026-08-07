import asyncio
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pytest
import httpx

from app.bot.handlers.autocontent_ui import _menu_kb
from app.core import autopilot, credits, scenarist, threads_api
from app.core.autopost_status import (
    AutopostStatusService,
    SAFE_ERROR_MESSAGES,
    normalize_error,
    parse_slots,
    recover_autopost_state,
    render_history,
    render_status,
    select_next_slots,
    serialize_slots,
)
from app.core.llm import LLMError
from app.core.threads_api import ThreadsAPIError
from app.schemas.autopost import (
    AutopostAccount,
    AutopostRun,
    AutopostSettings,
    AutopostStatus,
)
from app.worker import autocontent, main as worker_main

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


class FakeResult:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.rowcount = len(self.rows)

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class QueueSession:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), dict(params or {})))
        rows = self.responses.pop(0) if self.responses else []
        return FakeResult(rows)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def make_status(
    *,
    enabled=True,
    next_run_at=NOW + timedelta(hours=2, minutes=14),
    last_status="success",
    error_code=None,
    error_message=None,
):
    return AutopostStatus(
        account=AutopostAccount(
            id=11,
            username="creator",
            expires_at=NOW + timedelta(days=20),
        ),
        settings=AutopostSettings(
            enabled=enabled,
            posts_per_day=1,
            slots=(time(10), time(18, 30)),
            days="all",
            timezone="Europe/Moscow",
        ),
        next_run_at=next_run_at if enabled else None,
        last_run_at=NOW,
        last_run_status=last_status,
        last_success_at=(
            NOW - timedelta(hours=2)
            if last_status == "success"
            else None
        ),
        last_threads_post_id=(
            "threads-post-1" if last_status == "success" else None
        ),
        safe_error_code=error_code,
        safe_error_message=error_message,
    )


def make_run(index, status="success", message=None):
    return AutopostRun(
        id=index,
        user_id=7,
        threads_account_id=11,
        scheduled_post_id=100 + index,
        scheduled_at=NOW - timedelta(days=index),
        started_at=NOW - timedelta(days=index, minutes=1),
        finished_at=NOW - timedelta(days=index),
        status=status,
        threads_post_id=(
            f"threads-{index}" if status == "success" else None
        ),
        error_code=(
            "THREADS_TEMPORARY_ERROR"
            if status == "failed"
            else None
        ),
        safe_error_message=message,
    )


def test_slot_parser_supports_legacy_hours_and_minutes():
    slots = parse_slots("9, 14:30, 19,14:30")
    assert slots == (time(9), time(14, 30), time(19))
    assert serialize_slots(slots) == "09:00,14:30,19:00"


def test_next_run_uses_exact_today_slot():
    candidates = select_next_slots(
        now=NOW,
        slots=(time(18, 30),),
        days="all",
        timezone_name="Europe/Moscow",
        posts_per_day=1,
    )
    assert candidates == (
        datetime(2026, 7, 31, 15, 30, tzinfo=timezone.utc),
    )


def test_next_run_moves_to_tomorrow_when_today_is_full():
    occupied = [datetime(2026, 7, 31, 7, 0, tzinfo=timezone.utc)]
    candidates = select_next_slots(
        now=NOW,
        slots=(time(10), time(18, 30)),
        days="all",
        timezone_name="Europe/Moscow",
        posts_per_day=1,
        occupied=occupied,
    )
    assert candidates[0] == datetime(
        2026,
        8,
        1,
        7,
        0,
        tzinfo=timezone.utc,
    )


def test_schedule_respects_different_timezones():
    berlin = select_next_slots(
        now=NOW,
        slots=(time(18, 30),),
        days="all",
        timezone_name="Europe/Berlin",
        posts_per_day=1,
    )
    almaty = select_next_slots(
        now=NOW,
        slots=(time(18, 30),),
        days="all",
        timezone_name="Asia/Almaty",
        posts_per_day=1,
    )
    assert berlin[0] == datetime(
        2026, 7, 31, 16, 30, tzinfo=timezone.utc
    )
    assert almaty[0] == datetime(
        2026, 7, 31, 13, 30, tzinfo=timezone.utc
    )


def test_schedule_without_slots_has_no_next_run():
    assert select_next_slots(
        now=NOW,
        slots=(),
        days="all",
        timezone_name="Europe/Moscow",
        posts_per_day=1,
    ) == ()


def test_enabled_telegram_status_shows_next_and_account():
    rendered = render_status(make_status(), now=NOW)
    assert "Статус: 🟢 Включён" in rendered
    assert "Аккаунт: @creator" in rendered
    assert "Сегодня, 17:14" in rendered
    assert "через 2 ч 14 мин" in rendered


def test_disabled_status_has_no_scheduled_post():
    rendered = render_status(
        make_status(enabled=False, next_run_at=None),
        now=NOW,
    )
    assert "Статус: ⚪ Выключен" in rendered
    assert "Следующий пост:\nне запланирован" in rendered


def test_menu_has_required_enabled_and_disabled_controls():
    account = make_status().account
    enabled_labels = [
        button.text
        for row in _menu_kb(make_status(), [account]).inline_keyboard
        for button in row
    ]
    disabled_labels = [
        button.text
        for row in _menu_kb(
            make_status(enabled=False),
            [account],
        ).inline_keyboard
        for button in row
    ]
    assert "⏸ Остановить" in enabled_labels
    assert "📋 Очередь" in enabled_labels
    assert "🕘 История" in enabled_labels
    assert "⚙️ Настройки" in enabled_labels
    assert "▶️ Включить Автопилот" in disabled_labels


def test_menu_exposes_each_connected_account():
    first = make_status()
    second_account = AutopostAccount(
        id=22,
        username="second",
        expires_at=NOW + timedelta(days=20),
    )
    labels = [
        button.text
        for row in _menu_kb(
            first,
            [first.account, second_account],
        ).inline_keyboard
        for button in row
    ]
    assert "✅ @creator" in labels
    assert "@second" in labels


def test_status_service_returns_structured_last_result():
    session = QueueSession([
        [{
            "account_id": 11,
            "username": "creator",
            "expires_at": NOW + timedelta(days=10),
            "active": False,
            "posts_per_day": 2,
            "slots": "09:00,18:30",
            "days": "weekdays",
            "timezone": "Europe/Moscow",
        }],
        [{
            "last_run_at": NOW - timedelta(hours=1),
            "last_run_status": "success",
            "safe_error_code": None,
            "safe_error_message": None,
            "last_success_at": NOW - timedelta(hours=1),
            "last_threads_post_id": "threads-42",
            "next_run_at": None,
        }],
    ])
    status = asyncio.run(AutopostStatusService(session).get_status(
        7,
        11,
        now=NOW,
    ))
    assert status.settings.enabled is False
    assert status.settings.posts_per_day == 2
    assert status.last_run_status == "success"
    assert status.last_threads_post_id == "threads-42"
    assert all(call[1]["account_id"] == 11 for call in session.calls)


def test_history_is_limited_and_contains_only_safe_messages():
    runs = [
        make_run(
            index,
            status="failed",
            message=SAFE_ERROR_MESSAGES["THREADS_TEMPORARY_ERROR"],
        )
        for index in range(12)
    ]
    rendered = render_history(runs, "Europe/Moscow")
    assert rendered.count("Threads API временно недоступен") == 10
    assert "access_token" not in rendered
    assert "stack trace" not in rendered


@pytest.mark.parametrize(
    ("error", "stage", "expected"),
    [
        (
            ThreadsAPIError("HTTP 401", status_code=401),
            "publication",
            "AUTH_EXPIRED",
        ),
        (
            ThreadsAPIError(
                'OAuthException {"code":190}',
                status_code=400,
            ),
            "publication",
            "AUTH_EXPIRED",
        ),
        (
            ThreadsAPIError("permission denied", status_code=403),
            "publication",
            "PERMISSION_DENIED",
        ),
        (
            ThreadsAPIError("HTTP 503", status_code=503),
            "publication",
            "THREADS_TEMPORARY_ERROR",
        ),
        (
            httpx.ReadTimeout("network timeout"),
            "publication",
            "THREADS_TEMPORARY_ERROR",
        ),
        (
            credits.NotEnoughCredits(),
            "generation",
            "INSUFFICIENT_CREDITS",
        ),
        (
            LLMError("provider failed"),
            "generation",
            "GENERATION_FAILED",
        ),
        (
            scenarist.ContentQualityError("quality failed"),
            "generation",
            "QUALITY_FAILED",
        ),
    ],
)
def test_error_normalization(error, stage, expected):
    code, message = normalize_error(error, stage=stage)
    assert code == expected
    assert message == SAFE_ERROR_MESSAGES[expected]
    if str(error):
        assert str(error) not in message


def test_run_reservation_is_database_idempotent():
    session = QueueSession([[(51,)], []])
    service = AutopostStatusService(session)
    first = asyncio.run(service.reserve_run(7, 11, NOW))
    second = asyncio.run(service.reserve_run(7, 11, NOW))
    assert first == 51
    assert second is None
    sql = session.calls[0][0]
    assert "ON CONFLICT DO NOTHING" in sql
    assert "DO NOTHING" in sql
    assert "AND setting.active" in sql


def test_disabling_skips_future_runs_but_not_publishing():
    session = QueueSession([
        [(901,), (None,)],
        [],
    ])
    skipped = asyncio.run(
        AutopostStatusService(session).skip_pending_for_account(7, 11)
    )
    assert skipped == 2
    sql = session.calls[0][0]
    assert "post.status = 'pending'" in sql
    assert "status = 'skipped'" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert session.calls[1][1]["post_ids"] == [901]
    assert session.calls[0][1]["account_id"] == 11


def test_publication_claim_is_atomic_and_timestamped():
    session = QueueSession([[(901, 7, 11, "body", None, None)]])
    rows = asyncio.run(autopilot.claim_due_posts(session))
    assert rows[0][0] == 901
    sql = session.calls[0][0]
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "publish_started_at = now()" in sql
    assert "status = 'pending'" in sql


def test_recovery_marks_interrupted_misfire_and_orphan():
    session = QueueSession([
        [(70,)],
        [(901,)],
        [],
        [(902,)],
        [],
        [(77,)],
    ])
    result = asyncio.run(recover_autopost_state(session, now=NOW))
    assert result == {
        "projected": 1,
        "interrupted": 1,
        "misfired": 1,
        "orphaned": 1,
    }
    sql = "\n".join(call[0] for call in session.calls)
    assert "INSERT INTO autopost_runs" in sql
    assert "post.status = 'publishing'" in sql
    assert "post.publish_started_at < :publish_cutoff" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "candidate.scheduled_at < :cutoff" in sql
    assert "scheduled_post_id IS NULL" in sql


def test_scheduler_coalesces_and_runs_planner_immediately(monkeypatch):
    captured = {"jobs": []}

    class FakeScheduler:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        def add_job(self, func, trigger, **kwargs):
            captured["jobs"].append((func, trigger, kwargs))

    monkeypatch.setattr(worker_main, "AsyncIOScheduler", FakeScheduler)
    scheduler = worker_main.build_scheduler()
    assert isinstance(scheduler, FakeScheduler)
    assert captured["init"]["job_defaults"]["coalesce"] is True
    planner_job = next(
        job
        for job in captured["jobs"]
        if job[0] is worker_main.autocontent_planner
    )
    assert planner_job[2]["minutes"] == 5
    assert planner_job[2]["next_run_time"] is not None
    recovery_job = next(
        job
        for job in captured["jobs"]
        if job[0] is worker_main.autopost_recovery_job
    )
    assert recovery_job[2]["minutes"] == 5


def test_second_publish_attempt_does_not_call_threads(monkeypatch):
    calls = []

    async def create(*_args, **_kwargs):
        calls.append(True)

    monkeypatch.setattr(autopilot, "create_container", create)
    session = QueueSession([[("done",)]])
    result = asyncio.run(autopilot.publish_one(
        session,
        (901, 7, 11, "body", None, None),
    ))
    assert result == (False, "Публикация уже обработана.")
    assert calls == []


def test_threads_error_stores_safe_result_only(monkeypatch):
    async def fail_create(*_args, **_kwargs):
        raise ThreadsAPIError(
            "technical response access_token=secret",
            status_code=503,
        )

    monkeypatch.setattr(autopilot, "decrypt_token", lambda _value: "token")
    monkeypatch.setattr(autopilot, "create_container", fail_create)
    session = QueueSession([
        [("publishing",)],
        [(0,)],
        [(
            "threads-user",
            b"encrypted",
            datetime(2099, 1, 1, tzinfo=timezone.utc),
        )],
        [],
        [],
    ])
    ok, message = asyncio.run(autopilot.publish_one(
        session,
        (901, 7, 11, "body", None, None),
    ))
    assert ok is False
    assert message == "❌ Threads API временно недоступен"
    serialized_params = repr([call[1] for call in session.calls])
    assert "access_token" not in serialized_params
    assert "secret" not in serialized_params


def test_threads_response_error_redacts_credentials():
    response = httpx.Response(
        400,
        request=httpx.Request(
            "POST",
            "https://graph.threads.net/v1.0/1/threads",
        ),
        text='{"access_token":"secret-value","error":"bad request"}',
    )
    error = threads_api._safe_response_error(response)
    assert "secret-value" not in str(error)
    assert "<redacted>" in str(error)
    assert error.status_code == 400


class PlannerSession(QueueSession):
    def __init__(self, slot_text):
        super().__init__()
        self.slot_text = slot_text

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, dict(params or {})))
        if "FROM autocontent_settings WHERE user_id" in sql:
            return FakeResult([(
                "",
                self.slot_text,
                "all",
                "",
                "UTC",
            )])
        if "SELECT count(*) FROM scheduled_posts" in sql:
            return FakeResult([(0,)])
        if "SELECT scheduled_at" in sql and "UNION" in sql:
            return FakeResult([])
        if "INSERT INTO autopost_runs" in sql:
            return FakeResult([(51,)])
        if "INSERT INTO ai_credit_events" in sql:
            return FakeResult([("autocontent:51",)])
        if "INSERT INTO scheduled_posts" in sql:
            return FakeResult([(901,)])
        return FakeResult([])


class EmptyMemoryRepo:
    def __init__(self, _session):
        pass

    async def load(self, *_args, **_kwargs):
        return []


def planner_slot_text():
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    return f"{future.hour:02d}:{future.minute:02d}"


def configure_planner(monkeypatch, session):
    async def get_voice(*_args, **_kwargs):
        return {"tone": "direct"}

    async def build_brain(*_args, **_kwargs):
        return None

    monkeypatch.setattr(autocontent, "Session", lambda: session)
    monkeypatch.setattr(autocontent.scenarist, "get_voice", get_voice)
    monkeypatch.setattr(
        autocontent.social_brain,
        "build_account_context",
        build_brain,
    )
    monkeypatch.setattr(
        autocontent,
        "ContentMemoryRepo",
        EmptyMemoryRepo,
    )


def test_insufficient_credits_records_skipped_run(monkeypatch):
    session = PlannerSession(planner_slot_text())
    configure_planner(monkeypatch, session)

    async def spend(*_args, **_kwargs):
        raise credits.NotEnoughCredits()

    monkeypatch.setattr(autocontent.credits, "spend", spend)
    result = asyncio.run(autocontent._plan_for_user(
        7,
        1,
        "creator tools",
        ["automation"],
        11,
        account_expires_at=NOW + timedelta(days=30),
        max_generations=1,
    ))
    assert result == 0
    finish = next(
        params
        for sql, params in session.calls
        if "SET status = :status" in sql
    )
    assert finish["status"] == "skipped"
    assert finish["error_code"] == "INSUFFICIENT_CREDITS"


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (RuntimeError("provider failed"), "GENERATION_FAILED"),
        (
            scenarist.ContentQualityError("quality failed"),
            "QUALITY_FAILED",
        ),
    ],
)
def test_generation_and_quality_failures_are_recorded(
    monkeypatch,
    error,
    expected_code,
):
    session = PlannerSession(planner_slot_text())
    configure_planner(monkeypatch, session)

    async def spend(*_args, **_kwargs):
        return 10

    async def topup(*_args, **_kwargs):
        return 11

    async def generate(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(autocontent.credits, "spend", spend)
    monkeypatch.setattr(autocontent.credits, "topup", topup)
    monkeypatch.setattr(
        autocontent.scenarist,
        "generate_post",
        generate,
    )
    result = asyncio.run(autocontent._plan_for_user(
        7,
        1,
        "creator tools",
        ["automation"],
        11,
        account_expires_at=NOW + timedelta(days=30),
    ))
    assert result == 1
    finish = next(
        params
        for sql, params in session.calls
        if "SET status = :status" in sql
    )
    assert finish["status"] == "failed"
    assert finish["error_code"] == expected_code


def test_successful_generation_attaches_post_to_run(monkeypatch):
    session = PlannerSession(planner_slot_text())
    configure_planner(monkeypatch, session)
    spend_calls = []

    async def spend(*_args, **_kwargs):
        spend_calls.append((_args, _kwargs))
        return 10

    async def forbidden_topup(*_args, **_kwargs):
        raise AssertionError("successful generation must not refund")

    async def generate(*_args, **_kwargs):
        return {
            "hooks": [{"type": "insight", "text": "Opening"}],
            "selected_hook": {"text": "Opening"},
            "body": "A concrete body for the scheduled post.",
            "metadata": {"source": "autocontent"},
        }

    monkeypatch.setattr(autocontent.credits, "spend", spend)
    monkeypatch.setattr(autocontent.credits, "topup", forbidden_topup)
    monkeypatch.setattr(
        autocontent.scenarist,
        "generate_post",
        generate,
    )
    result = asyncio.run(autocontent._plan_for_user(
        7,
        1,
        "creator tools",
        ["automation"],
        11,
        account_expires_at=NOW + timedelta(days=30),
        max_generations=1,
    ))
    assert result == 1
    assert len(spend_calls) == 1
    insert = next(
        params
        for sql, params in session.calls
        if "INSERT INTO scheduled_posts" in sql
    )
    assert insert["run_id"] == 51
    assert '"source":"autocontent"' in insert["metadata"]
    attach = next(
        params
        for sql, params in session.calls
        if "SET scheduled_post_id = :post_id" in sql
    )
    assert attach == {"run_id": 51, "post_id": 901}


def test_migration_is_additive_and_has_rollback():
    migration = (
        ROOT / "migrations" / "008_autopilot_status.sql"
    ).read_text(encoding="utf-8").casefold()
    rollback = (
        ROOT / "migrations" / "rollback" / "008_autopilot_status.sql"
    ).read_text(encoding="utf-8").casefold()
    assert "create table if not exists autopost_runs" in migration
    assert "add column if not exists timezone" in migration
    assert "add column if not exists publish_started_at" in migration
    assert "unique (\n    threads_account_id,\n    scheduled_at" in migration
    assert "drop table if exists autopost_runs" in rollback
