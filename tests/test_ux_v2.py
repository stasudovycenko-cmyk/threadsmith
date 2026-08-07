import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.bot.handlers.analytics import (
    PERIODS,
    analytics_kb,
    render_overview,
)
from app.bot.handlers.autopilot import render_clear_confirmation
from app.bot.handlers.cabinet import render_delete_confirmation
from app.bot.handlers.neuro import _strategy_label, neuro_kb
from app.bot.handlers.onboarding import render_onboarding_step
from app.bot.handlers.radar import radar_kb
from app.bot.ux import (
    ERROR_TEXTS,
    CallbackDeduplicator,
    dashboard_keyboard,
    render_dashboard,
    render_error,
)
from app.core.activity import ActivityFeedService
from app.core.analytics.repository import AnalyticsRepository
from app.core.brain_coach import build_recommendations
from app.core.dashboard import DashboardService
from app.core import ux as ux_module
from app.core.ux import UXService
from app.schemas.ux import (
    DashboardAnalytics,
    DashboardAutopilot,
    DashboardBalance,
    DashboardData,
    DashboardNeuro,
    DashboardRadar,
    OnboardingProgress,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def _labels(keyboard):
    return [button.text for row in keyboard.inline_keyboard for button in row]


def _dashboard(**changes):
    values = {
        "user_id": 7,
        "account_id": 101,
        "username": "creator",
        "interface_mode": "advanced",
        "autopilot": DashboardAutopilot(
            enabled=True,
            posts_today=4,
            daily_limit=5,
            queue_size=18,
            next_post_at=NOW,
            timezone="Europe/Moscow",
        ),
        "radar": DashboardRadar(
            ready_count=7,
            last_search_at=NOW,
            last_status="success",
        ),
        "neuro": DashboardNeuro(
            enabled=True,
            posted_today=3,
            pending_count=2,
        ),
        "analytics": DashboardAnalytics(
            posts_30d=12,
            views_30d=245_000,
            avg_er=0.048,
            brain_score=87,
        ),
        "balance": DashboardBalance(credits=824, plan="pro"),
    }
    values.update(changes)
    return DashboardData(**values)


def test_dashboard_with_full_data_is_mobile_readable():
    rendered = render_dashboard(_dashboard())
    assert "Аккаунт: @creator" in rendered
    assert "Сегодня: 4 из 5 постов" in rendered
    assert "Подходящих постов: 7" in rendered
    assert "245 000" in rendered
    assert "4,8%" in rendered
    assert "Brain Score: 87" in rendered
    assert "824 кредитов" in rendered
    assert len(rendered) < 4096
    assert max(map(len, rendered.splitlines())) < 100


def test_dashboard_partial_failure_does_not_hide_other_blocks():
    rendered = render_dashboard(_dashboard(
        radar=DashboardRadar(
            available=False,
            warning="Radar временно недоступен.",
        ),
        balance=DashboardBalance(
            available=False,
            warning="Баланс временно недоступен.",
        ),
    ))
    assert "Часть данных временно недоступна" in rendered
    assert "Radar временно недоступен" in rendered
    assert "Баланс временно недоступен" in rendered
    assert "Сегодня: 4 из 5 постов" in rendered
    assert "0 кредитов" not in rendered


def test_dashboard_without_statistics_has_explanatory_empty_state():
    rendered = render_dashboard(_dashboard(
        analytics=DashboardAnalytics(posts_30d=0),
    ))
    assert "Пока недостаточно статистики" in rendered
    assert "продолжайте публикации" in rendered


def test_simple_and_advanced_modes_expose_expected_sections():
    simple = _labels(dashboard_keyboard("simple"))
    advanced = _labels(dashboard_keyboard("advanced"))
    assert "🤖 Автопилот" in simple
    assert "📊 Аналитика" in simple
    assert all("Radar" not in label and "Neuro" not in label for label in simple)
    assert "🔎 Radar" in advanced
    assert "💬 Neuro" in advanced
    simple_text = render_dashboard(_dashboard(interface_mode="simple"))
    assert "🎯 Radar" not in simple_text
    assert "🧠 Neuro" not in simple_text


def test_new_and_existing_user_mode_migration_contract():
    source = (ROOT / "migrations/013_ux_v2_dashboard.sql").read_text(
        encoding="utf-8"
    )
    rollback = (
        ROOT / "migrations/rollback/013_ux_v2_dashboard.sql"
    ).read_text(encoding="utf-8")
    assert "set interface_mode = 'advanced'" in source
    assert "interface_mode set default 'simple'" in source
    assert "interface_mode in ('simple', 'advanced')" in source
    assert "drop table if exists ux_onboarding" in rollback
    assert "drop column if exists interface_mode" in rollback
    assert "scheduled_posts" not in rollback
    assert "threads_accounts" not in rollback


def test_onboarding_progress_resume_and_final_summary():
    progress = OnboardingProgress(
        user_id=7,
        threads_account_id=101,
        status="in_progress",
        current_step=5,
        data={"topic": "маркетинг"},
    )
    text, keyboard = render_onboarding_step(progress, "creator")
    assert "Шаг 5 из 9" in text
    assert "Стиль текста" in text
    assert "Продолжить позже" in _labels(keyboard)
    final = progress.model_copy(update={
        "current_step": 9,
        "data": {
            "topic": "маркетинг",
            "goal_label": "охваты",
            "daily_limit": 2,
            "schedule_label": "09:00 и 19:00",
            "autopilot": True,
            "radar": True,
            "neuro": "approve",
        },
    })
    summary, final_keyboard = render_onboarding_step(final, "creator")
    assert "Финальная проверка" in summary
    assert "Автопилот: включён" in summary
    assert "Neuro: сначала спрашивать" in summary
    assert "🚀 Запустить работу" in _labels(final_keyboard)


class _MappingResult:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class _CaptureSession:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        result = next(self.results, [])
        if isinstance(result, Exception):
            raise result
        return _MappingResult(result)

    @asynccontextmanager
    async def begin_nested(self):
        yield


def test_onboarding_updates_are_account_scoped_and_skippable():
    row = {
        "user_id": 7,
        "threads_account_id": 101,
        "status": "skipped",
        "current_step": 0,
        "data": {},
        "updated_at": NOW,
    }
    session = _CaptureSession([[row]])
    result = asyncio.run(UXService(session).update_onboarding(
        7, 101, step=0, status="skipped"
    ))
    assert result.status == "skipped"
    sql, params = session.calls[0]
    assert "account.user_id = progress.user_id" in sql
    assert params["user_id"] == 7
    assert params["account_id"] == 101


def test_onboarding_style_is_connected_to_account_brain_context(monkeypatch):
    captured = []

    class FakeBrainRepo:
        def __init__(self, _session):
            pass

        async def get_or_create(self, user_id, account_id):
            assert (user_id, account_id) == (7, 101)
            return SimpleNamespace(
                id=55,
                dna={"voice": {"tone": "Личный голос"}},
            )

        async def update_section(self, brain_id, section, value, **owner):
            captured.append((brain_id, section, value, owner))

    monkeypatch.setattr(ux_module, "BrainRepo", FakeBrainRepo)
    assert asyncio.run(UXService(object()).save_style(
        7, 101, "expert"
    )) is True
    assert captured == [(
        55,
        "dna",
        {"voice": {
            "tone": "Личный голос",
            "manual_style": "Экспертно и по делу",
        }},
        {"user_id": 7, "account_id": 101},
    )]


def test_radar_empty_temporary_and_permission_states_are_safe():
    source = (ROOT / "app/bot/handlers/radar.py").read_text(encoding="utf-8")
    assert "Пока подходящих постов не найдено" in source
    assert "широкие ключевые слова" in source
    assert "Threads временно недоступен" in render_error("threads_temporary")
    permission = render_error("permission_denied")
    assert "Недостаточно разрешений Threads" in permission
    assert "Переподключите аккаунт" in permission


def test_neuro_labels_confirmation_and_daily_timezone_contract():
    labels = _labels(neuro_kb(True, "approve"))
    assert "Режим: сначала спрашивать" in labels
    assert "🎭 Стратегии" in labels
    assert _strategy_label("clarifying_question") == "Уточняющий вопрос"
    source = (ROOT / "app/bot/handlers/neuro.py").read_text(encoding="utf-8")
    assert "Комментарии будут публиковаться без ручного подтверждения" in source
    assert "AT TIME ZONE coalesce" in source
    assert "comment.threads_account_id = :account_id" in source


def test_analytics_missing_metrics_and_period_switching():
    rendered = render_overview("creator", {
        "posts_total": 2,
        "views_total": None,
        "likes_total": None,
        "avg_er": None,
        "avg_views": None,
        "profile_visits_coverage": 0,
    }, days=7)
    assert "Период: 7 дней" in rendered
    assert "Просмотров: нет данных" in rendered
    assert "Средний ER: нет данных" in rendered
    assert "Threads пока не предоставляет данные" in rendered
    callbacks = [
        button.callback_data
        for row in analytics_kb(90).inline_keyboard
        for button in row
    ]
    assert "an:period:7" in callbacks
    assert "an:period:90" in callbacks
    assert PERIODS["all"] is None


def test_period_overview_uses_dimension_aggregates_and_account_scope():
    session = _CaptureSession([[{"posts_total": 0}]])
    asyncio.run(AnalyticsRepository(session).period_overview(
        7, 101, days=30
    ))
    sql, params = session.calls[0]
    assert "GROUP BY candidate.topic" in sql
    assert "avg(candidate.brain_score) DESC NULLS LAST" in sql
    assert "threads_account_id = :account_id" in sql
    assert params == {"user_id": 7, "account_id": 101, "days": 30}


def test_dangerous_action_warnings_are_explicit_and_account_scoped():
    summary = SimpleNamespace(
        account=SimpleNamespace(id=101, username="creator"),
        posts=[1, 2, 3],
    )
    clear_text = render_clear_confirmation(summary)
    assert "@creator" in clear_text
    assert "Будет удалено постов: 3" in clear_text
    assert "кредиты не возвращаются" in clear_text
    assert "Другие аккаунты не будут затронуты" in clear_text
    delete_text = render_delete_confirmation(summary.account)
    assert "@creator" in delete_text
    assert "Действие необратимо" in delete_text
    assert "тариф, кредиты пользователя и другие Threads-аккаунты" in delete_text


def test_activity_pagination_and_account_isolation():
    rows = [{
        "event_type": "post_published",
        "occurred_at": NOW,
        "payload": {"preview": "Готовый пост"},
    }]
    session = _CaptureSession([rows])
    events = asyncio.run(ActivityFeedService(session).list_events(
        7, 101, page=2, page_size=8
    ))
    assert events[0].title == "Пост опубликован"
    sql, params = session.calls[0]
    assert sql.count("threads_account_id = :account_id") >= 4
    assert params == {
        "user_id": 7,
        "account_id": 101,
        "row_limit": 8,
        "row_offset": 16,
    }


def test_brain_recommendations_require_enough_data():
    insufficient = build_recommendations(
        {"posts_total": 4}, [], [], None
    )
    assert [item.kind for item in insufficient] == ["insufficient_data"]
    enough = build_recommendations(
        {"posts_total": 8, "best_hour": 16},
        [{"dimension_key": "AI", "posts_count": 4}],
        [{"dimension_key": "Вопрос", "posts_count": 3}],
        {"recent_avg": 50, "previous_avg": 100},
    )
    kinds = {item.kind for item in enough}
    assert {"best_time", "strong_topic", "best_hook", "views_decline"} <= kinds
    assert all(item.sample_size >= 3 for item in enough)


def test_unified_errors_do_not_expose_internal_details():
    assert set(ERROR_TEXTS) == {
        "threads_temporary", "auth_expired", "permission_denied",
        "account_disconnected", "insufficient_credits", "no_data",
        "already_done", "publish_unknown", "internal_temporary",
    }
    for category in ERROR_TEXTS:
        rendered = render_error(category)
        assert "Что делать:" in rendered
        assert "access_token" not in rendered
        assert "Traceback" not in rendered
        assert "SELECT " not in rendered


def test_duplicate_callback_protection_expires_by_ttl():
    guard = CallbackDeduplicator(ttl_seconds=1.5)
    assert guard.claim(7, "action", now=10.0) is True
    assert guard.claim(7, "action", now=10.1) is False
    assert guard.claim(8, "action", now=10.1) is True
    assert guard.claim(7, "other", now=10.1) is True
    assert guard.claim(7, "action", now=11.6) is True


def test_dashboard_queries_do_not_mix_accounts():
    results = [[{
        "active": True,
        "posts_per_day": 2,
        "timezone": "Europe/Moscow",
        "posts_today": 1,
        "queue_size": 2,
        "next_post_at": NOW,
    }], [{
        "ready_count": 1,
        "last_search_at": NOW,
        "last_status": "success",
    }], [{
        "active": True,
        "posted_today": 1,
        "pending_count": 0,
    }], [{
        "posts_30d": 2,
        "views_30d": 100,
        "avg_er": 0.1,
        "brain_score": 70,
    }], [{"credits_balance": 50, "plan": "free"}]]
    session = _CaptureSession(results)
    account = SimpleNamespace(
        id=101,
        username="creator",
        connection_status="connected",
    )
    data = asyncio.run(DashboardService(session).load(
        7, account, mode="advanced"
    ))
    assert data.account_id == 101
    account_queries = session.calls[:4]
    assert all(params["account_id"] == 101 for _, params in account_queries)
    assert all("threads_account_id = :account_id" in sql for sql, _ in account_queries)


def test_dashboard_service_isolates_a_failed_block():
    session = _CaptureSession([
        [{
            "active": True,
            "posts_per_day": 2,
            "timezone": "Europe/Moscow",
            "posts_today": 1,
            "queue_size": 2,
            "next_post_at": NOW,
        }],
        RuntimeError("radar unavailable"),
        [{"active": True, "posted_today": 1, "pending_count": 0}],
        [{
            "posts_30d": 2,
            "views_30d": 100,
            "avg_er": 0.1,
            "brain_score": 70,
        }],
        [{"credits_balance": 50, "plan": "free"}],
    ])
    account = SimpleNamespace(
        id=101,
        username="creator",
        connection_status="connected",
    )
    data = asyncio.run(DashboardService(session).load(
        7, account, mode="advanced"
    ))
    assert data.autopilot.available is True
    assert data.radar.available is False
    assert data.neuro.available is True
    assert data.analytics.posts_30d == 2
    assert data.balance.credits == 50


def test_primary_ux_labels_use_russian_terminology():
    labels = (
        _labels(dashboard_keyboard("advanced"))
        + _labels(radar_kb())
        + _labels(neuro_kb(True, "approve"))
        + _labels(analytics_kb())
    )
    combined = "\n".join(labels)
    for forbidden in (
        "Daily cap", "Minimum score", "Minimum interval", "Keywords",
        "approve", "auto", "candidate", "provider", "clear queue",
        "rebuild queue", "permission denied", "status unknown",
    ):
        assert forbidden not in combined
