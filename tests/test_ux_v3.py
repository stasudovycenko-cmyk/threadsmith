import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.bot.handlers import settings as settings_module
from app.bot.handlers.settings import (
    TopicInput,
    _edit_list_keyboard,
    _list_screen,
    _style_keyboard,
)
from app.bot.handlers.analytics import (
    _screen_html as analytics_screen_html,
    analytics_kb,
    render_dimension,
)
from app.bot.handlers.activity import _activity_target
from app.bot.handlers.nav import clear_active_flow
from app.bot.ux import (
    UIScreenManager,
    dashboard_keyboard,
    escape_html,
    escape_truncated,
    format_local_time,
    render_dashboard,
    settings_keyboard,
)
from app.core import ux as ux_module
from app.bot.publication_notifications import (
    format_publication_time,
    publication_keyboard,
    render_publication_notification,
)
from app.core.publication_notifications import PublicationNotificationService
from app.core.ux import UXService
from app.worker import m3_jobs
from app.schemas.notifications import PublicationNotification
from app.schemas.ux import (
    DashboardAnalytics,
    DashboardAutopilot,
    DashboardBalance,
    DashboardData,
    DashboardIntelligence,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 7, 11, 35, tzinfo=timezone.utc)


def _labels(keyboard):
    return [button.text for row in keyboard.inline_keyboard for button in row]


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class _Session:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), dict(params or {})))
        return _Result(next(self.results, []))


def test_primary_navigation_and_account_settings_are_beta_readable():
    labels = _labels(dashboard_keyboard("advanced"))
    assert labels == [
        "🤖 Автопилот", "✍️ Создать пост", "📊 Аналитика",
        "🔎 Radar", "💬 Neuro", "👤 Аккаунты", "⚙️ Настройки",
        "❓ Помощь",
    ]
    settings = _labels(settings_keyboard("advanced", 101))
    assert "🎙 Стиль общения" in settings
    assert "🎯 Темы" in settings
    assert "🔑 Ключевые слова" in settings
    assert "🔔 Уведомления" in settings
    callbacks = [
        button.callback_data
        for row in settings_keyboard("advanced", 101).inline_keyboard
        for button in row
    ]
    assert "ac:sched:101" in callbacks
    assert "ac:timezone:101" in callbacks


def test_analytics_detail_back_returns_to_period_overview():
    callbacks = [
        button.callback_data
        for row in analytics_kb(7, detail=True).inline_keyboard
        for button in row
    ]
    assert "an:overview:7" in callbacks
    assert "home" in callbacks
    rows = [{
        "dimension_key": "<" * 1000,
        "posts_count": 1,
        "avg_views": 10,
        "avg_er": 0.1,
        "avg_brain_score": 50,
    } for _ in range(10)]
    rendered = analytics_screen_html(render_dimension("Результаты", rows))
    assert len(rendered) < 4096


def test_activity_callback_preserves_notification_account_scope():
    assert _activity_target("activity:101:2") == (101, 2)
    assert _activity_target("activity:2") == (None, 2)


def test_dashboard_is_html_escaped_and_does_not_invent_metrics():
    rendered = render_dashboard(DashboardData(
        user_id=7,
        account_id=101,
        username="creator<admin>",
        interface_mode="simple",
        autopilot=DashboardAutopilot(
            enabled=True,
            posts_today=1,
            daily_limit=2,
            queue_size=4,
            next_post_at=NOW,
            timezone="Europe/Moscow",
        ),
        analytics=DashboardAnalytics(
            posts_7d=2,
            views_7d=None,
            avg_er_7d=None,
        ),
        balance=DashboardBalance(credits=10, plan="free"),
        intelligence=DashboardIntelligence(
            available=True,
            status="healthy",
            health_score=82,
            human_message="Очередь заполнена.",
        ),
    ))
    assert "creator&lt;admin&gt;" in rendered
    assert "creator<admin>" not in rendered
    assert "Просмотров: <b>Нет данных</b>" in rendered
    assert "Вовлечённость: <b>Нет данных</b>" in rendered
    assert "Состояние аккаунта: <b>82/100</b>" in rendered


def test_html_helpers_escape_and_truncate_without_cutting_entities():
    assert escape_html("<b>&") == "&lt;b&gt;&amp;"
    rendered, shortened = escape_truncated("<&" * 3000, 120)
    assert shortened is True
    assert len(rendered) <= 120
    assert not rendered.endswith("&")
    assert format_local_time(
        NOW,
        "Europe/Moscow",
        now=NOW,
    ) == "Сегодня, 14:35"


