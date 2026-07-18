"""
Обёртка над Claude API. Одна задача: промпт -> валидный JSON.

Почему JSON, а не свободный текст: бот рисует варианты хуков кнопками,
для этого нужна структура. Модель иногда оборачивает JSON в ```-фенсы
или добавляет преамбулу - чистим и парсим с одним ретраем.
"""
import json
import logging

from anthropic import AsyncAnthropic

from app.core.config import settings

log = logging.getLogger("llm")
client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

MODEL = "claude-sonnet-4-6"  # sonnet: качество/цена ок для генерации постов


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


async def ask_json(system: str, user: str, max_tokens: int = 2000) -> dict | list:
    last_err = None
    for attempt in range(2):
        try:
            resp = await client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=system + "\n\nОтвечай ТОЛЬКО валидным JSON. Без преамбулы, без markdown.",
                messages=[{"role": "user", "content": user}],
            )
            return _extract_json(resp.content[0].text)
        except Exception as e:
            last_err = e
            log.warning("llm attempt %s failed: %s", attempt, e)
    raise LLMError(str(last_err))
