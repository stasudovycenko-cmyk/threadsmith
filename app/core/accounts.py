"""Ownership boundary and lifecycle operations for Threads accounts."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.accounts import (
    AccountMutationResult,
    AuthorizationStatus,
    OAuthConnectionResult,
    OAuthState,
    SelectedThreadsAccount,
    ThreadsAccount,
)

EXPIRING_SOON_DAYS = 7


class AccountNotFoundError(LookupError):
    pass


class AccountBusyError(RuntimeError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def authorization_status(
    account: ThreadsAccount,
    *,
    now: datetime | None = None,
) -> AuthorizationStatus:
    if account.connection_status == "disconnected":
        return "DISCONNECTED"
    if account.connection_status == "error":
        return "ERROR"
    current = ensure_aware(now or utc_now())
    expires_at = ensure_aware(account.expires_at)
    if expires_at <= current:
        return "EXPIRED"
    if expires_at <= current + timedelta(days=EXPIRING_SOON_DAYS):
        return "EXPIRING_SOON"
    return "CONNECTED"


def authorization_label(
    account: ThreadsAccount,
    *,
    now: datetime | None = None,
) -> str:
    current = ensure_aware(now or utc_now())
    state = authorization_status(account, now=current)
    if state == "CONNECTED":
        return "✅ Авторизация действует"
    if state == "EXPIRING_SOON":
        days = max(
            0,
            (ensure_aware(account.expires_at) - current).days,
        )
        return f"⚠️ Истекает через {days} дн."
    if state == "EXPIRED":
        return "❌ Авторизация истекла"
    if state == "DISCONNECTED":
        return "⚪ Аккаунт отключён"
    return "❌ Ошибка подключения"


def safe_threads_id(value: str) -> str:
    if len(value) <= 6:
        return value
    return "…" + value[-6:]


def _mapping(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    mapping = getattr(row, "_mapping", row)
    return dict(mapping)


def _account_from_row(row: Any) -> ThreadsAccount:
    data = _mapping(row)
    return ThreadsAccount(
        id=data["id"],
        user_id=data["user_id"],
        threads_user_id=data["threads_user_id"],
        username=data.get("username"),
        expires_at=data["expires_at"],
        connection_status=data.get("connection_status") or "connected",
        disconnected_at=data.get("disconnected_at"),
        selected=bool(data.get("selected")),
        autoposting_enabled=bool(data.get("autoposting_enabled")),
    )


_ACCOUNT_COLUMNS = """
    account.id,
    account.user_id,
    account.threads_user_id,
    account.username,
    account.expires_at,
    account.connection_status,
    account.disconnected_at,
    (preference.selected_threads_account_id = account.id) AS selected,
    coalesce(settings.active, false) AS autoposting_enabled
