import asyncio
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from app.core import autopost_status, credits
from app.core.autopost_status import (
    AutopostStatusService,
    QUEUE_CLEARED_MESSAGE,
    available_day_capacity,
    render_clear_result,
    render_queue_summary,
    select_planning_slots,
)
from app.schemas.autopost import (
    AutopostAccount,
    AutopostSettings,
    AutopostStatus,
    AutopostQueueClearResult,
    AutopostQueueDay,
    AutopostQueueItem,
    AutopostQueueSummary,
)
from app.worker import autocontent


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 3, 4, 0, tzinfo=timezone.utc)
FIVE_SLOTS = (
    time(9),
    time(11),
    time(13),
    time(15),
    time(17),
)


def _counts(values, timezone_name="Europe/Moscow"):
    tz = autopost_status.resolve_timezone(timezone_name)
    return Counter(value.astimezone(tz).date() for value in values)


def test_five_posts_are_planned_for_each_full_active_day():
    planned = select_planning_slots(
        now=NOW,
        slots=FIVE_SLOTS,
        days="all",
        timezone_name="Europe/Moscow",
        posts_per_day=5,
        active_days=2,
    )
    assert list(_counts(planned).values()) == [5, 5]


def test_today_uses_only_future_slots_and_tomorrow_is_full():
    now = datetime(2026, 8, 3, 9, 30, tzinfo=timezone.utc)
    planned = select_planning_slots(
        now=now,
        slots=FIVE_SLOTS,
        days="all",
        timezone_name="Europe/Moscow",
        posts_per_day=5,
        active_days=2,
    )
    counts = list(_counts(planned).values())
    assert counts == [3, 5]
    assert all(value > now + timedelta(minutes=9) for value in planned)


def test_repeated_planning_does_not_duplicate_reserved_slots():
    first = select_planning_slots(
        now=NOW,
        slots=FIVE_SLOTS,
        days="all",
        timezone_name="Europe/Moscow",
        posts_per_day=5,
        active_days=4,
    )
    second = select_planning_slots(
        now=NOW,
        slots=FIVE_SLOTS,
        days="all",
        timezone_name="Europe/Moscow",
        posts_per_day=5,
        occupied=first,
        active_days=4,
    )
    assert len(first) == 20
    assert second == ()


def test_timezone_change_recomputes_the_same_local_slot():
    moscow = select_planning_slots(
        now=NOW,
        slots=(time(10),),
        days="all",
        timezone_name="Europe/Moscow",
        posts_per_day=1,
        active_days=1,
    )
    berlin = select_planning_slots(
        now=NOW,
        slots=(time(10),),
        days="all",
        timezone_name="Europe/Berlin",
        posts_per_day=1,
        active_days=1,
    )
    assert moscow[0].hour == 7
    assert berlin[0].hour == 8


def test_inactive_weekend_is_skipped():
    friday = datetime(2026, 8, 7, 4, 0, tzinfo=timezone.utc)
    planned = select_planning_slots(
        now=friday,
        slots=(time(9),),
        days="weekdays",
        timezone_name="Europe/Moscow",
        posts_per_day=1,
        active_days=2,
    )
    assert [value.astimezone(
        autopost_status.resolve_timezone("Europe/Moscow")
    ).date().isoformat() for value in planned] == [
        "2026-08-07",
        "2026-08-10",
    ]


def test_posts_per_day_change_from_three_to_five_changes_capacity():
    three = select_planning_slots(
        now=NOW,
        slots=FIVE_SLOTS,
        days="all",
        timezone_name="Europe/Moscow",
        posts_per_day=3,
        active_days=1,
    )
    five = select_planning_slots(
        now=NOW,
        slots=FIVE_SLOTS,
        days="all",
        timezone_name="Europe/Moscow",
        posts_per_day=5,
        active_days=1,
    )
    assert len(three) == 3
    assert len(five) == 5


def test_slot_change_is_reflected_without_past_slots():
    planned = select_planning_slots(
        now=NOW,
        slots=(time(10), time(16)),
        days="all",
        timezone_name="Europe/Moscow",
        posts_per_day=2,
        active_days=1,
    )
    assert [value.hour for value in planned] == [7, 13]


def test_empty_slots_and_elapsed_today_capacity():
    assert select_planning_slots(
        now=NOW,
        slots=(),
        days="all",
        timezone_name="Europe/Moscow",
        posts_per_day=5,
    ) == ()
    capacity = available_day_capacity(
        now=datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc),
        day=NOW.date(),
        slots=FIVE_SLOTS,
        days="all",
        timezone_name="Europe/Moscow",
        posts_per_day=5,
    )
    assert capacity == 1


