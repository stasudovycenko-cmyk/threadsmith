import asyncio
import json
from datetime import datetime, timezone

import pytest

from app.core import llm, scenarist, social_brain
from app.bot.handlers import scenarist as scenarist_handler
from app.schemas.llm import PostGenerationResponse
from app.schemas.social_brain import (
    BrainAccountIdentity,
    BrainAudience,
    BrainConstraints,
    BrainContentPreferences,
    BrainFact,
    BrainGoals,
    BrainNiche,
    BrainPerformance,
    BrainStrategy,
    BrainUserIdentity,
    BrainVoice,
    PerformanceMetrics,
    SocialBrainContext,
)


class FakeResult:
    def __init__(self, rows):
        self.rows = list(rows)

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class FakeSession:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params or {}))
        if not self.responses:
            return FakeResult([])
        return FakeResult(self.responses.pop(0))


class FakeSessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return None


def base_row(**overrides):
    row = {
        "user_id": 7,
        "threads_account_id": 101,
        "threads_username": None,
        "account_count": 1,
        "voice_profile_json": None,
        "voice_updated_at": None,
        "niche": None,
        "niche_keywords": None,
        "primary_goal": None,
        "secondary_goal": None,
        "strategy_json": None,
        "autonomy_level": None,
        "strategy_updated_at": None,
        "autocontent_active": None,
        "posts_per_day": None,
        "neuro_active": None,
        "neuro_mode": None,
        "neuro_daily_cap": None,
    }
    row.update(overrides)
    return row


def performance_row(**overrides):
    row = {
        "total_posts_30d": 0,
        "published_posts_30d": 0,
        "failed_posts_30d": 0,
        "insight_posts_30d": 0,
        "views": 0,
        "likes": 0,
        "replies": 0,
        "reposts": 0,
        "quotes": 0,
        "shares": 0,
    }
    row.update(overrides)
    return row


def context_responses(
    base,
    *,
    facts=None,
    performance=None,
    generations=None,
    neuro=None,
):
    return [
        [base],
        facts or [],
        [performance or performance_row()],
        generations or [],
        neuro or [],
    ]


def fact_row(
    fact_type,
    key,
    value,
    *,
    threads_account_id=None,
    confidence=1.0,
):
    return {
        "threads_account_id": threads_account_id,
        "fact_type": fact_type,
        "fact_key": key,
        "fact_value_json": value,
        "confidence": confidence,
        "source": "test",
        "updated_at": datetime(
            2026,
            7,
            2,
            tzinfo=timezone.utc,
        ),
    }


def test_empty_brain_for_unknown_user():
    session = FakeSession([[]])

    with pytest.raises(
        social_brain.SocialBrainAccountError,
        match="not owned",
    ):
        asyncio.run(
            social_brain.build_brain_context(session, 404, 999)
        )
    assert len(session.calls) == 1


def test_brain_from_existing_voice_profile():
    updated_at = datetime(2026, 7, 1, tzinfo=timezone.utc)
    profile = {
        "lexicon": ["короче", "по делу"],
        "sentence_length": "short",
        "punctuation": "periods",
        "tone": "direct",
        "structure": "hook then point",
        "taboo": ["water"],
        "sample_phrases": ["Начнём с факта."],
    }
    session = FakeSession(context_responses(base_row(
        voice_profile_json=profile,
        voice_updated_at=updated_at,
    )))

    brain = asyncio.run(
        social_brain.build_brain_context(session, 7, 101)
    )

    assert brain.voice.available is True
    assert brain.voice.lexicon == ["короче", "по делу"]
    assert brain.voice.tone == "direct"
    assert brain.voice.updated_at == updated_at


def test_brain_combines_voice_and_niche():
    session = FakeSession(context_responses(base_row(
        voice_profile_json={"tone": "calm"},
        niche="SaaS",
        niche_keywords=["retention", "activation"],
    )))

    brain = asyncio.run(
        social_brain.build_brain_context(session, 7, 101)
    )

    assert brain.voice.tone == "calm"
    assert brain.niche.name == "SaaS"
    assert brain.niche.keywords == ["retention", "activation"]
    assert brain.account.uses_user_defaults is True


