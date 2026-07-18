"""
Модуль 4 - нейрокомментинг.

Механика: краулим свежие посты ниши -> LLM решает, релевантен ли пост,
и пишет коммент голосом юзера -> премодерация в телеге или автопост.

Защита аккаунта юзера (вшито, юзером не отключается):
- один коммент на пост (unique в базе)
- один коммент автору в сутки
- суточный кэп
- без ссылок в комментах - LLM запрещено, плюс постфильтр
- LLM-фильтр релевантности: мимо темы/токсично/реклама -> скип
"""
import json
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm import ask_json

log = logging.getLogger("neuro")

NEURO_SYSTEM_TMPL = """Ты пишешь комментарии в Threads от лица автора с этим голосом:

{profile}

Тебе дают чужой пост из ниши автора. Два шага:

1. РЕШИ, стоит ли комментировать. НЕ комментируй если пост: не по теме ниши
"{niche}", токсичный, реклама/спам, слишком личный (горе, болезнь), или
коммент будет выглядеть натянуто.

2. Если стоит - напиши ОДИН коммент. Правила:
- Голосом автора, используй sample_phrases как камертон
- Добавляй ценность: дополни мысль, дай пример, задай острый вопрос,
  культурно не согласись. НЕ пиши "супер!", "согласен", "полезно"
- 1-3 предложения, до 280 символов
- ЗАПРЕЩЕНО: ссылки, упоминания своих продуктов, "переходи ко мне в профиль"
- Коммент должен вызывать желание глянуть, кто это написал

JSON:
{{"relevant": true/false, "skip_reason": "если false - почему", "comment": "текст или null"}}"""

_LINK_RE = re.compile(r"(https?://|t\.me/|@[\w.]+|www\.)", re.I)


async def generate_comment(profile: dict, niche: str,
                           post_text: str, author: str) -> dict:
    result = await ask_json(
        NEURO_SYSTEM_TMPL.format(
            profile=json.dumps(profile, ensure_ascii=False), niche=niche),
        f"Пост от @{author}:\n\n{post_text}",
    )
    # постфильтр: LLM сказали без ссылок, но доверяй и проверяй
    comment = result.get("comment") or ""
    if result.get("relevant") and _LINK_RE.search(comment):
        result = {"relevant": False, "skip_reason": "link in comment", "comment": None}
    return result


async def today_count(session: AsyncSession, user_id: int) -> int:
    row = (await session.execute(text("""
        SELECT count(*) FROM neuro_comments
        WHERE user_id = :uid AND created_at::date = current_date
          AND status IN ('pending', 'posted')
    """), {"uid": user_id})).first()
    return row[0]


async def author_commented_today(session: AsyncSession, user_id: int,
                                 author: str) -> bool:
    row = (await session.execute(text("""
        SELECT 1 FROM neuro_comments
        WHERE user_id = :uid AND target_author = :a
          AND created_at::date = current_date
        LIMIT 1
    """), {"uid": user_id, "a": author})).first()
    return row is not None


async def pick_candidates(session: AsyncSession, user_id: int, niche: str,
                          limit: int = 5) -> list:
    """Свежие посты ниши из библиотеки, которые юзер ещё не комментил.
    Свои посты юзера отсекаем по username его threads-аккаунта."""
    return (await session.execute(text("""
        SELECT pl.threads_post_id, pl.author_id, pl.text
        FROM posts_library pl
        WHERE pl.niche = :niche
          AND pl.fetched_at > now() - interval '24 hours'
          AND length(pl.text) > 50
          AND pl.author_id NOT IN (
              SELECT username FROM threads_accounts WHERE user_id = :uid
          )
          AND NOT EXISTS (
              SELECT 1 FROM neuro_comments nc
              WHERE nc.user_id = :uid
                AND nc.target_post_id = pl.threads_post_id
          )
        ORDER BY pl.virality_score DESC
        LIMIT :lim
    """), {"niche": niche, "uid": user_id, "lim": limit})).all()