def test_queue_summary_uses_remaining_today_capacity():
    summary = AutopostQueueSummary(
        account=_status().account,
        settings=_status().settings,
        posts=(
            AutopostQueueItem(
                id=1,
                text="one",
                run_at=NOW + timedelta(hours=1),
            ),
            AutopostQueueItem(
                id=2,
                text="two",
                run_at=NOW + timedelta(hours=2),
            ),
        ),
        days=(
            AutopostQueueDay(day=NOW.date(), queued=2, capacity=2),
            AutopostQueueDay(
                day=NOW.date() + timedelta(days=1),
                queued=5,
                capacity=5,
            ),
            AutopostQueueDay(
                day=NOW.date() + timedelta(days=2),
                queued=5,
                capacity=5,
            ),
        ),
    )
    rendered = render_queue_summary(summary)
    assert "Аккаунт: @creator" in rendered
    assert "Сегодня: 2 из 2 доступных слотов" in rendered
    assert "Завтра: 5 из 5 доступных слотов" in rendered


def test_clear_result_warns_when_planner_remains_enabled():
    rendered = render_clear_result(AutopostQueueClearResult(
        deleted_posts=4,
        autoposting_disabled=False,
    ))
    assert "Удалено будущих постов: 4" in rendered
    assert "Возвращено кредитов: 0" in rendered
    assert "planner снова начнёт создавать новые посты" in rendered


class FakeResult:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class MutationSession:
    def __init__(self, *, queue_rows=(), clear_ids=()):
        self.queue_rows = list(queue_rows)
        self.clear_ids = list(clear_ids)
        self.calls = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        values = dict(params or {})
        self.calls.append((sql, values))
        normalized = " ".join(sql.split())
        if "SELECT post.id, post.text, post.run_at" in normalized:
            return FakeResult(self.queue_rows)
        if "SELECT post.id FROM scheduled_posts" in normalized:
            return FakeResult([(value,) for value in self.clear_ids])
        if "UPDATE scheduled_posts SET run_at" in normalized:
            return FakeResult([(values["post_id"],)])
        if "UPDATE autopost_runs SET scheduled_at" in normalized:
            return FakeResult([(values["post_id"] + 1000,)])
        if "DELETE FROM scheduled_posts" in normalized:
            return FakeResult([(value,) for value in self.clear_ids])
        return FakeResult()


def _status(posts_per_day=5, slots=FIVE_SLOTS):
    return AutopostStatus(
        account=AutopostAccount(
            id=11,
            username="creator",
            expires_at=NOW + timedelta(days=90),
        ),
        settings=AutopostSettings(
            enabled=True,
            posts_per_day=posts_per_day,
            slots=slots,
            days="all",
            timezone="Europe/Moscow",
        ),
    )


class MutationService(AutopostStatusService):
    def __init__(self, session, status=None):
        super().__init__(session)
        self.status = status or _status()
        self.locked = []

    async def get_status(self, user_id, account_id, *, now=None):
        return self.status

    async def lock_queue(self, user_id, account_id):
        self.locked.append((user_id, account_id))

    async def occupied_slots(self, *args, **kwargs):
        return []

    async def blocked_manual_slots(self, *args, **kwargs):
        return []


def _forbid_credit_call(*_args, **_kwargs):
    raise AssertionError("queue mutations must not touch credits")


def test_rebuild_preserves_post_order_and_does_not_charge(monkeypatch):
    rows = [
        {"id": 101, "text": "first", "run_at": NOW + timedelta(days=5)},
        {"id": 102, "text": "second", "run_at": NOW + timedelta(days=6)},
        {"id": 103, "text": "third", "run_at": NOW + timedelta(days=7)},
    ]
    session = MutationSession(queue_rows=rows)
    service = MutationService(session)
    monkeypatch.setattr(credits, "spend", _forbid_credit_call)
    monkeypatch.setattr(credits, "topup", _forbid_credit_call)
    result = asyncio.run(service.rebuild_queue(7, 11, now=NOW))
    updates = [
        params
        for sql, params in session.calls
        if "UPDATE scheduled_posts SET run_at" in " ".join(sql.split())
    ]
    assert [params["post_id"] for params in updates] == [101, 102, 103]
    assert [params["scheduled_at"] for params in updates] == sorted(
        params["scheduled_at"] for params in updates
    )
    assert result.moved_posts == 3
    assert service.locked == [(7, 11)]