def test_two_accounts_have_isolated_brains_and_global_facts():
    global_fact = fact_row(
        "audience",
        "shared_goal",
        "learn faster",
    )
    facts_a = [
        global_fact,
        fact_row(
            "voice",
            "profile",
            {"tone": "bold", "taboo": ["hedging"]},
            threads_account_id=101,
        ),
        fact_row(
            "topic",
            "niche",
            {"name": "SaaS", "keywords": ["retention"]},
            threads_account_id=101,
        ),
        fact_row(
            "performance",
            "winning_pattern",
            "numbers",
            threads_account_id=101,
        ),
    ]
    facts_b = [
        global_fact,
        fact_row(
            "voice",
            "profile",
            {"tone": "calm", "taboo": ["hype"]},
            threads_account_id=202,
        ),
        fact_row(
            "topic",
            "niche",
            {"name": "Fitness", "keywords": ["mobility"]},
            threads_account_id=202,
        ),
        fact_row(
            "performance",
            "winning_pattern",
            "stories",
            threads_account_id=202,
        ),
    ]
    session_a = FakeSession([
        [base_row(
            threads_account_id=101,
            threads_username="account_a",
            account_count=2,
            voice_profile_json={"tone": "ambiguous"},
            niche="ambiguous",
            primary_goal="grow SaaS founders",
            strategy_json={"cadence": "daily"},
            autonomy_level="assist",
        )],
        facts_a,
        [performance_row(
            total_posts_30d=4,
            published_posts_30d=3,
            insight_posts_30d=2,
            views=900,
        )],
    ])
    session_b = FakeSession([
        [base_row(
            threads_account_id=202,
            threads_username="account_b",
            account_count=2,
            voice_profile_json={"tone": "ambiguous"},
            niche="ambiguous",
            primary_goal="grow fitness audience",
            strategy_json={"cadence": "weekly"},
            autonomy_level="manual",
        )],
        facts_b,
        [performance_row(
            total_posts_30d=2,
            published_posts_30d=2,
            insight_posts_30d=1,
            views=300,
        )],
    ])

    brain_a = asyncio.run(
        social_brain.build_brain_context(
            session_a,
            7,
            101,
        )
    )
    brain_b = asyncio.run(
        social_brain.build_brain_context(
            session_b,
            7,
            202,
        )
    )

    assert brain_a.voice.tone == "bold"
    assert brain_b.voice.tone == "calm"
    assert brain_a.niche.name == "SaaS"
    assert brain_b.niche.name == "Fitness"
    assert brain_a.goals.primary == "grow SaaS founders"
    assert brain_b.goals.primary == "grow fitness audience"
    assert brain_a.strategy.values == {"cadence": "daily"}
    assert brain_b.strategy.values == {"cadence": "weekly"}
    assert brain_a.performance.metrics_30d.views == 900
    assert brain_b.performance.metrics_30d.views == 300
    assert brain_a.performance.facts[0].value == "numbers"
    assert brain_b.performance.facts[0].value == "stories"
    assert brain_a.audience.facts[0].scope == "global"
    assert brain_b.audience.facts[0].scope == "global"
    assert "stories" not in brain_a.compact_json()
    assert "numbers" not in brain_b.compact_json()
    assert brain_a.account.uses_user_defaults is False
    assert brain_b.account.uses_user_defaults is False
    assert len(session_a.calls) == 3
    assert len(session_b.calls) == 3
    for _, params in session_a.calls:
        assert params["account_id"] == 101
    for _, params in session_b.calls:
        assert params["account_id"] == 202


def test_multi_account_brain_omits_ambiguous_user_defaults():
    session = FakeSession([
        [base_row(
            account_count=2,
            voice_profile_json={"tone": "ambiguous"},
            niche="ambiguous",
            niche_keywords=["ambiguous"],
            autocontent_active=True,
            posts_per_day=3,
            neuro_active=True,
            neuro_mode="auto",
            neuro_daily_cap=10,
        )],
        [],
        [performance_row()],
    ])

    brain = asyncio.run(
        social_brain.build_brain_context(session, 7, 101)
    )

    assert brain.voice.available is False
    assert brain.niche.name is None
    assert brain.niche.keywords == []
    assert brain.content_preferences.autocontent_active is None
    assert brain.content_preferences.posts_per_day is None
    assert brain.constraints.neuro_active is None
    assert brain.performance.generated_30d == 0
    assert len(session.calls) == 3


