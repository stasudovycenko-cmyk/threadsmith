from __future__ import annotations

import asyncpg

try:
    from scripts.migration_010_common import (
        column_exists,
        relation_exists,
        run_read_only,
    )
except ModuleNotFoundError:
    from migration_010_common import (  # type: ignore[no-redef]
        column_exists,
        relation_exists,
        run_read_only,
    )


REQUIRED_RELATIONS = (
    "users",
    "threads_accounts",
    "oauth_states",
    "autocontent_settings",
    "neuro_settings",
    "neuro_comments",
)
MIGRATION_RELATIONS = (
    "threads_account_cabinet_migration_010",
    "user_preferences",
    "autocontent_settings_user_backup_010",
    "neuro_settings_user_backup_010",
    "threads_data_deletion_requests",
)


async def _count(conn: asyncpg.Connection, sql: str) -> int:
    return int(await conn.fetchval(sql) or 0)


async def audit_preflight(conn: asyncpg.Connection) -> int:
    blockers: list[str] = []
    required = {
        name: await relation_exists(conn, name) for name in REQUIRED_RELATIONS
    }
    migration_objects = {
        name: await relation_exists(conn, name) for name in MIGRATION_RELATIONS
    }

    print("Migration 010 preflight (read-only)")
    for name, exists in migration_objects.items():
        print(f"migration_object.{name}={str(exists).lower()}")

    missing = [name for name, exists in required.items() if not exists]
    if missing:
        blockers.append("missing required relations: " + ", ".join(missing))
    if missing:
        for reason in blockers:
            print(f"BLOCKER: {reason}")
        print("RESULT=BLOCKED")
        return 2

    account_scoped_autocontent = await column_exists(
        conn,
        "autocontent_settings",
        "threads_account_id",
    )
    account_scoped_neuro = await column_exists(
        conn,
        "neuro_settings",
        "threads_account_id",
    )
    connection_status_exists = await column_exists(
        conn,
        "threads_accounts",
        "connection_status",
    )
    oauth_action_exists = await column_exists(conn, "oauth_states", "action")
    oauth_expected_account_exists = await column_exists(
        conn,
        "oauth_states",
        "expected_threads_account_id",
    )
    neuro_comment_account_scoped = await column_exists(
        conn,
        "neuro_comments",
        "threads_account_id",
    )
    print(
        "migration_column.threads_accounts.connection_status="
        f"{str(connection_status_exists).lower()}"
    )
    print(
        "migration_column.oauth_states.action="
        f"{str(oauth_action_exists).lower()}"
    )
    print(
        "migration_column.oauth_states.expected_threads_account_id="
        f"{str(oauth_expected_account_exists).lower()}"
    )
    print(
        "migration_column.autocontent_settings.threads_account_id="
        f"{str(account_scoped_autocontent).lower()}"
    )
    print(
        "migration_column.neuro_settings.threads_account_id="
        f"{str(account_scoped_neuro).lower()}"
    )
    print(
        "migration_column.neuro_comments.threads_account_id="
        f"{str(neuro_comment_account_scoped).lower()}"
    )
    applied_shape = (
        all(migration_objects.values())
        and account_scoped_autocontent
        and account_scoped_neuro
        and connection_status_exists
        and oauth_action_exists
        and oauth_expected_account_exists
        and neuro_comment_account_scoped
    )
    partial_shape = (
        any(migration_objects.values())
        or account_scoped_autocontent
        or account_scoped_neuro
        or connection_status_exists
        or oauth_action_exists
        or oauth_expected_account_exists
        or neuro_comment_account_scoped
    )

    users = await _count(conn, "select count(*) from users")
    accounts = await _count(conn, "select count(*) from threads_accounts")
    if connection_status_exists:
        connected = await _count(
            conn,
            "select count(*) from threads_accounts "
            "where connection_status = 'connected'",
        )
        disconnected = await _count(
            conn,
            "select count(*) from threads_accounts "
            "where connection_status = 'disconnected'",
        )
        error_accounts = await _count(
            conn,
            "select count(*) from threads_accounts "
            "where connection_status = 'error'",
        )
    else:
        connected = accounts
        disconnected = 0
        error_accounts = 0

    autocontent_source = (
        "autocontent_settings_user_backup_010"
        if migration_objects["autocontent_settings_user_backup_010"]
        else "autocontent_settings"
    )
    neuro_source = (
        "neuro_settings_user_backup_010"
        if migration_objects["neuro_settings_user_backup_010"]
        else "neuro_settings"
    )
    autocontent_settings = await _count(
        conn,
        f"select count(*) from {autocontent_source}",
    )
    neuro_settings = await _count(
        conn,
        f"select count(*) from {neuro_source}",
    )
    multiple_account_users = await _count(
        conn,
        """
        select count(*) from (
          select user_id from threads_accounts
          where user_id is not null
          group by user_id having count(*) > 1
        ) users_with_multiple_accounts
        """,
    )
    null_owners = await _count(
        conn,
        "select count(*) from threads_accounts where user_id is null",
    )
    duplicate_threads_ids = await _count(
        conn,
        """
        select count(*) from (
          select threads_user_id from threads_accounts
          group by threads_user_id having count(*) > 1
        ) duplicate_threads_ids
        """,
    )
    orphan_autocontent = await _count(
        conn,
        f"""
        select count(*) from {autocontent_source} setting
        left join users owner on owner.id = setting.user_id
        where owner.id is null
        """,
    )
    orphan_neuro = await _count(
        conn,
        f"""
        select count(*) from {neuro_source} setting
        left join users owner on owner.id = setting.user_id
        where owner.id is null
        """,
    )
    autocontent_without_account = await _count(
        conn,
        f"""
        select count(*) from {autocontent_source} setting
        where not exists (
          select 1 from threads_accounts account
          where account.user_id = setting.user_id
        )
        """,
    )
    neuro_without_account = await _count(
        conn,
        f"""
        select count(*) from {neuro_source} setting
        where not exists (
          select 1 from threads_accounts account
          where account.user_id = setting.user_id
        )
        """,
    )
    duplicate_neuro_targets = await _count(
        conn,
        """
        select count(*) from (
          select single_account.account_id, comment.target_post_id
          from neuro_comments comment
          join (
            select user_id, min(id) as account_id
            from threads_accounts
            group by user_id having count(*) = 1
          ) single_account on single_account.user_id = comment.user_id
          group by single_account.account_id, comment.target_post_id
          having count(*) > 1
        ) conflicts
        """,
    )

    print(f"users={users}")
    print(f"threads_accounts={accounts}")
    print(f"connected_accounts={connected}")
    print(f"disconnected_accounts={disconnected}")
    print(f"error_accounts={error_accounts}")
    print(f"legacy_autocontent_settings={autocontent_settings}")
    print(f"legacy_neuro_settings={neuro_settings}")
    print(f"users_with_multiple_accounts={multiple_account_users}")
    print(f"accounts_with_null_owner={null_owners}")
    print(f"duplicate_threads_user_ids={duplicate_threads_ids}")
    print(f"orphan_autocontent_settings={orphan_autocontent}")
    print(f"orphan_neuro_settings={orphan_neuro}")
    print(f"autocontent_settings_without_account={autocontent_without_account}")
    print(f"neuro_settings_without_account={neuro_without_account}")
    print(f"duplicate_neuro_targets={duplicate_neuro_targets}")

    catalog_rows = await conn.fetch(
        """
        select 'constraint' as object_type, relation.relname, item.conname as name
        from pg_constraint item
        join pg_class relation on relation.oid = item.conrelid
        join pg_namespace namespace on namespace.oid = relation.relnamespace
        where namespace.nspname = current_schema()
          and relation.relname in (
            'threads_accounts', 'oauth_states', 'autocontent_settings',
            'neuro_settings', 'neuro_comments', 'user_preferences'
          )
        union all
        select 'index', tablename, indexname
        from pg_indexes
        where schemaname = current_schema()
          and tablename in (
            'threads_accounts', 'oauth_states', 'autocontent_settings',
            'neuro_settings', 'neuro_comments', 'user_preferences'
          )
        order by object_type, relname, name
        """
    )
    print("catalog_objects:")
    for row in catalog_rows:
        print(f"  {row['object_type']} {row['relname']}.{row['name']}")

    if null_owners:
        blockers.append("threads_accounts contains NULL owners")
    if duplicate_threads_ids:
        blockers.append("duplicate Threads identities exist")
    if orphan_autocontent or orphan_neuro:
        blockers.append("settings rows reference missing users")
    if duplicate_neuro_targets:
        blockers.append("single-account neuro comments contain duplicates")
    if applied_shape:
        blockers.append("migration 010 is already applied")
        result = "ALREADY_APPLIED"
    elif partial_shape:
        blockers.append("migration 010 objects form a partial/mixed shape")
        result = "BLOCKED"
    else:
        result = "READY"

    for reason in blockers:
        print(f"BLOCKER: {reason}")
    print(f"RESULT={result if not blockers or result != 'READY' else 'BLOCKED'}")
    return 0 if not blockers else 2


if __name__ == "__main__":
    run_read_only(
        "Read-only preflight for migration 010",
        audit_preflight,
    )
