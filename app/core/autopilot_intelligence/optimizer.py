"""Health calculation and deterministic conflict optimization."""

from __future__ import annotations

from datetime import timedelta, timezone
from typing import Iterable

from app.core.autopilot_intelligence.localization import reason_message
from app.core.autopilot_intelligence.models import (
    ActionType,
    DecisionContext,
    DecisionResult,
    DecisionStatus,
    HealthBreakdown,
    QueueHealth,
    RuleKind,
    RuleResult,
)

HEALTH_WEIGHTS = {
    "token": 20,
    "credits": 15,
    "queue": 20,
    "analytics": 15,
    "radar": 10,
    "neuro": 10,
    "publishing": 10,
}

_KIND_ORDER = {
    RuleKind.INFO: 0,
    RuleKind.WARNING: 1,
    RuleKind.RECOMMENDATION: 2,
    RuleKind.ACTION: 3,
    RuleKind.BLOCKER: 4,
}


def calculate_health(context: DecisionContext) -> HealthBreakdown:
    token = HEALTH_WEIGHTS["token"] if context.publisher_enabled else 0

    if context.credits_balance <= 0:
        credits = 0
    elif context.credits_balance < 5:
        credits = 8
    else:
        credits = HEALTH_WEIGHTS["credits"]

    if context.queue_health == QueueHealth.DISABLED:
        queue = HEALTH_WEIGHTS["queue"]
    elif context.queue_health == QueueHealth.RECOVERY_REQUIRED:
        queue = 0
    elif context.queue_health == QueueHealth.EMPTY:
        queue = 5
    elif context.queue_health == QueueHealth.LOW:
        queue = 12
    elif context.queue_health == QueueHealth.FULL:
        queue = 17
    else:
        queue = HEALTH_WEIGHTS["queue"]

    analytics_data = context.analytics_summary
    if not analytics_data.available:
        analytics = 5
    elif analytics_data.stale:
        analytics = 8
    elif analytics_data.posts_total < 5:
        analytics = 10
    else:
        analytics = HEALTH_WEIGHTS["analytics"]

    radar_data = context.radar_summary
    if not radar_data.active:
        radar = HEALTH_WEIGHTS["radar"]
    elif radar_data.last_status == "permission_denied":
        radar = 0
    elif radar_data.last_status == "failed":
        radar = 4
    elif radar_data.search_due:
        radar = 7
    else:
        radar = HEALTH_WEIGHTS["radar"]

    neuro_data = context.neuro_summary
    if not neuro_data.active:
        neuro = HEALTH_WEIGHTS["neuro"]
    elif neuro_data.permission_denied:
        neuro = 0
    elif neuro_data.unknown_count:
        neuro = 3
    elif neuro_data.failed_count:
        neuro = 6
    elif neuro_data.pending_count:
        neuro = 8
    else:
        neuro = HEALTH_WEIGHTS["neuro"]

    if not context.publisher_enabled:
        publishing = 0
    elif context.stuck_publishing or context.unknown_publications:
        publishing = 0
    elif context.failed_today:
        publishing = 3
    else:
        publishing = HEALTH_WEIGHTS["publishing"]

    total = token + credits + queue + analytics + radar + neuro + publishing
    return HealthBreakdown(
        token=token,
        credits=credits,
        queue=queue,
        analytics=analytics,
        radar=radar,
        neuro=neuro,
        publishing=publishing,
        total=total,
    )


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


class DecisionOptimizer:
    """Choose one final recommendation from deterministic rule results."""

    def optimize(
        self,
        context: DecisionContext,
        results: Iterable[RuleResult],
        health: HealthBreakdown,
    ) -> DecisionResult:
        values = tuple(results)
        ordered = tuple(sorted(
            values,
            key=lambda item: (
                -_KIND_ORDER[item.kind],
                -item.priority,
                item.reason_code,
                item.rule_id,
            ),
        ))
        blockers = tuple(
            item.reason_code for item in ordered
            if item.kind == RuleKind.BLOCKER
        )
        warnings = tuple(
            item.reason_code for item in ordered
            if item.kind == RuleKind.WARNING
        )
        actionable = tuple(
            item for item in ordered
            if item.action != ActionType.NONE
        )
        selected = (
            next(
                (item for item in actionable if item.kind == RuleKind.BLOCKER),
                None,
            )
            or (max(actionable, key=lambda item: item.priority) if actionable else None)
            or (ordered[0] if ordered else None)
        )
        if selected is None:
            selected = RuleResult(
                rule_id="optimizer.fallback",
                kind=RuleKind.INFO,
                reason_code="SYSTEM_HEALTHY",
            )

        if blockers:
            status = DecisionStatus.BLOCKED
        elif (
            selected.reason_code == "ANALYTICS_UNAVAILABLE"
            and health.total >= 70
        ):
            status = DecisionStatus.INSUFFICIENT_DATA
        elif selected.reason_code == "AUTOPILOT_DISABLED" and not warnings:
            status = DecisionStatus.WAITING
        elif selected.kind in {
            RuleKind.WARNING,
            RuleKind.RECOMMENDATION,
            RuleKind.ACTION,
        }:
            status = DecisionStatus.ATTENTION
        else:
            status = DecisionStatus.HEALTHY

        now = context.current_time.astimezone(timezone.utc).replace(microsecond=0)
        reason_codes = _unique(item.reason_code for item in ordered)
        return DecisionResult(
            status=status,
            health_score=health.total,
            health_breakdown=health,
            priority=selected.priority,
            recommendation=selected.reason_code,
            reason_codes=reason_codes,
            warnings=_unique(warnings),
            blockers=_unique(blockers),
            safe_action=selected.action,
            next_check=now + timedelta(minutes=15),
            next_recommended_action=selected.action,
            human_message=reason_message(selected.reason_code),
            generated_at=now,
        )
