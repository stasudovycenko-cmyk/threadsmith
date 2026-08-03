"""Verification helpers for Meta deauthorization and deletion callbacks."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any


class InvalidSignedRequest(ValueError):
    pass


def _decode_base64url(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (ValueError, TypeError) as error:
        raise InvalidSignedRequest("Invalid base64url payload") from error


def verify_signed_request(signed_request: str, app_secret: str) -> dict[str, Any]:
    """Return a verified Meta payload or raise a non-sensitive error."""
    try:
        encoded_signature, encoded_payload = signed_request.split(".", 1)
    except (AttributeError, ValueError) as error:
        raise InvalidSignedRequest("Malformed signed request") from error

    signature = _decode_base64url(encoded_signature)
    expected = hmac.new(
        app_secret.encode("utf-8"),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(signature, expected):
        raise InvalidSignedRequest("Invalid signed request signature")
    try:
        payload = json.loads(_decode_base64url(encoded_payload))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise InvalidSignedRequest("Invalid signed request payload") from error
    if not isinstance(payload, dict):
        raise InvalidSignedRequest("Signed request payload must be an object")
    if str(payload.get("algorithm", "")).upper() != "HMAC-SHA256":
        raise InvalidSignedRequest("Unsupported signed request algorithm")
    if not payload.get("user_id"):
        raise InvalidSignedRequest("Signed request user is missing")
    return payload
