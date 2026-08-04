import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

from scripts.migration_010_common import normalize_database_url

ROOT = Path(__file__).resolve().parents[1]
TEST_DSN = os.getenv("THREADSMITH_TEST_POSTGRES_DSN")
FORWARD = (
    ROOT / "migrations/014_autopilot_intelligence_v1.sql"
).read_text(encoding="utf-8")
ROLLBACK = (
    ROOT / "migrations/rollback/014_autopilot_intelligence_v1.sql"
).read_text(encoding="utf-8")
BASE = tuple(
    path.read_text(encoding="utf-8")
    for path in sorted((ROOT / "migrations").glob("*.sql"))
    if path.name[:3].isdigit() and int(path.name[:3]) < 14
)

pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="THREADSMITH_TEST_POSTGRES_DSN is not configured",
)


async def _script(connection, source):
    async with connection.transaction():
        await connection.execute(source)


@asynccontextmanager
async def _database():
    assert TEST_DSN
    connection = await asyncpg.connect(normalize_database_url(TEST_DSN))
    schema = f"migration_014_{uuid.uuid4().hex}"
    try:
        await connection.execute(f'create schema "{schema}"')
        await connection.execute(f'set search_path to "{schema}"')
        for source in BASE:
            await _script(connection, source)
        yield connection
    finally:
        await connection.execute("reset search_path")
        await connection.execute(f'drop schema if exists "{schema}" cascade')
        await connection.close()


async def _account(connection, telegram_id, suffix):
    user_id = await connection.fetchval(
        "insert into users (telegram_id) values ($1) returning id",
        telegram_id,
    )
    account_id = await connection.fetchval(
        """
        insert into threads_accounts (
          user_id, threads_user_id, username, access_token_enc,
          expires_at, connection_status
        ) values ($1, $2, $3, $4, now() + interval '30 days', 'connected')
        returning id
        """,
        user_id,
        f"threads-{suffix}",
        f"creator-{suffix}",
        b"encrypted-token",
    )
    return int(user_id), int(account_id)


def _result():
    return json.dumps({
        "status": "healthy",
        "health_score": 100,
        "health_breakdown": {
            "token": 20,
            "credits": 15,
            "queue": 20,
            "analytics": 15,
            "radar": 10,
            "neuro": 10,
            "publishing": 10,
            "total": 100,
        },
        "priority": 1,
        "recommendation": "SYSTEM_HEALTHY",
        "reason_codes": ["SYSTEM_HEALTHY"],
        "warnings": [],
        "blockers": [],
        "safe_action": "NONE",
        "next_check": "2026-08-04T11:00:00Z",
        "next_recommended_action": "NONE",
        "human_message": "Всё работает.",
        "generated_at": "2026-08-04T10:45:00Z",
        "rules_version": 1,
    }, ensure_ascii=False)


async def _insert_run(connection, user_id, account_id, **changes):
    values = {
        "context_hash": "a" * 64,
        "decision_hash": "b" * 64,
        "status": "healthy",
        "health_score": 100,
        "priority": 1,
        "result_json": _result(),
    }
    values.update(changes)
    return await connection.fetchval(
        """
        insert into decision_runs (
          user_id, threads_account_id, context_hash, decision_hash,
          status, health_score, priority, reason_codes, result_json,
          next_check, bucket_start
        ) values (
          $1, $2, $3, $4, $5, $6, $7, array['SYSTEM_HEALTHY'],
          $8::jsonb, now() + interval '15 minutes',
          date_trunc('hour', now())
        ) returning id
        """,
        user_id,
        account_id,
        values["context_hash"],
        values["decision_hash"],
        values["status"],
        values["health_score"],
        values["priority"],
        values["result_json"],
    )


def test_forward_constraints_indexes_idempotency_and_rollback():
    async def scenario():
        async with _database() as connection:
            first_user, first_account = await _account(
                connection, 14001, "first"
            )
            second_user, second_account = await _account(
                connection, 14002, "second"
            )
            await _script(connection, FORWARD)

            run_id = await _insert_run(
                connection, first_user, first_account
            )
            assert run_id
            with pytest.raises(asyncpg.UniqueViolationError):
                await _insert_run(connection, first_user, first_account)
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await _insert_run(
                    connection,
                    first_user,
                    second_account,
                    context_hash="c" * 64,
                )
            with pytest.raises(asyncpg.CheckViolationError):
                await _insert_run(
                    connection,
                    second_user,
                    second_account,
                    context_hash="short",
                )
            with pytest.raises(asyncpg.CheckViolationError):
                await _insert_run(
                    connection,
                    second_user,
                    second_account,
                    context_hash="f" * 64,
                    decision_hash="short",
                )
            with pytest.raises(asyncpg.CheckViolationError):
                await _insert_run(
                    connection,
                    second_user,
                    second_account,
                    context_hash="d" * 64,
                    health_score=101,
                )
            with pytest.raises(asyncpg.CheckViolationError):
                await _insert_run(
                    connection,
                    second_user,
                    second_account,
                    context_hash="9" * 64,
                    health_score=99,
                )
            with pytest.raises(asyncpg.CheckViolationError):
                await _insert_run(
                    connection,
                    second_user,
                    second_account,
                    context_hash="e" * 64,
                    status="random",
                )
            with pytest.raises(asyncpg.CheckViolationError):
                await _insert_run(
                    connection,
                    second_user,
                    second_account,
                    context_hash="1" * 64,
                    result_json="[]",
                )

            indexes = await connection.fetch(
                """
                select indexname from pg_indexes
                where schemaname = current_schema()
                  and tablename = 'decision_runs'
                """
            )
            index_names = {row["indexname"] for row in indexes}
            assert "decision_runs_account_history_idx" in index_names
            assert "decision_runs_retention_idx" in index_names

            await _script(connection, ROLLBACK)
            assert not await connection.fetchval(
                "select to_regclass('decision_runs') is not null"
            )
            assert not await connection.fetchval(
                "select to_regclass('autopilot_intelligence_migration_014') "
                "is not null"
            )

    asyncio.run(scenario())


def test_repeat_forward_and_missing_marker_rollback_are_rejected():
    async def scenario():
        async with _database() as connection:
            await _script(connection, FORWARD)
            with pytest.raises(asyncpg.RaiseError):
                await _script(connection, FORWARD)
            await _script(connection, ROLLBACK)
            with pytest.raises(asyncpg.RaiseError):
                await _script(connection, ROLLBACK)

    asyncio.run(scenario())
