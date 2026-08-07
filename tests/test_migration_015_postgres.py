import asyncio
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
    ROOT / "migrations/015_autocontent_cost_hardening.sql"
).read_text(encoding="utf-8")
ROLLBACK = (
    ROOT / "migrations/rollback/015_autocontent_cost_hardening.sql"
).read_text(encoding="utf-8")
BASE = tuple(
    path.read_text(encoding="utf-8")
    for path in sorted((ROOT / "migrations").glob("*.sql"))
    if path.name[:3].isdigit() and int(path.name[:3]) < 15
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
    schema = f"migration_015_{uuid.uuid4().hex}"
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


async def _account(connection):
    user_id = await connection.fetchval(
        "insert into users (telegram_id) values (15001) returning id"
    )
    account_id = await connection.fetchval(
        """
        insert into threads_accounts (
          user_id, threads_user_id, username, access_token_enc,
          expires_at, connection_status
        ) values ($1, 'threads-15', 'creator-15', $2,
                  now() + interval '30 days', 'connected')
        returning id
        """,
        user_id,
        b"encrypted-token",
    )
    await connection.execute(
        """
        insert into autocontent_settings (
          threads_account_id, user_id, active, posts_per_day
        ) values ($1, $2, true, 1)
        """,
        account_id,
        user_id,
    )
    return int(user_id), int(account_id)


def test_cost_guard_state_index_and_immediate_rollback():
    async def scenario():
        async with _database() as connection:
            user_id, account_id = await _account(connection)
            await _script(connection, FORWARD)
            await connection.execute(
                """
                update autocontent_settings
                set cost_guard_until = now() + interval '30 minutes',
                    cost_guard_reason = 'REPAIR_RATE_HIGH',
                    cost_guard_observed_at = now()
                where user_id = $1 and threads_account_id = $2
                """,
                user_id,
                account_id,
            )
            state = await connection.fetchrow(
                """
                select cost_guard_until, cost_guard_reason,
                       cost_guard_observed_at
                from autocontent_settings
                where threads_account_id = $1
                """,
                account_id,
            )
            assert state[0] is not None
            assert state[1] == "REPAIR_RATE_HIGH"
            assert state[2] is not None
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(
                    """
                    update autocontent_settings
                    set cost_guard_reason = 'UNSUPPORTED'
                    where threads_account_id = $1
                    """,
                    account_id,
                )
            index_exists = await connection.fetchval(
                """
                select exists (
                  select 1 from pg_indexes
                  where schemaname = current_schema()
                    and indexname =
                      'ai_usage_events_account_feature_created_idx'
                )
                """
            )
            assert index_exists is True

            await _script(connection, FORWARD)
            await _script(connection, ROLLBACK)
            columns = await connection.fetchval(
                """
                select count(*)
                from information_schema.columns
                where table_schema = current_schema()
                  and table_name = 'autocontent_settings'
                  and column_name in (
                    'cost_guard_until', 'cost_guard_reason',
                    'cost_guard_observed_at'
                  )
                """
            )
            assert columns == 0
            assert await connection.fetchval(
                "select to_regclass('ai_usage_events_account_feature_created_idx')"
            ) is None

    asyncio.run(scenario())
