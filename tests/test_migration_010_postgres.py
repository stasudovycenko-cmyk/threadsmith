import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
import pytest

from scripts.migration_010_common import normalize_database_url
from scripts.preflight_migration_010 import audit_preflight
from scripts.validate_migration_010 import audit_post_migration

ROOT = Path(__file__).resolve().parents[1]
TEST_DSN = os.getenv("THREADSMITH_TEST_POSTGRES_DSN")
FORWARD_SQL = (
    ROOT / "migrations/010_threads_account_cabinet.sql"
).read_text(encoding="utf-8")
ROLLBACK_SQL = (
    ROOT / "migrations/rollback/010_threads_account_cabinet.sql"
).read_text(encoding="utf-8")
BASE_MIGRATIONS = tuple(
    (ROOT / f"migrations/{number:03d}_{name}.sql").read_text(
        encoding="utf-8"
    )
    for number, name in (
        (1, "init"),
        (2, "radar"),
        (3, "neuro"),
        (4, "admin_autocontent"),
        (5, "social_brain"),
        (6, "ai_cost_engine"),
        (7, "content_engine_v2"),
        (8, "autopilot_status"),
        (9, "autopost_active_slot_unique"),
    )
)

pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="THREADSMITH_TEST_POSTGRES_DSN is not configured",
)


@dataclass(frozen=True)
class Seed:
    user_one: int
    user_multi: int
    user_without_account: int
    user_without_settings: int
    account_one: int
    account_multi_old: int
    account_multi_new: int
    account_without_settings: int


async def _execute_script(conn: asyncpg.Connection, sql: str) -> None:
    async with conn.transaction():
        await conn.execute(sql)


@asynccontextmanager
async def _database():
    assert TEST_DSN is not None
    conn = await asyncpg.connect(normalize_database_url(TEST_DSN))
    schema = f"migration_010_{uuid.uuid4().hex}"
    try:
        await conn.execute(f'create schema "{schema}"')
        await conn.execute(f'set search_path to "{schema}"')
        for migration in BASE_MIGRATIONS:
            await _execute_script(conn, migration)
        yield conn
    finally:
        await conn.execute("reset search_path")
        await conn.execute(f'drop schema if exists "{schema}" cascade')
        await conn.close()


