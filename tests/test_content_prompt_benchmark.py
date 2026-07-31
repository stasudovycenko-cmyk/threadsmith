from app.core import scenarist
from app.core.content_engine import (
    build_content_plan,
    compact_memory_prompt,
)
from app.core.content_prompt_benchmark import (
    PROFILE,
    REFERENCE,
    TOPIC,
    _brain,
    _memory,
    run_benchmark_scenarios,
)
from app.core.context_builder import estimate_text_tokens
from app.schemas.content_engine import (
    ContentGenerationDraft,
    ContentGenerationResponse,
)


def test_benchmark_covers_acceptance_scenarios_deterministically():
    first = {
        name: result.as_dict()
        for name, result in run_benchmark_scenarios().items()
    }
    second = {
        name: result.as_dict()
        for name, result in run_benchmark_scenarios().items()
    }
    assert first == second
    assert set(first) == {
        "A_no_brain_no_memory",
        "B_brain_no_memory",
        "C_brain_memory",
        "D_brain_patterns_performance",
        "E_reference",
        "F_engagement",
    }


def test_optimized_prompt_meets_token_targets():
    results = run_benchmark_scenarios()
    for result in results.values():
        assert result.optimized_tokens <= result.pre_optimization_tokens * 0.75
    assert (
        results["A_no_brain_no_memory"].optimized_vs_legacy_percent
        <= 5.0
    )
    assert results["B_brain_no_memory"].optimized_vs_legacy_percent <= 15.0
    rich = results["D_brain_patterns_performance"].optimized_breakdown
    assert rich["brain"] <= 350
    assert rich["memory"] <= 250


def test_compact_projection_keeps_only_generation_relevant_brain_data():
    brain = _brain("reach", include_patterns=True, include_performance=True)
    plan = build_content_plan(
        profile=PROFILE,
        topic=TOPIC,
        brain=brain,
        memory=_memory(),
        source="autocontent",
    )
    prompt = scenarist._optimized_generation_prompt(
        profile=PROFILE,
        plan=plan,
        memory=_memory(),
        reference=REFERENCE,
    )
    assert "patterns=hook=insight(+18%),format=observation(+12%)" in prompt.brain
    assert "performance=views,n=12,median=+11%,latest=+8%" in prompt.brain
    assert "pattern_ids" not in prompt.user
    assert "pattern_keys" not in prompt.user
    assert "threads_account_id" not in prompt.user
    assert "source=autocontent" not in prompt.user
    assert "desired_action" not in prompt.user
    assert scenarist._HOOKS_TEXT not in prompt.system
    assert REFERENCE in prompt.reference


def test_prompt_memory_is_small_while_repository_memory_can_stay_longer():
    memory = _memory()
    compact = compact_memory_prompt(memory)
    assert memory[3].opening in compact
    assert memory[4].opening not in compact
    assert "История 1" not in compact
    assert "autocontent" not in compact
    assert estimate_text_tokens(compact) <= 250


def test_llm_draft_omits_python_injected_public_fields():
    assert set(ContentGenerationDraft.model_fields) == {
        "hooks",
        "body",
        "selected_hook_index",
        "specificity",
    }
    assert {
        "brief",
        "metadata",
        "quality",
    }.issubset(ContentGenerationResponse.model_fields)
