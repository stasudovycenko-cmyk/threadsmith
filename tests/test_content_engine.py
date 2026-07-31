import asyncio
import json
import logging
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.bot.handlers.scenarist import _render
from app.core import scenarist
from app.core.ai_cost import AIUsageContext
from app.core.content_engine import (
    CONTENT_ANGLES,
    ContentMemoryItem,
    ContentMemoryRepo,
    build_content_plan,
    quality_gate,
    repeated_reason,
)
from app.core.llm import LLMGuardError
from app.schemas.content_engine import (
    ContentBrief,
    ContentGenerationDraft,
    ContentGenerationResponse,
)
from app.schemas.social_brain import BrainTaskContext

ROOT = Path(__file__).resolve().parents[1]


def content_response(
    *,
    body: str = (
        "Specific evidence and a useful conclusion make this draft concrete "
        "enough for publication."
    ),
    opening: str = "A concrete opening worth reading",
    angle: str = "observation",
    content_format: str = "observation",
    goal: str = "reach",
    selected_index: int = 0,
    has_cta: bool = False,
) -> ContentGenerationResponse:
    hooks = [
        {
            "type": "insight",
            "text": opening,
            "intent": "stop the right reader",
        },
        {
            "type": "pain",
            "text": "This familiar mistake has a cost",
            "intent": "name a relevant pain",
        },
        {
            "type": "number",
            "text": "Three signals reveal the problem",
            "intent": "promise concrete evidence",
        },
    ]
    return ContentGenerationResponse(
        brief={
            "goal": goal,
            "topic": "creator research",
            "angle": angle,
            "format": content_format,
            "source": "manual",
        },
        hooks=hooks,
        body=body,
        metadata={
            "goal": goal,
            "angle": angle,
            "hook_type": hooks[selected_index]["type"],
            "format": content_format,
            "topic": "creator research",
            "has_cta": has_cta,
            "source": "manual",
            "selected_hook_index": selected_index,
        },
        quality={
            "clarity": 0.8,
            "hook_strength": 0.8,
            "specificity": 0.8,
            "voice_match": 0.8,
            "goal_fit": 0.8,
        },
    )


def draft_response(
    *,
    body: str = (
        "Specific evidence and a useful conclusion make this draft concrete "
        "enough for publication."
    ),
    opening: str = "A concrete opening worth reading",
    selected_index: int = 0,
    specificity: float = 0.8,
) -> ContentGenerationDraft:
    return ContentGenerationDraft(
        hooks=[
            {"type": "insight", "text": opening},
            {"type": "pain", "text": "This familiar mistake has a cost"},
            {"type": "number", "text": "Three signals reveal the problem"},
        ],
        body=body,
        selected_hook_index=selected_index,
        specificity=specificity,
    )


def brain_context(
    goal: str,
    *,
    patterns: list[dict] | None = None,
    pattern_ids: list[int] | None = None,
    pattern_keys: list[str] | None = None,
) -> BrainTaskContext:
    return BrainTaskContext(
        task="generation",
        context={
            "dna": {"voice": {"tone": "direct"}},
            "audience": {"niche": "creator tools"},
            "primary_goal": goal,
            "patterns": patterns or [],
            "performance": {
                "metrics": {
                    "views": {"posts_analyzed": 12},
                },
            },
        },
        budget_tokens=800,
        estimated_tokens=80,
        brain_version=4,
        pattern_ids=pattern_ids or [],
        pattern_keys=pattern_keys or [],
    )


class RowsResult:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def mappings(self):
        return self

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


class AccountMemorySession:
    def __init__(self, scheduled_by_account=None, generation_by_account=None):
        self.scheduled_by_account = scheduled_by_account or {}
        self.generation_by_account = generation_by_account or {}
        self.calls = []

    async def execute(self, statement, params):
        sql = str(statement)
        self.calls.append((sql, dict(params)))
        account_id = int(params["account_id"])
        if "FROM scheduled_posts" in sql:
            return RowsResult(self.scheduled_by_account.get(account_id, []))
        return RowsResult(self.generation_by_account.get(account_id, []))


