"""Typed contracts for connected Threads account management."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

ConnectionStatus = Literal["connected", "disconnected", "error"]
AuthorizationStatus = Literal[
    "CONNECTED",
    "EXPIRING_SOON",
    "EXPIRED",
    "DISCONNECTED",
    "ERROR",
]
OAuthConnectionStatus = Literal[
    "connected_new",
    "refreshed",
    "ownership_conflict",
    "reconnect_mismatch",
]


class AccountModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ThreadsAccount(AccountModel):
    id: int
    user_id: int
    threads_user_id: str
    username: str | None = None
    expires_at: datetime
    connection_status: ConnectionStatus = "connected"
    disconnected_at: datetime | None = None
    selected: bool = False
    autoposting_enabled: bool = False


class SelectedThreadsAccount(ThreadsAccount):
    access_token_enc: bytes


class AccountMutationResult(AccountModel):
    account: ThreadsAccount
    affected_posts: int = 0
    next_selected: ThreadsAccount | None = None


class OAuthState(AccountModel):
    state: str
    action: Literal["connect", "reconnect"]
    expected_threads_account_id: int | None = None


class OAuthConnectionResult(AccountModel):
    status: OAuthConnectionStatus
    account_id: int | None = None
    username: str | None = None
    expected_username: str | None = None
    returned_username: str | None = None
    selected: bool = False