def test_account_fact_overrides_same_key_global_fact():
    session = FakeSession([
        [base_row(account_count=2)],
        [
            fact_row(
                "audience",
                "preferred_offer",
                "account-specific",
                threads_account_id=101,
            )
        ],
        [performance_row()],
    ])

    brain = asyncio.run(
        social_brain.build_brain_context(session, 7, 101)
    )

    assert brain.audience.facts[0].value == "account-specific"
    facts_sql, params = session.calls[1]
    assert "(threads_account_id IS NOT NULL) DESC" in facts_sql
    assert "threads_account_id = :account_id" in facts_sql
    assert params["account_id"] == 101


def test_wrong_account_ownership_is_rejected():
    build_session = FakeSession([[]])
    fact_session = FakeSession([[]])
    strategy_session = FakeSession([[]])

    with pytest.raises(social_brain.SocialBrainAccountError):
        asyncio.run(social_brain.build_brain_context(
            build_session,
            7,
            202,
        ))
    with pytest.raises(social_brain.SocialBrainAccountError):
        asyncio.run(social_brain.upsert_social_fact(
            fact_session,
            7,
            threads_account_id=202,
            fact_type="audience",
            key="pain",
            value="wrong owner",
            source="test",
        ))
    with pytest.raises(social_brain.SocialBrainAccountError):
        asyncio.run(social_brain.upsert_strategy_state(
            strategy_session,
            7,
            202,
            strategy={"cadence": "daily"},
        ))


def test_social_facts_are_routed_and_sensitive_keys_are_filtered():
    timestamp = datetime(2026, 7, 2, tzinfo=timezone.utc)
    facts = [
        {
            "threads_account_id": 101,
            "fact_type": fact_type,
            "fact_key": f"{fact_type}_key",
            "fact_value_json": {
                "safe": fact_type,
                "access_token": "must-not-leak",
            },
            "confidence": 0.8,
            "source": "test",
            "updated_at": timestamp,
        }
        for fact_type in (
            "voice",
            "audience",
            "content_pattern",
            "topic",
            "constraint",
            "performance",
            "business",
        )
    ]
    session = FakeSession(context_responses(
        base_row(strategy_json={
            "positioning": "expert",
            "api_key": "must-not-leak",
        }),
        facts=facts,
    ))

    brain = asyncio.run(
        social_brain.build_brain_context(session, 7, 101)
    )

    assert len(brain.voice.facts) == 1
    assert len(brain.audience.facts) == 1
    assert len(brain.content_preferences.facts) == 1
    assert len(brain.niche.topic_facts) == 1
    assert len(brain.constraints.facts) == 1
    assert len(brain.performance.facts) == 1
    assert len(brain.memory.facts) == 1
    assert brain.memory.facts[0].value == {"safe": "business"}
    assert brain.strategy.values == {"positioning": "expert"}


def test_strategy_and_performance_are_aggregated():
    updated_at = datetime(2026, 7, 3, tzinfo=timezone.utc)
    session = FakeSession(context_responses(
        base_row(
            primary_goal="grow qualified audience",
            secondary_goal="learn winning topics",
            strategy_json={"cadence": "daily"},
            autonomy_level="assist",
            strategy_updated_at=updated_at,
            autocontent_active=False,
            posts_per_day=2,
            neuro_active=True,
            neuro_mode="approve",
            neuro_daily_cap=5,
        ),
        performance=performance_row(
            total_posts_30d=4,
            published_posts_30d=3,
            failed_posts_30d=1,
            insight_posts_30d=2,
            views=900,
            likes=45,
            replies=12,
        ),
        generations=[
            {
                "generation_type": "generate_post",
                "generation_count": 6,
            },
            {
                "generation_type": "rewrite",
                "generation_count": 2,
            },
        ],
        neuro=[
            {"status": "posted", "status_count": 3},
            {"status": "rejected", "status_count": 1},
        ],
    ))

    brain = asyncio.run(
        social_brain.build_brain_context(session, 7, 101)
    )

    assert brain.goals.primary == "grow qualified audience"
    assert brain.strategy == BrainStrategy(
        autonomy_level="assist",
        values={"cadence": "daily"},
        updated_at=updated_at,
    )
    assert brain.content_preferences.generation_mix_30d == {
        "generate_post": 6,
        "rewrite": 2,
    }
    assert brain.performance.generated_30d == 8
    assert brain.performance.metrics_30d.views == 900
    assert brain.performance.neuro_status_30d == {
        "posted": 3,
        "rejected": 1,
    }