def test_content_brief_schema_is_typed_and_compact():
    brief = ContentBrief(
        goal="reach",
        topic="AI workflows",
        angle="contrarian",
        constraints=["No fabricated claims"],
    )
    assert brief.angle == "contrarian"
    assert brief.pattern_hints == []
    with pytest.raises(ValidationError):
        ContentBrief(goal="reach", topic="x", unexpected="value")


def test_compact_generation_draft_requires_exactly_three_hooks():
    with pytest.raises(ValidationError):
        ContentGenerationDraft(
            hooks=[
                {"type": "insight", "text": "One"},
                {"type": "pain", "text": "Two"},
            ],
            body="A complete body",
            selected_hook_index=0,
            specificity=0.8,
        )


def test_reach_goal_selects_views_patterns():
    patterns = [{
        "kind": "length_bucket",
        "key": "long",
        "metric": "views",
        "lift": 0.2,
        "samples": 8,
        "confidence": 0.82,
    }]
    plan = build_content_plan(
        profile={},
        topic="AI workflows",
        brain=brain_context("reach", patterns=patterns),
    )
    assert plan.goal_metric == "views"
    assert "share" in plan.brief.desired_action
    assert "views" in plan.brief.pattern_hints[0]


def test_engagement_goal_selects_engagement_patterns_and_cta_intent():
    patterns = [{
        "kind": "hook_type",
        "key": "question",
        "metric": "engagement_rate",
        "lift": 0.18,
        "samples": 7,
        "confidence": 0.79,
    }]
    plan = build_content_plan(
        profile={},
        topic="AI workflows",
        brain=brain_context("engagement", patterns=patterns),
    )
    assert plan.goal_metric == "engagement_rate"
    assert "comments" in plan.brief.desired_action
    assert "engagement_rate" in plan.brief.pattern_hints[0]


def test_unsupported_goal_is_editorial_only():
    patterns = [{
        "kind": "hook_type",
        "key": "question",
        "metric": "views",
        "lift": 0.4,
        "samples": 20,
        "confidence": 0.9,
    }]
    plan = build_content_plan(
        profile={},
        topic="AI workflows",
        brain=brain_context("followers", patterns=patterns),
    )
    assert plan.brief.goal == "followers"
    assert plan.goal_metric is None
    assert plan.brief.pattern_hints == []
    assert "without feedback-metric optimization" in (
        plan.brief.desired_action
    )


def test_angle_and_hook_strategy_are_deterministic_metadata():
    plan = build_content_plan(
        profile={},
        topic="AI workflows",
        brain=brain_context("reach"),
    )
    assert plan.brief.angle in CONTENT_ANGLES
    assert plan.brief.hook_strategy
    assert plan.brief.format


def test_mature_pattern_metadata_is_preserved():
    pattern = {
        "kind": "hook_type",
        "key": "insight",
        "metric": "views",
        "lift": 0.12,
        "samples": 6,
        "confidence": 0.74,
    }
    plan = build_content_plan(
        profile={},
        topic="AI workflows",
        brain=brain_context(
            "reach",
            patterns=[pattern],
            pattern_ids=[91],
            pattern_keys=["hook_type:insight:views"],
        ),
    )
    assert plan.pattern_ids == (91,)
    assert plan.pattern_keys == ("hook_type:insight:views",)
    assert "preference hint, not a rule" in plan.brief.pattern_hints[0]
    assert plan.brief.performance_context == "views,n=12"


@pytest.mark.parametrize(
    "pattern",
    [
        {
            "kind": "hook_type",
            "key": "insight",
            "metric": "views",
            "lift": 0.2,
            "samples": 4,
            "confidence": 0.9,
        },
        {
            "kind": "hook_type",
            "key": "insight",
            "metric": "views",
            "lift": 0.2,
            "samples": 8,
            "confidence": 0.69,
        },
        {
            "kind": "hook_type",
            "key": "insight",
            "metric": "engagement_rate",
            "lift": 0.2,
            "samples": 8,
            "confidence": 0.9,
        },
    ],
    ids=["immature-samples", "immature-confidence", "wrong-metric"],
)
def test_immature_and_wrong_metric_patterns_are_ignored(pattern):
    plan = build_content_plan(
        profile={},
        topic="AI workflows",
        brain=brain_context("reach", patterns=[pattern]),
    )
    assert plan.brief.pattern_hints == []


