"""Account-scoped Autocontent cost guard and content-free telemetry."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.autocontent import CostGuardReason, RepairReason

log = logging.getLogger("autocontent.cost")

REPAIR_WINDOW_SIZE = 20
REPAIR_RATE_THRESHOLD = 0.70
REPAIR_CIRCUIT_COOLDOWN = timedelta(minutes=30)
REPAIR_CIRCUIT_MESSAGE = (
    "Автогенерация временно приостановлена из-за частых исправлений. "
    "Следующая безопасная попытка будет выполнена автоматически."
)


@dataclass(frozen=True)
class RepairWindow:
    samples: int = 0
    repairs: int = 0
    latest_generation_at: datetime | None = None

    @property
    def rate(self) -> float:
        return self.repairs / self.samples if self.samples else 0.0


@dataclass(frozen=True)
class CostGuardDecision:
    blocked: bool
    reason: CostGuardReason | None = None
    retry_at: datetime | None = None
    window: RepairWindow = RepairWindow()


@dataclass
class PlannerAccountTelemetry:
    account_id: int
    deficit_before: int = 0
    slots_claimed: int = 0
    generated: int = 0
    repaired: int = 0
    failed: int = 0
    deficit_after: int = 0


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


class AutocontentCostGuard:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _window(self, account_id: int) -> RepairWindow:
        row = (
            await self.session.execute(
                text("""
                    WITH recent_generations AS (
                      SELECT request_id, run_id, created_at
                      FROM ai_usage_events
                      WHERE threads_account_id = :account_id
                        AND feature = 'autocontent'
                        AND attempt = 1
                        AND status IN ('success', 'failure')
                        AND run_id IS NOT NULL
                      ORDER BY created_at DESC, id DESC
                      LIMIT :window_size
                    )
                    SELECT
                      count(*) AS samples,
                      count(*) FILTER (WHERE EXISTS (
                        SELECT 1
                        FROM ai_usage_events repair
                        WHERE repair.threads_account_id = :account_id
                          AND repair.feature = 'autocontent_repair'
                          AND repair.attempt = 1
                          AND repair.run_id = recent_generations.run_id
                      )) AS repairs,
                      max(created_at) AS latest_generation_at
                    FROM recent_generations
                """),
                {
                    "account_id": account_id,
                    "window_size": REPAIR_WINDOW_SIZE,
                },
            )
        ).mappings().first()
        if not row:
            return RepairWindow()
        return RepairWindow(
            samples=int(row.get("samples") or 0),
            repairs=int(row.get("repairs") or 0),
            latest_generation_at=_aware(row.get("latest_generation_at")),
        )

    async def check(
        self,
        user_id: int,
        account_id: int,
        *,
        now: datetime | None = None,
    ) -> CostGuardDecision:
        current = _aware(now or datetime.now(timezone.utc))
        state = (
            await self.session.execute(
                text("""
                    SELECT cost_guard_until, cost_guard_reason,
                           cost_guard_observed_at
                    FROM autocontent_settings
                    WHERE user_id = :user_id
                      AND threads_account_id = :account_id
                      AND active
                """),
                {"user_id": user_id, "account_id": account_id},
            )
        ).mappings().first()
        if not state:
            return CostGuardDecision(blocked=False)

        guard_until = _aware(state.get("cost_guard_until"))
        reason_value = state.get("cost_guard_reason")
        reason = CostGuardReason(reason_value) if reason_value else None
        if guard_until and guard_until > current:
            return CostGuardDecision(
                blocked=True,
                reason=reason,
                retry_at=guard_until,
            )

        window = await self._window(account_id)
        is_high = (
            window.samples >= REPAIR_WINDOW_SIZE
            and window.rate > REPAIR_RATE_THRESHOLD
        )
        if not is_high:
            if reason is not None:
                await self._clear(user_id, account_id)
            return CostGuardDecision(blocked=False, window=window)

        observed_at = _aware(state.get("cost_guard_observed_at"))
        if (
            guard_until is not None
            and guard_until <= current
            and observed_at is not None
            and window.latest_generation_at is not None
            and window.latest_generation_at <= observed_at
        ):
            # One probe is allowed after cooldown. A new generation makes the
            # window newer and can open the circuit again on the next check.
            return CostGuardDecision(blocked=False, window=window)

        retry_at = current + REPAIR_CIRCUIT_COOLDOWN
        await self.session.execute(
            text("""
                UPDATE autocontent_settings
                SET cost_guard_until = :retry_at,
                    cost_guard_reason = :reason,
                    cost_guard_observed_at = :observed_at,
                    updated_at = now()
                WHERE user_id = :user_id
                  AND threads_account_id = :account_id
                  AND active
            """),
            {
                "retry_at": retry_at,
                "reason": CostGuardReason.REPAIR_RATE_HIGH.value,
                "observed_at": window.latest_generation_at,
                "user_id": user_id,
                "account_id": account_id,
            },
        )
        log.warning(
            "autocontent_circuit_open %s",
            json.dumps(
                {
                    "account_id": account_id,
                    "repair_samples": window.samples,
                    "repairs": window.repairs,
                    "repair_rate": round(window.rate, 4),
                    "retry_at": retry_at.isoformat(),
                },
                separators=(",", ":"),
            ),
        )
        return CostGuardDecision(
            blocked=True,
            reason=CostGuardReason.REPAIR_RATE_HIGH,
            retry_at=retry_at,
            window=window,
        )

    async def _clear(self, user_id: int, account_id: int) -> None:
        await self.session.execute(
            text("""
                UPDATE autocontent_settings
                SET cost_guard_until = NULL,
                    cost_guard_reason = NULL,
                    cost_guard_observed_at = NULL,
                    updated_at = now()
                WHERE user_id = :user_id
                  AND threads_account_id = :account_id
            """),
            {"user_id": user_id, "account_id": account_id},
        )


async def load_run_token_usage(
    session: AsyncSession,
    *,
    account_id: int,
    run_id: str,
) -> dict[str, dict[str, int]]:
    rows = (
        await session.execute(
            text("""
                SELECT feature,
                       sum(input_tokens) AS input_tokens,
                       sum(output_tokens) AS output_tokens
                FROM ai_usage_events
                WHERE threads_account_id = :account_id
                  AND run_id = :run_id
                  AND feature IN ('autocontent', 'autocontent_repair')
                GROUP BY feature
            """),
            {"account_id": account_id, "run_id": run_id},
        )
    ).mappings().all()
    return {
        str(row["feature"]): {
            "input_tokens": int(row.get("input_tokens") or 0),
            "output_tokens": int(row.get("output_tokens") or 0),
        }
        for row in rows
    }


def log_semantic_repair(
    *,
    account_id: int,
    run_id: str,
    reasons: list[str],
    usage: dict[str, dict[str, int]],
    status: str,
) -> None:
    original = usage.get("autocontent", {})
    repair = usage.get("autocontent_repair", {})
    log.info(
        "autocontent_repair %s",
        json.dumps(
            {
                "account_id": account_id,
                "run_id": run_id,
                "repair_reasons": reasons or [RepairReason.UNKNOWN.name],
                "original_input_tokens": original.get("input_tokens", 0),
                "repair_input_tokens": repair.get("input_tokens", 0),
                "status": status,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ),
    )


def log_planner_account(
    telemetry: PlannerAccountTelemetry,
    *,
    planner_run_id: str,
) -> None:
    payload: dict[str, Any] = asdict(telemetry)
    payload.update({
        "event": "autocontent_planner_account",
        "planner_run_id": planner_run_id,
    })
    log.info(
        "autocontent_planner_account %s",
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
    )