def rich_brain(threads_account_id=101):
    fact = BrainFact(
        fact_type="audience",
        key="pain",
        value="slow content production",
        scope="account",
        confidence=0.9,
        source="manual",
        updated_at=datetime(2026, 7, 4, tzinfo=timezone.utc),
    )
    return SocialBrainContext(
        user=BrainUserIdentity(user_id=7),
        account=BrainAccountIdentity(
            threads_account_id=threads_account_id,
            threads_username="author",
        ),
        voice=BrainVoice(
            available=True,
            lexicon=["short", "clear"],
            tone="direct",
            taboo=["fluff"],
            sample_phrases=["Start with proof."],
        ),
        niche=BrainNiche(
            name="creator tools",
            keywords=["automation"],
            topic_facts=[
                fact.model_copy(update={
                    "fact_type": "topic",
                    "key": "content systems",
                })
            ],
        ),
        goals=BrainGoals(primary="qualified growth"),
        audience=BrainAudience(facts=[fact]),
        content_preferences=BrainContentPreferences(
            autocontent_active=False,
            posts_per_day=1,
            generation_mix_30d={"generate_post": 5},
            facts=[
                fact.model_copy(update={
                    "fact_type": "content_pattern",
                    "key": "strong_hooks",
                    "value": "numbers",
                })
            ],
        ),
        constraints=BrainConstraints(
            voice_taboo=["fluff"],
            neuro_active=True,
            neuro_mode="approve",
            neuro_daily_cap=5,
        ),
        performance=BrainPerformance(
            generated_30d=5,
            published_posts_30d=3,
            insight_posts_30d=2,
            metrics_30d=PerformanceMetrics(
                views=1200,
                likes=60,
                replies=8,
            ),
        ),
        strategy=BrainStrategy(
            autonomy_level="assist",
            values={"cadence": "daily"},
        ),
    )


def test_task_specific_contexts_are_compact_and_scoped():
    brain = rich_brain()

    generation = brain.for_generation().compact_dict()
    radar = brain.for_radar().compact_dict()
    neuro = brain.for_neuro().compact_dict()
    autocontent = brain.for_autocontent().compact_dict()

    assert set(generation) == {
        "voice",
        "niche",
        "goals",
        "content_patterns",
        "performance",
    }
    assert set(radar) == {
        "niche",
        "goals",
        "audience",
        "topics",
        "performance",
    }
    assert set(neuro) == {
        "voice",
        "niche",
        "audience",
        "constraints",
    }
    assert generation["voice"]["taboo"] == ["fluff"]
    assert neuro["constraints"]["neuro_mode"] == "approve"
    assert "constraints" not in generation
    assert set(autocontent) == {
        "voice",
        "niche",
        "goals",
        "strategy",
        "content_preferences",
        "performance",
    }
    for context in (generation, radar, neuro, autocontent):
        serialized = json.dumps(context)
        assert "user_id" not in serialized
        assert "threads_account_id" not in serialized
        assert "scope" not in serialized
        assert "source" not in serialized
        assert "updated_at" not in serialized


def test_task_context_character_count():
    brain = rich_brain()
    contexts = (
        brain.for_generation(),
        brain.for_radar(),
        brain.for_neuro(),
        brain.for_autocontent(),
    )

    for context in contexts:
        assert context.character_count() == len(
            context.compact_json()
        )
        assert context.character_count() < 3000


def test_task_context_limits_long_term_facts():
    brain = rich_brain()
    brain.audience.facts = [
        BrainFact(
            fact_type="audience",
            key=f"fact_{index}",
            value=index,
            scope="account",
            confidence=index / 20,
            source="test",
        )
        for index in range(12)
    ]

    audience = brain.for_radar().audience

    assert len(audience) == 8
    assert audience[0].key == "fact_11"
    assert audience[-1].key == "fact_4"


def test_build_brain_context_does_not_call_llm(monkeypatch):
    async def forbidden_llm_call(*_args, **_kwargs):
        raise AssertionError("LLM must not be called")

    monkeypatch.setattr(llm, "ask_json", forbidden_llm_call)
    session = FakeSession(context_responses(base_row()))

    brain = asyncio.run(
        social_brain.build_brain_context(session, 7, 101)
    )

    assert "ask_json" not in vars(social_brain)
    assert brain.account.threads_account_id == 101
    assert len(session.calls) == 5


