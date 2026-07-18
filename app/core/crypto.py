"""
AES-256-GCM для Threads-токенов. Nonce кладём в начало шифртекста.
Ключ - в env, не в базе. Утечёт дамп базы - токены бесполезны.
"""
import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings

_key = base64.b64decode(settings.TOKEN_ENC_KEY)
assert len(_key) == 32, "TOKEN_ENC_KEY должен быть 32 байта в base64"


def encrypt_token(token: str) -> bytes:
    nonce = os.urandom(12)
    ct = AESGCM(_key).encrypt(nonce, token.encode(), None)
    return nonce + ct


def decrypt_token(blob: bytes) -> str:
    nonce, ct = blob[:12], blob[12:]
    return AESGCM(_key).decrypt(nonce, ct, None).decode()
