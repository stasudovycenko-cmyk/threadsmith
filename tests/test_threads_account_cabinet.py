import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.handlers.cabinet import _callback_account_id
from app.bot.handlers.menu import main_menu_kb
from app.core import autopilot
from app.core.accounts import (
    AccountBusyError,
    ThreadsAccountService,
    authorization_status,
    safe_threads_id,
)
from app.core.autopost_status import AutopostStatusService
from app.core.meta_callbacks import InvalidSignedRequest, verify_signed_request
from app.schemas.accounts import ThreadsAccount
from scripts.migration_010_common import (
    normalize_database_url,
    run_read_only,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


class FakeResult:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def mappings(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class ScriptedSession:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, dict(params or {})))
        rows = self.responses.pop(0) if self.responses else []
        return FakeResult(rows)


class DeleteSession(ScriptedSession):
    def __init__(self, *, publishing=False):
        super().__init__()
        self.publishing = publishing

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, dict(params or {})))
        if "status = 'publishing'" in sql and "SELECT 1" in sql:
            return FakeResult([(1,)] if self.publishing else [])
        if "SELECT count(*)" in sql and "scheduled_posts" in sql:
            return FakeResult([(3,)])
        if "DELETE FROM threads_accounts" in sql:
            return FakeResult([(11,)])
        return FakeResult()


def account_row(account_id=11, *, selected=False, status="connected"):
    return {
        "id": account_id,
        "user_id": 7,
        "threads_user_id": f"threads-{account_id}",
        "username": f"account_{account_id}",
        "expires_at": NOW + timedelta(days=30),
        "connection_status": status,
        "disconnected_at": None,
        "selected": selected,
        "autoposting_enabled": True,
    }


def account(account_id=11, *, selected=False, status="connected"):
    return ThreadsAccount.model_validate(
        account_row(account_id, selected=selected, status=status)
    )


@pytest.mark.parametrize(
    ("expires_at", "connection_status", "expected"),
    [
        (NOW + timedelta(days=30), "connected", "CONNECTED"),
        (NOW + timedelta(days=4), "connected", "EXPIRING_SOON"),
        (NOW - timedelta(seconds=1), "connected", "EXPIRED"),
        (NOW + timedelta(days=30), "disconnected", "DISCONNECTED"),
        (NOW + timedelta(days=30), "error", "ERROR"),
    ],
)
def test_authorization_states(expires_at, connection_status, expected):
    item = account().model_copy(update={
        "expires_at": expires_at,
        "connection_status": connection_status,
    })
    assert authorization_status(item, now=NOW) == expected


def test_safe_threads_id_and_callback_parsing():
    assert safe_threads_id("1234567890") == "…567890"
    assert _callback_account_id("cab:account:42") == 42
    assert _callback_account_id("cab:account:not-an-id") is None


