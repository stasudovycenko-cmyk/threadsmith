"""
Авто-контент для Автопилота. Джоба крутится раз в час.

Для каждого юзера, у кого включён авто-контент (autocontent_settings.active):
  1. Смотрим, сколько постов уже стоит в очереди на сегодня.
  2. Если меньше плана (posts_per_day) — генерим недостающие Сценаристом
     его голосом по его нише и ставим в scheduled_posts на разное время.

Требует: обученный голос + заданная ниша + подключённый Threads.
Списывает кредиты как обычная генерация (generate_post). Нет кредитов —
пропускаем юзера (не копим долг).

Так Сценарист + Автопилот работают в автономном режиме: контент сам
пишется и сам публикуется (публикацией занимается publisher из m3_jobs).
"""
import logging
import random
import uuid
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import text

from app.core import credits, scenarist
from app.core.ai_cost import (
    AUTOCONTENT_MAX_GENERATIONS_PER_PLANNER_RUN,
    AUTOCONTENT_MAX_GENERATIONS_PER_USER_RUN,
    AUTOCONTENT_MAX_PENDING_POSTS,
    AUTOCONTENT_MAX_POSTS_PER_USER_DAY,
    AIUsageContext,
)
from app.core.config import CREDIT_COSTS
from app.core.db import Session
from app.core.llm import LLMGuardError

log = logging.getLogger("autocontent")

MSK = timezone(timedelta(hours=3))
# в какие часы (мск) раскидывать авто-посты
SLOTS = [9, 12, 15, 18, 21]


async def autocontent_planner():
    async with Session() as s:
        users = (await s.execute(text("""
            SELECT ac.user_id, ac.posts_per_day,
                   un.niche, un.keywords,
                   ta.id
            FROM autocontent_settings ac
            JOIN user_niches un ON un.user_id = ac.user_id
            JOIN voice_profiles vp ON vp.user_id = ac.user_id
            JOIN threads_accounts ta ON ta.user_id = ac.user_id
                 AND ta.expires_at > now()
            WHERE ac.active
        """))).all()

    planner_run_id = uuid.uuid4().hex
    remaining = AUTOCONTENT_MAX_GENERATIONS_PER_PLANNER_RUN
    for uid, per_day, niche, keywords, acc_id in users:
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
                planner_run_id=planner_run_id,
                max_generations=remaining,
            )
            remaining -= generated
        except Exception:
            log.exception("autocontent failed uid=%s", uid)


def _target_day(now: datetime, slots: list[int], days: str) -> date | None:
    earliest = now + timedelta(minutes=10)
    for offset in range(8):
        candidate = now.date() + timedelta(days=offset)
        if days == "weekdays" and candidate.weekday() >= 5:
            continue
        if any(
            datetime.combine(
                candidate,
                time(hour=hour, minute=59),
                tzinfo=MSK,
            ) >= earliest
            for hour in slots
        ):
            return candidate
    return None