async def _seed(conn: asyncpg.Connection) -> Seed:
    user_ids = []
    for telegram_id in (101, 102, 103, 104):
        user_ids.append(await conn.fetchval(
            "insert into users (telegram_id) values ($1) returning id",
            telegram_id,
        ))
    user_one, user_multi, user_without_account, user_without_settings = (
        user_ids
    )

    async def account(
        user_id: int,
        identity: str,
        created_at: datetime,
    ) -> int:
        return int(await conn.fetchval(
            """
            insert into threads_accounts (
              user_id, threads_user_id, username, access_token_enc,
              expires_at, created_at
            ) values ($1, $2, $3, $4, now() + interval '30 days', $5)
            returning id
            """,
            user_id,
            identity,
            identity,
            b"encrypted-token",
            created_at,
        ))

    account_one = await account(
        user_one,
        "threads-one",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    account_multi_old = await account(
        user_multi,
        "threads-multi-old",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    account_multi_new = await account(
        user_multi,
        "threads-multi-new",
        datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    account_without_settings = await account(
        user_without_settings,
        "threads-no-settings",
        datetime(2026, 3, 1, tzinfo=timezone.utc),
    )

    await conn.execute("""
        alter table autocontent_settings
          add column topics text not null default '',
          add column slots text not null default '',
          add column days text not null default 'all',
          add column goal text not null default ''
    """)
    for values in (
        (user_one, True, 3, "python", "09:00", "weekdays", "reach"),
        (user_multi, False, 5, "growth", "12:00", "all", "engagement"),
        (user_without_account, True, 2, "orphan-safe", "15:00", "all", "reach"),
    ):
        await conn.execute(
            """
            insert into autocontent_settings (
              user_id, active, posts_per_day, topics, slots, days, goal,
              timezone
            ) values ($1, $2, $3, $4, $5, $6, $7, 'Europe/Moscow')
            """,
            *values,
        )
    for values in (
        (user_one, True, "auto", 17),
        (user_multi, False, "approve", 9),
        (user_without_account, True, "auto", 4),
    ):
        await conn.execute(
            """
            insert into neuro_settings (user_id, active, mode, daily_cap)
            values ($1, $2, $3, $4)
            """,
            *values,
        )
    await conn.execute(
        """
        insert into neuro_comments (
          user_id, target_post_id, target_text, comment_text
        ) values
          ($1, 'single-target', 'post', 'single comment'),
          ($2, 'ambiguous-target', 'post', 'ambiguous comment')
        """,
        user_one,
        user_multi,
    )
    await conn.execute(
        "insert into oauth_states (state, user_id) values ($1, $2)",
        uuid.uuid4(),
        user_one,
    )
    return Seed(
        user_one=user_one,
        user_multi=user_multi,
        user_without_account=user_without_account,
        user_without_settings=user_without_settings,
        account_one=account_one,
        account_multi_old=account_multi_old,
        account_multi_new=account_multi_new,
        account_without_settings=account_without_settings,
    )


def test_forward_migration_copies_values_and_attributes_comments_safely():
    async def scenario():
        async with _database() as conn:
            seed = await _seed(conn)
            async with conn.transaction(readonly=True):
                assert await audit_preflight(conn) == 0
            await _execute_script(conn, FORWARD_SQL)
            async with conn.transaction(readonly=True):
                assert await audit_post_migration(conn) == 0

            assert await conn.fetchval(
                "select count(*) from autocontent_settings"
            ) == 4
            assert await conn.fetchval(
                "select count(*) from neuro_settings"
            ) == 4
            copied = await conn.fetchrow(
                """
                select posts_per_day, topics, slots, days, goal, active
                from autocontent_settings where threads_account_id = $1
                """,
                seed.account_one,
            )
            assert tuple(copied) == (
                3,
                "python",
                "09:00",
                "weekdays",
                "reach",
                True,
            )
            multi_rows = await conn.fetch(
                """
                select threads_account_id, posts_per_day, topics
                from autocontent_settings where user_id = $1
                order by threads_account_id
                """,
                seed.user_multi,
            )
            assert [tuple(row[1:]) for row in multi_rows] == [
                (5, "growth"),
                (5, "growth"),
            ]
            assert await conn.fetchval(
                "select daily_cap from neuro_settings "
                "where threads_account_id = $1",
                seed.account_one,
            ) == 17
            assert await conn.fetchval(
                "select daily_cap from neuro_settings "
                "where threads_account_id = $1",
                seed.account_without_settings,
            ) == 10
            assert await conn.fetchval(
                "select selected_threads_account_id from user_preferences "
                "where user_id = $1",
                seed.user_multi,
            ) == seed.account_multi_new
            assert await conn.fetchval(
                "select threads_account_id from neuro_comments "
                "where user_id = $1",
                seed.user_one,
            ) == seed.account_one
            assert await conn.fetchval(
                "select threads_account_id from neuro_comments "
                "where user_id = $1",
                seed.user_multi,
            ) is None
            oauth = await conn.fetchrow(
                "select action, expected_threads_account_id from oauth_states"
            )
            assert tuple(oauth) == ("connect", None)
            assert await conn.fetchval(
                "select to_regclass('autocontent_settings_user_backup_010')"
            ) is not None
            assert await conn.fetchval(
                "select to_regclass('neuro_settings_user_backup_010')"
            ) is not None

    asyncio.run(scenario())


def test_owner_foreign_key_and_active_account_deletion_semantics():
    async def scenario():
        async with _database() as conn:
            seed = await _seed(conn)
            await _execute_script(conn, FORWARD_SQL)
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    """
                    update user_preferences
                    set selected_threads_account_id = $1
                    where user_id = $2
                    """,
                    seed.account_multi_old,
                    seed.user_one,
                )
            with pytest.raises(asyncpg.ForeignKeyViolationError):
                await conn.execute(
                    "delete from threads_accounts where id = $1",
                    seed.account_multi_new,
                )
            await conn.execute(
                """
                update user_preferences set selected_threads_account_id = $1
                where user_id = $2
                """,
                seed.account_multi_old,
                seed.user_multi,
            )
            await conn.execute(
                "delete from threads_accounts where id = $1",
                seed.account_multi_new,
            )
            assert await conn.fetchval(
                "select selected_threads_account_id from user_preferences "
                "where user_id = $1",
                seed.user_multi,
            ) == seed.account_multi_old

    asyncio.run(scenario())


def test_duplicate_neuro_target_blocks_without_migration_side_effects():
    async def scenario():
        async with _database() as conn:
            seed = await _seed(conn)
            await conn.execute(
                "alter table neuro_comments drop constraint "
                "neuro_comments_user_id_target_post_id_key"
            )
            await conn.execute(
                """
                insert into neuro_comments (
                  user_id, target_post_id, target_text, comment_text
                ) values ($1, 'single-target', 'duplicate', 'duplicate')
                """,
                seed.user_one,
            )
            async with conn.transaction(readonly=True):
                assert await audit_preflight(conn) == 2
            with pytest.raises(
                asyncpg.PostgresError,
                match="duplicate neuro target",
            ):
                await _execute_script(conn, FORWARD_SQL)
            assert not await conn.fetchval(
                """
                select exists (
                  select 1 from information_schema.columns
                  where table_schema = current_schema()
                    and table_name = 'threads_accounts'
                    and column_name = 'connection_status'
                )
                """
            )
            assert await conn.fetchval(
                "select to_regclass('autocontent_settings_user_backup_010')"
            ) is None

    asyncio.run(scenario())


def test_late_failure_rolls_back_all_transactional_ddl():
    async def scenario():
        async with _database() as conn:
            await _seed(conn)
            broken_sql = FORWARD_SQL + "\nselect migration_010_forced_failure;"
            with pytest.raises(asyncpg.UndefinedColumnError):
                await _execute_script(conn, broken_sql)
            assert await conn.fetchval(
                "select to_regclass('threads_account_cabinet_migration_010')"
            ) is None
            assert await conn.fetchval(
                "select to_regclass('autocontent_settings_user_backup_010')"
            ) is None
            assert await conn.fetchval(
                "select count(*) from autocontent_settings"
            ) == 3

    asyncio.run(scenario())


def test_repeat_run_refuses_without_changing_fingerprints():
    async def scenario():
        async with _database() as conn:
            await _seed(conn)
            await _execute_script(conn, FORWARD_SQL)
            before = await conn.fetchrow(
                "select * from threads_account_cabinet_migration_010"
            )
            with pytest.raises(
                asyncpg.PostgresError,
                match="already applied; refusing a repeat run",
            ):
                await _execute_script(conn, FORWARD_SQL)
            after = await conn.fetchrow(
                "select * from threads_account_cabinet_migration_010"
            )
            assert dict(after) == dict(before)

    asyncio.run(scenario())


def test_immediate_rollback_restores_backup_tables_exactly():
    async def scenario():
        async with _database() as conn:
            await _seed(conn)
            await _execute_script(conn, FORWARD_SQL)
            backup_autocontent = await conn.fetch(
                """
                select to_jsonb(setting) as value
                from autocontent_settings_user_backup_010 setting
                order by user_id
                """
            )
            backup_neuro = await conn.fetch(
                """
                select to_jsonb(setting) as value
                from neuro_settings_user_backup_010 setting
                order by user_id
                """
            )
            await _execute_script(conn, ROLLBACK_SQL)
            restored_autocontent = await conn.fetch(
                """
                select to_jsonb(setting) as value
                from autocontent_settings setting order by user_id
                """
            )
            restored_neuro = await conn.fetch(
                """
                select to_jsonb(setting) as value
                from neuro_settings setting order by user_id
                """
            )
            assert [row["value"] for row in restored_autocontent] == [
                row["value"] for row in backup_autocontent
            ]
            assert [row["value"] for row in restored_neuro] == [
                row["value"] for row in backup_neuro
            ]
            assert await conn.fetchval(
                "select to_regclass('threads_account_cabinet_migration_010')"
            ) is None
            assert await conn.fetchval(
                "select obj_description('autocontent_settings'::regclass)"
            ) is None
            assert await conn.fetchval(
                "select obj_description('neuro_settings'::regclass)"
            ) is None
            assert not await conn.fetchval(
                """
                select exists (
                  select 1 from information_schema.columns
                  where table_schema = current_schema()
                    and table_name = 'threads_accounts'
                    and column_name = 'connection_status'
                )
                """
            )

    asyncio.run(scenario())


def test_rollback_blocks_changed_settings_and_new_users():
    async def changed_settings():
        async with _database() as conn:
            seed = await _seed(conn)
            await _execute_script(conn, FORWARD_SQL)
            await conn.execute(
                "update autocontent_settings set goal = 'changed' "
                "where threads_account_id = $1",
                seed.account_one,
            )
            with pytest.raises(
                asyncpg.PostgresError,
                match="account settings changed after migration",
            ):
                await _execute_script(conn, ROLLBACK_SQL)

    async def new_user():
        async with _database() as conn:
            await _seed(conn)
            await _execute_script(conn, FORWARD_SQL)
            await conn.execute(
                "insert into users (telegram_id) values (999999)"
            )
            with pytest.raises(
                asyncpg.PostgresError,
                match="users changed after migration",
            ):
                await _execute_script(conn, ROLLBACK_SQL)

    asyncio.run(changed_settings())
    asyncio.run(new_user())


def test_rollback_blocks_disconnected_account_and_active_publication():
    async def disconnected():
        async with _database() as conn:
            seed = await _seed(conn)
            await _execute_script(conn, FORWARD_SQL)
            await conn.execute(
                """
                update threads_accounts
                set connection_status = 'disconnected', access_token_enc = null
                where id = $1
                """,
                seed.account_one,
            )
            with pytest.raises(
                asyncpg.PostgresError,
                match="reconnect disconnected/error accounts first",
            ):
                await _execute_script(conn, ROLLBACK_SQL)

    async def publishing():
        async with _database() as conn:
            seed = await _seed(conn)
            await _execute_script(conn, FORWARD_SQL)
            await conn.execute(
                """
                insert into scheduled_posts (
                  user_id, threads_account_id, text, run_at, status
                ) values ($1, $2, 'publishing', now(), 'publishing')
                """,
                seed.user_one,
                seed.account_one,
            )
            with pytest.raises(
                asyncpg.PostgresError,
                match="publication is in progress",
            ):
                await _execute_script(conn, ROLLBACK_SQL)

    asyncio.run(disconnected())
    asyncio.run(publishing())