def _signed_request(payload, secret="secret"):
    encoded_payload = base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).decode().rstrip("=")
    signature = hmac.new(
        secret.encode(),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{encoded_signature}.{encoded_payload}"


def test_meta_signed_request_is_verified_and_tampering_is_rejected():
    signed = _signed_request({
        "algorithm": "HMAC-SHA256",
        "user_id": "threads-11",
    })
    assert verify_signed_request(signed, "secret")["user_id"] == "threads-11"
    with pytest.raises(InvalidSignedRequest):
        verify_signed_request(signed + "x", "secret")
    with pytest.raises(InvalidSignedRequest):
        verify_signed_request(
            _signed_request({"algorithm": "none", "user_id": "11"}),
            "secret",
        )


def test_selected_account_handles_zero_one_and_two_accounts():
    empty_session = ScriptedSession([[], []])
    assert asyncio.run(
        ThreadsAccountService(empty_session).selected_account(7)
    ) is None

    one_session = ScriptedSession([
        [],
        [account_row(11)],
        [account_row(11)],
        [(1,)],
        [],
    ])
    selected = asyncio.run(
        ThreadsAccountService(one_session).selected_account(7)
    )
    assert selected.id == 11
    assert selected.selected is True

    two_session = ScriptedSession([[account_row(22, selected=True)]])
    selected = asyncio.run(
        ThreadsAccountService(two_session).selected_account(7)
    )
    assert selected.id == 22


def test_select_rejects_foreign_account_and_switches_owned_account():
    foreign_session = ScriptedSession([[]])
    assert asyncio.run(
        ThreadsAccountService(foreign_session).select_account(7, 99)
    ) is None
    assert len(foreign_session.calls) == 1
    assert foreign_session.calls[0][1] == {"account_id": 99, "user_id": 7}

    owned_session = ScriptedSession([
        [account_row(22)],
        [(1,)],
        [],
    ])
    selected = asyncio.run(
        ThreadsAccountService(owned_session).select_account(7, 22)
    )
    assert selected.id == 22
    assert selected.selected is True
    preference_sql, params = owned_session.calls[-1]
    assert "user_preferences" in preference_sql
    assert params == {"user_id": 7, "account_id": 22}


def test_oauth_state_is_ttl_bound_owned_and_action_scoped():
    connect_session = ScriptedSession([[], []])
    connect = asyncio.run(
        ThreadsAccountService(connect_session).create_oauth_state(
            7,
            action="connect",
        )
    )
    assert connect.action == "connect"
    assert connect.expected_threads_account_id is None
    assert "interval '30 minutes'" in connect_session.calls[0][0]
    assert connect_session.calls[1][1]["action"] == "connect"

    reconnect_session = ScriptedSession([[], []])
    service = ThreadsAccountService(reconnect_session)
    service.get_owned = AsyncMock(return_value=account(11))
    reconnect = asyncio.run(service.create_oauth_state(
        7,
        action="reconnect",
        expected_account_id=11,
    ))
    assert reconnect.action == "reconnect"
    assert reconnect.expected_threads_account_id == 11
    service.get_owned.assert_awaited_once_with(7, 11)
    assert reconnect_session.calls[1][1]["expected_account_id"] == 11


def test_new_account_gets_owned_default_settings():
    session = ScriptedSession([[(11,)], []])
    created = asyncio.run(
        ThreadsAccountService(session).ensure_settings(7, 11)
    )
    assert created is True
    autocontent_sql, autocontent_params = session.calls[0]
    neuro_sql, neuro_params = session.calls[1]
    radar_sql, radar_params = session.calls[2]
    assert "INSERT INTO autocontent_settings" in autocontent_sql
    assert "account.user_id = :user_id" in autocontent_sql
    assert "INSERT INTO neuro_settings" in neuro_sql
    assert "INSERT INTO radar_settings" in radar_sql
    assert autocontent_params == neuro_params == radar_params == {
        "account_id": 11,
        "user_id": 7,
    }


@pytest.mark.parametrize(
    ("insert_row", "expected"),
    [
        ((11, True), "connected_new"),
        ((11, False), "refreshed"),
        (None, "ownership_conflict"),
    ],
)
def test_connect_new_existing_and_foreign_owner(insert_row, expected):
    session = ScriptedSession([[insert_row] if insert_row else []])
    service = ThreadsAccountService(session)
    service.ensure_settings = AsyncMock(return_value=True)
    service.selected_account = AsyncMock(
        return_value=account(11, selected=True)
    )
    result = asyncio.run(service.apply_oauth_connection(
        7,
        action="connect",
        expected_account_id=None,
        threads_user_id="threads-11",
        username="creator",
        access_token_enc=b"encrypted",
        expires_at=NOW + timedelta(days=60),
    ))
    assert result.status == expected
    sql = session.calls[0][0]
    assert "WHERE threads_accounts.user_id = excluded.user_id" in sql
    assert "user_id = excluded.user_id" not in sql.split(
        "DO UPDATE SET", 1
    )[1].split("WHERE", 1)[0]
    if expected == "ownership_conflict":
        service.ensure_settings.assert_not_awaited()
    else:
        service.ensure_settings.assert_awaited_once_with(7, 11)


def test_reconnect_updates_only_expected_identity_and_rejects_mismatch():
    mismatch_session = ScriptedSession()
    mismatch = ThreadsAccountService(mismatch_session)
    mismatch.get_owned = AsyncMock(return_value=account(11))
    result = asyncio.run(mismatch.apply_oauth_connection(
        7,
        action="reconnect",
        expected_account_id=11,
        threads_user_id="threads-other",
        username="other",
        access_token_enc=b"encrypted",
        expires_at=NOW + timedelta(days=60),
    ))
    assert result.status == "reconnect_mismatch"
    assert mismatch_session.calls == []

    session = ScriptedSession([[(11,)], [(1,)]])
    service = ThreadsAccountService(session)
    service.get_owned = AsyncMock(return_value=account(11))
    service.ensure_settings = AsyncMock(return_value=True)
    result = asyncio.run(service.apply_oauth_connection(
        7,
        action="reconnect",
        expected_account_id=11,
        threads_user_id="threads-11",
        username="account_11",
        access_token_enc=b"encrypted",
        expires_at=NOW + timedelta(days=60),
    ))
    assert result.status == "refreshed"
    assert result.account_id == 11
    update_sql, update_params = session.calls[0]
    assert "id = :account_id" in update_sql
    assert "user_id = :user_id" in update_sql
    assert "threads_user_id = :threads_user_id" in update_sql
    assert update_params["account_id"] == 11


def test_disconnect_is_account_scoped_preserves_history_and_credits(monkeypatch):
    session = ScriptedSession([[]])
    service = ThreadsAccountService(session)
    service.get_owned = AsyncMock(return_value=account(11, selected=True))
    service._set_replacement = AsyncMock(
        return_value=account(22, selected=True)
    )
    clear = AsyncMock(return_value=SimpleNamespace(deleted_posts=4))
    monkeypatch.setattr(AutopostStatusService, "clear_queue", clear)
    result = asyncio.run(service.disconnect(7, 11, now=NOW))
    assert result.affected_posts == 4
    assert result.next_selected.id == 22
    clear.assert_awaited_once_with(
        7,
        11,
        disable_autoposting=True,
        now=NOW,
    )
    sql, params = session.calls[0]
    assert "access_token_enc = NULL" in sql
    assert params["user_id"] == 7 and params["account_id"] == 11
    all_sql = "\n".join(call[0] for call in session.calls)
    assert "credits" not in all_sql
    assert "DELETE FROM autopost_runs" not in all_sql


def test_disconnect_last_account_clears_selection(monkeypatch):
    session = ScriptedSession([[]])
    service = ThreadsAccountService(session)
    service.get_owned = AsyncMock(return_value=account(11, selected=True))
    service._set_replacement = AsyncMock(return_value=None)
    monkeypatch.setattr(
        AutopostStatusService,
        "clear_queue",
        AsyncMock(return_value=SimpleNamespace(deleted_posts=0)),
    )
    result = asyncio.run(service.disconnect(7, 11, now=NOW))
    assert result.next_selected is None
    assert result.account.connection_status == "disconnected"


def test_full_delete_is_scoped_and_refuses_publishing(monkeypatch):
    busy_session = DeleteSession(publishing=True)
    busy = ThreadsAccountService(busy_session)
    busy.get_owned = AsyncMock(return_value=account(11))
    monkeypatch.setattr(AutopostStatusService, "lock_queue", AsyncMock())
    with pytest.raises(AccountBusyError):
        asyncio.run(busy.delete_account_data(7, 11))
    assert not any(
        "DELETE FROM threads_accounts" in sql
        for sql, _ in busy_session.calls
    )

    session = DeleteSession()
    service = ThreadsAccountService(session)
    service.get_owned = AsyncMock(return_value=account(11))
    result = asyncio.run(service.delete_account_data(7, 11))
    assert result.affected_posts == 3
    delete_calls = [
        (sql, params)
        for sql, params in session.calls
        if "DELETE FROM" in sql
    ]
    assert delete_calls
    assert all(params.get("account_id") == 11 for _, params in delete_calls)
    all_sql = "\n".join(sql for sql, _ in session.calls)
    assert "DELETE FROM users" not in all_sql
    assert "subscriptions" not in all_sql
    assert "credits_ledger" not in all_sql


def test_settings_copy_is_account_scoped_and_does_not_copy_active():
    session = ScriptedSession([[(22,)]])
    copied = asyncio.run(
        ThreadsAccountService(session).copy_settings(7, 11, 22)
    )
    assert copied is True
    sql, params = session.calls[0]
    assert "target.threads_account_id = :target_account_id" in sql
    assert "source.threads_account_id = :source_account_id" in sql
    assert "target.user_id = :user_id" in sql
    assert "active = source.active" not in sql
    assert params == {
        "user_id": 7,
        "source_account_id": 11,
        "target_account_id": 22,
    }


def test_deletion_confirmation_is_random_and_stores_only_identity_hash():
    session = ScriptedSession()
    service = ThreadsAccountService(session)
    first = asyncio.run(service.record_deletion_request(
        "threads-secret-identity",
        status="received",
    ))
    second = asyncio.run(service.record_deletion_request(
        "threads-secret-identity",
        status="received",
    ))

    assert re.fullmatch(r"[0-9a-f]{32}", first)
    assert re.fullmatch(r"[0-9a-f]{32}", second)
    assert first != second
    for _, params in session.calls:
        assert params["confirmation_code"] not in {
            "threads-secret-identity",
            params["threads_user_id_hash"],
        }
        assert params["threads_user_id_hash"] == hashlib.sha256(
            b"threads-secret-identity"
        ).hexdigest()


def test_migration_keeps_backups_validates_copy_and_refuses_repeat_run():
    migration = (ROOT / "migrations/010_threads_account_cabinet.sql").read_text(
        encoding="utf-8"
    )
    rollback = (
        ROOT / "migrations/rollback/010_threads_account_cabinet.sql"
    ).read_text(encoding="utf-8")
    assert "create table user_preferences" in migration.lower()
    assert "selected_threads_account_id" in migration
    assert "autocontent_settings_user_backup_010" in migration
    assert "neuro_settings_user_backup_010" in migration
    assert "drop table autocontent_settings_user_backup_010" not in migration.lower()
    assert "drop table neuro_settings_user_backup_010" not in migration.lower()
    assert "autocontent count mismatch" in migration
    assert "neuro count mismatch" in migration
    assert "already applied; refusing a repeat run" in migration
    assert "unique (id, user_id)" in migration.lower()
    assert "on delete no action" in migration.lower()
    assert "account.id" in migration and "account.user_id" in migration
    assert "expected_threads_account_id" in migration
    assert "action = 'reconnect'" in migration
    assert "access_token_enc drop not null" in migration.lower()
    assert "rename to autocontent_settings" in rollback.lower()
    assert "rename to neuro_settings" in rollback.lower()
    assert "migration backup tables were modified" in rollback.lower()
    assert "reconnect disconnected/error accounts first" in rollback.lower()


def test_migration_helpers_are_read_only_and_runtime_ignores_backups():
    preflight = (
        ROOT / "scripts/preflight_migration_010.py"
    ).read_text(encoding="utf-8")
    validation = (
        ROOT / "scripts/validate_migration_010.py"
    ).read_text(encoding="utf-8")
    common = (
        ROOT / "scripts/migration_010_common.py"
    ).read_text(encoding="utf-8")
    assert "transaction(readonly=True)" in common
    assert "RESULT=BLOCKED" in preflight
    assert "RESULT={'READY' if not blockers else 'BLOCKED'}" in validation

    runtime = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "app").rglob("*.py")
    )
    assert "autocontent_settings_user_backup_010" not in runtime
    assert "neuro_settings_user_backup_010" not in runtime