def _available_slot_times(
    target_day: date,
    slots: list[int],
    occupied_hours: set[int],
    now: datetime,
) -> list[datetime]:
    earliest = now + timedelta(minutes=10)
    available = []
    for hour in slots:
        if hour in occupied_hours:
            continue
        first_minute = 0
        if target_day == now.date() and hour == now.hour:
            first_minute = now.minute + 11
        if first_minute > 59:
            continue
        run_at = datetime.combine(
            target_day,
            time(hour=hour, minute=random.randint(first_minute, 59)),
            tzinfo=MSK,
        )
        if run_at >= earliest:
            available.append(run_at)
    return available


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
            "SELECT topics, slots, days, coalesce(goal,'') "
            "FROM autocontent_settings WHERE user_id = :uid"
        ), {"uid": uid})).first()
        topics_raw, slots_raw, days, goal_key = (
            settings_row
            if settings_row
            else ("", "", "all", "")
        )
        topics = [
            item.strip()
            for item in (topics_raw or "").splitlines()
            if item.strip()
        ]
        slots = sorted({
            int(item)
            for item in (slots_raw or "").split(",")
            if item.strip().isdigit() and 0 <= int(item) <= 23
        }) or SLOTS
        days = days or "all"
        trow2 = (await s.execute(text(
            "SELECT count(*) FROM scheduled_posts "
            "WHERE user_id = :uid AND threads_account_id = :acc"
        ), {"uid": uid, "acc": acc_id})).first()
        total_posts = trow2[0] if trow2 else 0
        rrows = (await s.execute(text(
            "SELECT text FROM scheduled_posts "
            "WHERE user_id = :uid AND threads_account_id = :acc "
            "ORDER BY id DESC LIMIT 8"
        ), {"uid": uid, "acc": acc_id})).all()
        recent = [r[0] for r in rrows if r[0]]
        pending_row = (await s.execute(text("""
            SELECT count(*) FROM scheduled_posts
            WHERE user_id = :uid
              AND threads_account_id = :acc
              AND status = 'pending'
        """), {"uid": uid, "acc": acc_id})).first()
        pending_count = int(pending_row[0] if pending_row else 0)

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
    now = datetime.now(MSK)
    target_day = _target_day(now, slots, days)
    if target_day is None:
        log.warning("autocontent no eligible day uid=%s", uid)
        return 0
    day_start = datetime.combine(target_day, time.min, tzinfo=MSK)
    day_end = day_start + timedelta(days=1)
    async with Session() as s:
        scheduled_rows = (await s.execute(text("""
            SELECT run_at FROM scheduled_posts
            WHERE user_id = :uid
              AND threads_account_id = :acc
              AND run_at >= :day_start
              AND run_at < :day_end
              AND status IN ('pending','publishing','done')
        """), {
            "uid": uid,
            "acc": acc_id,
            "day_start": day_start,
            "day_end": day_end,
        })).all()
    already = len(scheduled_rows)
    occupied_hours = {
        row[0].astimezone(MSK).hour
        for row in scheduled_rows
        if row[0] is not None
    }
    need = max(0, per_day - already)
    if need > AUTOCONTENT_MAX_GENERATIONS_PER_USER_RUN:
        log.warning(
            "autocontent absurd need uid=%s need=%s limit=%s",
            uid,
            need,
            AUTOCONTENT_MAX_GENERATIONS_PER_USER_RUN,
        )
        return 0
    available_times = _available_slot_times(
        target_day,
        slots,
        occupied_hours,
        now,
    )
    generation_count = _bounded_generation_count(
        need=need,
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
                log.info("autocontent: нет кредитов uid=%s, стоп", uid)
                return

        try:
            attempts += 1
            out = await scenarist.generate_post(
                profile,
                topic,
                recent=recent,
                goal=goal_key,
                feature="autocontent",
                usage_context=usage_context,
            )
        except LLMGuardError as error:
            async with Session() as s:
                await credits.topup(s, uid, cost, "refund_autocontent")
                await s.commit()
            log.warning(
                "autocontent AI guard stop uid=%s reason=%s",
                uid,
                error,
            )
            return attempts
        except Exception:
            async with Session() as s:
                await credits.topup(s, uid, cost, "refund_autocontent")
                await s.commit()
            log.exception("autocontent gen failed uid=%s", uid)
            continue

        # собираем пост: первый хук + тело
        hooks = out.get("hooks", [])
        hook = hooks[0]["text"] if hooks else ""
        body = out.get("body", "")
        post_text = scenarist.trim_post(hook + chr(10) + chr(10) + body)

        run_at = available_times[i]

        async with Session() as s:
            await s.execute(text("""
                INSERT INTO scheduled_posts
                    (user_id, threads_account_id, text, run_at, status)
                VALUES (:uid, :acc, :txt, :run, 'pending')
            """), {"uid": uid, "acc": acc_id, "txt": post_text, "run": run_at})
            await s.commit()
        log.info("autocontent: +пост uid=%s на %s", uid, run_at.isoformat())
    return attempts
