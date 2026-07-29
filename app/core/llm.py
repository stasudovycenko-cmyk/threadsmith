"""
Обёртка над Claude API. Одна задача: промпт -> валидный JSON.

Без response_model сохраняется старый режим: JSON парсится в dict/list,
а при любой ошибке исходный запрос повторяется один раз. С response_model
ответ дополнительно валидируется Pydantic, а невалидный ответ один раз
отправляется модели на исправление формата.
"""
import json
import logging
import time
from dataclasses import dataclass
from typing import TypeVar, overload

from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError

from app.core.config import settings

log = logging.getLogger("llm")
client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

MODEL = "claude-sonnet-4-6"  # sonnet: качество/цена ок для генерации постов
DEFAULT_MAX_TOKENS = 2000
LLM_MAX_TOKENS = {
    "voice_profile": 1600,
    "generate_post": 1000,
    "autocontent": 1000,
    "rewrite": 1000,
    "generate_thread": 2600,
    "radar_analysis": 1200,
    "neuro_comment": 500,
}
_JSON_INSTRUCTION = (
    "Отвечай ТОЛЬКО валидным JSON. Без преамбулы, без markdown."
)
_REPAIR_SYSTEM = "Исправь JSON по ошибке валидации, не меняя смысл."

ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel)


class LLMError(Exception):
    pass


@dataclass(frozen=True)
class _CallResult:
    raw: str
    usage: object | None
    latency_ms: int


