import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from app.core.brain_repo import BrainNotFoundError, BrainRepo
from app.core.context_builder import ContextBuilder
from app.core.feedback_loop import (
    MANAGED_PATTERN_KINDS,
    FeedbackLoop,
    baseline_for,
    confidence_for,
    normalize_goal,
    normalize_post,
    rebuild_patterns,
)
from app.schemas.feedback import (
    AccountFeedbackResult,
    BrainPatternWrite,
    PostPerformance,
)
from app.schemas.social_brain import BrainPattern, BrainRecord
from app.worker import feedback_jobs

NOW = datetime(2026, 7, 29, 5, 0, tzinfo=timezone.utc)


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
    def __init__(self, rows=()):
        self.rows = rows
        self.calls = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        return FakeResult(self.rows)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class QueueSession(FakeSession):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        rows = self.responses.pop(0) if self.responses else []
        return FakeResult(rows)


class SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return None


def make_brain(
    brain_id=11,
    user_id=7,
    account_id=101,
    **overrides,
):
    values = {
        "id": brain_id,
        "user_id": user_id,
        "threads_account_id": account_id,
        "dna": {},
        "audience": {},
        "goals": {"primary": "reach"},
        "constraints": {},
        "performance": {"rolling_30d": {"published_posts": 8}},
        "version": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return BrainRecord.model_validate(values)


def make_post(
    index,
    *,
    views=100,
    engagement_rate=0.1,
    user_id=7,
    account_id=101,
    text_length=100,
    has_link=False,
):
    published = datetime(
        2026,
        7,
        1,
        9,
        tzinfo=timezone.utc,
    ) + timedelta(days=index)
    return PostPerformance(
        scheduled_post_id=1000 + index,
        user_id=user_id,
        threads_account_id=account_id,
        threads_post_id=f"threads-{account_id}-{index}",
        published_at=published,
        snapshot_date=published.date(),
        text="x" * text_length,
        text_length=text_length,
        has_link=has_link,
        views=views,
        engagement_rate=engagement_rate,
        available_metrics=("views", "engagement_rate"),
    )


class MemoryRepo:
    def __init__(self, brain=None, patterns=None):
        self.brain = brain or make_brain()
        self.patterns = list(patterns or [])
        self.pattern_writes = 0
        self.update_calls = 0
        self.last_managed_kinds = None

    async def get(self, brain_id, **scope):
        if brain_id != self.brain.id:
            return None
        if scope:
            if (
                scope.get("user_id") != self.brain.user_id
                or scope.get("account_id")
                != self.brain.threads_account_id
            ):
                return None
        return self.brain

    async def replace_patterns(
        self,
        brain_id,
        patterns,
        *,
        managed_kinds,
        **_scope,
    ):
        assert brain_id == self.brain.id
        self.pattern_writes += 1
        self.last_managed_kinds = tuple(managed_kinds)
        self.patterns = [
            BrainPattern(
                id=index + 1,
                brain_id=brain_id,
                updated_at=NOW,
                **pattern.model_dump(),
            )
            for index, pattern in enumerate(patterns)
        ]
        return len(patterns)

    async def update_section(
        self,
        brain_id,
        section,
        value,
        **_scope,
    ):
        assert brain_id == self.brain.id
        self.update_calls += 1
        self.brain = self.brain.model_copy(update={
            section: value,
            "version": self.brain.version + 1,
            "updated_at": NOW,
        })
        return self.brain

    async def get_patterns(
        self,
        brain_id,
        *,
        min_samples,
        min_confidence,
        limit,
    ):
        assert brain_id == self.brain.id
        return [
            pattern
            for pattern in self.patterns
            if pattern.samples >= min_samples
            and pattern.confidence >= min_confidence
        ][:limit]


class MemoryWriter:
    def __init__(self):
        self.events = []

    async def record_event(self, brain_id, event_type, **kwargs):
        self.events.append((brain_id, event_type, kwargs))
        return len(self.events)


class MemoryFeedbackLoop(FeedbackLoop):
    def __init__(self, repo, posts, writer=None):
        super().__init__(
            FakeSession(),
            repo=repo,
            writer=writer or MemoryWriter(),
            clock=lambda: NOW,
        )
        self.posts = posts
        self.loaded_scopes = []

    async def _load_posts(self, user_id, account_id):
        self.loaded_scopes.append((user_id, account_id))
        return list(self.posts.get((user_id, account_id), []))


def test_post_loader_is_explicitly_account_scoped_and_uses_latest_snapshot():
    session = FakeSession()
    loop = FeedbackLoop(session)
    assert asyncio.run(loop._load_posts(7, 101)) == []
    sql, params = session.calls[0]
    assert params == {"uid": 7, "account_id": 101}
    assert "post.user_id = :uid" in sql
    assert "post.threads_account_id = :account_id" in sql
    assert "ORDER BY insight.snapshot_date DESC" in sql
    assert "LIMIT 1" in sql


def test_repo_replaces_only_managed_pattern_projection():
    brain = make_brain()
    session = QueueSession([
        [brain.model_dump()],
        [
            {
                "kind": "length_bucket",
                "key": "long",
                "metric": "views",
            },
            {
                "kind": "length_bucket",
                "key": "short",
                "metric": "views",
            },
        ],
        [],
        [],
    ])
    count = asyncio.run(BrainRepo(session).replace_patterns(
        11,
        [
            BrainPatternWrite(
                kind="length_bucket",
                key="short",
                metric="views",
                lift=0.2,
                samples=5,
                confidence=0.75,
            )
        ],
        managed_kinds=MANAGED_PATTERN_KINDS,
        user_id=7,
        account_id=101,
    ))
    assert count == 1
    assert session.calls[0][1] == {
        "brain_id": 11,
        "uid": 7,
        "account_id": 101,
    }
    assert session.calls[1][1]["managed_kinds"] == sorted(
        MANAGED_PATTERN_KINDS
    )
    assert session.calls[2][1]["key"] == "long"
    assert session.calls[3][1]["key"] == "short"


def test_cross_account_post_is_rejected():
    repo = MemoryRepo()
    loop = MemoryFeedbackLoop(
        repo,
        {(7, 101): [make_post(1, account_id=202)]},
    )
    with pytest.raises(ValueError, match="outside account scope"):
        asyncio.run(loop.analyze_account(
            11,
            user_id=7,
            account_id=101,
        ))


def test_baseline_excludes_current_post_and_uses_median():
    previous = [
        make_post(0, views=10),
        make_post(1, views=10),
        make_post(2, views=10),
        make_post(3, views=10_000),
    ]
    current = make_post(4, views=20)
    baseline = baseline_for(previous, "views")
    score = FeedbackLoop(FakeSession()).analyze_post(
        current,
        previous,
        normalize_goal("reach"),
    )
    assert baseline.value == 10
    assert baseline.samples == 4
    assert score.baseline_value == 10
    assert score.post_value == 20
    assert score.lift == 1


def test_reach_goal_uses_views():
    previous = [make_post(index, views=100) for index in range(3)]
    score = FeedbackLoop(FakeSession()).analyze_post(
        make_post(3, views=150),
        previous,
        normalize_goal("охваты"),
    )
    assert score.metric == "views"
    assert score.status == "ok"
    assert score.lift == 0.5


def test_engagement_goal_uses_engagement_rate():
    previous = [
        make_post(index, engagement_rate=0.1)
        for index in range(3)
    ]
    score = FeedbackLoop(FakeSession()).analyze_post(
        make_post(3, engagement_rate=0.2),
        previous,
        normalize_goal("вовлечение"),
    )
    assert score.metric == "engagement_rate"
    assert score.status == "ok"
    assert score.lift == 1


@pytest.mark.parametrize(
    "goal,normalized",
    [
        ("подписчики", "followers"),
        ("переходы по ссылке", "traffic"),
        ("лиды", "leads"),
    ],
)
def test_unavailable_goals_are_explicitly_unsupported(goal, normalized):
    selection = normalize_goal(goal)
    score = FeedbackLoop(FakeSession()).analyze_post(
        make_post(3),
        [make_post(index) for index in range(3)],
        selection,
    )
    assert selection.normalized == normalized
    assert selection.supported is False
    assert score.status == "unsupported"
    assert score.metric is None


def test_canonical_goal_is_used_only_for_single_account():
    single = FeedbackLoop(FakeSession([{
        "account_count": 1,
        "goal": "вовлечение",
    }]))
    multiple = FeedbackLoop(FakeSession([{
        "account_count": 2,
        "goal": "охваты",
    }]))
    assert asyncio.run(single._canonical_goal(7)) == "вовлечение"
    assert asyncio.run(multiple._canonical_goal(7)) is None


def raw_post_row(metrics):
    return {
        "scheduled_post_id": 1001,
        "user_id": 7,
        "threads_account_id": 101,
        "threads_post_id": "threads-1",
        "published_at": NOW,
        "snapshot_date": NOW.date(),
        "text": "body",
        "link": None,
        "metrics_json": metrics,
    }


def test_missing_engagement_metric_does_not_create_false_rate():
    post = normalize_post(raw_post_row({
        "views": 100,
        "likes": 10,
        "replies": 1,
        "reposts": 1,
        "quotes": 0,
    }))
    assert post.views == 100
    assert post.shares is None
    assert post.engagement is None
    assert post.engagement_rate is None


def test_zero_views_does_not_divide_by_zero():
    post = normalize_post(raw_post_row({
        "views": 0,
        "likes": 10,
        "replies": 1,
        "reposts": 1,
        "quotes": 0,
        "shares": 1,
    }))
    assert post.engagement == 13
    assert post.engagement_rate is None


def test_pattern_metrics_are_isolated_and_samples_are_observations():
    patterns = rebuild_patterns([
        make_post(index)
        for index in range(6)
    ])
    length_patterns = [
        pattern
        for pattern in patterns
        if pattern.kind == "length_bucket"
        and pattern.key == "short"
    ]
    assert {pattern.metric for pattern in length_patterns} == {
        "views",
        "engagement_rate",
    }
    assert {pattern.samples for pattern in length_patterns} == {3}


def test_confidence_grows_with_samples_when_results_are_stable():
    small, _ = confidence_for([0.2, 0.2])
    mature, _ = confidence_for([0.2] * 5)
    assert small < mature
    assert mature >= 0.70


def test_unstable_results_reduce_confidence():
    stable, stable_dispersion = confidence_for([0.2] * 5)
    unstable, unstable_dispersion = confidence_for(
        [-1.0, -0.5, 0.2, 1.0, 2.0]
    )
    assert unstable_dispersion > stable_dispersion
    assert unstable < stable


def test_pattern_sample_and_confidence_thresholds_remain_conservative():
    immature_samples = rebuild_patterns([
        make_post(index)
        for index in range(7)
    ])
    mature = rebuild_patterns([
        make_post(index)
        for index in range(8)
    ])
    assert all(pattern.samples == 4 for pattern in immature_samples)
    assert all(pattern.confidence < 0.70 for pattern in immature_samples)
    assert all(pattern.samples == 5 for pattern in mature)
    assert all(pattern.confidence >= 0.70 for pattern in mature)


def test_immature_patterns_are_not_promoted_to_best_metrics():
    repo = MemoryRepo()
    loop = MemoryFeedbackLoop(
        repo,
        {(7, 101): [make_post(index) for index in range(7)]},
    )
    asyncio.run(loop.analyze_account(
        11,
        user_id=7,
        account_id=101,
    ))
    feedback = repo.brain.performance["feedback_v1"]
    assert feedback["patterns"] == {"total": 6, "mature": 0}
    assert feedback["best_metrics"] == {}


def test_feedback_rebuild_updates_performance_and_version_once():
    repo = MemoryRepo()
    writer = MemoryWriter()
    loop = MemoryFeedbackLoop(
        repo,
        {(7, 101): [make_post(index) for index in range(8)]},
        writer,
    )
    result = asyncio.run(loop.analyze_account(
        11,
        user_id=7,
        account_id=101,
    ))
    feedback = repo.brain.performance["feedback_v1"]
    assert result.changed is True
    assert result.brain_version == 2
    assert repo.brain.version == 2
    assert repo.brain.performance["rolling_30d"]["published_posts"] == 8
    assert feedback["baseline"] == {
        "metric": "views",
        "value": 100.0,
        "samples": 7,
    }
    assert feedback["patterns"] == {"total": 6, "mature": 6}
    assert repo.last_managed_kinds == MANAGED_PATTERN_KINDS
    assert len(writer.events) == 1


def test_feedback_rebuild_is_idempotent_for_same_source():
    repo = MemoryRepo()
    writer = MemoryWriter()
    loop = MemoryFeedbackLoop(
        repo,
        {(7, 101): [make_post(index) for index in range(8)]},
        writer,
    )
    first = asyncio.run(loop.analyze_account(
        11,
        user_id=7,
        account_id=101,
    ))
    second = asyncio.run(loop.analyze_account(
        11,
        user_id=7,
        account_id=101,
    ))
    assert first.changed is True
    assert second.status == "no_new_data"
    assert second.changed is False
    assert repo.pattern_writes == 1
    assert repo.update_calls == 1
    assert repo.brain.version == 2
    assert len(writer.events) == 1


def test_second_account_does_not_affect_first_account_feedback():
    repo = MemoryRepo()
    first_posts = [make_post(index, views=100) for index in range(4)]
    second_posts = [
        make_post(
            index,
            views=1_000_000,
            user_id=7,
            account_id=202,
        )
        for index in range(10)
    ]
    loop = MemoryFeedbackLoop(
        repo,
        {
            (7, 101): first_posts,
            (7, 202): second_posts,
        },
    )
    asyncio.run(loop.analyze_account(
        11,
        user_id=7,
        account_id=101,
    ))
    feedback = repo.brain.performance["feedback_v1"]
    assert loop.loaded_scopes == [(7, 101)]
    assert feedback["baseline"]["value"] == 100
    assert feedback["posts_analyzed"] == 4


def test_wrong_brain_owner_is_rejected_before_loading_posts():
    repo = MemoryRepo()
    loop = MemoryFeedbackLoop(repo, {})
    with pytest.raises(BrainNotFoundError):
        asyncio.run(loop.analyze_account(
            11,
            user_id=7,
            account_id=202,
        ))
    assert loop.loaded_scopes == []


def test_context_builder_receives_only_mature_patterns():
    patterns = [
        BrainPattern(
            id=1,
            brain_id=11,
            kind="length_bucket",
            key="short",
            metric="views",
            lift=0.2,
            samples=5,
            confidence=0.71,
            updated_at=NOW,
        ),
        BrainPattern(
            id=2,
            brain_id=11,
            kind="length_bucket",
            key="long",
            metric="views",
            lift=0.8,
            samples=4,
            confidence=0.95,
            updated_at=NOW,
        ),
        BrainPattern(
            id=3,
            brain_id=11,
            kind="has_link",
            key="true",
            metric="views",
            lift=0.9,
            samples=10,
            confidence=0.69,
            updated_at=NOW,
        ),
    ]
    context = asyncio.run(
        ContextBuilder(MemoryRepo(patterns=patterns)).build_context(
            11,
            "generation",
            1000,
        )
    )
    assert context.compact_dict()["patterns"] == [
        patterns[0].prompt_dict()
    ]


def test_job_failure_for_one_account_does_not_stop_next(monkeypatch):
    brains = [
        make_brain(brain_id=11, account_id=101),
        make_brain(brain_id=12, account_id=202),
    ]
    sessions = []
    analyzed = []

    class JobRepo:
        def __init__(self, _session):
            pass

        async def list_all(self):
            return brains

    class JobLoop:
        def __init__(self, _session):
            pass

        async def analyze_account(
            self,
            brain_id,
            *,
            user_id,
            account_id,
        ):
            analyzed.append((brain_id, user_id, account_id))
            if brain_id == 11:
                raise RuntimeError("isolated failure")
            return AccountFeedbackResult(
                brain_id=brain_id,
                user_id=user_id,
                threads_account_id=account_id,
                status="no_new_data",
                changed=False,
                posts_analyzed=0,
                patterns_written=0,
                brain_version=1,
            )

    def session_factory():
        session = FakeSession()
        sessions.append(session)
        return SessionContext(session)

    monkeypatch.setattr(feedback_jobs, "Session", session_factory)
    monkeypatch.setattr(feedback_jobs, "BrainRepo", JobRepo)
    monkeypatch.setattr(feedback_jobs, "FeedbackLoop", JobLoop)
    result = asyncio.run(feedback_jobs.feedback_loop_job())
    assert analyzed == [(11, 7, 101), (12, 7, 202)]
    assert result == {
        "brains": 2,
        "changed": 0,
        "unchanged": 1,
        "failed": 1,
    }
    assert sessions[1].rollbacks == 1
    assert sessions[2].commits == 1


def test_pattern_write_schema_rejects_invalid_confidence():
    with pytest.raises(ValueError):
        BrainPatternWrite(
            kind="length_bucket",
            key="short",
            metric="views",
            lift=0.1,
            samples=5,
            confidence=1.1,
        )