def test_initial_brain_update_upserts_deterministic_summaries(
    monkeypatch,
):
    initial = SocialBrainContext(
        user=BrainUserIdentity(user_id=7),
        account=BrainAccountIdentity(threads_account_id=101),
        content_preferences=BrainContentPreferences(
            generation_mix_30d={"generate_post": 4}
        ),
        performance=BrainPerformance(
            total_posts_30d=3,
            published_posts_30d=2,
            insight_posts_30d=2,
            metrics_30d=PerformanceMetrics(views=500, likes=20),
            neuro_status_30d={"posted": 1},
        ),
    )
    refreshed = initial.model_copy(deep=True)
    builds = [initial, refreshed]
    upserts = []

    async def fake_build(_session, _user_id, _account_id):
        return builds.pop(0)

    async def fake_upsert(_session, user_id, **kwargs):
        upserts.append((user_id, kwargs))

    monkeypatch.setattr(
        social_brain, "build_brain_context", fake_build
    )
    monkeypatch.setattr(
        social_brain, "upsert_social_fact", fake_upsert
    )

    result = asyncio.run(
        social_brain.initialize_brain_from_existing_data(
            FakeSession(), 7, 101
        )
    )

    assert result == refreshed
    assert [item[1]["key"] for item in upserts] == [
        "generation_mix_30d",
        "rolling_30d",
    ]
    assert all(
        item[1]["threads_account_id"] == 101
        for item in upserts
    )
    assert upserts[0][1]["value"] == {
        "window_days": 30,
        "by_type": {"generate_post": 4},
    }
    assert upserts[1][1]["value"]["metrics"]["views"] == 500


def test_fact_upserts_match_partial_unique_scopes():
    global_session = FakeSession([[(1,)]])
    account_session = FakeSession([[(2,)]])

    asyncio.run(social_brain.upsert_social_fact(
        global_session,
        7,
        threads_account_id=None,
        fact_type="audience",
        key="pain",
        value="shared",
        source="test",
    ))
    asyncio.run(social_brain.upsert_social_fact(
        account_session,
        7,
        threads_account_id=101,
        fact_type="audience",
        key="pain",
        value="account",
        source="test",
    ))

    global_sql, global_params = global_session.calls[0]
    account_sql, account_params = account_session.calls[0]
    assert "WHERE threads_account_id IS NULL" in global_sql
    assert "WHERE threads_account_id IS NOT NULL" in account_sql
    assert global_params["account_id"] is None
    assert account_params["account_id"] == 101


def test_sensitive_fields_cannot_be_persisted():
    session = FakeSession()

    with pytest.raises(ValueError, match="sensitive field"):
        asyncio.run(social_brain.upsert_social_fact(
            session,
            7,
            threads_account_id=101,
            fact_type="business",
            key="integration",
            value={"nested": {"access_token": "secret"}},
            source="test",
        ))

    with pytest.raises(ValueError, match="sensitive field"):
        asyncio.run(social_brain.upsert_strategy_state(
            session,
            7,
            101,
            strategy={"oauth_secret": "secret"},
        ))

    assert session.calls == []


def test_scenarist_loads_brain_for_selected_account(monkeypatch):
    captured = {}
    expected = rich_brain(threads_account_id=202)

    async def fake_build(session, user_id, account_id):
        captured.update(
            session=session,
            user_id=user_id,
            account_id=account_id,
        )
        return expected

    session = FakeSession()
    monkeypatch.setattr(
        scenarist_handler,
        "Session",
        lambda: FakeSessionContext(session),
    )
    monkeypatch.setattr(
        scenarist_handler.social_brain,
        "build_brain_context",
        fake_build,
    )

    result = asyncio.run(
        scenarist_handler._load_brain(7, 202)
    )

    assert result is expected
    assert captured == {
        "session": session,
        "user_id": 7,
        "account_id": 202,
    }


def test_scenarist_falls_back_when_selected_account_is_invalid(
    monkeypatch,
):
    async def fake_build(*_args):
        raise social_brain.SocialBrainAccountError("not owned")

    monkeypatch.setattr(
        scenarist_handler,
        "Session",
        lambda: FakeSessionContext(FakeSession()),
    )
    monkeypatch.setattr(
        scenarist_handler.social_brain,
        "build_brain_context",
        fake_build,
    )

    result = asyncio.run(
        scenarist_handler._load_brain(7, 202)
    )

    assert result is None


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

    single = asyncio.run(
        scenarist_handler._single_account_id(7)
    )
    ambiguous = asyncio.run(
        scenarist_handler._single_account_id(7)
    )

    assert single == 101
    assert ambiguous is None


