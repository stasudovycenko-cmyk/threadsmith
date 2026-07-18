"""
Модуль 1 - Радар.

ГЛАВНОЕ ОГРАНИЧЕНИЕ API: метрик чужих постов нет. keyword_search отдаёт
только контент. Insights - только по своим постам.

Поэтому виральность чужих постов - ПРОКСИ:
  virality_score = вес позиции в TOP-выдаче + бонус за свежесть.
Threads сам ранжирует TOP по популярности - крадём их ранжирование.
Реальные метрики (лайки/просмотры) - за интерфейсом fetch_public_metrics():
на MVP он пустой, потом туда встаёт скрейпер-API отдельным бизнес-решением.
Схема готова: metrics_json в posts_library ждёт данные из любого источника.

Квота поиска: 2200/юзер/24ч по докам. Краулер жжём консервативно -
до CRAWL_BUDGET запросов с токена в сутки, round-robin по аккаунтам
с наименьшим расходом. Юзерские поиски (за кредиты) идут с токена юзера.
"""
import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_token
from app.core.llm import ask_json
from app.core.threads_api import keyword_search

log = logging.getLogger("radar")

CRAWL_BUDGET_PER_ACC = 50  # запросов/сутки с одного токена на краулер


async def fetch_public_metrics(post_ids: list[str]) -> dict[str, dict]:
    """Заглушка под внешний источник метрик (скрейпер-API).
    Возвращает {post_id: {views, likes, ...}}. На MVP - пусто."""
    return {}


def proxy_virality(rank: int, posted_at: datetime | None) -> float:
    """Позиция в TOP-выдаче (0 = первый) + свежесть."""
    score = max(0.0, 100.0 - rank * 3)
    if posted_at:
        age_h = (datetime.now(timezone.utc) - posted_at).total_seconds() / 3600
        if age_h < 48:
            score += 20 * (1 - age_h / 48)
    return round(score, 2)


async def _bump_quota(session: AsyncSession, acc_id: int, n: int = 1):
    await session.execute(text("""
        INSERT INTO search_quota (threads_account_id, window_start, used)
        VALUES (:acc, current_date, :n)
        ON CONFLICT (threads_account_id, window_start)
        DO UPDATE SET used = search_quota.used + :n
    """), {"acc": acc_id, "n": n})


async def pick_crawler_account(session: AsyncSession):
    """Аккаунт с наименьшим расходом квоты сегодня и живым токеном."""
    row = (await session.execute(text("""
        SELECT ta.id, ta.access_token_enc,
               coalesce(sq.used, 0) AS used
        FROM threads_accounts ta
        LEFT JOIN search_quota sq
          ON sq.threads_account_id = ta.id AND sq.window_start = current_date
        WHERE ta.expires_at > now()
          AND coalesce(sq.used, 0) < :budget
        ORDER BY used ASC LIMIT 1
    """), {"budget": CRAWL_BUDGET_PER_ACC})).first()
    return row


async def search_and_store(session: AsyncSession, token: str, acc_id: int,
                           niche: str, query: str) -> list[dict]:
    """Один поисковый запрос: дёрнули API, положили в библиотеку, вернули посты."""
    posts = await keyword_search(token, query)
    await _bump_quota(session, acc_id)

    for rank, p in enumerate(posts):
        if p.get("is_reply"):
            continue
        posted_at = None
        if p.get("timestamp"):
            posted_at = datetime.fromisoformat(
                p["timestamp"].replace("+0000", "+00:00"))
        await session.execute(text("""
            INSERT INTO authors (threads_author_id, username, updated_at)
            VALUES (:aid, :un, now())
            ON CONFLICT (threads_author_id) DO NOTHING
        """), {"aid": p["username"], "un": p["username"]})
        await session.execute(text("""
            INSERT INTO posts_library
                (threads_post_id, niche, author_id, text, metrics_json,
                 virality_score, fetched_at)
            VALUES (:pid, :niche, :aid, :txt, :mj, :vs, now())
            ON CONFLICT (threads_post_id) DO UPDATE SET
                virality_score = greatest(posts_library.virality_score, :vs),
                fetched_at = now()
        """), {"pid": p["id"], "niche": niche, "aid": p["username"],
               "txt": p.get("text", ""),
               "mj": json.dumps({"permalink": p.get("permalink"),
                                 "has_replies": p.get("has_replies")}),
               "vs": proxy_virality(rank, posted_at)})
    return posts


async def top_posts(session: AsyncSession, niche: str,
                    limit: int = 7) -> list:
    """Топ ниши из накопленной библиотеки, свежее - выше."""
    return (await session.execute(text("""
        SELECT threads_post_id, author_id, text, virality_score,
               metrics_json->>'permalink' AS permalink
        FROM posts_library
        WHERE niche = :niche AND fetched_at > now() - interval '14 days'
          AND length(text) > 30
        ORDER BY virality_score DESC, fetched_at DESC
        LIMIT :lim
    """), {"niche": niche, "lim": limit})).all()


RAZBOR_SYSTEM = """Ты - аналитик виральности Threads. Тебе дают пост, который залетел.
Разбери механику. Без воды и комплиментов автору.

JSON:
{
 "hook": "какой хук в первой строке и почему цепляет",
 "structure": "как построен пост: ритм, абзацы, длина",
 "trigger": "на какую эмоцию/боль давит",
 "ending": "чем закрывает: CTA, вопрос, панч",
 "how_to_repeat": "механика в 2-3 предложениях: как повторить с ДРУГОЙ темой",
 "hook_type": "один из: pain/number/myth/list/story/ban/compare/question/insight/provocation/unpopular"
}"""


async def razbor(session: AsyncSession, post_id: str) -> dict:
    row = (await session.execute(text(
        "SELECT text FROM posts_library WHERE threads_post_id = :pid"
    ), {"pid": post_id})).first()
    if not row:
        raise ValueError("post not in library")
    result = await ask_json(RAZBOR_SYSTEM, f"Пост:\n{row[0]}")
    await session.execute(text("""
        UPDATE posts_library SET hook_type = :ht WHERE threads_post_id = :pid
    """), {"ht": result.get("hook_type"), "pid": post_id})
    return result