def test_content_memory_loads_recent_account_history():
    session = AccountMemorySession(
        scheduled_by_account={
            11: [{
                "text": "Scheduled opening\n\nBody",
                "content_metadata": {
                    "angle": "mistake",
                    "hook_type": "pain",
                    "topic": "automation",
                    "format": "compact_post",
                    "source": "autocontent",
                },
            }],
        },
        generation_by_account={
            11: [{
                "output": {
                    "selected_hook": {"text": "Generated opening"},
                    "metadata": {
                        "angle": "observation",
                        "hook_type": "insight",
                        "topic": "research",
                        "format": "observation",
                        "source": "manual",
                    },
                },
            }],
        },
    )
    memory = asyncio.run(ContentMemoryRepo(session).load(7, 11))
    assert [item.opening for item in memory] == [
        "Scheduled opening",
        "Generated opening",
    ]
    assert all(call[1]["uid"] == 7 for call in session.calls)
    assert all(int(call[1]["account_id"]) == 11 for call in session.calls)


def test_content_memory_isolates_second_account():
    session = AccountMemorySession(
        scheduled_by_account={
            11: [{"text": "First account", "content_metadata": None}],
            22: [{"text": "Second account", "content_metadata": None}],
        },
    )
    first = asyncio.run(ContentMemoryRepo(session).load(7, 11))
    second = asyncio.run(ContentMemoryRepo(session).load(7, 22))
    assert [item.opening for item in first] == ["First account"]
    assert [item.opening for item in second] == ["Second account"]


def test_ambiguous_account_never_queries_mixed_memory():
    class NoQuerySession:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("ambiguous account must not query history")

    memory = asyncio.run(ContentMemoryRepo(NoQuerySession()).load(7, None))
    assert memory == []


def test_anti_repeat_detects_exact_duplicate():
    response = content_response(opening="Same opening!")
    memory = [ContentMemoryItem(opening="  same opening  ")]
    assert repeated_reason(response, memory) == "repeated_opening_exact"


def test_anti_repeat_detects_similar_opening():
    response = content_response(
        opening="Three ways to make creator research faster today"
    )
    memory = [ContentMemoryItem(
        opening="Three ways to make creator research faster",
    )]
    assert repeated_reason(response, memory) == "repeated_opening_similar"


def test_anti_repeat_gate_checks_memory_beyond_prompt_window():
    response = content_response(opening="The sixth opening still matters")
    memory = [
        ContentMemoryItem(opening=f"Unrelated opening {index}")
        for index in range(5)
    ]
    memory.append(ContentMemoryItem(opening="The sixth opening still matters"))
    assert repeated_reason(response, memory) == "repeated_opening_exact"


def test_different_angle_and_opening_are_allowed():
    response = content_response(
        opening="A fresh comparison changes the decision",
        angle="comparison",
        content_format="comparison",
    )
    memory = [ContentMemoryItem(
        opening="My research workflow failed last week",
        angle="mistake",
        hook_type="pain",
        topic="creator research",
        format="compact_post",
    )]
    assert repeated_reason(response, memory) is None
    assert quality_gate(response, memory=memory).passed


def test_deterministic_quality_gate_fails_with_reasons():
    response = content_response(
        body="Short",
        opening="Repeated opening",
    )
    memory = [ContentMemoryItem(opening="Repeated opening")]
    gate = quality_gate(response, memory=memory)
    assert not gate.passed
    assert "empty_or_too_short_body" in gate.reasons
    assert "repeated_opening_exact" in gate.reasons


def test_quality_gate_keeps_banned_phrase_and_length_checks():
    banned = quality_gate(content_response(
        body="Вот что понял: конкретный вывод с достаточной длиной текста.",
    ))
    too_long = quality_gate(content_response(body="x" * 500))
    assert "banned_phrase" in banned.reasons
    assert "post_too_long" in too_long.reasons


