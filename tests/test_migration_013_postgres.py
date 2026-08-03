import asyncio
import json
import os
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import pytest

from scripts.migration_010_common import normalize_database_url

ROOT = Path(__file__).resolve().parents[1]
TEST_DSN = os.getenv("THREADSMITH_TEST_POSTGRES_DSN")
FORWARD = (ROOT / "migrations/013_ux_v2_dashboard.sql").read_text(
    encoding="utf-8"
)
ROLLBACK = (
    ROOT / "migrations/rollback/013_ux_v2_dashboard.sql"
).read_text(encoding="utf-8")
BASE = tuple(
    path.read_text(encoding="utf-8")
    for path in sorted((ROOT / "migrations").glob("*.sql"))
    if path.name[:3].isdigit() and int(path.name[:3]) < 13
)

pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="THREADSMITH_TEST_POSTGRES_DSN is not configured",
)


async def _script(connection, source):
    async with connection.transaction():
        await connection.execute(source)


def _json_object(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise AssertionError(
            f"Expected a PostgreSQL JSON object, got {type(value).__name__}"
        )
    return dict(value)


@asynccontextmanager
async def _database():
    assert TEST_DSN
    connection = await asyncpg.connect(normalize_database_url(TEST_DSN))
    schema = f"migration_013_{uuid.uuid4().hex}"
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


def test_modes_onboarding_constraints_and_immediate_rollback():
    async def scenario():
        async with _database() as connection:
            first_user, first_account = await _account(
                connection, 13001, "first"
            )
            second_user, second_account = await _account(
                connection, 13002, "second"
            )
            await connection.execute(
                """
                insert into user_preferences (
                  user_id, selected_threads_account_id
                ) values ($1, $2)
                """,
                first_user,
                first_account,
            )
            await _script(connection, FORWARD)

            assert await connection.fetchval(
                "select interface_mode from user_preferences where user_id=$1",
                first_user,
            ) == "advanced"
            await connection.execute(
                "insert into user_preferences (user_id) values ($1)",
                second_user,
            )
            assert await connection.fetchval(
                "select interface_mode from user_preferences where user_id=$1",
                second_user,
            ) == "simple"
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(
                    "update user_preferences set interface_mode='expert' "
                    "where user_id=$1",
                    second_user,
                )

            assert await connection.fetchval(
                """
                select data_type
                from information_schema.columns
                where table_schema=current_schema()
                  and table_name='ux_onboarding'
                  and column_name='data'
                """
            ) == "jsonb"
            await connection.execute(
                """
                insert into ux_onboarding (
                  user_id, threads_account_id, status, current_step, data
                ) values ($1, $2, 'in_progress', 4, '{"goal":"reach"}')
                """,
                first_user,
                first_account,
            )
            progress = await connection.fetchrow(
                """
                select status, current_step, data
                from ux_onboarding
                where user_id=$1 and threads_account_id=$2
                """,
                first_user,
                first_account,
            )
            assert progress[0] == "in_progress"
            assert progress[1] == 4
            assert _json_object(progress[2]) == {"goal": "reach"}
            assert await connection.fetchval(
                """
                select jsonb_typeof(data)
                from ux_onboarding
                where user_id=$1 and threads_account_id=$2
                """,
                first_user,
                first_account,
            ) == "object"
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await connection.execute(
                    """
                    insert into ux_onboarding (user_id, threads_account_id)
                    values ($1, $2)
                    """,
                    first_user,
                    second_account,
                )
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(
                    """
                    update ux_onboarding set current_step=10
                    where user_id=$1 and threads_account_id=$2
                    """,
                    first_user,
                    first_account,
                )
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(
                    """
                    update ux_onboarding set data='[]'::jsonb
                    where user_id=$1 and threads_account_id=$2
                    """,
                    first_user,
                    first_account,
                )
            index_definition = await connection.fetchval(
                """
                select indexdef from pg_indexes
                where schemaname=current_schema()
                  and indexname='ux_onboarding_account_status_idx'
                """
            )
            assert "threads_account_id" in index_definition
            assert "updated_at DESC" in index_definition

            await _script(connection, ROLLBACK)
            assert await connection.fetchval(
                "select to_regclass('ux_onboarding')"
            ) is None
            assert await connection.fetchval(
                "select to_regclass('threads_accounts')"
            ) is not None
            assert not await connection.fetchval(
                """
                select exists (
                  select 1 from information_schema.columns
                  where table_schema=current_schema()
                    and table_name='user_preferences'
                    and column_name='interface_mode'
                )
                """
            )

    asyncio.run(scenario())