"""


class ThreadsAccountService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def user_id_for_telegram(self, telegram_id: int) -> int | None:
        row = (
            await self.session.execute(
                text("SELECT id FROM users WHERE telegram_id = :telegram_id"),
                {"telegram_id": telegram_id},
            )
        ).first()
        return int(row[0]) if row else None

    async def list_accounts(
        self,
        user_id: int,
        *,
        include_disconnected: bool = True,
    ) -> list[ThreadsAccount]:
        status_filter = "" if include_disconnected else (
            "AND account.connection_status = 'connected' "
            "AND account.access_token_enc IS NOT NULL"
        )
        rows = (
            await self.session.execute(
                text(f"""
                    SELECT {_ACCOUNT_COLUMNS}
                    FROM threads_accounts account
                    LEFT JOIN user_preferences preference
                      ON preference.user_id = account.user_id
                    LEFT JOIN autocontent_settings settings
                      ON settings.threads_account_id = account.id
                     AND settings.user_id = account.user_id
                    WHERE account.user_id = :user_id
                      {status_filter}
                    ORDER BY
                      (account.connection_status = 'connected') DESC,
                      account.created_at DESC,
                      account.id DESC
                """),
                {"user_id": user_id},
            )
        ).mappings().all()
        return [_account_from_row(row) for row in rows]

    async def get_owned(
        self,
        user_id: int,
        account_id: int,
        *,
        for_update: bool = False,
    ) -> ThreadsAccount | None:
        lock = "FOR UPDATE OF account" if for_update else ""
        row = (
            await self.session.execute(
                text(f"""
                    SELECT {_ACCOUNT_COLUMNS}
                    FROM threads_accounts account
                    LEFT JOIN user_preferences preference
                      ON preference.user_id = account.user_id
                    LEFT JOIN autocontent_settings settings
                      ON settings.threads_account_id = account.id
                     AND settings.user_id = account.user_id
                    WHERE account.id = :account_id
                      AND account.user_id = :user_id
                    {lock}
                """),
                {"account_id": account_id, "user_id": user_id},
            )
        ).mappings().first()
        return _account_from_row(row) if row else None

    async def selected_account(
        self,
        user_id: int,
    ) -> ThreadsAccount | None:
        row = (
            await self.session.execute(
                text(f"""
                    SELECT {_ACCOUNT_COLUMNS}
                    FROM user_preferences preference
                    JOIN threads_accounts account
                      ON account.id = preference.selected_threads_account_id
                     AND account.user_id = preference.user_id
                    LEFT JOIN autocontent_settings settings
                      ON settings.threads_account_id = account.id
                     AND settings.user_id = account.user_id
                    WHERE preference.user_id = :user_id
                      AND account.connection_status = 'connected'
                      AND account.access_token_enc IS NOT NULL
                """),
                {"user_id": user_id},
            )
        ).mappings().first()
        if row:
            return _account_from_row(row)
        accounts = await self.list_accounts(
            user_id,
            include_disconnected=False,
        )
        if not accounts:
            return None
        await self.select_account(user_id, accounts[0].id)
        return accounts[0].model_copy(update={"selected": True})

    async def selected_credentials(
        self,
        user_id: int,
    ) -> SelectedThreadsAccount | None:
        selected = await self.selected_account(user_id)
        if selected is None:
            return None
        row = (
            await self.session.execute(
                text("""
                    SELECT access_token_enc
                    FROM threads_accounts
                    WHERE id = :account_id
                      AND user_id = :user_id
                      AND connection_status = 'connected'
                      AND access_token_enc IS NOT NULL
                """),
                {"account_id": selected.id, "user_id": user_id},
            )
        ).first()
        if not row:
            return None
        token = bytes(row[0])
        return SelectedThreadsAccount(
            **selected.model_dump(),
            access_token_enc=token,
        )

    async def select_account(
        self,
        user_id: int,
        account_id: int,
    ) -> ThreadsAccount | None:
        owned = await self.get_owned(user_id, account_id)
        if (
            owned is None
            or owned.connection_status != "connected"
        ):
            return None
        token = (
            await self.session.execute(
                text("""
                    SELECT 1
                    FROM threads_accounts
                    WHERE id = :account_id
                      AND user_id = :user_id
                      AND connection_status = 'connected'
                      AND access_token_enc IS NOT NULL
                """),
                {"user_id": user_id, "account_id": account_id},
            )
        ).first()
        if token is None:
            return None
        await self.session.execute(
            text("""
                INSERT INTO user_preferences (
                  user_id, selected_threads_account_id, updated_at
                ) VALUES (:user_id, :account_id, now())
                ON CONFLICT (user_id) DO UPDATE SET
                  selected_threads_account_id = excluded.selected_threads_account_id,
                  updated_at = now()
            """),
            {"user_id": user_id, "account_id": account_id},
        )
        return owned.model_copy(update={"selected": True})

    async def ensure_settings(
        self,
        user_id: int,
        account_id: int,
    ) -> bool:
        row = (
            await self.session.execute(
                text("""
                    INSERT INTO autocontent_settings (
                      threads_account_id, user_id
                    )
                    SELECT account.id, account.user_id
                    FROM threads_accounts account
                    WHERE account.id = :account_id
                      AND account.user_id = :user_id
                    ON CONFLICT (threads_account_id) DO NOTHING
                    RETURNING threads_account_id
                """),
                {"account_id": account_id, "user_id": user_id},
            )
        ).first()
        await self.session.execute(
            text("""
                INSERT INTO neuro_settings (
                  threads_account_id, user_id
                )
                SELECT account.id, account.user_id
                FROM threads_accounts account
                WHERE account.id = :account_id
                  AND account.user_id = :user_id
                ON CONFLICT (threads_account_id) DO NOTHING
            """),
            {"account_id": account_id, "user_id": user_id},
        )
        if row:
            return True
        return await self.get_owned(user_id, account_id) is not None

    async def copy_settings(
        self,
        user_id: int,
        source_account_id: int,
        target_account_id: int,
    ) -> bool:
        if source_account_id == target_account_id:
            return False
        row = (
            await self.session.execute(
                text("""
                    UPDATE autocontent_settings target
                    SET posts_per_day = source.posts_per_day,
                        topics = source.topics,
                        slots = source.slots,
                        days = source.days,
                        goal = source.goal,
                        timezone = source.timezone,
                        updated_at = now()
                    FROM autocontent_settings source
                    WHERE target.threads_account_id = :target_account_id
                      AND target.user_id = :user_id
                      AND source.threads_account_id = :source_account_id
                      AND source.user_id = :user_id
                    RETURNING target.threads_account_id
                """),
                {
                    "user_id": user_id,
                    "source_account_id": source_account_id,
                    "target_account_id": target_account_id,
                },
            )
        ).first()
        return row is not None

    async def create_oauth_state(
        self,
        user_id: int,
        *,
        action: str = "connect",
        expected_account_id: int | None = None,
    ) -> OAuthState:
        if action not in {"connect", "reconnect"}:
            raise ValueError("Unsupported OAuth action")
        if action == "reconnect":
            if expected_account_id is None:
                raise ValueError("Reconnect requires an account")
            if await self.get_owned(user_id, expected_account_id) is None:
                raise AccountNotFoundError("Threads account not found")
        else:
            expected_account_id = None
        state = str(uuid.uuid4())
        await self.session.execute(
            text("""
                DELETE FROM oauth_states
                WHERE user_id = :user_id
                  AND created_at <= now() - interval '30 minutes'
            """),
            {"user_id": user_id},
        )
        await self.session.execute(
            text("""
                INSERT INTO oauth_states (
                  state, user_id, action, expected_threads_account_id
                ) VALUES (
                  :state, :user_id, :action, :expected_account_id
                )
            """),
            {
                "state": state,
                "user_id": user_id,
                "action": action,
                "expected_account_id": expected_account_id,
            },
        )
        return OAuthState(
            state=state,
            action=action,
            expected_threads_account_id=expected_account_id,
        )

    async def apply_oauth_connection(
        self,
        user_id: int,
        *,
        action: str,
        expected_account_id: int | None,
        threads_user_id: str,
        username: str | None,
        access_token_enc: bytes,
        expires_at: datetime,
    ) -> OAuthConnectionResult:
        """Apply a provider identity without ever transferring ownership."""
        if action == "reconnect":
            if expected_account_id is None:
                raise AccountNotFoundError("Reconnect account is missing")
            expected = await self.get_owned(
                user_id,
                expected_account_id,
                for_update=True,
            )
            if expected is None:
                raise AccountNotFoundError("Threads account not found")
            if expected.threads_user_id != threads_user_id:
                return OAuthConnectionResult(
                    status="reconnect_mismatch",
                    expected_username=expected.username,
                    returned_username=username,
                )
            row = (
                await self.session.execute(
                    text("""
                        UPDATE threads_accounts
                        SET username = coalesce(:username, username),
                            access_token_enc = :access_token_enc,
                            expires_at = :expires_at,
                            connection_status = 'connected',
                            disconnected_at = NULL
                        WHERE id = :account_id
                          AND user_id = :user_id
                          AND threads_user_id = :threads_user_id
                        RETURNING id
                    """),
                    {
                        "account_id": expected_account_id,
                        "user_id": user_id,
                        "threads_user_id": threads_user_id,
                        "username": username,
                        "access_token_enc": access_token_enc,
                        "expires_at": expires_at,
                    },
                )
            ).first()
            if row is None:
                raise AccountNotFoundError("Threads account not found")
            await self.ensure_settings(user_id, expected_account_id)
            selected = (
                await self.session.execute(
                    text("""
                        SELECT 1 FROM user_preferences
                        WHERE user_id = :user_id
                          AND selected_threads_account_id = :account_id
                    """),
                    {
                        "user_id": user_id,
                        "account_id": expected_account_id,
                    },
                )
            ).first()
            return OAuthConnectionResult(
                status="refreshed",
                account_id=expected_account_id,
                username=username,
                selected=selected is not None,
            )

        if action != "connect":
            raise ValueError("Unsupported OAuth action")
        row = (
            await self.session.execute(
                text("""
                    INSERT INTO threads_accounts (
                      user_id, threads_user_id, username,
                      access_token_enc, expires_at,
                      connection_status, disconnected_at
                    ) VALUES (
                      :user_id, :threads_user_id, :username,
                      :access_token_enc, :expires_at,
                      'connected', NULL
                    )
                    ON CONFLICT (threads_user_id) DO UPDATE SET
                      username = coalesce(
                        excluded.username,
                        threads_accounts.username
                      ),
                      access_token_enc = excluded.access_token_enc,
                      expires_at = excluded.expires_at,
                      connection_status = 'connected',
                      disconnected_at = NULL
                    WHERE threads_accounts.user_id = excluded.user_id
                    RETURNING id, (xmax = 0) AS created
                """),
                {
                    "user_id": user_id,
                    "threads_user_id": threads_user_id,
                    "username": username,
                    "access_token_enc": access_token_enc,
                    "expires_at": expires_at,
                },
            )
        ).first()
        if row is None:
            return OAuthConnectionResult(status="ownership_conflict")
        account_id = int(row[0])
        created = bool(row[1])
        await self.ensure_settings(user_id, account_id)
        current = await self.selected_account(user_id)
        selected = current is not None and current.id == account_id
        return OAuthConnectionResult(
            status="connected_new" if created else "refreshed",
            account_id=account_id,
            username=username,
            selected=selected,
        )

    async def _choose_replacement(
        self,
        user_id: int,
        excluded_account_id: int,
    ) -> ThreadsAccount | None:
        row = (
            await self.session.execute(
                text(f"""
                    SELECT {_ACCOUNT_COLUMNS}
                    FROM threads_accounts account
                    LEFT JOIN user_preferences preference
                      ON preference.user_id = account.user_id
                    LEFT JOIN autocontent_settings settings
                      ON settings.threads_account_id = account.id
                     AND settings.user_id = account.user_id
                    WHERE account.user_id = :user_id
                      AND account.id <> :excluded_account_id
                      AND account.connection_status = 'connected'
                      AND account.access_token_enc IS NOT NULL
                    ORDER BY account.created_at DESC, account.id DESC
                    LIMIT 1
                """),
                {
                    "user_id": user_id,
                    "excluded_account_id": excluded_account_id,
                },
            )
        ).mappings().first()
        return _account_from_row(row) if row else None

    async def _set_replacement(
        self,
        user_id: int,
        removed_account_id: int,
    ) -> ThreadsAccount | None:
        replacement = await self._choose_replacement(
            user_id,
            removed_account_id,
        )
        if replacement:
            await self.select_account(user_id, replacement.id)
            return replacement.model_copy(update={"selected": True})
        await self.session.execute(
            text("""
                DELETE FROM user_preferences
                WHERE user_id = :user_id
                  AND selected_threads_account_id = :account_id
            """),
            {"user_id": user_id, "account_id": removed_account_id},
        )
        return None

    async def disconnect(
        self,
        user_id: int,
        account_id: int,
        *,
        now: datetime | None = None,
    ) -> AccountMutationResult:
        from app.core.autopost_status import AutopostStatusService

        account = await self.get_owned(
            user_id,
            account_id,
            for_update=True,
        )
        if account is None:
            raise AccountNotFoundError("Threads account not found")
        current = ensure_aware(now or utc_now())
        queue = await AutopostStatusService(self.session).clear_queue(
            user_id,
            account_id,
            disable_autoposting=True,
            now=current,
        )
        await self.session.execute(
            text("""
                UPDATE threads_accounts
                SET access_token_enc = NULL,
                    connection_status = 'disconnected',
                    disconnected_at = :now
                WHERE id = :account_id
                  AND user_id = :user_id
            """),
            {
                "account_id": account_id,
                "user_id": user_id,
                "now": current,
            },
        )
        next_selected = (
            await self._set_replacement(user_id, account_id)
            if account.selected
            else None
        )
        disconnected = account.model_copy(
            update={
                "connection_status": "disconnected",
                "disconnected_at": current,
                "selected": False,
                "autoposting_enabled": False,
            }
        )
        return AccountMutationResult(
            account=disconnected,
            affected_posts=queue.deleted_posts,
            next_selected=next_selected,
        )

    async def delete_account_data(
        self,
        user_id: int,
        account_id: int,
        *,
        now: datetime | None = None,
    ) -> AccountMutationResult:
        from app.core.autopost_status import AutopostStatusService

        account = await self.get_owned(
            user_id,
            account_id,
            for_update=True,
        )
        if account is None:
            raise AccountNotFoundError("Threads account not found")
        service = AutopostStatusService(self.session)
        await service.lock_queue(user_id, account_id)
        publishing = (
            await self.session.execute(
                text("""
                    SELECT 1
                    FROM scheduled_posts
                    WHERE user_id = :user_id
                      AND threads_account_id = :account_id
                      AND status = 'publishing'
                    FOR UPDATE
                    LIMIT 1
                """),
                {"user_id": user_id, "account_id": account_id},
            )
        ).first()
        if publishing:
            raise AccountBusyError("Account has a publishing post")
        next_selected = (
            await self._set_replacement(user_id, account_id)
            if account.selected
            else None
        )
        post_count = (
            await self.session.execute(
                text("""
                    SELECT count(*)
                    FROM scheduled_posts
                    WHERE user_id = :user_id
                      AND threads_account_id = :account_id
                """),
                {"user_id": user_id, "account_id": account_id},
            )
        ).first()
        params = {"user_id": user_id, "account_id": account_id}
        await self.session.execute(text("""
            DELETE FROM insights_snapshots snapshot
            WHERE snapshot.threads_post_id IN (
              SELECT post.threads_post_id
              FROM scheduled_posts post
              WHERE post.user_id = :user_id
                AND post.threads_account_id = :account_id
                AND post.threads_post_id IS NOT NULL
            )
        """), params)
        await self.session.execute(text("""
            DELETE FROM replies_log reply
            WHERE reply.threads_post_id IN (
              SELECT post.threads_post_id
              FROM scheduled_posts post
              WHERE post.user_id = :user_id
                AND post.threads_account_id = :account_id
                AND post.threads_post_id IS NOT NULL
            )
        """), params)
        for statement in (
            "DELETE FROM poll_state WHERE threads_account_id = :account_id",
            "DELETE FROM autopost_runs WHERE user_id = :user_id AND threads_account_id = :account_id",
            "DELETE FROM search_quota WHERE threads_account_id = :account_id",
            "DELETE FROM ai_usage_events WHERE user_id = :user_id AND threads_account_id = :account_id",
            "DELETE FROM brains WHERE user_id = :user_id AND threads_account_id = :account_id",
            "DELETE FROM neuro_comments WHERE user_id = :user_id AND threads_account_id = :account_id",
            "DELETE FROM neuro_settings WHERE user_id = :user_id AND threads_account_id = :account_id",
            "DELETE FROM generations WHERE user_id = :user_id AND (input ->> 'threads_account_id' = CAST(:account_id AS text) OR output -> 'metadata' ->> 'threads_account_id' = CAST(:account_id AS text))",
            "DELETE FROM scheduled_posts WHERE user_id = :user_id AND threads_account_id = :account_id",
            "DELETE FROM autocontent_settings WHERE user_id = :user_id AND threads_account_id = :account_id",
        ):
            await self.session.execute(text(statement), params)
        deleted = (
            await self.session.execute(
                text("""
                    DELETE FROM threads_accounts
                    WHERE id = :account_id
                      AND user_id = :user_id
                    RETURNING id
                """),
                params,
            )
        ).first()
        if not deleted:
            raise AccountNotFoundError("Threads account not found")
        return AccountMutationResult(
            account=account.model_copy(update={"selected": False}),
            affected_posts=int(post_count[0] if post_count else 0),
            next_selected=next_selected,
        )

    async def record_deletion_request(
        self,
        threads_user_id: str,
        *,
        status: str,
    ) -> str:
        confirmation_code = uuid.uuid4().hex
        digest = hashlib.sha256(threads_user_id.encode("utf-8")).hexdigest()
        await self.session.execute(
            text("""
                INSERT INTO threads_data_deletion_requests (
                  confirmation_code, threads_user_id_hash,
                  status, completed_at
                ) VALUES (
                  :confirmation_code, :threads_user_id_hash,
                  :status,
                  CASE WHEN :status = 'completed' THEN now() ELSE NULL END
                )
            """),
            {
                "confirmation_code": confirmation_code,
                "threads_user_id_hash": digest,
                "status": status,
            },
        )
        return confirmation_code
