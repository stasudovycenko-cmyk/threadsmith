"""
Джобы Модуля 1.

library_crawler - раз в час берёт активные ниши юзеров + сидовые ниши,
гоняет поиск токенами аккаунтов по round-robin (у кого квота свободнее),
копит posts_library. Это ров: чем дольше крутится, тем жирнее база.

insights_snapshotter - раз в сутки снимает метрики СВОИХ постов юзеров
(опубликованных через Автопилот) в insights_snapshots. Динамика по дням -
основа для "что залетает у тебя" и апдейта базлайна авторов.
"""
import json
import logging

from sqlalchemy import text

from app.core import radar
from app.core.brain_writer import BrainWriter
from app.core.crypto import decrypt_token
from app.core.db import Session
from app.core.threads_api import get_insights

log = logging.getLogger("m1_jobs")

async def library_crawler():
    """Run account-scoped Radar discovery with each account's own token."""
    async with Session() as s:
        accounts = (await s.execute(text("""
            SELECT setting.user_id, setting.threads_account_id,
                   account.access_token_enc
            FROM radar_settings setting
            JOIN threads_accounts account
              ON account.id = setting.threads_account_id
             AND account.user_id = setting.user_id
            WHERE cardinality(setting.keywords) > 0
              AND account.connection_status = 'connected'
              AND account.access_token_enc IS NOT NULL
              AND account.expires_at > now()
              AND coalesce((
                SELECT quota.used FROM search_quota quota
                WHERE quota.threads_account_id = account.id
                  AND quota.window_start = current_date
              ), 0) < :budget
              AND coalesce((
                SELECT run.status FROM radar_search_runs run
                WHERE run.user_id = setting.user_id
                  AND run.threads_account_id = setting.threads_account_id
                ORDER BY run.started_at DESC LIMIT 1
              ), 'success') <> 'permission_denied'
            ORDER BY setting.threads_account_id
        """), {"budget": radar.CRAWL_BUDGET_PER_ACC})).all()

    for user_id, account_id, token_enc in accounts:
        async with Session() as s:
            try:
                summary = await radar.discover_account_posts(
                    s,
                    user_id=user_id,
                    account_id=account_id,
                    token=decrypt_token(token_enc),
                )
                if summary.status == "success":
                    await radar.semantic_score_candidates(
                        s,
                        user_id=user_id,
                        account_id=account_id,
                    )
                await s.commit()
                log.info(
                    "radar account=%s status=%s seen=%s saved=%s",
                    account_id,
                    summary.status,
                    summary.results_seen,
                    summary.candidates_saved,
                )
            except Exception:
                await s.rollback()
                log.exception("radar account run failed account=%s", account_id)


async def insights_snapshotter():
    """Метрики своих постов за последние 30 дней - в снапшоты."""
    async with Session() as s:
        rows = (await s.execute(text("""
            SELECT
                sp.threads_post_id,
                sp.user_id,
                sp.threads_account_id,
                ta.access_token_enc
            FROM scheduled_posts sp
            JOIN threads_accounts ta ON ta.id = sp.threads_account_id
                                    AND ta.user_id = sp.user_id
            WHERE sp.status = 'done' AND sp.threads_post_id IS NOT NULL
              AND sp.run_at > now() - interval '30 days'
              AND ta.expires_at > now()
              AND ta.connection_status = 'connected'
              AND ta.access_token_enc IS NOT NULL
        """))).all()

    for post_id, user_id, account_id, tok_enc in rows:
        try:
            metrics = await get_insights(decrypt_token(tok_enc), post_id)
        except Exception:
            log.exception("insights failed post=%s", post_id)
            continue
        async with Session() as s:
            snapshot_result = await s.execute(text("""
                INSERT INTO insights_snapshots (threads_post_id, snapshot_date, metrics_json)
                VALUES (:pid, current_date, :m)
                ON CONFLICT (threads_post_id, snapshot_date)
                DO UPDATE SET metrics_json = :m
                RETURNING snapshot_date
            """), {
                "pid": post_id,
                "m": json.dumps(metrics),
            })
            snapshot_date = snapshot_result.first()[0]
            try:
                async with s.begin_nested():
                    await BrainWriter(s).record_insights_snapshot(
                        user_id,
                        account_id,
                        threads_post_id=post_id,
                        snapshot_date=snapshot_date,
                        metrics=metrics,
                    )
            except Exception as exc:
                log.warning(
                    "Brain insights_snapshot event failed post=%s "
                    "account=%s error_type=%s",
                    post_id,
                    account_id,
                    type(exc).__name__,
                )
            await s.commit()