def test_migration_helper_normalizes_sqlalchemy_dsn_and_owns_event_loop():
    assert normalize_database_url(
        "postgresql+asyncpg://user:pass@db.example/database"
    ) == "postgresql://user:pass@db.example/database"
    assert normalize_database_url(
        "postgresql://user:pass@db.example/database"
    ) == "postgresql://user:pass@db.example/database"
    assert not inspect.iscoroutinefunction(run_read_only)


def test_runtime_sql_encodes_planner_publisher_disconnect_race_guards():
    planner = (
        ROOT / "app/worker/autocontent.py"
    ).read_text(encoding="utf-8")
    accounts = (ROOT / "app/core/accounts.py").read_text(encoding="utf-8")
    publisher = (ROOT / "app/core/autopilot.py").read_text(encoding="utf-8")
    assert "ta.id = ac.threads_account_id" in planner
    assert "ta.connection_status = 'connected'" in planner
    assert "ta.access_token_enc IS NOT NULL" in planner
    assert "account.connection_status = 'connected'" in publisher
    assert "post.status = 'pending'" in publisher
    publishing_guard = accounts.split("status = 'publishing'", 1)[1][:160]
    assert "FOR UPDATE" in publishing_guard
    assert "SKIP LOCKED" not in publishing_guard


def test_main_menu_exposes_cabinet():
    labels = [
        button.text
        for row in main_menu_kb().inline_keyboard
        for button in row
    ]
    assert "👤 Личный кабинет" in labels


def test_claim_due_posts_requires_connected_owned_account():
    session = ScriptedSession([[]])
    asyncio.run(autopilot.claim_due_posts(session))
    sql = session.calls[0][0]
    assert "account.id = post.threads_account_id" in sql
    assert "account.user_id = post.user_id" in sql
    assert "account.connection_status = 'connected'" in sql
    assert "account.access_token_enc IS NOT NULL" in sql
