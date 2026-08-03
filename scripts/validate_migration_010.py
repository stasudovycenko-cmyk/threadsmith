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
    "threads_account_cabinet_migration_010",
    "user_preferences",
    "autocontent_settings",
    "neuro_settings",
    "autocontent_settings_user_backup_010",
    "neuro_settings_user_backup_010",
    "threads_data_deletion_requests",
)
REQUIRED_COLUMNS = (
    ("threads_accounts", "connection_status"),
    ("threads_accounts", "disconnected_at"),
    ("oauth_states", "action"),
    ("oauth_states", "expected_threads_account_id"),
    ("autocontent_settings", "threads_account_id"),
    ("neuro_settings", "threads_account_id"),
    ("neuro_comments", "threads_account_id"),
)
REQUIRED_CONSTRAINTS = {
    "threads_accounts": {
        "threads_accounts_connection_status_check",
        "threads_accounts_id_user_id_key",
    },
    "user_preferences": {"user_preferences_selected_owner_fk"},
    "oauth_states": {
        "oauth_states_action_check",
        "oauth_states_expected_owner_fk",
    },
    "autocontent_settings": {"autocontent_settings_account_owner_fk"},
    "neuro_settings": {"neuro_settings_account_owner_fk"},
}
REQUIRED_INDEXES = {
    ("autocontent_settings", "autocontent_settings_account_owner_idx"),
    ("neuro_settings", "neuro_settings_account_owner_idx"),
    ("neuro_comments", "neuro_comments_account_target_unique"),
}


async def _count(conn: asyncpg.Connection, sql: str) -> int:
    return int(await conn.fetchval(sql) or 0)


