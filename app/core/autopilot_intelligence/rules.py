"""Independent deterministic rules for Autopilot Intelligence."""

from __future__ import annotations

from typing import Protocol

from app.core.autopilot_intelligence.models import (
    ActionType,
    DecisionContext,
    QueueHealth,
    RuleKind,
    RuleResult,
)
from app.core.autopilot_intelligence.optimizer import calculate_health


class DecisionRule(Protocol):
    rule_id: str
    version: int

    def evaluate(self, context: DecisionContext) -> tuple[RuleResult, ...]: ...


def result(
    rule_id: str,
    kind: RuleKind,
    reason_code: str,
    priority: int,
    action: ActionType = ActionType.NONE,
    **parameters,
) -> RuleResult:
    return RuleResult(
        rule_id=rule_id,
        kind=kind,
        reason_code=reason_code,
        priority=priority,
        action=action,
        parameters=parameters,
    )


class PermissionRule:
    rule_id = "permissions"
    version = 1

    def evaluate(self, context: DecisionContext) -> tuple[RuleResult, ...]:
        now = context.current_time
        if context.connection_status != "connected":
            return (result(
                self.rule_id, RuleKind.BLOCKER, "ACCOUNT_DISCONNECTED", 100,
                ActionType.RECONNECT_ACCOUNT,
            ),)
        if not context.has_access_token:
            return (result(
                self.rule_id, RuleKind.BLOCKER, "NO_TOKEN", 100,
                ActionType.RECONNECT_ACCOUNT,
            ),)
        if context.token_expires_at and context.token_expires_at <= now:
            return (result(
                self.rule_id, RuleKind.BLOCKER, "TOKEN_EXPIRED", 100,
                ActionType.RECONNECT_ACCOUNT,
            ),)
        return ()


class CreditsRule:
    rule_id = "credits"
    version = 1

    def evaluate(self, context: DecisionContext) -> tuple[RuleResult, ...]:
        if context.credits_balance <= 0:
            return (result(
                self.rule_id,
                RuleKind.BLOCKER if context.autopilot_active else RuleKind.WARNING,
                "NO_CREDITS",
                88 if context.autopilot_active else 40,
                ActionType.OPEN_BALANCE,
            ),)
        if context.credits_balance < 5:
            return (result(
                self.rule_id, RuleKind.WARNING, "LOW_CREDITS", 55,
                ActionType.OPEN_BALANCE,
                balance=context.credits_balance,
            ),)
        return ()


class QueueRule:
    rule_id = "queue"
    version = 1

    def evaluate(self, context: DecisionContext) -> tuple[RuleResult, ...]:
        if context.queue_health == QueueHealth.DISABLED:
            return (result(
                self.rule_id, RuleKind.INFO, "AUTOPILOT_DISABLED", 10,
            ),)
        if context.queue_health == QueueHealth.RECOVERY_REQUIRED:
            return ()
        if context.queue_health == QueueHealth.EMPTY:
            return (result(
                self.rule_id, RuleKind.ACTION, "QUEUE_EMPTY", 75,
                ActionType.OPEN_QUEUE,
            ),)
        if context.queue_health == QueueHealth.LOW:
            return (result(
                self.rule_id, RuleKind.ACTION, "QUEUE_LOW", 65,
                ActionType.OPEN_QUEUE,
                queue_size=context.queue_size,
            ),)
        if context.queue_health == QueueHealth.FULL:
            return (result(
                self.rule_id, RuleKind.INFO, "QUEUE_FULL", 20,
                queue_size=context.queue_size,
            ),)
        return (result(
            self.rule_id, RuleKind.INFO, "QUEUE_HEALTHY", 5,
            queue_size=context.queue_size,
        ),)


