"""Pure orchestration for deterministic rules and optimization."""

from collections.abc import Iterable

from app.core.autopilot_intelligence.models import DecisionContext, DecisionResult
from app.core.autopilot_intelligence.optimizer import (
    DecisionOptimizer,
    calculate_health,
)
from app.core.autopilot_intelligence.rules import DEFAULT_RULES, DecisionRule


class AutopilotIntelligenceEngine:
    def __init__(
        self,
        rules: Iterable[DecisionRule] = DEFAULT_RULES,
        *,
        optimizer: DecisionOptimizer | None = None,
    ):
        self.rules = tuple(rules)
        self.optimizer = optimizer or DecisionOptimizer()

    def evaluate(self, context: DecisionContext) -> DecisionResult:
        results = tuple(
            result
            for rule in self.rules
            for result in rule.evaluate(context)
        )
        return self.optimizer.optimize(
            context,
            results,
            calculate_health(context),
        )
