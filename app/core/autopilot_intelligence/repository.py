"""Account-scoped persistence for deterministic decision history."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.autopilot_intelligence.models import DecisionResult, DecisionRun

HISTORY_RETENTION_DAYS = 90
DECISION_BUCKET_MINUTES = 15


class DecisionRepositoryError(RuntimeError):
    pass


class DecisionOwnershipError(DecisionRepositoryError):
    pass


def _mapping(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    value = getattr(row, "_mapping", row)
    return dict(value) if isinstance(value, Mapping) else {}


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise DecisionRepositoryError("decision result is not a JSON object")
    return dict(value)


def decision_bucket(value: datetime) -> datetime:
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    utc = aware.astimezone(timezone.utc)
    minute = utc.minute - (utc.minute % DECISION_BUCKET_MINUTES)
    return utc.replace(minute=minute, second=0, microsecond=0)


class DecisionRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def save(
        self,
        user_id: int,
        account_id: int,
        context_hash: str,
        result: DecisionResult,
    ) -> DecisionRun:
        serialized = json.dumps(
            result.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        row = (
            await self.session.execute(text("""
                INSERT INTO decision_runs (
                  user_id, threads_account_id, context_hash, decision_hash,
                  status, health_score, priority, reason_codes,
                  result_json, next_check, bucket_start
                )
                SELECT account.user_id, account.id, :context_hash,
                       :decision_hash, :status, :health_score, :priority,
                       :reason_codes, CAST(:result_json AS jsonb),
                       :next_check, :bucket_start
                FROM threads_accounts account
                WHERE account.user_id = :user_id
                  AND account.id = :account_id
                ON CONFLICT (
                  threads_account_id, context_hash, bucket_start
                ) DO UPDATE SET context_hash = decision_runs.context_hash
                RETURNING *
            """), {
                "user_id": user_id,
                "account_id": account_id,
                "context_hash": context_hash,
                "decision_hash": result.decision_hash(),
                "status": result.status.value,
                "health_score": result.health_score,
                "priority": result.priority,
                "reason_codes": list(result.reason_codes),
                "result_json": serialized,
                "next_check": result.next_check,
                "bucket_start": decision_bucket(result.generated_at),
            })
        ).mappings().first()
        data = _mapping(row)
        if not data:
            raise DecisionOwnershipError("Threads account is not owned")
        return self._run(data)

    async def latest(
        self,
        user_id: int,
        account_id: int,
    ) -> DecisionRun | None:
        row = (
            await self.session.execute(text("""
                SELECT run.* FROM decision_runs run
                WHERE run.user_id = :user_id
                  AND run.threads_account_id = :account_id
                  AND EXISTS (
                    SELECT 1 FROM threads_accounts account
                    WHERE account.id = run.threads_account_id
                      AND account.user_id = run.user_id
                  )
                ORDER BY run.created_at DESC, run.id DESC LIMIT 1
            """), {"user_id": user_id, "account_id": account_id})
        ).mappings().first()
        return self._run(_mapping(row)) if row else None

    async def get(
        self,
        user_id: int,
        account_id: int,
        run_id: int,
    ) -> DecisionRun | None:
        row = (
            await self.session.execute(text("""
                SELECT run.* FROM decision_runs run
                WHERE run.id = :run_id
                  AND run.user_id = :user_id
                  AND run.threads_account_id = :account_id
                  AND EXISTS (
                    SELECT 1 FROM threads_accounts account
                    WHERE account.id = run.threads_account_id
                      AND account.user_id = run.user_id
                  )
            """), {
                "run_id": run_id,
                "user_id": user_id,
                "account_id": account_id,
            })
        ).mappings().first()
        return self._run(_mapping(row)) if row else None

    async def history(
        self,
        user_id: int,
        account_id: int,
        *,
        limit: int = 10,
        offset: int = 0,
    ) -> list[DecisionRun]:
        rows = (
            await self.session.execute(text("""
                SELECT run.* FROM decision_runs run
                WHERE run.user_id = :user_id
                  AND run.threads_account_id = :account_id
                  AND EXISTS (
                    SELECT 1 FROM threads_accounts account
                    WHERE account.id = run.threads_account_id
                      AND account.user_id = run.user_id
                  )
                ORDER BY run.created_at DESC, run.id DESC
                LIMIT :row_limit OFFSET :row_offset
            """), {
                "user_id": user_id,
                "account_id": account_id,
                "row_limit": max(1, min(50, int(limit))),
                "row_offset": max(0, int(offset)),
            })
        ).mappings().all()
        return [self._run(_mapping(row)) for row in rows]

    async def prune_history(self, user_id: int, account_id: int) -> int:
        rows = (
            await self.session.execute(text("""
                DELETE FROM decision_runs
                WHERE user_id = :user_id
                  AND threads_account_id = :account_id
                  AND created_at < now() - make_interval(days => :days)
                RETURNING id
            """), {
                "user_id": user_id,
                "account_id": account_id,
                "days": HISTORY_RETENTION_DAYS,
            })
        ).all()
        return len(rows)

    @staticmethod
    def _run(row: Mapping[str, Any]) -> DecisionRun:
        result = DecisionResult.model_validate(
            _json_object(row.get("result_json"))
        )
        return DecisionRun(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            threads_account_id=int(row["threads_account_id"]),
            context_hash=str(row["context_hash"]),
            decision_hash=str(row["decision_hash"]),
            result=result,
            created_at=row["created_at"],
        )