class AnalyticsRule:
    rule_id = "analytics"
    version = 1

    def evaluate(self, context: DecisionContext) -> tuple[RuleResult, ...]:
        summary = context.analytics_summary
        if not summary.available:
            return (result(
                self.rule_id, RuleKind.WARNING, "ANALYTICS_UNAVAILABLE", 35,
                ActionType.OPEN_ANALYTICS,
            ),)
        if summary.stale:
            return (result(
                self.rule_id, RuleKind.WARNING, "ANALYTICS_DELAYED", 45,
                ActionType.OPEN_ANALYTICS,
            ),)
        if summary.posts_total < 5:
            return (result(
                self.rule_id, RuleKind.INFO, "ANALYTICS_COLLECTING", 18,
                posts=summary.posts_total,
            ),)
        values = []
        if summary.engagement_rate == 0:
            values.append(result(
                self.rule_id, RuleKind.WARNING, "LOW_ENGAGEMENT", 58,
                ActionType.OPEN_ANALYTICS,
            ))
        elif summary.brain_score is not None and summary.brain_score < 40:
            values.append(result(
                self.rule_id, RuleKind.WARNING, "LOW_PERFORMANCE", 58,
                ActionType.OPEN_ANALYTICS,
            ))
        if (
            summary.best_topic
            and summary.brain_score is not None
            and summary.brain_score >= 60
        ):
            values.append(result(
                self.rule_id, RuleKind.RECOMMENDATION,
                "GOOD_TOPIC_FOUND", 52, ActionType.OPEN_ANALYTICS,
                topic=summary.best_topic,
            ))
        return tuple(values)


class RadarRule:
    rule_id = "radar"
    version = 1

    def evaluate(self, context: DecisionContext) -> tuple[RuleResult, ...]:
        summary = context.radar_summary
        if not summary.active:
            return (result(
                self.rule_id, RuleKind.INFO, "RADAR_DISABLED", 5,
            ),)
        if summary.last_status == "permission_denied":
            return (result(
                self.rule_id, RuleKind.BLOCKER, "PERMISSION_DENIED", 90,
                ActionType.RECONNECT_ACCOUNT,
                component="radar",
            ),)
        values = []
        if summary.last_status == "failed":
            values.append(result(
                self.rule_id, RuleKind.WARNING, "RADAR_FAILED", 50,
                ActionType.OPEN_RADAR,
            ))
        if summary.ready_count:
            values.append(result(
                self.rule_id, RuleKind.RECOMMENDATION,
                "HOT_TOPIC_FOUND", 72, ActionType.OPEN_RADAR,
                count=summary.ready_count,
                score=summary.best_score,
            ))
        elif summary.search_due:
            values.append(result(
                self.rule_id, RuleKind.ACTION, "RADAR_DELAYED", 42,
                ActionType.OPEN_RADAR,
            ))
        return tuple(values)


class NeuroRule:
    rule_id = "neuro"
    version = 1

    def evaluate(self, context: DecisionContext) -> tuple[RuleResult, ...]:
        summary = context.neuro_summary
        if not summary.active:
            return (result(
                self.rule_id, RuleKind.INFO, "NEURO_DISABLED", 5,
            ),)
        if summary.permission_denied:
            return (result(
                self.rule_id, RuleKind.BLOCKER, "PERMISSION_DENIED", 91,
                ActionType.RECONNECT_ACCOUNT,
                component="neuro",
            ),)
        if summary.unknown_count:
            return (result(
                self.rule_id, RuleKind.BLOCKER, "RECOVERY_REQUIRED", 96,
                ActionType.OPEN_NEURO,
                component="neuro",
            ),)
        values = []
        if summary.pending_count:
            values.append(result(
                self.rule_id, RuleKind.ACTION, "NEURO_QUEUE_READY", 68,
                ActionType.OPEN_NEURO,
                count=summary.pending_count,
            ))
        if summary.failed_count:
            values.append(result(
                self.rule_id, RuleKind.WARNING, "NEURO_FAILED", 48,
                ActionType.OPEN_NEURO,
                count=summary.failed_count,
            ))
        if summary.daily_cap and summary.posted_today >= summary.daily_cap:
            values.append(result(
                self.rule_id, RuleKind.INFO, "NEURO_LIMIT_REACHED", 25,
            ))
        return tuple(values)


