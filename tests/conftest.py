import os

_TEST_ENV = {
    "BOT_TOKEN": "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
    "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
    "THREADS_APP_ID": "test-app-id",
    "THREADS_APP_SECRET": "test-app-secret",
    "THREADS_REDIRECT_URI": "https://example.test/oauth/callback",
    "TOKEN_ENC_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "ROBOKASSA_LOGIN": "test-login",
    "ROBOKASSA_PASS1": "test-pass-1",
    "ROBOKASSA_PASS2": "test-pass-2",
}

for key, value in _TEST_ENV.items():
    os.environ.setdefault(key, value)