class _Bot:
    def __init__(self, *, edit_error=None, delete_error=None):
        self.edit_error = edit_error
        self.delete_error = delete_error
        self.edited = []
        self.deleted = []

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)
        if self.edit_error:
            raise self.edit_error
        return SimpleNamespace(message_id=kwargs["message_id"])

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        if self.delete_error:
            raise self.delete_error


class _Target:
    def __init__(self, bot, *, message_id=10, is_bot=True):
        self.bot = bot
        self.chat = SimpleNamespace(id=77)
        self.message_id = message_id
        self.from_user = SimpleNamespace(is_bot=is_bot)
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs))
        return SimpleNamespace(message_id=20)


def test_clean_chat_prefers_edit_and_uses_html():
    bot = _Bot()
    target = _Target(bot)
    manager = UIScreenManager()
    asyncio.run(manager.show(target, "🏠 <b>Главная</b>"))
    assert bot.edited[0]["message_id"] == 10
    assert bot.edited[0]["parse_mode"].value == "HTML"
    assert target.sent == []


def test_clean_chat_edit_and_delete_failures_fall_back_without_touching_events():
    bot = _Bot(
        edit_error=RuntimeError("edit failed"),
        delete_error=RuntimeError("delete failed"),
    )
    target = _Target(bot, message_id=10)
    manager = UIScreenManager()
    asyncio.run(manager.show(target, "Настройки"))
    assert bot.deleted == [(77, 10)]
    assert target.sent[0][0] == "Настройки"
    assert all(message_id != 99 for _, message_id in bot.deleted)


def test_event_notification_is_not_replaced_by_later_navigation():
    bot = _Bot()
    manager = UIScreenManager()
    command = _Target(bot, message_id=1, is_bot=False)
    asyncio.run(manager.show(
        command, "Главная", prefer_edit=False
    ))
    event = _Target(bot, message_id=99, is_bot=True)
    asyncio.run(manager.show(event, "Настройки"))
    assert bot.edited[-1]["message_id"] == 20
    assert bot.edited[-1]["message_id"] != 99


def test_main_navigation_clears_fsm_and_only_transient_prompt():
    bot = _Bot()
    message = _Target(bot, message_id=99, is_bot=False)

    class _State:
        cleared = False

        async def get_data(self):
            return {
                "transient_chat_id": 77,
                "transient_message_id": 44,
            }

        async def clear(self):
            self.cleared = True

    state = _State()
    asyncio.run(clear_active_flow(message, state))
    assert state.cleared is True
    assert bot.deleted == [(77, 44)]
    assert (77, 99) not in bot.deleted


def test_style_and_list_controls_have_safe_actions():
    style = _labels(_style_keyboard())
    assert "✏️ Изменить стиль" in style
    assert "📝 Добавить примеры" in style
    assert "🗑 Очистить примеры" in style
    topics = _labels(_edit_list_keyboard("topics"))
    assert topics == [
        "➕ Добавить", "✏️ Изменить", "🗑 Очистить",
        "⬅️ Назад", "🏠 Главная",
    ]
    bounded = _list_screen(
        "🎯 <b>Темы</b>",
        "creator",
        ["<" * 1000 for _ in range(20)],
        "",
    )
    assert len(bounded) < 4096


def test_fsm_cancel_clears_state_and_returns_to_previous_screen(monkeypatch):
    shown = []

    async def _show(_message, telegram_id):
        shown.append(telegram_id)
        return True

    class _State:
        cleared = False

        async def get_state(self):
            return TopicInput.value.state

        async def get_data(self):
            return {}

        async def clear(self):
            self.cleared = True

    class _Message:
        from_user = SimpleNamespace(id=123)
        answers = []

        async def answer(self, value):
            self.answers.append(value)

    monkeypatch.setattr(settings_module, "_show_topics", _show)
    state = _State()
    message = _Message()
    asyncio.run(settings_module.cancel_settings_input(message, state))
    assert state.cleared is True
    assert shown == [123]
    assert message.answers == []


def test_destructive_settings_callback_rejects_stale_account(monkeypatch):
    async def _selected(_telegram_id):
        return 7, SimpleNamespace(id=202)

    class _Callback:
        data = "ux:topics:clear_confirm:101"
        from_user = SimpleNamespace(id=123)
        answers = []

        async def answer(self, value, **kwargs):
            self.answers.append((value, kwargs))

    monkeypatch.setattr(settings_module, "_selected", _selected)
    callback = _Callback()
    asyncio.run(settings_module.cb_topics_clear_confirm(callback))
    assert callback.answers == [
        ("Активный аккаунт изменился", {"show_alert": True})
    ]


