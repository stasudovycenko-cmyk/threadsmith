import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.bot.handlers.analytics import (
    EMPTY,
    analytics_kb,
    render_dimension,
    render_overview,
)
from app.core.analytics.providers import threads as threads_provider
from app.core.analytics.repository import AnalyticsRepository
from app.core.analytics.scoring import (
    BrainScoreService,
    ViralityScoreService,
    with_engagement_rate,
)
from app.core.analytics.service import AnalyticsCollector, snapshot_bucket
from app.core.brain_writer import BrainWriter
from app.schemas.analytics import (
    AnalyticsAccountSummary,
    AnalyticsBaseline,
    AnalyticsFeatureBenchmarks,
    AnalyticsMetrics,
    ProviderAnalyticsPost,
    PublishedAnalyticsPost,
)
from app.worker import main as worker_main

NOW = datetime(2026, 8, 3, 12, 47, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def make_post(account_id=101, post_id="post-1", *, offset=0):
    return PublishedAnalyticsPost(
        scheduled_post_id=1000 + offset,
        user_id=7,
        threads_account_id=account_id,
        threads_post_id=post_id,
        published_at=NOW - timedelta(hours=2),
        timezone="Europe/Moscow",
        text="body",
        hook_type="question",
        cta_type="comment",
        topic="analytics",
    )


def make_summary(account_id=101):
    return AnalyticsAccountSummary(
        user_id=7,
        threads_account_id=account_id,
        posts_total=1,
        views_total=100,
        likes_total=10,
        comments_total=1,
        shares_total=1,
        avg_er=0.12,
        avg_views=100,
        best_post_id=f"post-{account_id}",
        worst_post_id=f"post-{account_id}",
        best_hour=15,
        best_weekday=0,
        best_topic="analytics",
        best_hook="question",
        best_cta="comment",
        brain_score=72,
        updated_at=NOW,
    )


class FakeProvider:
    name = "manual"

    def __init__(self, metrics_by_post, remote_posts=()):
        self.metrics_by_post = metrics_by_post
        self.remote_posts = list(remote_posts)
        self.calls = []

    async def list_recent_posts(self, *, since, limit):
        return [
            post for post in self.remote_posts
            if post.published_at >= since
        ][:limit]

    async def get_post_metrics(self, post_id):
        self.calls.append(post_id)
        value = self.metrics_by_post[post_id]
        if isinstance(value, Exception):
            raise value
        return value


class MemoryFeedback:
    def __init__(self):
        self.posts = []
        self.summaries = []

    async def record_post(self, snapshot_id, item):
        self.posts.append((snapshot_id, item))

    async def sync_account(self, summary):
        self.summaries.append(summary)


class MemoryRepository:
    def __init__(self, posts_by_account):
        self.posts_by_account = posts_by_account
        self.snapshots = {}
        self.post_summaries = {}
        self.legacy = {}
        self.rebuilt = []

    async def account_timezone(self, user_id, account_id):
        return "Europe/Moscow"

    async def load_published_posts(self, user_id, account_id):
        return list(self.posts_by_account.get((user_id, account_id), []))

    async def account_baseline(self, user_id, account_id):
        return AnalyticsBaseline(avg_views=80, avg_engagement_rate=0.08)

    async def previous_snapshot(self, account_id, post_id, *, before_bucket):
        return None

    async def feature_benchmarks(self, post):
        return AnalyticsFeatureBenchmarks(
            topic_score=60,
            hook_score=70,
            cta_score=55,
        )

    async def save_snapshot(self, item):
        key = (
            item.post.threads_account_id,
            item.provider,
            item.post.threads_post_id,
            item.bucket_at,
        )
        snapshot_id = self.snapshots.get(key, (len(self.snapshots) + 1,))[0]
        self.snapshots[key] = (snapshot_id, item)
        return snapshot_id

    async def upsert_post_summary(self, item, *, publish_hour, weekday):
        self.post_summaries[
            (item.post.threads_account_id, item.post.threads_post_id)
        ] = (item, publish_hour, weekday)

    async def save_legacy_daily_snapshot(self, post_id, snapshot_at, metrics):
        self.legacy[(post_id, snapshot_at.date())] = metrics

    async def rebuild_account(self, user_id, account_id):
        self.rebuilt.append((user_id, account_id))
        if not any(key[0] == account_id for key in self.post_summaries):
            return None
        return make_summary(account_id)


def test_snapshot_creation_aggregate_update_and_brain_feedback():
    post = make_post()
    repository = MemoryRepository({(7, 101): [post]})
    feedback = MemoryFeedback()
    provider = FakeProvider({
        "post-1": AnalyticsMetrics(
            views=100, likes=10, replies=1, reposts=1, quotes=0
        )
    })
    result = asyncio.run(AnalyticsCollector(
        repository,
        feedback=feedback,
        clock=lambda: NOW,
    ).collect_account(7, 101, provider))

    assert result.snapshots_written == 1
    assert result.failures == 0
    assert len(repository.snapshots) == 1
    item, hour, weekday = repository.post_summaries[(101, "post-1")]
    assert item.metrics.engagement_rate == 0.12
    assert item.snapshot_at == NOW
    assert item.bucket_at == NOW.replace(minute=30, second=0, microsecond=0)
    assert (hour, weekday) == (13, 0)
    assert repository.rebuilt == [(7, 101)]
    assert feedback.posts[0][1].scores.brain_score > 0
    assert feedback.summaries[0].threads_account_id == 101


def test_collector_is_idempotent_within_same_snapshot_bucket():
    repository = MemoryRepository({(7, 101): [make_post()]})
    provider = FakeProvider({
        "post-1": AnalyticsMetrics(
            views=10, likes=1, replies=0, reposts=0, quotes=0
        )
    })
    collector = AnalyticsCollector(repository, clock=lambda: NOW)
    asyncio.run(collector.collect_account(7, 101, provider))
    asyncio.run(collector.collect_account(7, 101, provider))
    assert len(repository.snapshots) == 1
    assert snapshot_bucket(NOW).minute == 30


def test_multiple_accounts_are_isolated():
    repository = MemoryRepository({
        (7, 101): [make_post(101, "a-post")],
        (7, 202): [make_post(202, "b-post", offset=1)],
    })
    first = FakeProvider({
        "a-post": AnalyticsMetrics(
            views=100, likes=1, replies=0, reposts=0, quotes=0
        )
    })
    second = FakeProvider({
        "b-post": AnalyticsMetrics(
            views=900, likes=9, replies=1, reposts=0, quotes=0
        )
    })
    collector = AnalyticsCollector(repository, clock=lambda: NOW)
    asyncio.run(collector.collect_account(7, 101, first))
    asyncio.run(collector.collect_account(7, 202, second))
    keys = set(repository.post_summaries)
    assert keys == {(101, "a-post"), (202, "b-post")}
    assert repository.post_summaries[(101, "a-post")][0].metrics.views == 100
    assert repository.post_summaries[(202, "b-post")][0].metrics.views == 900


def test_provider_discovered_post_does_not_require_scheduled_post():
    repository = MemoryRepository({})
    remote = ProviderAnalyticsPost(
        post_id="external-post",
        published_at=NOW - timedelta(hours=1),
        text="published outside ThreadFlow",
    )
    provider = FakeProvider(
        {"external-post": AnalyticsMetrics(
            views=30, likes=3, replies=0, reposts=0, quotes=0
        )},
        [remote],
    )
    result = asyncio.run(AnalyticsCollector(
        repository, clock=lambda: NOW
    ).collect_account(7, 101, provider))
    item = repository.post_summaries[(101, "external-post")][0]
    assert result.snapshots_written == 1
    assert item.post.scheduled_post_id is None
    assert repository.legacy == {}


def test_one_provider_failure_does_not_block_other_posts():
    posts = [make_post(post_id="bad"), make_post(post_id="good", offset=1)]
    repository = MemoryRepository({(7, 101): posts})
    provider = FakeProvider({
        "bad": RuntimeError("unavailable"),
        "good": AnalyticsMetrics(
            views=50, likes=5, replies=0, reposts=0, quotes=0
        ),
    })
    result = asyncio.run(AnalyticsCollector(
        repository, clock=lambda: NOW
    ).collect_account(7, 101, provider))
    assert result.failures == 1
    assert result.snapshots_written == 1
    assert (101, "good") in repository.post_summaries


def test_missing_required_metric_does_not_become_zero_or_er():
    metrics = with_engagement_rate(AnalyticsMetrics(
        views=100, likes=10, replies=1, reposts=1
    ))
    assert metrics.quotes is None
    assert metrics.engagement_rate is None


def test_virality_and_brain_scores_are_bounded_and_responsive():
    low = AnalyticsMetrics(
        views=100, likes=1, replies=0, reposts=0, quotes=0,
        engagement_rate=0.01,
    )
    high = AnalyticsMetrics(
        views=1000, likes=100, replies=30, reposts=20, quotes=10,
        engagement_rate=0.16,
    )
    scorer = ViralityScoreService()
    low_score = scorer.calculate(
        low, None, published_at=NOW - timedelta(hours=10), snapshot_at=NOW
    )
    high_score = scorer.calculate(
        high, None, published_at=NOW - timedelta(hours=1), snapshot_at=NOW
    )
    assert 0 <= low_score < high_score <= 100
    brain = BrainScoreService().calculate(
        high,
        high_score,
        AnalyticsFeatureBenchmarks(
            topic_score=90, hook_score=80, cta_score=70
        ),
    )
    assert 0 <= brain <= 100


def test_threads_provider_preserves_optional_missing_metrics(monkeypatch):
    async def fake_insights(_token, _post_id):
        return {"views": "12", "likes": 1, "shares": None}

    monkeypatch.setattr(threads_provider, "get_insights", fake_insights)
    metrics = asyncio.run(
        threads_provider.ThreadsAnalyticsProvider("secret").get_post_metrics("p")
    )
    assert metrics.views == 12
    assert metrics.likes == 1
    assert metrics.shares is None


def test_threads_provider_lists_recent_owned_posts(monkeypatch):
    async def fake_posts(_token, *, limit):
        assert limit == 50
        return [{
            "id": "owned-1",
            "text": "body",
            "timestamp": "2026-08-03T11:00:00+0000",
        }]

    monkeypatch.setattr(threads_provider, "get_own_threads", fake_posts)
    posts = asyncio.run(
        threads_provider.ThreadsAnalyticsProvider("secret").list_recent_posts(
            since=NOW - timedelta(days=1),
            limit=50,
        )
    )
    assert posts[0].post_id == "owned-1"
    assert posts[0].published_at.tzinfo is not None


def test_social_brain_performance_event_is_idempotently_identified():
    class Repo:
        async def get_or_create(self, user_id, account_id):
            assert (user_id, account_id) == (7, 101)
            return SimpleNamespace(id=11)

    session = FakeSession([[(51,)]])
    event_id = asyncio.run(BrainWriter(session, Repo()).record_post_performance_updated(
        7,
        101,
        analytics_snapshot_id=44,
        threads_post_id="post-1",
        snapshot_at=NOW,
        scores={
            "performance_score": 70.0,
            "virality_score": 60.0,
            "brain_score": 65.0,
        },
        available_metrics=["views", "likes"],
    ))
    assert event_id == 51
    _, params = session.calls[0]
    assert params["event_type"] == "POST_PERFORMANCE_UPDATED"
    assert params["event_key"] == (
        "post_performance_updated:analytics_snapshot:44"
    )
    assert "token" not in params["payload"].casefold()


class FakeResult:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class FakeSession:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        return FakeResult(next(self.results, ()))


def test_rebuild_creates_hook_topic_cta_and_publish_time_aggregates():
    summary = make_summary().model_dump()
    session = FakeSession([(), (), (), [summary]])
    result = asyncio.run(AnalyticsRepository(session).rebuild_account(7, 101))
    aggregate_sql, params = session.calls[2]
    assert result.posts_total == 1
    assert params == {"user_id": 7, "account_id": 101}
    for dimension in (
        "topic", "hook_type", "cta_type", "publish_hour", "weekday"
    ):
        assert f"'{dimension}'" in aggregate_sql
    account_sql = session.calls[3][0]
    assert "analytics_account_summary" in account_sql
    assert "metric_coverage" in account_sql


def test_telegram_rendering_and_empty_state():
    overview = render_overview("creator", make_summary().model_dump())
    assert "Аккаунт: @creator" in overview
    assert "Средний ER: 12%" in overview
    assert "Лучшее начало: question" in overview
    assert render_dimension("Темы", []) == EMPTY
    callbacks = {
        button.callback_data
        for row in analytics_kb().inline_keyboard
        for button in row
    }
    assert "an:growth" in callbacks
    assert any(value.startswith("an:top:") for value in callbacks)
    assert any(value.startswith("an:time:") for value in callbacks)
    assert any(value.startswith("an:brain:") for value in callbacks)


def test_scheduler_runs_analytics_every_thirty_minutes(monkeypatch):
    captured = []

    class FakeScheduler:
        def __init__(self, **_kwargs):
            pass

        def add_job(self, function, trigger, **kwargs):
            captured.append((function, trigger, kwargs))

    monkeypatch.setattr(worker_main, "AsyncIOScheduler", FakeScheduler)
    worker_main.build_scheduler()
    job = next(
        item for item in captured
        if item[0] is worker_main.analytics_collector
    )
    assert job[1] == "interval"
    assert job[2]["minutes"] == 30


def test_migration_and_rollback_are_additive():
    migration = (
        ROOT / "migrations/012_analytics_v2.sql"
    ).read_text(encoding="utf-8")
    rollback = (
        ROOT / "migrations/rollback/012_analytics_v2.sql"
    ).read_text(encoding="utf-8")
    for table in (
        "analytics_snapshots",
        "analytics_post_summary",
        "analytics_aggregates",
        "analytics_account_summary",
    ):
        assert f"create table {table}" in migration
        assert f"drop table if exists {table}" in rollback
    assert "scheduled_posts" not in rollback
    assert "insights_snapshots" not in rollback
    assert "foreign key (threads_account_id, user_id)" in migration
    assert "analytics_snapshots_scheduled_owner_fk" in migration
    assert "check (provider in ('threads', 'manual'))" in migration
    assert "numeric(6, 3)" in migration
    assert "numeric(10, 6)" in migration
    assert "numeric(18, 3)" in migration
    assert "analytics_post_summary_account_published_idx" in migration
