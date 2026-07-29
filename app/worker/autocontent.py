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
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.core import credits, scenarist
from app.core.config import CREDIT_COSTS
from app.core.db import Session

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

    for uid, per_day, niche, keywords, acc_id in users:
        try:
            await _plan_for_user(uid, per_day, niche, keywords, acc_id)
        except Exception:
            log.exception("autocontent failed uid=%s", uid)


async def _plan_for_user(uid, per_day, niche, keywords, acc_id):
    async with Session() as s:
        # сколько уже запланировано/опубликовано сегодня
        row = (await s.execute(text("""
            SELECT count(*) FROM scheduled_posts
            WHERE user_id = :uid
              AND run_at >= date_trunc('day', now() at time zone 'utc')
              AND status IN ('pending','publishing','done')
        """), {"uid": uid})).first()
        already = row[0]
        profile = await scenarist.get_voice(s, uid)
        trow = (await s.execute(text(
            "SELECT topics FROM autocontent_settings WHERE user_id = :uid"
        ), {"uid": uid})).first()
        topics = [t.strip() for t in ((trow[0] if trow else "") or "").splitlines() if t.strip()]
        srow = (await s.execute(text(
            "SELECT slots, days FROM autocontent_settings WHERE user_id = :uid"
        ), {"uid": uid})).first()
        slots = [int(x) for x in ((srow[0] if srow else "") or "").split(",")
                 if x.strip().isdigit()] or SLOTS
        days = (srow[1] if srow else "") or "all"
        trow2 = (await s.execute(text(
            "SELECT count(*) FROM scheduled_posts WHERE user_id = :uid"
        ), {"uid": uid})).first()
        total_posts = trow2[0] if trow2 else 0
        rrows = (await s.execute(text(
            "SELECT text FROM scheduled_posts WHERE user_id = :uid "
            "ORDER BY id DESC LIMIT 8"
        ), {"uid": uid})).all()
        recent = [r[0] for r in rrows if r[0]]
        grow = (await s.execute(text(
            "SELECT coalesce(goal,'') FROM autocontent_settings WHERE user_id = :uid"
        ), {"uid": uid})).first()
        goal_key = grow[0] if grow else ""

    need = max(0, per_day - already)
    if need == 0 or not profile:
        return

    kws = keywords or [niche]
    now = datetime.now(MSK)

    for i in range(need):
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
            out = await scenarist.generate_post(profile, topic)
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

        # время: ближайший свободный слот сегодня/завтра
        slot_idx = already + i
        slot_hour = slots[slot_idx % len(slots)]
        day_offset = slot_idx // len(slots)
        run_at = now.replace(hour=slot_hour, minute=random.randint(0, 59),
                             second=0, microsecond=0) + timedelta(days=day_offset)
        if run_at < now + timedelta(minutes=10):
            run_at = run_at + timedelta(days=1)
        if days == "weekdays":
            while run_at.weekday() >= 5:
                run_at = run_at + timedelta(days=1)

        async with Session() as s:
            await s.execute(text("""
                INSERT INTO scheduled_posts
                    (user_id, threads_account_id, text, run_at, status)
                VALUES (:uid, :acc, :txt, :run, 'pending')
            """), {"uid": uid, "acc": acc_id, "txt": post_text, "run": run_at})
            await s.commit()
        log.info("autocontent: +пост uid=%s на %s", uid, run_at.isoformat())