def test_manual_style_and_examples_update_existing_account_brain(monkeypatch):
    updates = []

    class _BrainRepo:
        def __init__(self, _session):
            pass

        async def get_or_create(self, user_id, account_id):
            assert (user_id, account_id) == (7, 101)
            return SimpleNamespace(
                id=55,
                dna={"voice": {"tone": "коротко"}},
            )

        async def update_section(self, brain_id, section, value, **owner):
            updates.append((brain_id, section, value, owner))

    monkeypatch.setattr(ux_module, "BrainRepo", _BrainRepo)
    service = UXService(object())
    assert asyncio.run(service.save_manual_style(
        7, 101, "Разговорно, уверенно, короткими абзацами."
    )) is True
    assert updates[-1][2]["voice"]["manual_style"].startswith("Разговорно")
    assert updates[-1][3] == {"user_id": 7, "account_id": 101}
    assert asyncio.run(service.save_style_examples(
        7, 101, ["Первый пример", "Второй пример"]
    )) is True
    assert updates[-1][2]["recent_examples"] == [
        "Первый пример", "Второй пример"
    ]


def test_topics_keywords_and_notifications_write_only_owned_account():
    session = _Session([[(101,)], [(101,)], [(101,)]])
    service = UXService(session)
    assert asyncio.run(service.save_topics(
        7, 101, ["Threads", "Маркетинг"]
    )) is True
    assert asyncio.run(service.save_radar_keywords(
        7, 101, ["CPA", "продвижение"]
    )) is True
    assert asyncio.run(service.set_publish_notifications(
        7, 101, False
    )) is True
    for sql, params in session.calls:
        assert "threads_account_id = :account_id" in sql
        assert "account.user_id = setting.user_id" in sql
        assert params["user_id"] == 7
        assert params["account_id"] == 101
    assert "radar_settings" in session.calls[1][0]
    assert "autocontent_settings" in session.calls[0][0]
    assert session.calls[0][1]["topics"] == "Threads\nМаркетинг"


def _notification(**changes):
    values = {
        "scheduled_post_id": 900,
        "user_id": 7,
        "telegram_id": 123,
        "threads_account_id": 101,
        "username": "creator<one>",
        "text": "Точный <текст> & поста",
        "timezone": "Europe/Moscow",
        "outcome": "success",
        "published_at": NOW,
        "threads_post_id": "threads-1",
        "source": "Автопилот",
    }
    values.update(changes)
    return PublicationNotification(**values)


def test_success_notification_uses_exact_text_timezone_source_and_escaping():
    notification = _notification()
    rendered = render_publication_notification(notification, now=NOW)
    assert "✅ <b>Пост опубликован</b>" in rendered
    assert "@creator&lt;one&gt;" in rendered
    assert "Точный &lt;текст&gt; &amp; поста" in rendered
    assert "Время: <b>Сегодня, 14:35</b>" in rendered
    assert "Источник: <b>Автопилот</b>" in rendered
    assert publication_keyboard(notification) is None


def test_long_publication_notification_is_bounded_and_explicit():
    rendered = render_publication_notification(
        _notification(text="<&" * 5000), now=NOW
    )
    assert len(rendered) < 4096
    assert "Текст сокращён в уведомлении." in rendered


def test_permalink_is_optional_and_never_constructed_from_threads_id():
    without = _notification(threads_post_id="123", permalink=None)
    assert publication_keyboard(without) is None
    with_link = _notification(permalink="https://www.threads.net/@creator/post/abc")
    keyboard = publication_keyboard(with_link)
    assert keyboard.inline_keyboard[0][0].url.endswith("/abc")


def test_failed_and_unknown_notifications_preserve_safety_semantics():
    failed = render_publication_notification(_notification(
        outcome="failed",
        safe_error_message="Истекла авторизация Threads",
    ))
    assert "Не удалось опубликовать пост" in failed
    assert "Истекла авторизация Threads" in failed
    assert "Повтор не запланирован" in failed
    unknown_notification = _notification(outcome="unknown")
    unknown = render_publication_notification(unknown_notification)
    assert "Нужно проверить публикацию" in unknown
    assert "не будем автоматически отправлять его повторно" in unknown
    callbacks = [
        button.callback_data
        for row in publication_keyboard(unknown_notification).inline_keyboard
        for button in row
    ]
    assert "ac:history:101" in callbacks
    assert "activity:101:0" in callbacks

    long_text = "<&" * 5000
    for outcome in ("failed", "unknown"):
        rendered = render_publication_notification(_notification(
            outcome=outcome,
            text=long_text,
        ))
        assert len(rendered) < 4096
        assert "Текст сокращён в уведомлении." in rendered


