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
FORWARD = (ROOT / "migrations/011_radar_neurocommenting_v2.sql").read_text(
    encoding="utf-8"
)
ROLLBACK = (
    ROOT / "migrations/rollback/011_radar_neurocommenting_v2.sql"
).read_text(encoding="utf-8")
BASE = tuple(
    path.read_text(encoding="utf-8")
    for path in sorted((ROOT / "migrations").glob("*.sql"))
    if path.name[:3].isdigit() and int(path.name[:3]) < 11
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
    schema = f"migration_011_{uuid.uuid4().hex}"
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


async def _seed_account(connection):
    user_id = await connection.fetchval(
        "insert into users (telegram_id) values (9911) returning id"
    )
    account_id = await connection.fetchval(
        """
        insert into threads_accounts (
          user_id, threads_user_id, username, access_token_enc,
          expires_at, connection_status
        ) values ($1, 'threads-9911', 'creator', $2,
                  now() + interval '30 days', 'connected')
        returning id
        """,
        user_id,
        b"encrypted-token",
    )
    await connection.execute(
        """
        insert into neuro_settings (threads_account_id, user_id)
        values ($1, $2)
        """,
        account_id,
        user_id,
    )
    await connection.execute(
        """
        insert into user_niches (user_id, niche, keywords)
        values ($1, 'AI', array['AI', 'automation'])
        """,
        user_id,
    )
    return int(user_id), int(account_id)


def test_forward_schema_defaults_constraints_and_immediate_rollback():
    async def scenario():
        async with _database() as connection:
            user_id, account_id = await _seed_account(connection)
            await _script(connection, FORWARD)
            setting = await connection.fetchrow(
                """
                select radar.niche, radar.keywords,
                       neuro.minimum_score,
                       neuro.minimum_interval_minutes,
                       neuro.auto_follow_up
                from radar_settings radar
                join neuro_settings neuro using (threads_account_id, user_id)
                where radar.threads_account_id = $1 and radar.user_id = $2
                """,
                account_id,
                user_id,
            )
            assert tuple(setting) == (
                "AI", ["AI", "automation"], 75, 30, False
            )
            assert await connection.fetchval(
                "select to_regclass('radar_candidates')"
            ) is not None
            assert await connection.fetchval(
                "select to_regclass('ai_credit_events')"
            ) is not None
            index_definition = await connection.fetchval(
                """
                select indexdef from pg_indexes
                where schemaname = current_schema()
                  and indexname = 'radar_candidates_account_status_score_idx'
                """
            )
            assert "final_score DESC NULLS LAST" in index_definition
            assert "discovered_at DESC" in index_definition
            assert index_definition.index("final_score") < index_definition.index(
                "discovered_at"
            )
            assert await connection.fetchval(
                """
                select confdeltype
                from pg_constraint
                where conrelid = 'neuro_comments'::regclass
                  and conname = 'neuro_comments_radar_candidate_owner_fk'
                """
            ) == "a"

            await _script(connection, ROLLBACK)
            assert await connection.fetchval(
                "select to_regclass('radar_candidates')"
            ) is None
            assert not await connection.fetchval(
                """
                select exists (
                  select 1 from information_schema.columns
                  where table_schema = current_schema()
                    and table_name = 'neuro_settings'
                    and column_name = 'minimum_score'
                )
                """
            )

    asyncio.run(scenario())


def test_account_post_and_credit_operation_uniqueness():
    async def scenario():
        async with _database() as connection:
            user_id, account_id = await _seed_account(connection)
            await _script(connection, FORWARD)
            values = (
                user_id, account_id, "post-1", "author-1", "body", 80
            )
            await connection.execute(
                """
                insert into radar_candidates (
                  user_id, threads_account_id, threads_post_id,
                  author_key, post_text, deterministic_score
                ) values ($1, $2, $3, $4, $5, $6)
                """,
                *values,
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await connection.execute(
                    """
                    insert into radar_candidates (
                      user_id, threads_account_id, threads_post_id,
                      author_key, post_text, deterministic_score
                    ) values ($1, $2, $3, $4, $5, $6)
                    """,
                    *values,
                )
            await connection.execute(
                """
                insert into ai_credit_events (
                  operation_key, user_id, threads_account_id, feature, credits
                ) values ('operation-1', $1, $2, 'neuro_comment', 2)
                """,
                user_id,
                account_id,
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await connection.execute(
                    """
                    insert into ai_credit_events (
                      operation_key, user_id, threads_account_id,
                      feature, credits
                    ) values ('operation-1', $1, $2, 'neuro_comment', 2)
                    """,
                    user_id,
                    account_id,
                )

    asyncio.run(scenario())