async def audit_post_migration(conn: asyncpg.Connection) -> int:
    blockers: list[str] = []
    print("Migration 010 post-validation (read-only)")

    missing_relations = [
        name
        for name in REQUIRED_RELATIONS
        if not await relation_exists(conn, name)
    ]
    missing_columns = [
        f"{table}.{column}"
        for table, column in REQUIRED_COLUMNS
        if not await column_exists(conn, table, column)
    ]
    if missing_relations:
        blockers.append(
            "missing required relations: " + ", ".join(missing_relations)
        )
    if missing_columns:
        blockers.append(
            "missing required columns: " + ", ".join(missing_columns)
        )
    if blockers:
        for reason in blockers:
            print(f"BLOCKER: {reason}")
        print("RESULT=BLOCKED")
        return 2

    account_count = await _count(
        conn,
        "select count(*) from threads_accounts where user_id is not null",
    )
    autocontent_count = await _count(
        conn,
        "select count(*) from autocontent_settings",
    )
    neuro_count = await _count(conn, "select count(*) from neuro_settings")
    autocontent_ownership = await _count(
        conn,
        """
        select count(*) from autocontent_settings setting
        left join threads_accounts account
          on account.id = setting.threads_account_id
         and account.user_id = setting.user_id
        where account.id is null
        """,
    )
    neuro_ownership = await _count(
        conn,
        """
        select count(*) from neuro_settings setting
        left join threads_accounts account
          on account.id = setting.threads_account_id
         and account.user_id = setting.user_id
        where account.id is null
        """,
    )
    selected_ownership = await _count(
        conn,
        """
        select count(*) from user_preferences preference
        left join threads_accounts account
          on account.id = preference.selected_threads_account_id
         and account.user_id = preference.user_id
        where preference.selected_threads_account_id is not null
          and account.id is null
        """,
    )
    connected_without_token = await _count(
        conn,
        """
        select count(*) from threads_accounts
        where connection_status = 'connected' and access_token_enc is null
        """,
    )
    disconnected_without_token = await _count(
        conn,
        """
        select count(*) from threads_accounts
        where connection_status = 'disconnected' and access_token_enc is null
        """,
    )
    invalid_statuses = await _count(
        conn,
        """
        select count(*) from threads_accounts
        where connection_status not in ('connected', 'disconnected', 'error')
        """,
    )
    duplicate_autocontent = await _count(
        conn,
        """
        select count(*) from (
          select threads_account_id from autocontent_settings
          group by threads_account_id having count(*) > 1
        ) duplicates
        """,
    )
    duplicate_neuro = await _count(
        conn,
        """
        select count(*) from (
          select threads_account_id from neuro_settings
          group by threads_account_id having count(*) > 1
        ) duplicates
        """,
    )
    invalid_oauth = await _count(
        conn,
        """
        select count(*) from oauth_states state
        left join threads_accounts account
          on account.id = state.expected_threads_account_id
         and account.user_id = state.user_id
        where (state.action = 'connect'
               and state.expected_threads_account_id is not null)
           or (state.action = 'reconnect'
               and (state.expected_threads_account_id is null
                    or account.id is null))
           or state.action not in ('connect', 'reconnect')
        """,
    )
    marker_rows = await _count(
        conn,
        "select count(*) from threads_account_cabinet_migration_010",
    )

    print(f"owned_accounts={account_count}")
    print(f"autocontent_settings={autocontent_count}")
    print(f"neuro_settings={neuro_count}")
    print(f"autocontent_ownership_mismatches={autocontent_ownership}")
    print(f"neuro_ownership_mismatches={neuro_ownership}")
    print(f"selected_account_ownership_mismatches={selected_ownership}")
    print(f"connected_accounts_without_token={connected_without_token}")
    print(f"disconnected_accounts_without_token={disconnected_without_token}")
    print(f"invalid_connection_statuses={invalid_statuses}")
    print(f"duplicate_autocontent_settings={duplicate_autocontent}")
    print(f"duplicate_neuro_settings={duplicate_neuro}")
    print(f"invalid_oauth_states={invalid_oauth}")
    print(f"migration_marker_rows={marker_rows}")

    if autocontent_count != account_count:
        blockers.append("autocontent row count does not match account count")
    if neuro_count != account_count:
        blockers.append("neuro row count does not match account count")
    if autocontent_ownership or neuro_ownership:
        blockers.append("account settings contain ownership/orphan anomalies")
    if selected_ownership:
        blockers.append("a selected account belongs to another user or is missing")
    if connected_without_token:
        blockers.append("a connected account has no encrypted token")
    if invalid_statuses:
        blockers.append("an account has an invalid connection status")
    if duplicate_autocontent or duplicate_neuro:
        blockers.append("duplicate account settings exist")
    if invalid_oauth:
        blockers.append("invalid OAuth action or reconnect ownership exists")
    if marker_rows != 1:
        blockers.append("migration marker must contain exactly one row")

    constraint_rows = await conn.fetch(
        """
        select relation.relname, item.conname
        from pg_constraint item
        join pg_class relation on relation.oid = item.conrelid
        join pg_namespace namespace on namespace.oid = relation.relnamespace
        where namespace.nspname = current_schema()
        """
    )
    present_constraints = {
        (row["relname"], row["conname"]) for row in constraint_rows
    }
    for table, names in REQUIRED_CONSTRAINTS.items():
        for name in names:
            if (table, name) not in present_constraints:
                blockers.append(f"missing constraint {table}.{name}")

    present_indexes = {
        (row["tablename"], row["indexname"])
        for row in await conn.fetch(
            "select tablename, indexname from pg_indexes "
            "where schemaname = current_schema()"
        )
    }
    for table, name in sorted(REQUIRED_INDEXES - present_indexes):
        blockers.append(f"missing index {table}.{name}")

    for table in (
        "autocontent_settings_user_backup_010",
        "neuro_settings_user_backup_010",
    ):
        comment = await conn.fetchval(
            "select obj_description(to_regclass($1), 'pg_class')",
            table,
        )
        print(f"backup_comment.{table}={comment or '<missing>'}")
        if not comment:
            blockers.append(f"backup table {table} has no comment")

    for reason in blockers:
        print(f"BLOCKER: {reason}")
    print(f"RESULT={'READY' if not blockers else 'BLOCKED'}")
    return 0 if not blockers else 2


if __name__ == "__main__":
    run_read_only(
        "Read-only post-validation for migration 010",
        audit_post_migration,
    )
