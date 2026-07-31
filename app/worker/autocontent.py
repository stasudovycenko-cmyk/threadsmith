"""
Авто-контент для Автопилота. Джоба крутится раз в пять минут.

Для каждого юзера, у кого включён авто-контент (autocontent_settings.active):
  1. Смотрим, сколько постов уже стоит в очереди на сегодня.
  2. Если меньше плана (posts_per_day) — генерим недостающие Сценаристом
     его голосом по его нише и ставим в scheduled_posts на точные slots.

Требует: обученный голос + заданная ниша + подключённый Threads.
Списывает кредиты как обычная генерация (generate_post). Нет кредитов —
пропускаем юзера (не копим долг).

Так Сценарист + Автопилот работают в автономном режиме: контент сам
пишется и сам публикуется (публикацией занимается publisher из m3_jobs).
"""
import json
import logging
import random
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from app.core import credits, scenarist, social_brain
from app.core.ai_cost import (
    AUTOCONTENT_MAX_GENERATIONS_PER_PLANNER_RUN,
    AUTOCONTENT_MAX_GENERATIONS_PER_USER_RUN,
    AUTOCONTENT_MAX_PENDING_POSTS,
    AUTOCONTENT_MAX_POSTS_PER_USER_DAY,
    AIUsageContext,
)
from app.core.autopost_status import (
    AutopostStatusService,
    DEFAULT_TIMEZONE,
    SAFE_ERROR_MESSAGES,
    ensure_aware,
    normalize_error,
    parse_slots,
    select_next_slots,
)
from app.core.config import CREDIT_COSTS
from app.core.content_engine import ContentMemoryRepo
from app.core.db import Session
from app.core.llm import LLMGuardError

log = logging.getLogger("autocontent")


async def autocontent_planner():
    async with Session() as s:
        users = (await s.execute(text("""
            SELECT ac.user_id, ac.posts_per_day,
                   un.niche, un.keywords,
                   ta.id, ta.expires_at
            FROM autocontent_settings ac
            JOIN user_niches un ON un.user_id = ac.user_id
            JOIN voice_profiles vp ON vp.user_id = ac.user_id
            JOIN threads_accounts ta ON ta.user_id = ac.user_id
            WHERE ac.active
        """))).all()

    planner_run_id = uuid.uuid4().hex
    remaining = AUTOCONTENT_MAX_GENERATIONS_PER_PLANNER_RUN
    for uid, per_day, niche, keywords, acc_id, expires_at in users:
        if remaining <= 0:
            log.warning(
                "autocontent cap stop run_id=%s generated=%s limit=%s",
                planner_run_id,
                AUTOCONTENT_MAX_GENERATIONS_PER_PLANNER_RUN,
                AUTOCONTENT_MAX_GENERATIONS_PER_PLANNER_RUN,
            )
            break
        try:
            generated = await _plan_for_user(
                uid,
                per_day,
                niche,
                keywords,
                acc_id,
                account_expires_at=expires_at,
                planner_run_id=planner_run_id,
                max_generations=remaining,
            )
            remaining -= generated
        except Exception as exc:
            log.error(
                "autocontent failed uid=%s account=%s error_type=%s",
                uid,
                acc_id,
                type(exc).__name__,
            )


def _bounded_generation_count(
    *,
    need: int,
    pending_count: int,
    available_slots: int,
    max_generations: int,
) -> int:
    return min(
        max(0, need),
        max(0, int(max_generations)),
        max(0, AUTOCONTENT_MAX_PENDING_POSTS - pending_count),
        max(0, available_slots),
        AUTOCONTENT_MAX_GENERATIONS_PER_USER_RUN,
    )