def test_deterministic_quality_gate_passes_publishable_content():
    assert quality_gate(content_response()).passed


def test_selected_hook_and_metadata_are_canonicalized(monkeypatch):
    calls = []

    async def fake_ask_json(_system, _user, **kwargs):
        calls.append(kwargs)
        return draft_response(selected_index=1)

    monkeypatch.setattr(scenarist, "ask_json", fake_ask_json)
    result = asyncio.run(scenarist.generate_post(
        {},
        "AI workflows",
        usage_context=AIUsageContext(
            user_id=7,
            threads_account_id=11,
        ),
    ))
    assert result["selected_hook"]["index"] == 1
    assert result["selected_hook"]["type"] == "pain"
    assert result["metadata"]["hook_type"] == "pain"
    assert result["metadata"]["selected_hook"] == result["hooks"][1]["text"]
    assert result["metadata"]["user_id"] == 7
    assert result["metadata"]["threads_account_id"] == 11
    assert calls[0]["response_model"] is ContentGenerationDraft


def test_generation_uses_cost_engine_feature_and_one_normal_call(monkeypatch):
    calls = []

    async def fake_ask_json(_system, _user, **kwargs):
        calls.append(kwargs)
        return draft_response()

    monkeypatch.setattr(scenarist, "ask_json", fake_ask_json)
    result = asyncio.run(scenarist.generate_post({}, "AI workflows"))
    assert result["quality_gate"]["passed"]
    assert [call["feature"] for call in calls] == ["content_generate"]