def test_notification_claim_is_atomic_account_owned_and_respects_toggle():
    row = {
        "scheduled_post_id": 900,
        "user_id": 7,
        "telegram_id": 123,
        "threads_account_id": 101,
        "username": "creator",
        "text": "body",
        "timezone": "Europe/Moscow",
        "run_at": NOW,
        "threads_post_id": "threads-1",
        "content_metadata": '{"source":"autocontent"}',
        "finished_at": NOW,
        "safe_error_message": None,
    }
    session = _Session([[row], []])
    service = PublicationNotificationService(session)
    first = asyncio.run(service.claim(900, "success"))
    second = asyncio.run(service.claim(900, "success"))
    assert first.source == "Автопилот"
    assert second is None
    sql, params = session.calls[0]
    assert "publication_notification_claimed_at IS NULL" in sql
    assert "setting.publish_notifications_enabled" in sql
    assert "account.user_id = post.user_id" in sql
    assert params == {"post_id": 900, "outcome": "success"}


def test_manual_notification_source_and_disabled_success_are_explicit():
    row = {
        "scheduled_post_id": 901,
        "user_id": 7,
        "telegram_id": 123,
        "threads_account_id": 101,
        "username": "creator",
        "text": "manual body",
        "timezone": "Europe/Moscow",
        "run_at": NOW,
        "threads_post_id": "threads-2",
        "content_metadata": {"source": "manual"},
        "finished_at": NOW,
        "safe_error_message": None,
    }
    service = PublicationNotificationService(_Session([[row]]))
    claimed = asyncio.run(service.claim(901, "success"))
    assert claimed.source == "Вручную"

    disabled_session = _Session([[]])
    disabled = asyncio.run(
        PublicationNotificationService(disabled_session).claim(
            902, "success"
        )
    )
    assert disabled is None
    sql = disabled_session.calls[0][0]
    assert "setting.publish_notifications_enabled" in sql
    assert "(:outcome = 'failed' AND post.status = 'failed')" in sql


def test_recovery_query_only_selects_confirmed_unknown_publications():
    session = _Session([[(900,), (901,)]])
    ids = asyncio.run(
        PublicationNotificationService(session).recovered_unknown_post_ids()
    )
    assert ids == [900, 901]
    sql = session.calls[0][0]
    assert "UNKNOWN_ERROR: interrupted worker" in sql
    assert "Состояние публикации не подтверждено" in sql
    assert "publication_notification_claimed_at IS NULL" in sql


def test_fsm_source_rechecks_selected_account_and_keeps_user_message():
    source = (
        ROOT / "app/bot/handlers/settings.py"
    ).read_text(encoding="utf-8")
    assert "account.id != data.get(\"account_id\")" in source
    assert "Активный аккаунт изменился" in source
    assert "delete_message(chat_id, message_id)" in source
    assert "message.bot.delete_message(message.chat.id, message.message_id)" not in source


def test_telegram_failure_is_controlled_and_not_retried(monkeypatch):
    notification = _notification()

    class _ContextSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def commit(self):
            return None

    class _Service:
        def __init__(self, _session):
            pass

        async def claim(self, post_id, outcome):
            assert (post_id, outcome) == (900, "success")
            return notification

    class _FailingBot:
        def __init__(self):
            self.calls = 0

        async def send_message(self, *_args, **_kwargs):
            self.calls += 1
            raise RuntimeError("Telegram unavailable")

    bot = _FailingBot()
    monkeypatch.setattr(m3_jobs, "Session", _ContextSession)
    monkeypatch.setattr(
        m3_jobs, "PublicationNotificationService", _Service
    )
    monkeypatch.setattr(m3_jobs, "_bot", bot)
    result = asyncio.run(m3_jobs.send_publication_notification(
        900, "success"
    ))
    assert result is False
    assert bot.calls == 1


def test_migration_016_is_small_idempotent_and_reversible():
    forward = (ROOT / "migrations/016_ux_v3_beta_readiness.sql").read_text(
        encoding="utf-8"
    )
    rollback = (
        ROOT / "migrations/rollback/016_ux_v3_beta_readiness.sql"
    ).read_text(encoding="utf-8")
    assert "publish_notifications_enabled boolean" in forward
    assert "not null default true" in forward
    assert "publication_notification_claimed_at timestamptz" in forward
    assert "incidents that predate UX V3" in forward
    assert "UNKNOWN_ERROR: interrupted worker" in forward
    assert forward.count("add column if not exists") == 2
    assert rollback.count("drop column if exists") == 2
    assert "drop table" not in rollback
