"""
Джобы Модуля 1.

library_crawler - раз в час берёт активные ниши юзеров + сидовые ниши,
гоняет поиск токенами аккаунтов по round-robin (у кого квота свободнее),
копит posts_library. Это ров: чем дольше крутится, тем жирнее база.

insights_snapshotter - раз в сутки снимает метрики СВОИХ постов юзеров
(опубликованных через Автопилот) в insights_snapshots. Динамика по дням -
основа для "что залетает у тебя" и апдейта базлайна авторов.
"""
import logging

from sqlalchemy import text

from app.core import radar
from app.core.crypto import decrypt_token
from app.core.db import Session
from app.core.threads_api import get_insights

log = logging.getLogger("m1_jobs")

# сидовые ниши - чтобы база копилась до того, как юзеры зададут свои
SEED_NICHES = {
    "трафик и продвижение": ["продвижение threads", "органический трафик"],
    "заработок онлайн": ["заработок в интернете", "доход онлайн"],
    "ai и нейросети": ["нейросети", "ai контент"],
}


async def library_crawler():
    async with Session() as s:
        user_niches = (await s.execute(text("""
            SELECT DISTINCT niche, keywords FROM user_niches
        """))).all()

    targets: list[tuple[str, str]] = []
    for niche, kws in user_niches:
        for kw in (kws or [])[:3]:
            targets.append((niche, kw))
    for niche, kws in SEED_NICHES.items():
        for kw in kws:
            targets.append((niche, kw))

    for niche, query in targets:
        async with Session() as s:
            acc = await radar.pick_crawler_account(s)
            if not acc:
                log.info("crawler: суточный бюджет квот исчерпан, стоп")
                return
            acc_id, tok_enc, _used = acc
            try:
                token = decrypt_token(tok_enc)
                posts = await radar.search_and_store(s, token, acc_id, niche, query)
                await s.commit()
                log.info("crawler: %s '%s' +%s постов", niche, query, len(posts))
            except Exception:
                await s.rollback()
                log.exception("crawler failed niche=%s q=%s", niche, query)


async def insights_snapshotter():
    """Метрики своих постов за последние 30 дней - в снапшоты."""
    async with Session() as s:
        rows = (await s.execute(text("""
            SELECT sp.threads_post_id, ta.access_token_enc
            FROM scheduled_posts sp
            JOIN threads_accounts ta ON ta.id = sp.threads_account_id
            WHERE sp.status = 'done' AND sp.threads_post_id IS NOT NULL
              AND sp.run_at > now() - interval '30 days'
              AND ta.expires_at > now()
        """))).all()

    for post_id, tok_enc in rows:
        try:
            metrics = await get_insights(decrypt_token(tok_enc), post_id)
        except Exception:
            log.exception("insights failed post=%s", post_id)
            continue
        async with Session() as s:
            await s.execute(text("""
                INSERT INTO insights_snapshots (threads_post_id, snapshot_date, metrics_json)
                VALUES (:pid, current_date, :m)
                ON CONFLICT (threads_post_id, snapshot_date)
                DO UPDATE SET metrics_json = :m
            """), {"pid": post_id, "m": __import__("json").dumps(metrics)})
            await s.commit()