def test_repair_uses_cost_engine_and_runs_at_most_once(monkeypatch):
    calls = []
    responses = [
        draft_response(body="x" * 500),
        draft_response(),
    ]

    async def fake_ask_json(_system, user, **kwargs):
        calls.append((user, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(scenarist, "ask_json", fake_ask_json)
    result = asyncio.run(scenarist.generate_post({}, "AI workflows"))
    assert result["metadata"]["pipeline_stage"] == "repair"
    assert [call[1]["feature"] for call in calls] == [
        "content_generate",
        "content_repair",
    ]
    assert "post_too_long" in calls[1][0]


def test_engagement_cta_policy_survives_compact_response(monkeypatch):
    responses = [
        draft_response(),
        draft_response(body=(
            "Конкретный вывод из эксперимента. Напиши в комментариях, "
            "какой подход сработал у тебя."
        )),
    ]

    async def fake_ask_json(*_args, **_kwargs):
        return responses.pop(0)

    monkeypatch.setattr(scenarist, "ask_json", fake_ask_json)
    result = asyncio.run(scenarist.generate_post(
        {},
        "AI workflows",
        brain=brain_context("engagement"),
    ))
    assert result["metadata"]["pipeline_stage"] == "repair"
    assert result["metadata"]["has_cta"] is True
    assert result["metadata"]["cta_type"] == "comment"


def test_second_quality_failure_stops_after_one_repair(monkeypatch):
    calls = []

    async def fake_ask_json(_system, _user, **kwargs):
        calls.append(kwargs["feature"])
        return draft_response(body="x" * 500)

    monkeypatch.setattr(scenarist, "ask_json", fake_ask_json)
    with pytest.raises(scenarist.ContentQualityError):
        asyncio.run(scenarist.generate_post({}, "AI workflows"))
    assert calls == ["content_generate", "content_repair"]


def test_budget_guard_blocks_repair_without_fallback_call(monkeypatch):
    calls = []

    async def fake_ask_json(_system, _user, **kwargs):
        calls.append(kwargs["feature"])
        if kwargs["feature"] == "content_repair":
            raise LLMGuardError("budget exhausted")
        return draft_response(body="x" * 500)

    monkeypatch.setattr(scenarist, "ask_json", fake_ask_json)
    with pytest.raises(LLMGuardError, match="budget exhausted"):
        asyncio.run(scenarist.generate_post({}, "AI workflows"))
    assert calls == ["content_generate", "content_repair"]


def test_old_generate_post_caller_and_public_shape_remain_compatible(
    monkeypatch,
):
    async def fake_ask_json(*_args, **_kwargs):
        return draft_response()

    monkeypatch.setattr(scenarist, "ask_json", fake_ask_json)
    result = asyncio.run(scenarist.generate_post(
        {"tone": "direct"},
        "AI workflows",
        None,
        ["An unrelated old opening"],
        "reach",
    ))
    assert len(result["hooks"]) == 3
    assert isinstance(result["body"], str)


def test_renderer_ignores_additional_content_engine_fields(monkeypatch):
    async def fake_ask_json(*_args, **_kwargs):
        return draft_response()

    monkeypatch.setattr(scenarist, "ask_json", fake_ask_json)
    result = asyncio.run(scenarist.generate_post({}, "AI workflows"))
    rendered = _render(result)
    assert "Варианты первой строки" in rendered
    assert result["body"] in rendered


def test_autocontent_uses_same_engine_and_preserves_metadata(monkeypatch):
    calls = []

    async def fake_ask_json(_system, _user, **kwargs):
        calls.append(kwargs["feature"])
        return draft_response()

    monkeypatch.setattr(scenarist, "ask_json", fake_ask_json)
    result = asyncio.run(scenarist.generate_post(
        {},
        "AI workflows",
        feature="autocontent",
        source="autocontent",
    ))
    assert calls == ["autocontent"]
    assert result["metadata"]["source"] == "autocontent"
    assert result["selected_hook"]["text"]


def test_generation_metadata_is_stored_in_existing_generations_json():
    class InsertSession:
        def __init__(self):
            self.params = None

        async def execute(self, _statement, params):
            self.params = params
            return RowsResult([(71,)])

    output = {
        "hooks": [{"type": "insight", "text": "Opening"}],
        "body": "Body",
        "metadata": {
            "goal": "reach",
            "angle": "observation",
            "hook_type": "insight",
            "format": "observation",
            "topic": "AI workflows",
            "selected_hook": "Opening",
        },
    }
    session = InsertSession()
    generation_id = asyncio.run(scenarist.log_generation(
        session,
        7,
        "generate_post",
        {"topic": "AI workflows"},
        output,
        1,
    ))
    stored = json.loads(session.params["o"])
    assert generation_id == 71
    assert stored["metadata"]["angle"] == "observation"
    assert stored["metadata"]["selected_hook"] == "Opening"


def test_storage_migration_adds_nullable_json_without_a_new_table():
    migration = (
        ROOT / "migrations" / "007_content_engine_v2.sql"
    ).read_text(encoding="utf-8").casefold()
    rollback = (
        ROOT / "migrations" / "rollback" / "007_content_engine_v2.sql"
    ).read_text(encoding="utf-8").casefold()
    assert "add column if not exists content_metadata jsonb" in migration
    assert "create table" not in migration
    assert "drop column if exists content_metadata" in rollback


def test_missing_brain_falls_back_to_compact_plan(monkeypatch):
    class BrokenBrain:
        estimated_tokens = 0

        def compact_dict(self):
            raise RuntimeError("unavailable")

    async def fake_ask_json(*_args, **_kwargs):
        return draft_response()

    monkeypatch.setattr(scenarist, "ask_json", fake_ask_json)
    result = asyncio.run(scenarist.generate_post(
        {},
        "AI workflows",
        brain=BrokenBrain(),
    ))
    assert result["metadata"]["goal"] == "unknown"
    assert result["hooks"]


def test_new_prompt_growth_stays_within_target(monkeypatch, caplog):
    async def fake_ask_json(*_args, **_kwargs):
        return draft_response()

    monkeypatch.setattr(scenarist, "ask_json", fake_ask_json)
    with caplog.at_level(logging.INFO, logger="scenarist"):
        asyncio.run(scenarist.generate_post(
            {"tone": "direct", "taboo": ["fluff"]},
            "How creators can automate research without losing their voice",
        ))
    message = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("content_engine_prompt_sizes")
    )
    delta = float(
        re.search(
            r"delta_vs_legacy_percent=([-0-9.]+)",
            message,
        ).group(1)
    )
    assert delta <= 15.0