def _extract_json(raw: str) -> dict | list:
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```")[1]
        if s.startswith("json"):
            s = s[4:]
    # отрезаем всё до первой скобки и после последней
    start = min((i for i in (s.find("{"), s.find("[")) if i != -1), default=0)
    end = max(s.rfind("}"), s.rfind("]")) + 1
    return json.loads(s[start:end])


def _usage_value(usage: object | None, field: str) -> int:
    if usage is None:
        return 0
    value = getattr(usage, field, None)
    if value is None and isinstance(usage, dict):
        value = usage.get(field)
    return int(value or 0)


def _log_call(
    *,
    feature: str,
    max_tokens: int,
    attempt: int,
    status: str,
    latency_ms: int,
    usage: object | None,
    failure_type: str | None = None,
) -> None:
    event = {
        "event": "llm_call",
        "feature": feature,
        "model": MODEL,
        "input_tokens": _usage_value(usage, "input_tokens"),
        "output_tokens": _usage_value(usage, "output_tokens"),
        "cache_read_tokens": _usage_value(
            usage, "cache_read_input_tokens"
        ),
        "cache_creation_tokens": _usage_value(
            usage, "cache_creation_input_tokens"
        ),
        "latency_ms": latency_ms,
        "attempt": attempt,
        "status": status,
        "max_tokens": max_tokens,
    }
    if failure_type:
        event["failure_type"] = failure_type
    log.info(
        "llm_call %s",
        json.dumps(event, ensure_ascii=True, separators=(",", ":")),
    )


async def _request(
    system: str,
    user: str,
    max_tokens: int,
    *,
    feature: str,
    attempt: int,
) -> _CallResult:
    started_at = time.perf_counter()
    resp = None
    try:
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system + f"\n\n{_JSON_INSTRUCTION}",
            messages=[{"role": "user", "content": user}],
        )
        raw = resp.content[0].text
    except Exception as error:
        latency_ms = round((time.perf_counter() - started_at) * 1000)
        _log_call(
            feature=feature,
            max_tokens=max_tokens,
            attempt=attempt,
            status="failure",
            latency_ms=latency_ms,
            usage=getattr(resp, "usage", None),
            failure_type=type(error).__name__,
        )
        raise
    return _CallResult(
        raw=raw,
        usage=getattr(resp, "usage", None),
        latency_ms=round((time.perf_counter() - started_at) * 1000),
    )


def _validation_error_text(error: Exception) -> str:
    """Describe validation failures without echoing response field values."""
    if isinstance(error, ValidationError):
        issues = []
        for item in error.errors(include_url=False, include_input=False):
            location = ".".join(str(part) for part in item["loc"]) or "<root>"
            issues.append(
                f"{location}: {item['msg']} ({item['type']})"
            )
        return "; ".join(issues)
    if isinstance(error, json.JSONDecodeError):
        return (
            f"invalid JSON at line {error.lineno}, column {error.colno} "
            f"({error.msg})"
        )
    return type(error).__name__


def _validate(raw: str, response_model: type[ResponseModelT]) -> ResponseModelT:
    return response_model.model_validate(_extract_json(raw))


def _repair_prompt(raw: str, error: Exception) -> str:
    return (
        f"Ошибка:\n{_validation_error_text(error)}\n\n"
        f"Исходный output:\n{raw}"
    )


def _parse_call(
    call: _CallResult,
    *,
    feature: str,
    max_tokens: int,
    attempt: int,
) -> dict | list:
    try:
        result = _extract_json(call.raw)
    except Exception as error:
        _log_call(
            feature=feature,
            max_tokens=max_tokens,
            attempt=attempt,
            status="failure",
            latency_ms=call.latency_ms,
            usage=call.usage,
            failure_type=type(error).__name__,
        )
        raise
    _log_call(
        feature=feature,
        max_tokens=max_tokens,
        attempt=attempt,
        status="success",
        latency_ms=call.latency_ms,
        usage=call.usage,
    )
    return result


def _validate_call(
    call: _CallResult,
    response_model: type[ResponseModelT],
    *,
    feature: str,
    max_tokens: int,
    attempt: int,
) -> ResponseModelT:
    try:
        result = _validate(call.raw, response_model)
    except Exception as error:
        _log_call(
            feature=feature,
            max_tokens=max_tokens,
            attempt=attempt,
            status="failure",
            latency_ms=call.latency_ms,
            usage=call.usage,
            failure_type=type(error).__name__,
        )
        raise
    _log_call(
        feature=feature,
        max_tokens=max_tokens,
        attempt=attempt,
        status="success",
        latency_ms=call.latency_ms,
        usage=call.usage,
    )
    return result


@overload
async def ask_json(
    system: str,
    user: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    *,
    response_model: None = None,
    feature: str = "unspecified",
) -> dict | list:
    ...


@overload
async def ask_json(
    system: str,
    user: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    *,
    response_model: type[ResponseModelT],
    feature: str = "unspecified",
) -> ResponseModelT:
    ...


async def ask_json(
    system: str,
    user: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    *,
    response_model: type[ResponseModelT] | None = None,
    feature: str = "unspecified",
) -> dict | list | ResponseModelT:
    if response_model is not None:
        return await _ask_typed(
            system,
            user,
            max_tokens=max_tokens,
            response_model=response_model,
            feature=feature,
        )

    # Backwards-compatible path for callers that still expect arbitrary JSON.
    last_err = None
    for attempt in range(2):
        try:
            call = await _request(
                system,
                user,
                max_tokens,
                feature=feature,
                attempt=attempt + 1,
            )
            return _parse_call(
                call,
                feature=feature,
                max_tokens=max_tokens,
                attempt=attempt + 1,
            )
        except Exception as e:
            last_err = e
            log.warning(
                "llm attempt %s failed: %s",
                attempt + 1,
                _validation_error_text(e),
            )
    raise LLMError(str(last_err))


async def _ask_typed(
    system: str,
    user: str,
    max_tokens: int,
    response_model: type[ResponseModelT],
    feature: str,
) -> ResponseModelT:
    try:
        call = await _request(
            system,
            user,
            max_tokens,
            feature=feature,
            attempt=1,
        )
    except Exception as first_error:
        log.warning(
            "llm request failed; retrying once: error=%s",
            _validation_error_text(first_error),
        )
        try:
            retry_call = await _request(
                system,
                user,
                max_tokens,
                feature=feature,
                attempt=2,
            )
            return _validate_call(
                retry_call,
                response_model,
                feature=feature,
                max_tokens=max_tokens,
                attempt=2,
            )
        except Exception as second_error:
            log.error(
                "llm request failed after retry: model=%s error=%s",
                response_model.__name__,
                _validation_error_text(second_error),
            )
            raise LLMError(
                f"LLM request failed after 2 attempts for "
                f"{response_model.__name__}"
            ) from second_error

    try:
        return _validate_call(
            call,
            response_model,
            feature=feature,
            max_tokens=max_tokens,
            attempt=1,
        )
    except (json.JSONDecodeError, ValidationError) as error:
        validation_error = error
        log.warning(
            "llm response validation failed; requesting one repair: "
            "model=%s error=%s",
            response_model.__name__,
            _validation_error_text(validation_error),
        )

    try:
        repair_call = await _request(
            _REPAIR_SYSTEM,
            _repair_prompt(call.raw, validation_error),
            max_tokens,
            feature=feature,
            attempt=2,
        )
        return _validate_call(
            repair_call,
            response_model,
            feature=feature,
            max_tokens=max_tokens,
            attempt=2,
        )
    except Exception as repair_error:
        log.error(
            "llm response repair failed: model=%s error=%s",
            response_model.__name__,
            _validation_error_text(repair_error),
        )
        raise LLMError(
            f"LLM response validation failed after one repair for "
            f"{response_model.__name__}"
        ) from repair_error
