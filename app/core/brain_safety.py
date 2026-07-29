"""Shared validation for data persisted by Social Brain services."""

from typing import Any

_SENSITIVE_KEY_PARTS = (
    "access_token",
    "refresh_token",
    "bot_token",
    "telegram_token",
    "api_key",
    "secret",
    "password",
    "oauth_token",
    "authorization",
    "oauth",
    "prompt",
)


def is_sensitive_key(key: Any) -> bool:
    normalized = str(key).strip().lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def assert_safe_payload(value: Any, path: str = "payload") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if is_sensitive_key(key):
                raise ValueError(
                    f"sensitive field is not allowed in Social Brain: "
                    f"{path}.{key}"
                )
            assert_safe_payload(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            assert_safe_payload(item, f"{path}[{index}]")
