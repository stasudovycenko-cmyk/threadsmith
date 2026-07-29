import asyncio
import json
import logging
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict

from app.core import ai_cost, llm
from app.core.ai_cost import (
    AICostEngine,
    BudgetExceeded,
    TokenUsage,
    UsageReservation,
    calculate_cost_usd,
)


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


class FakeCostEngine:
    def __init__(self):
        self.reservations = []
        self.completions = []

    async def reserve_call(self, **kwargs):
        self.reservations.append(kwargs)
        request_id = kwargs.get("request_id") or uuid.uuid4()
        return UsageReservation(
            event_key=f"{request_id}:{kwargs['attempt']}",
            request_id=request_id,
            feature=kwargs["feature"],
            model=kwargs["model"],
            attempt=kwargs["attempt"],
            user_id=kwargs["context"].user_id,
            threads_account_id=kwargs["context"].threads_account_id,
            run_id=kwargs["context"].run_id,
            reserved_cost_usd=Decimal("0.01"),
        )

    async def complete_call(
        self,
        reservation,
        *,
        usage: TokenUsage,
        status,
        latency_ms,
        failure_type=None,
    ):
        self.completions.append({
            "reservation": reservation,
            "usage": usage,
            "status": status,
            "latency_ms": latency_ms,
            "failure_type": failure_type,
        })
        return calculate_cost_usd(
            reservation.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
        )


@pytest.fixture(autouse=True)
def fake_cost_accounting(monkeypatch):
    engine = FakeCostEngine()
    monkeypatch.setattr(llm, "cost_engine", engine)
    return engine


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
    event = events[0]
    assert {
        "event": event["event"],
        "feature": event["feature"],
        "model": event["model"],
        "input_tokens": event["input_tokens"],
        "output_tokens": event["output_tokens"],
        "cache_read_tokens": event["cache_read_tokens"],
        "cache_creation_tokens": event["cache_creation_tokens"],
        "attempt": event["attempt"],
        "status": event["status"],
        "max_tokens": event["max_tokens"],
        "scope": event["scope"],
    } == {
        "event": "llm_call",
        "feature": "usage_test",
        "model": llm.MODEL,
        "input_tokens": 321,
        "output_tokens": 45,
        "cache_read_tokens": 120,
        "cache_creation_tokens": 30,
        "attempt": 1,
        "status": "success",
        "max_tokens": llm.DEFAULT_MAX_TOKENS,
        "scope": "system",
    }
    assert Decimal(event["estimated_cost_usd"]) > 0
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
        "content_generate": 1400,
        "content_repair": 1400,
        "autocontent": 1000,
        "autocontent_repair": 1400,
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


def test_budget_exhausted_during_repair_stops_before_second_provider_call(
    monkeypatch,
):
    class RepairBudgetEngine(FakeCostEngine):
        async def reserve_call(self, **kwargs):
            if kwargs["attempt"] == 2:
                raise BudgetExceeded(
                    "user_daily_cost",
                    scope="user:7",
                    current=Decimal("1"),
                    limit=Decimal("1"),
                )
            return await super().reserve_call(**kwargs)

    fake_client = FakeClient(['{"title": "missing count"}'])
    monkeypatch.setattr(llm, "client", fake_client)
    monkeypatch.setattr(llm, "cost_engine", RepairBudgetEngine())

    with pytest.raises(llm.LLMGuardError, match="user_daily_cost"):
        asyncio.run(llm.ask_json(
            "system",
            "user",
            response_model=SampleResponse,
            feature="generate_post",
        ))

    assert len(fake_client.messages.calls) == 1


def test_kill_switch_precheck_never_calls_provider_or_logs_prompt(
    monkeypatch,
    caplog,
):
    secret_prompt = "private-prompt-content"
    fake_client = FakeClient([])

    class ForbiddenStore:
        async def reserve(self, *_args, **_kwargs):
            raise AssertionError("store should not run behind kill switch")

    monkeypatch.setattr(ai_cost.settings, "AI_ENABLED", False)
    monkeypatch.setattr(llm, "client", fake_client)
    monkeypatch.setattr(
        llm,
        "cost_engine",
        AICostEngine(ForbiddenStore()),
    )

    with caplog.at_level(logging.WARNING):
        with pytest.raises(llm.LLMGuardError, match="kill_switch"):
            asyncio.run(llm.ask_json(
                "system",
                secret_prompt,
                response_model=SampleResponse,
                feature="generate_post",
            ))

    assert fake_client.messages.calls == []
    assert secret_prompt not in caplog.text