def test_rebuild_locks_pending_only_and_ignores_publishing():
    session = MutationSession(queue_rows=[])
    asyncio.run(MutationService(session).rebuild_queue(7, 11, now=NOW))
    selection = session.calls[0][0]
    assert "post.status = 'pending'" in selection
    assert "FOR UPDATE OF post SKIP LOCKED" in selection
    assert "publishing" not in selection


def test_empty_queue_rebuild_is_a_noop():
    result = asyncio.run(
        MutationService(MutationSession()).rebuild_queue(7, 11, now=NOW)
    )
    assert result.moved_posts == 0
    assert result.first_post_at is None


def test_clear_targets_only_future_account_auto_posts_and_keeps_history(
    monkeypatch,
):
    session = MutationSession(clear_ids=[101, 102])
    service = MutationService(session)
    monkeypatch.setattr(credits, "spend", _forbid_credit_call)
    monkeypatch.setattr(credits, "topup", _forbid_credit_call)
    result = asyncio.run(service.clear_queue(
        7,
        11,
        disable_autoposting=False,
        now=NOW,
    ))
    sql = "\n".join(statement for statement, _ in session.calls)
    selection = session.calls[0][0]
    assert "post.threads_account_id = :account_id" in selection
    assert "post.status = 'pending'" in selection
    assert "post.run_at > :now" in selection
    assert "content_metadata ->> 'source' = 'autocontent'" in selection
    assert "FOR UPDATE OF post SKIP LOCKED" in selection
    assert "DELETE FROM autopost_runs" not in sql
    assert result.deleted_posts == 2
    assert result.autoposting_disabled is False


def test_clear_can_disable_or_keep_autoposting_enabled():
    keep_session = MutationSession(clear_ids=[])
    keep = asyncio.run(MutationService(keep_session).clear_queue(
        7,
        11,
        disable_autoposting=False,
        now=NOW,
    ))
    disable_session = MutationSession(clear_ids=[])
    disable = asyncio.run(MutationService(disable_session).clear_queue(
        7,
        11,
        disable_autoposting=True,
        now=NOW,
    ))
    assert keep.autoposting_disabled is False
    assert not any(
        "UPDATE autocontent_settings" in sql
        for sql, _ in keep_session.calls
    )
    assert disable.autoposting_disabled is True
    assert any(
        "UPDATE autocontent_settings" in sql
        for sql, _ in disable_session.calls
    )


def test_planner_and_rebuild_share_account_lock_protocol():
    class LockSession(MutationSession):
        async def execute(self, statement, params=None):
            sql = str(statement)
            values = dict(params or {})
            self.calls.append((sql, values))
            if "INSERT INTO autopost_runs" in sql:
                return FakeResult([(51,)])
            return FakeResult()

    session = LockSession()
    reservation = asyncio.run(AutopostStatusService(
        session
    ).reserve_next_run(
        7,
        11,
        settings=_status().settings,
        now=NOW,
    ))
    assert reservation is not None
    assert "pg_advisory_xact_lock" in session.calls[0][0]
    assert session.calls[0][1]["lock_scope"] == "autopost_queue:7:11"


def test_publisher_clear_race_uses_row_lock_and_pending_guard():
    session = MutationSession(clear_ids=[])
    asyncio.run(MutationService(session).clear_queue(
        7,
        11,
        disable_autoposting=False,
        now=NOW,
    ))
    selection = session.calls[0][0]
    assert "FOR UPDATE OF post SKIP LOCKED" in selection
    assert "post.status = 'pending'" in selection


def test_clear_after_successful_generation_does_not_refund():
    assert autocontent._should_refund_unattached_generation(
        ("skipped", QUEUE_CLEARED_MESSAGE)
    ) is False
    assert autocontent._should_refund_unattached_generation(
        ("skipped", "Пропущено: автопостинг выключен")
    ) is True


def test_terminal_runs_do_not_occupy_slots_and_migration_is_reversible():
    occupied_sql = str(autopost_status._OCCUPIED_SQL)
    assert "run.status IN ('pending', 'success')" in occupied_sql
    assert "failed" not in occupied_sql
    assert "skipped" not in occupied_sql
    migration = (
        ROOT / "migrations" / "009_autopost_active_slot_unique.sql"
    ).read_text(encoding="utf-8")
    rollback = (
        ROOT / "migrations" / "rollback"
        / "009_autopost_active_slot_unique.sql"
    ).read_text(encoding="utf-8")
    assert "where status = 'pending'" in migration.casefold()
    assert "autopost_runs_slot_unique" in rollback