def test_scenarist_legacy_prompt_is_unchanged_without_brain(
    monkeypatch,
):
    captured = {}

    async def fake_ask_json(system, user, **kwargs):
        captured.update(system=system, user=user, kwargs=kwargs)
        return PostGenerationResponse(
            hooks=[
                {"type": "insight", "text": "Hook"},
                {"type": "pain", "text": "Hook 2"},
                {"type": "number", "text": "Hook 3"},
            ],
            body="Body",
        )

    monkeypatch.setattr(scenarist, "ask_json", fake_ask_json)
    profile = {"tone": "direct", "taboo": ["fluff"]}

    result = asyncio.run(
        scenarist.generate_post(profile, "topic")
    )

    expected = scenarist.GEN_SYSTEM_TMPL.format(
        profile=scenarist._profile_str(profile),
        hooks=scenarist._HOOKS_TEXT,
    )
    assert captured["system"] == expected
    assert result["body"] == "Body"


def test_scenarist_adds_compact_brain_context(monkeypatch):
    captured = {}

    async def fake_ask_json(system, _user, **_kwargs):
        captured["system"] = system
        return PostGenerationResponse(
            hooks=[
                {"type": "insight", "text": "Hook"},
                {"type": "pain", "text": "Hook 2"},
                {"type": "number", "text": "Hook 3"},
            ],
            body="Body",
        )

    monkeypatch.setattr(scenarist, "ask_json", fake_ask_json)
    profile = {"tone": "direct", "taboo": ["fluff"]}

    asyncio.run(scenarist.generate_post(
        profile,
        "topic",
        brain=rich_brain(),
    ))

    system = captured["system"]
    assert "ДОПОЛНИТЕЛЬНЫЙ SOCIAL BRAIN CONTEXT" in system
    assert '"niche":{"name":"creator tools"' in system
    assert '"goals":{"primary":"qualified growth"}' in system
    assert system.count('"tone":"direct"') == 1
    assert '"voice":' not in system.split(
        "ДОПОЛНИТЕЛЬНЫЙ SOCIAL BRAIN CONTEXT", 1
    )[1]
    assert "source" not in system
    assert "updated_at" not in system
    assert "threads_account_id" not in system


def test_scenarist_keeps_account_voice_facts(monkeypatch):
    captured = {}

    async def fake_ask_json(system, _user, **_kwargs):
        captured["system"] = system
        return PostGenerationResponse(
            hooks=[
                {"type": "insight", "text": "Hook"},
                {"type": "pain", "text": "Hook 2"},
                {"type": "number", "text": "Hook 3"},
            ],
            body="Body",
        )

    monkeypatch.setattr(scenarist, "ask_json", fake_ask_json)
    brain = rich_brain(threads_account_id=202)
    brain.voice.tone = "account-specific"
    brain.voice.facts = [
        BrainFact(
            fact_type="voice",
            key="profile",
            value={"tone": "account-specific"},
            scope="account",
            source="test",
        )
    ]

    asyncio.run(scenarist.generate_post(
        {"tone": "legacy"},
        "topic",
        brain=brain,
    ))

    supplemental = captured["system"].split(
        "ДОПОЛНИТЕЛЬНЫЙ SOCIAL BRAIN CONTEXT",
        1,
    )[1]
    assert '"voice":{' in supplemental
    assert '"tone":"account-specific"' in supplemental
    assert "threads_account_id" not in supplemental


def test_scenarist_uses_legacy_prompt_when_brain_fails(monkeypatch):
    captured = {}

    class BrokenBrain:
        def for_generation(self):
            raise RuntimeError("broken")

    async def fake_ask_json(system, _user, **_kwargs):
        captured["system"] = system
        return PostGenerationResponse(
            hooks=[
                {"type": "insight", "text": "Hook"},
                {"type": "pain", "text": "Hook 2"},
                {"type": "number", "text": "Hook 3"},
            ],
            body="Body",
        )

    monkeypatch.setattr(scenarist, "ask_json", fake_ask_json)
    profile = {"tone": "direct"}

    asyncio.run(scenarist.generate_post(
        profile,
        "topic",
        brain=BrokenBrain(),
    ))

    assert captured["system"] == scenarist.GEN_SYSTEM_TMPL.format(
        profile=scenarist._profile_str(profile),
        hooks=scenarist._HOOKS_TEXT,
    )