async def _plan_for_user(
    uid,
    per_day,
    niche,
    keywords,
    acc_id,
    *,
    account_expires_at: datetime | None = None,
    planner_run_id: str | None = None,
    max_generations: int = AUTOCONTENT_MAX_GENERATIONS_PER_USER_RUN,
) -> int:
    try:
        per_day = int(per_day)
    except (TypeError, ValueError):
        log.warning("autocontent invalid posts_per_day uid=%s", uid)
        return 0
    if (
        per_day < 0
        or per_day > AUTOCONTENT_MAX_POSTS_PER_USER_DAY
    ):
        log.warning(
            "autocontent anomaly stop uid=%s posts_per_day=%s limit=%s",
            uid,
            per_day,
            AUTOCONTENT_MAX_POSTS_PER_USER_DAY,
        )
        return 0

    async with Session() as s:
        profile = await scenarist.get_voice(s, uid)
        settings_row = (await s.execute(text(
            "SELECT topics, slots, days, coalesce(goal,''), "
            "coalesce(timezone, :default_timezone) "
            "FROM autocontent_settings WHERE user_id = :uid"
        ), {
            "uid": uid,
            "default_timezone": DEFAULT_TIMEZONE,
        })).first()
        topics_raw, slots_raw, days, goal_key, timezone_name = (
            settings_row
            if settings_row
            else ("", "", "all", "", DEFAULT_TIMEZONE)
        )
        topics = [
            item.strip()
            for item in (topics_raw or "").splitlines()
            if item.strip()
        ]
        slots = parse_slots(slots_raw)
        days = days or "all"
        trow2 = (await s.execute(text(
            "SELECT count(*) FROM scheduled_posts "
            "WHERE user_id = :uid AND threads_account_id = :acc"
        ), {"uid": uid, "acc": acc_id})).first()
        total_posts = trow2[0] if trow2 else 0
        pending_row = (await s.execute(text("""
            SELECT count(*) FROM scheduled_posts
            WHERE user_id = :uid
              AND threads_account_id = :acc
              AND status = 'pending'
        """), {"uid": uid, "acc": acc_id})).first()
        pending_count = int(pending_row[0] if pending_row else 0)
        try:
            brain = await social_brain.build_account_context(
                s,
                user_id=uid,
                threads_account_id=acc_id,
                task="autocontent",
                budget_tokens=scenarist.GENERATION_BRAIN_BUDGET_TOKENS,
            )
        except Exception as exc:
            brain = None
            log.warning(
                "autocontent Brain unavailable uid=%s account=%s "
                "error_type=%s",
                uid,
                acc_id,
                type(exc).__name__,
            )
        try:
            memory = await ContentMemoryRepo(s).load(uid, acc_id)
        except Exception as exc:
            memory = []
            log.warning(
                "autocontent memory unavailable uid=%s account=%s "
                "error_type=%s",
                uid,
                acc_id,
                type(exc).__name__,
            )
        await s.commit()

    daily_limit = min(
        AUTOCONTENT_MAX_POSTS_PER_USER_DAY,
        len(slots),
    )
    if per_day < 0 or per_day > daily_limit:
        log.warning(
            "autocontent anomaly stop uid=%s posts_per_day=%s limit=%s",
            uid,
            per_day,
            daily_limit,
        )
        return 0
    if per_day == 0 or not profile:
        return 0
    if pending_count >= AUTOCONTENT_MAX_PENDING_POSTS:
        log.warning(
            "autocontent pending cap uid=%s pending=%s limit=%s",
            uid,
            pending_count,
            AUTOCONTENT_MAX_PENDING_POSTS,
        )
        return 0

    kws = keywords or [niche]
    now = datetime.now(timezone.utc)
    async with Session() as s:
        occupied = await AutopostStatusService(s).occupied_slots(
            uid,
            acc_id,
            now=now,
            timezone_name=timezone_name,
        )
    available_times = select_next_slots(
        now=now,
        slots=slots,
        days=days,
        timezone_name=timezone_name,
        posts_per_day=per_day,
        occupied=occupied,
    )
    generation_count = _bounded_generation_count(
        need=len(available_times),
        max_generations=max_generations,
        pending_count=pending_count,
        available_slots=len(available_times),
    )
    if generation_count <= 0:
        return 0

    attempts = 0
    usage_context = AIUsageContext(
        user_id=uid,
        threads_account_id=acc_id,
        run_id=(
            f"autocontent:{planner_run_id or uuid.uuid4().hex}:"
            f"{uid}:{acc_id}"
        ),
    )
    for i in range(generation_count):
        run_at = available_times[i]
        async with Session() as s:
            run_id = await AutopostStatusService(s).reserve_run(
                uid,
                acc_id,
                run_at,
            )
            await s.commit()
        if run_id is None:
            continue

        if (
            account_expires_at is not None
            and ensure_aware(account_expires_at) <= now
        ):
            async with Session() as s:
                await AutopostStatusService(s).finish_run(
                    run_id,
                    status="skipped",
                    error_code="AUTH_EXPIRED",
                    safe_error_message=SAFE_ERROR_MESSAGES[
                        "AUTH_EXPIRED"
                    ],
                )
                await s.commit()
            continue

        # тема = ниша + случайный ключевик, для разнообразия
        if topics:
            topic = topics[(total_posts + i) % len(topics)]
        else:
            topic = f"{niche}: {random.choice(kws)}"
        cost = CREDIT_COSTS["generate_post"]

        async with Session() as s:
            try:
                await credits.spend(s, uid, cost, "autocontent")
                await s.commit()
            except credits.NotEnoughCredits:
                await s.rollback()
                await AutopostStatusService(s).finish_run(
                    run_id,
                    status="skipped",
                    error_code="INSUFFICIENT_CREDITS",
                    safe_error_message=SAFE_ERROR_MESSAGES[
                        "INSUFFICIENT_CREDITS"
                    ],
                )
                await s.commit()
                log.info("autocontent: нет кредитов uid=%s, стоп", uid)
                return attempts

        try:
            attempts += 1
            out = await scenarist.generate_post(
                profile,
                topic,
                goal=goal_key,
                feature="autocontent",
                brain=brain,
                usage_context=usage_context,
                memory=memory,
                source="autocontent",
            )
        except LLMGuardError as error:
            async with Session() as s:
                await credits.topup(s, uid, cost, "refund_autocontent")
                code, message = normalize_error(
                    error,
                    stage="generation",
                )
                await AutopostStatusService(s).finish_run(
                    run_id,
                    status="failed",
                    error_code=code,
                    safe_error_message=message,
                )
                await s.commit()
            log.warning(
                "autocontent AI guard stop uid=%s error_type=%s",
                uid,
                type(error).__name__,
            )
            return attempts
        except Exception as error:
            async with Session() as s:
                await credits.topup(s, uid, cost, "refund_autocontent")
                code, message = normalize_error(
                    error,
                    stage="generation",
                )
                await AutopostStatusService(s).finish_run(
                    run_id,
                    status="failed",
                    error_code=code,
                    safe_error_message=message,
                )
                await s.commit()
            log.error(
                "autocontent gen failed uid=%s account=%s "
                "error_type=%s",
                uid,
                acc_id,
                type(error).__name__,
            )
            continue

        # собираем пост: выбранный хук + тело
        hooks = out.get("hooks", [])
        selected_hook = out.get("selected_hook") or {}
        hook = (
            selected_hook.get("text")
            or (hooks[0]["text"] if hooks else "")
        )
        body = out.get("body", "")
        post_text = scenarist.trim_post(hook + chr(10) + chr(10) + body)
        metadata = out.get("metadata") or {}

        async with Session() as s:
            row = (await s.execute(text("""
                INSERT INTO scheduled_posts
                    (user_id, threads_account_id, text, run_at, status,
                     content_metadata)
                SELECT
                    :uid, :acc, :txt, :run, 'pending',
                    CAST(:metadata AS jsonb)
                WHERE EXISTS (
                    SELECT 1 FROM autopost_runs
                    WHERE id = :run_id
                      AND status = 'pending'
                )
                RETURNING id
            """), {
                "uid": uid,
                "acc": acc_id,
                "txt": post_text,
                "run": run_at,
                "run_id": run_id,
                "metadata": json.dumps(
                    metadata,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            })).first()
            if row is None:
                await credits.topup(
                    s,
                    uid,
                    cost,
                    "refund_autocontent_disabled",
                )
                await s.commit()
                continue
            await AutopostStatusService(s).attach_post(
                run_id,
                row[0],
            )
            await s.commit()
        log.info("autocontent: +пост uid=%s на %s", uid, run_at.isoformat())
    return attempts
