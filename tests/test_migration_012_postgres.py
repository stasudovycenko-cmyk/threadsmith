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
FORWARD = (ROOT / "migrations/012_analytics_v2.sql").read_text(
    encoding="utf-8"
)
ROLLBACK = (
    ROOT / "migrations/rollback/012_analytics_v2.sql"
).read_text(encoding="utf-8")
BASE = tuple(
    path.read_text(encoding="utf-8")
    for path in sorted((ROOT / "migrations").glob("*.sql"))
    if not path.name.startswith("012_")
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
    schema = f"migration_012_{uuid.uuid4().hex}"
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


async def _seed(connection):
    user_id = await connection.fetchval(
        "insert into users (telegram_id) values (12012) returning id"
    )
    account_id = await connection.fetchval(
        """
        insert into threads_accounts (
          user_id, threads_user_id, username, access_token_enc,
          expires_at, connection_status
        ) values ($1, 'threads-12012', 'analytics', $2,
                  now() + interval '30 days', 'connected')
        returning id
        """,
        user_id,
        b"encrypted-token",
    )
    post_id = await connection.fetchval(
        """
        insert into scheduled_posts (
          user_id, threads_account_id, text, run_at, status, threads_post_id
        ) values ($1, $2, 'body', now() - interval '1 hour',
                  'done', 'post-12012')
        returning id
        """,
        user_id,
        account_id,
    )
    await connection.execute(
        """
        insert into insights_snapshots (
          threads_post_id, snapshot_date, metrics_json
        ) values (
          'post-12012', current_date,
          '{"views":100,"likes":10,"replies":1,"reposts":1,"quotes":0}'
        )
        """
    )
    return int(user_id), int(account_id), int(post_id)


def test_forward_backfill_account_isolation_and_rollback():
    async def scenario():
        async with _database() as connection:
            user_id, account_id, post_id = await _seed(connection)
            await _script(connection, FORWARD)
            snapshot = await connection.fetchrow(
                """
                select user_id, threads_account_id, scheduled_post_id,
                       views, engagement_rate
                from analytics_snapshots
                """
            )
            assert tuple(snapshot[:4]) == (
                user_id, account_id, post_id, 100
            )
            assert float(snapshot["engagement_rate"]) == 0.12
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await connection.execute(
                    """
                    insert into analytics_account_summary (
                      threads_account_id, user_id
                    ) values ($1, $2)
                    """,
                    account_id,
                    user_id + 999,
                )
            await _script(connection, ROLLBACK)
            assert await connection.fetchval(
                "select to_regclass('analytics_snapshots')"
            ) is None
            assert await connection.fetchval(
                "select count(*) from insights_snapshots"
            ) == 1
            assert await connection.fetchval(
                "select count(*) from scheduled_posts"
            ) == 1

    asyncio.run(scenario())
