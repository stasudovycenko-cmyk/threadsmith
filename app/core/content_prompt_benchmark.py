"""Deterministic Content Engine prompt benchmark; no LLM calls."""

from __future__ import annotations

import json

from app.core.content_engine import (
    ContentMemoryItem,
    build_content_plan,
)
from app.core.context_builder import estimate_tokens
from app.core.scenarist import (
    ContentPromptBenchmark,
    benchmark_content_prompts,
)
from app.schemas.social_brain import BrainTaskContext

PROFILE = {
    "tone": "direct and practical",
    "lexicon": ["конкретно", "без воды", "проверил"],
    "taboo": ["fluff", "empty promises"],
    "sample_phrases": ["Начну с результата."],
}
TOPIC = "Как автоматизировать исследование контента и сохранить голос"
REFERENCE = (
    "Автор описывает конкретный эксперимент, называет исходную проблему, "
    "показывает одно решение и заканчивает практическим выводом."
)


def _memory() -> list[ContentMemoryItem]:
    return [
        ContentMemoryItem(
            opening=f"Недавний заход {index} про систему контента",
            angle="observation" if index % 2 else "mistake",
            hook_type="insight" if index % 2 else "pain",
            topic=f"История {index}",
            format="observation",
            source="autocontent",
        )
        for index in range(1, 7)
    ]


def _brain(
    goal: str,
    *,
    include_patterns: bool,
    include_performance: bool,
) -> BrainTaskContext:
    metric = "engagement_rate" if goal == "engagement" else "views"
    patterns = []
    if include_patterns:
        patterns = [
            {
                "kind": "hook_type",
                "key": "question" if goal == "engagement" else "insight",
                "metric": metric,
                "lift": 0.18,
                "samples": 8,
                "confidence": 0.82,
            },
            {
                "kind": "format",
                "key": "observation",
                "metric": metric,
                "lift": 0.12,
                "samples": 7,
                "confidence": 0.76,
            },
        ]
    performance = {}
    if include_performance:
        performance = {
            "feedback_v1": {
                "posts_analyzed": 12,
                "metrics": {
                    metric: {
                        "median_lift": 0.11,
                        "latest": {"lift": 0.08},
                    },
                },
            },
        }
    context = {
        "dna": {
            "voice": {"tone": "direct and practical"},
            "positioning": "практик системного контента",
        },
        "audience": {
            "niche": "creator tools",
            "keywords": ["automation", "growth"],
        },
        "primary_goal": goal,
        "critical_constraints": [
            "Не выдумывать цифры",
            "Не обещать гарантированный результат",
        ],
        "patterns": patterns,
        "performance": performance,
    }
    return BrainTaskContext(
        task="generation",
        context=context,
        budget_tokens=800,
        estimated_tokens=estimate_tokens(context),
        brain_id=11,
        brain_version=4,
        pattern_ids=[91, 92] if patterns else [],
        pattern_keys=[
            f"{item['kind']}:{item['key']}:{item['metric']}"
            for item in patterns
        ],
    )


def run_benchmark_scenarios() -> dict[str, ContentPromptBenchmark]:
    """Run the six acceptance scenarios without network or database access."""
    memory = _memory()
    scenarios = {
        "A_no_brain_no_memory": (None, [], None, "reach"),
        "B_brain_no_memory": (
            _brain("reach", include_patterns=False, include_performance=False),
            [],
            None,
            None,
        ),
        "C_brain_memory": (
            _brain("reach", include_patterns=False, include_performance=False),
            memory,
            None,
            None,
        ),
        "D_brain_patterns_performance": (
            _brain("reach", include_patterns=True, include_performance=True),
            memory,
            None,
            None,
        ),
        "E_reference": (
            _brain("reach", include_patterns=True, include_performance=True),
            memory,
            REFERENCE,
            None,
        ),
        "F_engagement": (
            _brain(
                "engagement",
                include_patterns=True,
                include_performance=True,
            ),
            memory,
            None,
            None,
        ),
    }
    results = {}
    for name, (brain, scenario_memory, reference, fallback_goal) in (
        scenarios.items()
    ):
        plan = build_content_plan(
            profile=PROFILE,
            topic=TOPIC,
            brain=brain,
            memory=scenario_memory,
            fallback_goal=fallback_goal,
        )
        results[name] = benchmark_content_prompts(
            profile=PROFILE,
            topic=TOPIC,
            plan=plan,
            brain=brain,
            memory=scenario_memory,
            reference=reference,
            recent=[item.opening for item in scenario_memory],
            goal=fallback_goal,
        )
    return results


def main() -> None:
    print(json.dumps(
        {
            name: result.as_dict()
            for name, result in run_benchmark_scenarios().items()
        },
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
