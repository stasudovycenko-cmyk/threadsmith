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
    ROOT / "migrations/016_ux_v3_beta_readiness.sql"
).read_text(encoding="utf-8")
ROLLBACK = (
    ROOT / "migrations/rollback/016_ux_v3_beta_readiness.sql"
).read_text(encoding="utf-8")
BASE = tuple(
    path.read_text(encoding="utf-8")
    for path in sorted((ROOT / "migrations").glob("*.sql"))
    if path.name[:3].isdigit() and int(path.name[:3]) < 16
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
    schema = f"migration_016_{uuid.uuid4().hex}"
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


def test_notification_defaults_idempotency_and_immediate_rollback():
    async def scenario():
        async with _database() as connection:
            user_id = await connection.fetchval(
                "insert into users (telegram_id) values (16001) returning id"
            )
            account_id = await connection.fetchval(
                """
                insert into threads_accounts (
                  user_id, threads_user_id, username, access_token_enc,
                  expires_at, connection_status
                ) values ($1, 'threads-16', 'creator-16', $2,
                          now() + interval '30 days', 'connected')
                returning id
                """,
                user_id,
                b"encrypted-token",
            )
            await connection.execute(
                """
                insert into autocontent_settings (
                  threads_account_id, user_id
                ) values ($1, $2)
                """,
                account_id,
                user_id,
            )
            post_id = await connection.fetchval(
                """
                insert into scheduled_posts (
                  user_id, threads_account_id, text, run_at
                ) values ($1, $2, 'body', now()) returning id
                """,
                user_id,
                account_id,
            )
            historical_unknown_id = await connection.fetchval(
                """
                insert into scheduled_posts (
                  user_id, threads_account_id, text, run_at,
                  status, error
                ) values ($1, $2, 'old unknown', now() - interval '1 day',
                          'failed', 'UNKNOWN_ERROR: interrupted worker')
                returning id
                """,
                user_id,
                account_id,
            )

            await _script(connection, FORWARD)
            columns = await connection.fetch(
                """
                select table_name, column_name, data_type,
                       is_nullable, column_default
                from information_schema.columns
                where table_schema = current_schema()
                  and (
                    (table_name = 'autocontent_settings'
                     and column_name = 'publish_notifications_enabled')
                    or (table_name = 'scheduled_posts'
                     and column_name =
                       'publication_notification_claimed_at')
                  )
                order by table_name, column_name
                """
            )
            metadata = {
                (row["table_name"], row["column_name"]): row
                for row in columns
            }
            enabled_column = metadata[
                ("autocontent_settings", "publish_notifications_enabled")
            ]
            assert enabled_column["data_type"] == "boolean"
            assert enabled_column["is_nullable"] == "NO"
            assert enabled_column["column_default"] == "true"
            claimed_column = metadata[
                ("scheduled_posts", "publication_notification_claimed_at")
            ]
            assert claimed_column["data_type"] == "timestamp with time zone"
            assert claimed_column["is_nullable"] == "YES"
            assert await connection.fetchval(
                """
                select exists(
                  select 1 from pg_constraint
                  where conrelid = 'autocontent_settings'::regclass
                    and conname =
                      'autocontent_settings_account_owner_fk'
                )
                """
            ) is True
            assert await connection.fetchval(
                "select to_regclass('scheduled_posts_status_run_at_idx') "
                "is not null"
            ) is True
            enabled = await connection.fetchval(
                """
                select publish_notifications_enabled
                from autocontent_settings
                where user_id = $1 and threads_account_id = $2
                """,
                user_id,
                account_id,
            )
            assert enabled is True
            assert await connection.fetchval(
                """
                select publication_notification_claimed_at
                from scheduled_posts where id = $1
                """,
                post_id,
            ) is None
            assert await connection.fetchval(
                """
                select publication_notification_claimed_at is not null
                from scheduled_posts where id = $1
                """,
                historical_unknown_id,
            ) is True
            await connection.execute(
                """
                update scheduled_posts
                set publication_notification_claimed_at = now()
                where id = $1 and user_id = $2 and threads_account_id = $3
                """,
                post_id,
                user_id,
                account_id,
            )
            assert await connection.fetchval(
                """
                select publication_notification_claimed_at is not null
                from scheduled_posts where id = $1
                """,
                post_id,
            ) is True

            await _script(connection, FORWARD)
            await _script(connection, ROLLBACK)
            columns = await connection.fetchval(
                """
                select count(*) from information_schema.columns
                where table_schema = current_schema()
                  and (
                    (table_name = 'autocontent_settings'
                     and column_name = 'publish_notifications_enabled')
                    or (table_name = 'scheduled_posts'
                     and column_name =
                       'publication_notification_claimed_at')
                  )
                """
            )
            assert columns == 0

            await _script(connection, FORWARD)
            assert await connection.fetchval(
                """
                select count(*) from information_schema.columns
                where table_schema = current_schema()
                  and (
                    (table_name = 'autocontent_settings'
                     and column_name = 'publish_notifications_enabled')
                    or (table_name = 'scheduled_posts'
                     and column_name =
                       'publication_notification_claimed_at')
                  )
                """
            ) == 2
            await _script(connection, ROLLBACK)

    asyncio.run(scenario())
