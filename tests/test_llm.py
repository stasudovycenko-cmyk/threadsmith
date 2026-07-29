import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict

from app.core import llm


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
        if hasattr(response, "content"):
            return response
        return SimpleNamespace(
            content=[SimpleNamespace(text=response)],
            usage=SimpleNamespace(input_tokens=0, output_tokens=0),
        )


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


def run_ask(monkeypatch, responses, *, typed=True, feature="test"):
    fake_client = FakeClient(responses)
    monkeypatch.setattr(llm, "client", fake_client)
    kwargs = {"response_model": SampleResponse} if typed else {}
    result = asyncio.run(
        llm.ask_json("system", "user", feature=feature, **kwargs)
    )
    return result, fake_client


def usage_events(caplog):
    return [
        json.loads(record.message.removeprefix("llm_call "))
        for record in caplog.records
        if record.message.startswith("llm_call ")
    ]


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
    fake_client = FakeClient(
        [invalid, '{"title": "same meaning", "count": 2}']
    )
    monkeypatch.setattr(llm, "client", fake_client)

    asyncio.run(
        llm.ask_json(
            "original-system-marker",
            "original-user-marker",
            response_model=SampleResponse,
            feature="repair_test",
        )
    )

    repair_call = fake_client.messages.calls[1]
    repair_prompt = repair_call["messages"][0]["content"]
    assert "Ошибка:" in repair_prompt
    assert invalid in repair_prompt
    assert "original-system-marker" not in repair_call["system"]
    assert "original-system-marker" not in repair_prompt
    assert "original-user-marker" not in repair_prompt
    assert "не меняя смысл" in repair_call["system"]
    assert "валидным JSON" in repair_call["system"]
    assert len(fake_client.messages.calls) == 2


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


def test_usage_is_extracted_into_structured_log(monkeypatch, caplog):
    response = SimpleNamespace(
        content=[
            SimpleNamespace(text='{"title": "hello", "count": 2}')
        ],
        usage=SimpleNamespace(
            input_tokens=321,
            output_tokens=45,
            cache_read_input_tokens=120,
            cache_creation_input_tokens=30,
        ),
    )

    with caplog.at_level(logging.INFO, logger="llm"):
        run_ask(
            monkeypatch,
            [response],
            feature="usage_test",
        )

    events = usage_events(caplog)
    assert events == [
        {
            "event": "llm_call",
            "feature": "usage_test",
            "model": llm.MODEL,
            "input_tokens": 321,
            "output_tokens": 45,
            "cache_read_tokens": 120,
            "cache_creation_tokens": 30,
            "latency_ms": events[0]["latency_ms"],
            "attempt": 1,
            "status": "success",
            "max_tokens": llm.DEFAULT_MAX_TOKENS,
        }
    ]
    assert events[0]["latency_ms"] >= 0


def test_validation_failure_and_repair_have_separate_usage_logs(
    monkeypatch, caplog
):
    with caplog.at_level(logging.INFO, logger="llm"):
        run_ask(
            monkeypatch,
            [
                '{"title": "hello"}',
                '{"title": "hello", "count": 2}',
            ],
            feature="repair_metrics",
        )

    events = usage_events(caplog)
    assert [event["attempt"] for event in events] == [1, 2]
    assert [event["status"] for event in events] == [
        "failure",
        "success",
    ]
    assert events[0]["failure_type"] == "ValidationError"


def test_feature_specific_max_tokens_are_conservative():
    assert llm.LLM_MAX_TOKENS == {
        "voice_profile": 1600,
        "generate_post": 1000,
        "autocontent": 1000,
        "rewrite": 1000,
        "generate_thread": 2600,
        "radar_analysis": 1200,
        "neuro_comment": 500,
    }


def test_default_max_tokens_remains_backwards_compatible(
    monkeypatch, caplog
):
    with caplog.at_level(logging.INFO, logger="llm"):
        _, fake = run_ask(
            monkeypatch,
            ['{"legacy": true}'],
            typed=False,
        )

    assert fake.messages.calls[0]["max_tokens"] == 2000
    assert usage_events(caplog)[0]["max_tokens"] == 2000