class PublishingRule:
    rule_id = "publishing"
    version = 1

    def evaluate(self, context: DecisionContext) -> tuple[RuleResult, ...]:
        if context.failed_today:
            return (result(
                self.rule_id, RuleKind.WARNING, "PUBLISH_FAILED", 86,
                ActionType.OPEN_RECOVERY,
                count=context.failed_today,
            ),)
        return ()


class RecoveryRule:
    rule_id = "recovery"
    version = 1

    def evaluate(self, context: DecisionContext) -> tuple[RuleResult, ...]:
        if context.stuck_publishing or context.unknown_publications:
            return (result(
                self.rule_id, RuleKind.BLOCKER, "RECOVERY_REQUIRED", 98,
                ActionType.OPEN_RECOVERY,
                stuck=context.stuck_publishing,
                unknown=context.unknown_publications,
            ),)
        return ()


class ScheduleRule:
    rule_id = "schedule"
    version = 1

    def evaluate(self, context: DecisionContext) -> tuple[RuleResult, ...]:
        if context.autopilot_active and context.posts_per_day <= 0:
            return (result(
                self.rule_id, RuleKind.WARNING,
                "SCHEDULE_NOT_CONFIGURED", 62, ActionType.OPEN_SCHEDULE,
            ),)
        return ()


class BrainRule:
    rule_id = "brain"
    version = 1

    def evaluate(self, context: DecisionContext) -> tuple[RuleResult, ...]:
        summary = context.brain_summary
        if not summary.available:
            return (result(
                self.rule_id, RuleKind.INFO, "BRAIN_UNAVAILABLE", 8,
            ),)
        if summary.performance_posts < 5:
            return (result(
                self.rule_id, RuleKind.INFO, "BRAIN_COLLECTING", 12,
                posts=summary.performance_posts,
            ),)
        return (result(
            self.rule_id, RuleKind.INFO, "BRAIN_READY", 15,
            goal=context.goal,
        ),)


class SafetyRule:
    rule_id = "safety"
    version = 1

    def evaluate(self, context: DecisionContext) -> tuple[RuleResult, ...]:
        values = []
        if context.subscription.status not in {"active", "trial"}:
            values.append(result(
                self.rule_id, RuleKind.BLOCKER,
                "SUBSCRIPTION_INACTIVE", 94, ActionType.OPEN_BALANCE,
            ))
        if context.autopilot_active and not context.topics:
            values.append(result(
                self.rule_id, RuleKind.WARNING,
                "TOPICS_NOT_CONFIGURED", 60, ActionType.OPEN_SCHEDULE,
            ))
        return tuple(values)


class HealthRule:
    rule_id = "health"
    version = 1

    def evaluate(self, context: DecisionContext) -> tuple[RuleResult, ...]:
        score = calculate_health(context).total
        if score < 50:
            return (result(
                self.rule_id, RuleKind.WARNING, "SYSTEM_HEALTH_LOW", 80,
                score=score,
            ),)
        if score < 75:
            return (result(
                self.rule_id, RuleKind.WARNING,
                "SYSTEM_HEALTH_WARNING", 30, score=score,
            ),)
        return (result(
            self.rule_id, RuleKind.INFO, "SYSTEM_HEALTHY", 1,
            score=score,
        ),)


DEFAULT_RULES: tuple[DecisionRule, ...] = (
    PermissionRule(),
    CreditsRule(),
    QueueRule(),
    AnalyticsRule(),
    RadarRule(),
    NeuroRule(),
    PublishingRule(),
    RecoveryRule(),
    ScheduleRule(),
    BrainRule(),
    SafetyRule(),
    HealthRule(),
)
