import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.bot.handlers import scenarist as scenarist_handler
from app.core import autopilot, llm, scenarist, social_brain
from app.core.brain_repo import (
    BrainOwnershipError,
    BrainRepo,
)
from app.core.brain_writer import BrainWriter
from app.core.context_builder import (
    PATTERN_MIN_CONFIDENCE,
    PATTERN_MIN_SAMPLES,
    ContextBuilder,
)
from app.schemas.content_engine import ContentGenerationResponse
from app.schemas.social_brain import (
    BrainPattern,
    BrainRecord,
    BrainTaskContext,
)
from app.worker import m1_jobs

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


class FakeResult:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class FakeSession:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []
        self.commits = 0

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        rows = self.responses.pop(0) if self.responses else []
        return FakeResult(rows)

    def begin_nested(self):
        return FakeTransaction()

    async def commit(self):
        self.commits += 1


class FakeSessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return None


def brain_row(**overrides):
    row = {
        "id": 11,
        "user_id": 7,
        "threads_account_id": 101,
        "dna": {},
        "audience": {},
        "goals": {},
        "constraints": {},
        "performance": {},
        "version": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


def rich_brain(**overrides):
    row = brain_row(
        dna={
            "voice": {
                "tone": "direct",
                "lexicon": ["plain", "specific"],
                "sample_phrases": ["Start with the result."],
            },
            "manual_overrides": {"avoid_sales_pitch": True},
            "recent_examples": [
                "A recent example " + "x" * 120,
                "Another example " + "y" * 120,
            ],
        },
        audience={
            "niche": "creator tools",
            "keywords": ["automation", "growth"],
        },
        goals={"primary": "reach"},
        constraints={
            "critical": ["No fabricated claims"],
            "autocontent": {"active": True, "posts_per_day": 2},
        },
        performance={
            "rolling_30d": {
                "published_posts": 8,
                "metrics": {"views": 1200},
            }
        },
    )
    row.update(overrides)
    return BrainRecord.model_validate(row)


def pattern_row(**overrides):
    row = {
        "id": 31,
        "brain_id": 11,
        "kind": "hook",
        "key": "story",
        "metric": "views",
        "lift": 0.24,
        "samples": 12,
        "confidence": 0.88,
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


class MemoryRepo:
    def __init__(self, brain=None, patterns=None):
        self.brain = brain or rich_brain()
        self.patterns = patterns or [
            BrainPattern.model_validate(pattern_row())
        ]
        self.update_calls = []
        self.pattern_args = None

    async def get_or_create(self, user_id, account_id):
        if (
            user_id != self.brain.user_id
            or account_id != self.brain.threads_account_id
        ):
            raise BrainOwnershipError("not owned")
        return self.brain

    async def get(self, brain_id, **_kwargs):
        return self.brain if brain_id == self.brain.id else None

    async def update_section(
        self,
        brain_id,
        section,
        value,
        **_kwargs,
    ):
        assert brain_id == self.brain.id
        self.update_calls.append((section, value))
        self.brain = self.brain.model_copy(update={
            section: value,
            "version": self.brain.version + 1,
            "updated_at": NOW,
        })
        return self.brain

    async def get_patterns(self, brain_id, **kwargs):
        assert brain_id == self.brain.id
        self.pattern_args = kwargs
        metric = kwargs.get("metric")
        return [
            pattern
            for pattern in self.patterns
            if metric is None or pattern.metric == metric
        ]


class MemoryBrainWriter(BrainWriter):
    def __init__(self, repo, sources):
        super().__init__(FakeSession(), repo)
        self.sources = sources
        self.events = []
        self.event_keys = set()

    async def _load_backfill_sources(self, user_id, account_id):
        return self.sources

    async def record_event(
        self,
        brain_id,
        event_type,
        *,
        event_key=None,
        **kwargs,
    ):
        if event_key in self.event_keys:
            return None
        self.event_keys.add(event_key)
        self.events.append({
            "brain_id": brain_id,
            "type": event_type,
            "event_key": event_key,
            **kwargs,
        })
        return len(self.events)


def backfill_sources(account_count=1, **config_overrides):
    config = {
        "account_count": account_count,
        "voice_profile": {
            "tone": "direct",
            "taboo": ["fluff"],
        },
        "voice_samples": ["sample one", "sample two"],
        "voice_updated_at": NOW,
        "niche": "creator tools",
        "keywords": ["automation", "growth"],
        "niche_created_at": NOW,
        "autocontent_active": True,
        "posts_per_day": 2,
        "autocontent_user_id": 7,
        "autocontent_goal": "reach",
        "autocontent_created_at": NOW,
    }
    config.update(config_overrides)
    return {
        "config": config,
        "performance": {
            "total_posts": 4,
            "published_posts": 3,
            "failed_posts": 1,
            "insight_posts": 2,
            "views": 100,
            "likes": 10,
            "replies": 4,
            "reposts": 2,
            "quotes": 1,
            "shares": 3,
        },
    }


def build_context(task="generation", budget=1000, repo=None):
    repo = repo or MemoryRepo()
    return asyncio.run(
        ContextBuilder(repo).build_context(11, task, budget)
    )


def llm_response():
    return ContentGenerationResponse(
        brief={
            "goal": "reach",
            "topic": "topic",
            "angle": "observation",
            "format": "observation",
            "source": "manual",
        },
        hooks=[
            {
                "type": "insight",
                "text": "Concrete opening",
                "intent": "stop the reader",
            },
            {
                "type": "pain",
                "text": "A costly mistake",
                "intent": "name the pain",
            },
            {
                "type": "number",
                "text": "Three useful signals",
                "intent": "promise specifics",
            },
        ],
        body=(
            "A concrete observation with enough detail to pass the "
            "deterministic quality gate."
        ),
        metadata={
            "goal": "reach",
            "angle": "observation",
            "hook_type": "insight",
            "format": "observation",
            "topic": "topic",
            "has_cta": False,
            "source": "manual",
        },
        quality={
            "clarity": 0.8,
            "hook_strength": 0.8,
            "specificity": 0.8,
            "voice_match": 0.8,
            "goal_fit": 0.8,
        },
    )


def test_empty_brain_for_unknown_user():
    session = FakeSession([[], []])
    with pytest.raises(BrainOwnershipError, match="not owned"):
        asyncio.run(BrainRepo(session).get_or_create(404, 999))


def test_brain_from_existing_voice_profile():
    repo = MemoryRepo(rich_brain(dna={}))
    writer = MemoryBrainWriter(repo, backfill_sources())
    brain = asyncio.run(writer.apply_backfill(7, 101))
    assert brain.dna["voice"]["tone"] == "direct"
    assert brain.dna["recent_examples"] == ["sample one", "sample two"]


def test_brain_combines_voice_and_niche():
    repo = MemoryRepo(rich_brain(dna={}, audience={}))
    writer = MemoryBrainWriter(repo, backfill_sources())
    brain = asyncio.run(writer.apply_backfill(7, 101))
    assert brain.dna["voice"]["taboo"] == ["fluff"]
    assert brain.audience["niche"] == "creator tools"
    assert brain.audience["keywords"] == ["automation", "growth"]


def test_two_accounts_have_isolated_brains_and_global_facts():
    session = FakeSession([
        [brain_row()],
        [brain_row(
            id=12,
            threads_account_id=202,
            audience={"niche": "finance"},
        )],
    ])
    repo = BrainRepo(session)
    first = asyncio.run(repo.get_by_account(7, 101))
    second = asyncio.run(repo.get_by_account(7, 202))
    assert first.id == 11
    assert second.id == 12
    assert session.calls[0][1]["account_id"] == 101
    assert session.calls[1][1]["account_id"] == 202


def test_multi_account_brain_omits_ambiguous_user_defaults():
    repo = MemoryRepo(rich_brain(
        dna={},
        audience={},
        constraints={},
        performance={},
    ))
    writer = MemoryBrainWriter(
        repo,
        backfill_sources(account_count=2),
    )
    brain = asyncio.run(writer.apply_backfill(7, 101))
    assert brain.dna == {}
    assert brain.audience == {}
    assert brain.constraints == {}
    assert "rolling_30d" in brain.performance
    assert {event["source_type"] for event in writer.events} == {
        "scheduled_posts"
    }


def test_account_fact_overrides_same_key_global_fact():
    repo = MemoryRepo(rich_brain(
        dna={"voice": {"tone": "account-specific"}},
        audience={},
        constraints={},
        performance={},
    ))
    writer = MemoryBrainWriter(repo, backfill_sources())
    brain = asyncio.run(writer.apply_backfill(7, 101))
    assert brain.dna["voice"]["tone"] == "account-specific"
    assert brain.dna["voice"]["taboo"] == ["fluff"]


def test_wrong_account_ownership_is_rejected():
    session = FakeSession([[], []])
    with pytest.raises(BrainOwnershipError):
        asyncio.run(BrainRepo(session).get_or_create(7, 202))
    assert session.calls[0][1] == {"uid": 7, "account_id": 202}


def test_social_facts_are_routed_and_sensitive_keys_are_filtered():
    session = FakeSession()
    with pytest.raises(ValueError, match="sensitive field"):
        asyncio.run(BrainRepo(session).update_section(
            11,
            "dna",
            {"access_token": "never-store"},
        ))
    assert session.calls == []


def test_strategy_and_performance_are_aggregated():
    repo = MemoryRepo(rich_brain(
        goals={},
        performance={},
        dna={},
        audience={},
        constraints={},
    ))
    updated = asyncio.run(repo.update_section(
        11,
        "goals",
        {"primary": "growth"},
    ))
    writer = MemoryBrainWriter(repo, backfill_sources())
    brain = asyncio.run(writer.apply_backfill(7, 101))
    assert updated.goals["primary"] == "growth"
    assert brain.goals["primary"] == "growth"
    assert brain.performance["rolling_30d"]["published_posts"] == 3


def test_single_account_canonical_goal_populates_brain():
    repo = MemoryRepo(rich_brain(goals={}))
    writer = MemoryBrainWriter(repo, backfill_sources())
    brain = asyncio.run(writer.apply_backfill(7, 101))
    assert brain.goals["primary"] == "reach"


@pytest.mark.parametrize(
    "task,required_key",
    [
        ("generation", "dna"),
        ("radar", "audience"),
        ("neuro", "critical_constraints"),
        ("analytics", "performance"),
        ("autocontent", "autocontent"),
    ],
)
def test_task_specific_contexts_are_compact_and_scoped(
    task,
    required_key,
):
    context = build_context(task)
    assert required_key in context.compact_dict()
    assert "threads_account_id" not in context.compact_json()
    assert "user_id" not in context.compact_json()


def test_task_context_character_count():
    context = build_context()
    assert context.character_count() == len(context.compact_json())
    assert context.estimated_tokens > 0


def test_task_context_limits_long_term_facts():
    context = build_context(budget=90)
    assert context.estimated_tokens <= 90
    assert "recent_examples" in context.trimmed_fields


def test_build_brain_context_does_not_call_llm(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("ContextBuilder must not call an LLM")

    monkeypatch.setattr(llm, "ask_json", fail)
    context = build_context("analytics")
    assert context.compact_dict()["performance"]


def test_initial_brain_update_upserts_deterministic_summaries():
    repo = MemoryRepo(rich_brain(
        dna={},
        audience={},
        constraints={},
        performance={},
    ))
    writer = MemoryBrainWriter(repo, backfill_sources())
    asyncio.run(writer.apply_backfill(7, 101))
    assert {event["source_type"] for event in writer.events} == {
        "voice_profiles",
        "user_niches",
        "autocontent_settings",
        "scheduled_posts",
    }


def test_fact_upserts_match_partial_unique_scopes():
    migration = (
        ROOT / "migrations" / "005_social_brain.sql"
    ).read_text(encoding="utf-8")
    assert "unique (brain_id, kind, key, metric)" in migration
    assert "on brain_events (brain_id, event_key)" in migration
    assert "where event_key is not null" in migration


def test_sensitive_fields_cannot_be_persisted():
    session = FakeSession()
    writer = BrainWriter(session, MemoryRepo())
    with pytest.raises(ValueError, match="sensitive field"):
        asyncio.run(writer.record_event(
            11,
            "test",
            payload={"system_prompt": "full prompt"},
        ))
    assert session.calls == []


def test_scenarist_loads_brain_for_selected_account(monkeypatch):
    captured = {}
    expected = build_context()
    session = FakeSession()

    class FakeRepo:
        def __init__(self, current_session):
            captured["session"] = current_session

    class FakeWriter:
        def __init__(self, current_session, repo):
            captured["writer_repo"] = repo

        async def apply_backfill(self, user_id, account_id):
            captured["owner"] = (user_id, account_id)
            return rich_brain()

    class FakeBuilder:
        def __init__(self, repo):
            captured["builder_repo"] = repo

        async def build_context(self, brain_id, task, budget):
            captured["build"] = (brain_id, task, budget)
            return expected

    monkeypatch.setattr(
        scenarist_handler,
        "Session",
        lambda: FakeSessionContext(session),
    )
    monkeypatch.setattr(social_brain, "BrainRepo", FakeRepo)
    monkeypatch.setattr(social_brain, "BrainWriter", FakeWriter)
    monkeypatch.setattr(social_brain, "ContextBuilder", FakeBuilder)

    result = asyncio.run(scenarist_handler._load_brain(7, 101))
    assert result is expected
    assert captured["owner"] == (7, 101)
    assert captured["build"] == (
        11,
        "generation",
        scenarist.GENERATION_BRAIN_BUDGET_TOKENS,
    )
    assert session.commits == 1


def test_scenarist_falls_back_when_selected_account_is_invalid(
    monkeypatch,
):
    class FakeRepo:
        def __init__(self, _session):
            pass

    class FakeWriter:
        def __init__(self, _session, _repo):
            pass

        async def apply_backfill(self, *_args):
            raise BrainOwnershipError("not owned")

    monkeypatch.setattr(
        scenarist_handler,
        "Session",
        lambda: FakeSessionContext(FakeSession()),
    )
    monkeypatch.setattr(social_brain, "BrainRepo", FakeRepo)
    monkeypatch.setattr(social_brain, "BrainWriter", FakeWriter)
    assert asyncio.run(
        scenarist_handler._load_brain(7, 202)
    ) is None


def test_scenarist_infers_only_unambiguous_single_account(
    monkeypatch,
):
    sessions = iter([
        FakeSession([[(101,)]]),
        FakeSession([[(101,), (202,)]]),
    ])
    monkeypatch.setattr(
        scenarist_handler,
        "Session",
        lambda: FakeSessionContext(next(sessions)),
    )
    assert asyncio.run(
        scenarist_handler._single_account_id(7)
    ) == 101
    assert asyncio.run(
        scenarist_handler._single_account_id(7)
    ) is None


def test_scenarist_legacy_prompt_is_unchanged_without_brain(
    monkeypatch,
):
    captured = {}

    async def fake_ask_json(system, user, **_kwargs):
        captured["system"] = system
        captured["user"] = user
        return llm_response()

    monkeypatch.setattr(scenarist, "ask_json", fake_ask_json)
    profile = {"tone": "direct", "taboo": ["fluff"]}
    asyncio.run(scenarist.generate_post(profile, "topic"))
    legacy_system = scenarist.GEN_SYSTEM_TMPL.format(
        profile=scenarist._profile_str(profile),
        hooks=scenarist._HOOKS_TEXT,
    )
    assert captured["system"].startswith(legacy_system)
    assert "CONTENT_BRIEF_JSON:" in captured["user"]


def test_scenarist_adds_compact_brain_context(monkeypatch):
    captured = {}

    async def fake_ask_json(system, user, **_kwargs):
        captured["system"] = system
        captured["user"] = user
        return llm_response()

    monkeypatch.setattr(scenarist, "ask_json", fake_ask_json)
    context = build_context()
    asyncio.run(scenarist.generate_post(
        {"tone": "legacy"},
        "topic",
        brain=context,
    ))
    assert "creator tools" in captured["user"]
    assert "threads_account_id" not in captured["user"]
    assert "created_at" not in captured["user"]


def test_scenarist_keeps_account_voice_facts(monkeypatch):
    captured = {}

    async def fake_ask_json(system, user, **_kwargs):
        captured["system"] = system
        captured["user"] = user
        return llm_response()

    monkeypatch.setattr(scenarist, "ask_json", fake_ask_json)
    context = build_context()
    asyncio.run(scenarist.generate_post(
        {"tone": "legacy"},
        "topic",
        brain=context,
    ))
    assert '\\"tone\\":\\"direct\\"' in captured["user"]


def test_scenarist_uses_legacy_prompt_when_brain_fails(monkeypatch):
    captured = {}

    class BrokenBrain:
        estimated_tokens = 0

        def compact_dict(self):
            raise RuntimeError("broken")

    async def fake_ask_json(system, user, **_kwargs):
        captured["system"] = system
        captured["user"] = user
        return llm_response()

    monkeypatch.setattr(scenarist, "ask_json", fake_ask_json)
    profile = {"tone": "direct"}
    asyncio.run(scenarist.generate_post(
        profile,
        "topic",
        brain=BrokenBrain(),
    ))
    legacy_system = scenarist.GEN_SYSTEM_TMPL.format(
        profile=scenarist._profile_str(profile),
        hooks=scenarist._HOOKS_TEXT,
    )
    assert captured["system"].startswith(legacy_system)
    assert "CONTENT_BRIEF_JSON:" in captured["user"]


def test_get_or_create_is_idempotent():
    session = FakeSession([
        [brain_row()],
        [],
        [brain_row()],
    ])
    repo = BrainRepo(session)
    first = asyncio.run(repo.get_or_create(7, 101))
    second = asyncio.run(repo.get_or_create(7, 101))
    assert first.id == second.id == 11
    assert len(session.calls) == 3


def test_get_enforces_optional_owner_scope():
    session = FakeSession([[], [brain_row()]])
    repo = BrainRepo(session)
    denied = asyncio.run(repo.get(
        11,
        user_id=7,
        account_id=202,
    ))
    allowed = asyncio.run(repo.get(
        11,
        user_id=7,
        account_id=101,
    ))
    assert denied is None
    assert allowed.threads_account_id == 101


def test_update_section_increments_version():
    session = FakeSession([[brain_row(
        goals={"primary": "growth"},
        version=2,
    )]])
    updated = asyncio.run(BrainRepo(session).update_section(
        11,
        "goals",
        {"primary": "growth"},
        user_id=7,
        account_id=101,
    ))
    sql, params = session.calls[0]
    assert updated.version == 2
    assert "version = version + 1" in sql
    assert json.loads(params["section_value"]) == {
        "primary": "growth"
    }


def test_increment_version_is_atomic():
    session = FakeSession([[brain_row(version=3)]])
    updated = asyncio.run(
        BrainRepo(session).increment_version(11)
    )
    assert updated.version == 3
    assert "version = version + 1" in session.calls[0][0]


def test_brain_patterns_are_metric_aware():
    migration = (
        ROOT / "migrations" / "005_social_brain.sql"
    ).read_text(encoding="utf-8")
    assert "unique (brain_id, kind, key, metric)" in migration
    reach = BrainPattern.model_validate(pattern_row(metric="reach"))
    followers = BrainPattern.model_validate(
        pattern_row(id=32, metric="followers")
    )
    assert reach.metric != followers.metric


def test_pattern_thresholds_are_centralized():
    repo = MemoryRepo()
    build_context(repo=repo)
    assert repo.pattern_args["min_samples"] == PATTERN_MIN_SAMPLES
    assert (
        repo.pattern_args["min_confidence"]
        == PATTERN_MIN_CONFIDENCE
    )
    assert repo.pattern_args["metric"] == "views"


def test_brain_events_are_idempotent():
    session = FakeSession([[(51,)], []])
    writer = BrainWriter(session, MemoryRepo())
    first = asyncio.run(writer.record_event(
        11,
        "test_event",
        event_key="source:1",
    ))
    second = asyncio.run(writer.record_event(
        11,
        "test_event",
        event_key="source:1",
    ))
    assert first == 51
    assert second is None
    assert session.calls[0][1]["event_key"] == "source:1"


def test_duplicate_post_published_uses_same_event_key():
    session = FakeSession([[(61,)], []])
    writer = BrainWriter(session, MemoryRepo())
    first = asyncio.run(writer.record_post_published(
        7,
        101,
        scheduled_post_id=900,
        threads_post_id="threads-1",
    ))
    second = asyncio.run(writer.record_post_published(
        7,
        101,
        scheduled_post_id=900,
        threads_post_id="threads-1",
    ))
    assert first == 61
    assert second is None
    keys = [params["event_key"] for _, params in session.calls]
    assert keys == [
        "post_published:scheduled_post:900",
        "post_published:scheduled_post:900",
    ]


def test_duplicate_insights_snapshot_uses_same_event_key():
    session = FakeSession([[(71,)], []])
    writer = BrainWriter(session, MemoryRepo())
    kwargs = {
        "threads_post_id": "threads-1",
        "snapshot_date": date(2026, 7, 29),
        "metrics": {"views": 10},
    }
    first = asyncio.run(
        writer.record_insights_snapshot(7, 101, **kwargs)
    )
    second = asyncio.run(
        writer.record_insights_snapshot(7, 101, **kwargs)
    )
    assert first == 71
    assert second is None
    assert session.calls[0][1]["event_key"] == (
        "insights_snapshot:threads-1:2026-07-29"
    )


def test_backfill_is_idempotent():
    repo = MemoryRepo(rich_brain(
        dna={},
        audience={},
        constraints={},
        performance={},
    ))
    writer = MemoryBrainWriter(repo, backfill_sources())
    first = asyncio.run(writer.apply_backfill(7, 101))
    calls_after_first = len(repo.update_calls)
    events_after_first = len(writer.events)
    second = asyncio.run(writer.apply_backfill(7, 101))
    assert first.version == second.version
    assert len(repo.update_calls) == calls_after_first
    assert len(writer.events) == events_after_first


def test_backfill_does_not_overwrite_newer_brain_values():
    repo = MemoryRepo(rich_brain(
        dna={"voice": {"tone": "manual"}},
        audience={},
        constraints={},
        performance={},
    ))
    writer = MemoryBrainWriter(repo, backfill_sources())
    brain = asyncio.run(writer.apply_backfill(7, 101))
    assert brain.dna["voice"]["tone"] == "manual"
    assert brain.dna["voice"]["taboo"] == ["fluff"]


def test_backfill_refreshes_unchanged_canonical_values():
    repo = MemoryRepo(rich_brain(
        dna={},
        audience={},
        constraints={},
        performance={},
    ))
    writer = MemoryBrainWriter(repo, backfill_sources())
    asyncio.run(writer.apply_backfill(7, 101))
    writer.sources = backfill_sources(
        voice_profile={"tone": "updated", "taboo": ["fluff"]}
    )
    brain = asyncio.run(writer.apply_backfill(7, 101))
    assert brain.dna["voice"]["tone"] == "updated"


def test_context_builder_generation_priority():
    payload = build_context("generation").compact_dict()
    assert list(payload)[:3] == [
        "dna",
        "primary_goal",
        "critical_constraints",
    ]
    assert "performance" not in payload


def test_context_builder_radar():
    payload = build_context("radar").compact_dict()
    assert "audience" in payload
    assert "performance" in payload
    assert "dna" not in payload


def test_context_builder_neuro():
    payload = build_context("neuro").compact_dict()
    assert "dna" in payload
    assert "critical_constraints" in payload
    assert "performance" not in payload


def test_context_builder_analytics():
    payload = build_context("analytics").compact_dict()
    assert "performance" in payload
    assert "dna" not in payload
    assert "recent_examples" not in payload


def test_context_builder_autocontent():
    payload = build_context("autocontent").compact_dict()
    assert payload["autocontent"]["posts_per_day"] == 2
    assert "dna" in payload


def test_critical_constraints_survive_budget_trimming():
    context = build_context("generation", budget=45)
    assert context.compact_dict()["critical_constraints"] == [
        "No fabricated claims"
    ]
    assert "recent_examples" not in context.compact_dict()


def test_scenarist_logs_prompt_sizes(monkeypatch, caplog):
    async def fake_ask_json(*_args, **_kwargs):
        return llm_response()

    monkeypatch.setattr(scenarist, "ask_json", fake_ask_json)
    with caplog.at_level(logging.INFO, logger="scenarist"):
        asyncio.run(scenarist.generate_post(
            {"tone": "legacy"},
            "topic",
            brain=build_context(),
        ))
    message = caplog.records[-1].getMessage()
    assert "legacy_estimated_tokens=" in message
    assert "new_estimated_tokens=" in message
    assert "delta_percent=" in message
    assert "brief_tokens=" in message
    assert "memory_tokens=" in message
    assert "brain_tokens=" in message


def test_publishing_event_only_after_success(monkeypatch):
    captured = []

    class FakeWriter:
        def __init__(self, _session):
            pass

        async def record_post_published(self, *args, **kwargs):
            captured.append((args, kwargs))

    async def fake_create(*_args, **_kwargs):
        return "container"

    async def fake_publish(*_args, **_kwargs):
        return "threads-post-1"

    monkeypatch.setattr(autopilot, "BrainWriter", FakeWriter)
    monkeypatch.setattr(autopilot, "decrypt_token", lambda _v: "token")
    monkeypatch.setattr(autopilot, "create_container", fake_create)
    monkeypatch.setattr(autopilot, "publish_container", fake_publish)
    session = FakeSession([
        [("publishing",)],
        [(0,)],
        [(
            "threads-user",
            b"encrypted",
            NOW + timedelta(days=30),
        )],
        [],
        [],
        [],
    ])
    result = asyncio.run(autopilot.publish_one(
        session,
        (900, 7, 101, "body", None, None),
    ))
    assert result[0] is True
    assert captured[0][0] == (7, 101)
    assert captured[0][1]["scheduled_post_id"] == 900
    run_update = next(
        params
        for sql, params in session.calls
        if "UPDATE autopost_runs" in sql
    )
    assert run_update["status"] == "success"
    assert run_update["threads_post_id"] == "threads-post-1"


def test_publishing_failure_does_not_record_event(monkeypatch):
    captured = []

    class FakeWriter:
        def __init__(self, _session):
            pass

        async def record_post_published(self, *_args, **_kwargs):
            captured.append(True)

    async def fail_create(*_args, **_kwargs):
        raise RuntimeError("Threads unavailable")

    monkeypatch.setattr(autopilot, "BrainWriter", FakeWriter)
    monkeypatch.setattr(autopilot, "decrypt_token", lambda _v: "token")
    monkeypatch.setattr(autopilot, "create_container", fail_create)
    session = FakeSession([
        [("publishing",)],
        [(0,)],
        [(
            "threads-user",
            b"encrypted",
            NOW + timedelta(days=30),
        )],
        [],
        [],
    ])
    result = asyncio.run(autopilot.publish_one(
        session,
        (900, 7, 101, "body", None, None),
    ))
    assert result[0] is False
    assert captured == []


def test_insights_event_only_after_success(monkeypatch):
    captured = []
    read_session = FakeSession([[
        ("threads-post-1", 7, 101, b"encrypted"),
    ]])
    write_session = FakeSession([[(date(2026, 7, 29),)]])
    sessions = iter([read_session, write_session])

    class FakeWriter:
        def __init__(self, _session):
            pass

        async def record_insights_snapshot(self, *args, **kwargs):
            captured.append((args, kwargs))

    async def fake_insights(*_args):
        return {"views": 42}

    monkeypatch.setattr(
        m1_jobs,
        "Session",
        lambda: FakeSessionContext(next(sessions)),
    )
    monkeypatch.setattr(m1_jobs, "BrainWriter", FakeWriter)
    monkeypatch.setattr(m1_jobs, "decrypt_token", lambda _v: "token")
    monkeypatch.setattr(m1_jobs, "get_insights", fake_insights)
    asyncio.run(m1_jobs.insights_snapshotter())
    assert captured[0][0] == (7, 101)
    assert captured[0][1]["threads_post_id"] == "threads-post-1"
    assert write_session.commits == 1


def test_insights_failure_does_not_record_event(monkeypatch):
    captured = []
    read_session = FakeSession([[
        ("threads-post-1", 7, 101, b"encrypted"),
    ]])

    class FakeWriter:
        def __init__(self, _session):
            pass

        async def record_insights_snapshot(self, *_args, **_kwargs):
            captured.append(True)

    async def fail_insights(*_args):
        raise RuntimeError("Threads unavailable")

    monkeypatch.setattr(
        m1_jobs,
        "Session",
        lambda: FakeSessionContext(read_session),
    )
    monkeypatch.setattr(m1_jobs, "BrainWriter", FakeWriter)
    monkeypatch.setattr(m1_jobs, "decrypt_token", lambda _v: "token")
    monkeypatch.setattr(m1_jobs, "get_insights", fail_insights)
    asyncio.run(m1_jobs.insights_snapshotter())
    assert captured == []


def test_migration_has_pattern_constraints():
    migration = (
        ROOT / "migrations" / "005_social_brain.sql"
    ).read_text(encoding="utf-8")
    assert "check (samples >= 0)" in migration
    assert "check (confidence >= 0 and confidence <= 1)" in migration


def test_migration_has_strict_account_ownership():
    migration = (
        ROOT / "migrations" / "005_social_brain.sql"
    ).read_text(encoding="utf-8")
    assert "threads_account_id bigint not null" in migration
    assert "foreign key (threads_account_id, user_id)" in migration
    assert "unique (user_id, threads_account_id)" in migration


def test_rollback_drops_only_final_brain_tables():
    rollback = (
        ROOT / "migrations" / "rollback" / "005_social_brain.sql"
    ).read_text(encoding="utf-8")
    assert "drop table if exists brain_events" in rollback
    assert "drop table if exists brain_patterns" in rollback
    assert "drop table if exists brains" in rollback
    assert "social_facts" not in rollback
