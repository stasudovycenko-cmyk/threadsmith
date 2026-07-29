"""
Обёртка над Claude API. Одна задача: промпт -> валидный JSON.

Без response_model сохраняется старый режим: JSON парсится в dict/list,
а при любой ошибке исходный запрос повторяется один раз. С response_model
ответ дополнительно валидируется Pydantic, а невалидный ответ один раз
отправляется модели на исправление формата.
"""
import json
import logging
from typing import TypeVar, overload

from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError

from app.core.config import settings

log = logging.getLogger("llm")
client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

MODEL = "claude-sonnet-4-6"  # sonnet: качество/цена ок для генерации постов
_JSON_INSTRUCTION = (
    "Отвечай ТОЛЬКО валидным JSON. Без преамбулы, без markdown."
)
_REPAIR_SYSTEM = """Ты исправляешь только формат JSON-ответа.
Верни только исправленный валидный JSON без преамбулы и markdown.
Не добавляй новые идеи и не меняй смысл исходного ответа."""

ResponseModelT = TypeVar("ResponseModelT", bound=BaseModel)


class LLMError(Exception):
    pass


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


async def _request(system: str, user: str, max_tokens: int) -> str:
    resp = await client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=system + f"\n\n{_JSON_INSTRUCTION}",
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text


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
        "Исправь JSON согласно ошибке валидации. Не меняй смысл ответа.\n\n"
        f"Ошибка валидации:\n{_validation_error_text(error)}\n\n"
        f"Исходный ответ модели:\n{raw}\n\n"
        "Верни только исправленный JSON."
    )


@overload
async def ask_json(
    system: str,
    user: str,
    max_tokens: int = 2000,
    *,
    response_model: None = None,
) -> dict | list:
    ...


@overload
async def ask_json(
    system: str,
    user: str,
    max_tokens: int = 2000,
    *,
    response_model: type[ResponseModelT],
) -> ResponseModelT:
    ...


async def ask_json(
    system: str,
    user: str,
    max_tokens: int = 2000,
    *,
    response_model: type[ResponseModelT] | None = None,
) -> dict | list | ResponseModelT:
    if response_model is not None:
        return await _ask_typed(
            system, user, max_tokens=max_tokens, response_model=response_model
        )

    # Backwards-compatible path for callers that still expect arbitrary JSON.
    last_err = None
    for attempt in range(2):
        try:
            return _extract_json(await _request(system, user, max_tokens))
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
) -> ResponseModelT:
    try:
        raw = await _request(system, user, max_tokens)
    except Exception as first_error:
        log.warning(
            "llm request failed; retrying once: error=%s",
            _validation_error_text(first_error),
        )
        try:
            raw = await _request(system, user, max_tokens)
            return _validate(raw, response_model)
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
        return _validate(raw, response_model)
    except (json.JSONDecodeError, ValidationError) as error:
        validation_error = error
        log.warning(
            "llm response validation failed; requesting one repair: "
            "model=%s error=%s",
            response_model.__name__,
            _validation_error_text(validation_error),
        )

    try:
        repaired_raw = await _request(
            _REPAIR_SYSTEM,
            _repair_prompt(raw, validation_error),
            max_tokens,
        )
        return _validate(repaired_raw, response_model)
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
