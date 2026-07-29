import asyncio
import logging
import os
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict

os.environ.setdefault("BOT_TOKEN", "test-bot-token")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test"
)
os.environ.setdefault("THREADS_APP_ID", "test-app-id")
os.environ.setdefault("THREADS_APP_SECRET", "test-app-secret")
os.environ.setdefault(
    "THREADS_REDIRECT_URI", "https://example.test/oauth/callback"
)
os.environ.setdefault("TOKEN_ENC_KEY", "test-encryption-key")
os.environ.setdefault("ROBOKASSA_LOGIN", "test-login")
os.environ.setdefault("ROBOKASSA_PASS1", "test-pass-1")
os.environ.setdefault("ROBOKASSA_PASS2", "test-pass-2")

from app.core import llm  # noqa: E402


class SampleResponse(BaseModel):
    model_config = ConfigDict(strict=True)

    title: str
    count: int


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(
            content=[SimpleNamespace(text=response)]
        )


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


def run_ask(monkeypatch, responses, *, typed=True):
    fake_client = FakeClient(responses)
    monkeypatch.setattr(llm, "client", fake_client)
    kwargs = {"response_model": SampleResponse} if typed else {}
    result = asyncio.run(llm.ask_json("system", "user", **kwargs))
    return result, fake_client


def test_valid_typed_json(monkeypatch):
    result, fake = run_ask(
        monkeypatch, ['{"title": "hello", "count": 2}']
    )

    assert result == SampleResponse(title="hello", count=2)
    assert len(fake.messages.calls) == 1


def test_wrong_field_type_is_repaired(monkeypatch):
    result, fake = run_ask(
        monkeypatch,
        [
            '{"title": "hello", "count": "wrong"}',
            '{"title": "hello", "count": 2}',
        ],
    )

    assert result.count == 2
    assert len(fake.messages.calls) == 2


def test_missing_required_field_is_repaired(monkeypatch):
    result, fake = run_ask(
        monkeypatch,
        [
            '{"title": "hello"}',
            '{"title": "hello", "count": 2}',
        ],
    )

    assert result.count == 2
    assert len(fake.messages.calls) == 2


def test_malformed_json_is_repaired(monkeypatch):
    result, fake = run_ask(
        monkeypatch,
        [
            '{"title": "hello", "count":',
            '{"title": "hello", "count": 2}',
        ],
    )

    assert result.count == 2
    assert len(fake.messages.calls) == 2


def test_repair_prompt_contains_error_and_original_response(monkeypatch):
    invalid = '{"title": "same meaning", "count": "two"}'
    _, fake = run_ask(
        monkeypatch,
        [invalid, '{"title": "same meaning", "count": 2}'],
    )

    repair_call = fake.messages.calls[1]
    repair_prompt = repair_call["messages"][0]["content"]
    assert "Ошибка валидации" in repair_prompt
    assert invalid in repair_prompt
    assert "Не меняй смысл ответа" in repair_prompt
    assert "Верни только исправленный JSON" in repair_prompt


def test_failed_repair_raises_controlled_error(monkeypatch):
    fake_client = FakeClient(
        [
            '{"title": "hello"}',
            '{"title": "still missing"}',
        ]
    )
    monkeypatch.setattr(llm, "client", fake_client)

    with pytest.raises(
        llm.LLMError,
        match="failed after one repair for SampleResponse",
    ):
        asyncio.run(
            llm.ask_json(
                "system", "user", response_model=SampleResponse
            )
        )

    assert len(fake_client.messages.calls) == 2


def test_untyped_call_keeps_legacy_retry_and_returns_dict(monkeypatch):
    result, fake = run_ask(
        monkeypatch,
        [
            "not json",
            '{"title": "legacy", "arbitrary": [1, 2]}',
        ],
        typed=False,
    )

    assert result == {"title": "legacy", "arbitrary": [1, 2]}
    assert isinstance(result, dict)
    assert len(fake.messages.calls) == 2


def test_invalid_response_content_is_not_logged(monkeypatch, caplog):
    secret = "private-user-content"
    fake_client = FakeClient(
        [
            f'{{"title": "{secret}"}}',
            f'{{"title": "{secret}"}}',
        ]
    )
    monkeypatch.setattr(llm, "client", fake_client)

    with caplog.at_level(logging.WARNING, logger="llm"):
        with pytest.raises(llm.LLMError):
            asyncio.run(
                llm.ask_json(
                    "system", "user", response_model=SampleResponse
                )
            )

    assert secret not in caplog.text
