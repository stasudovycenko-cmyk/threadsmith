import asyncio, logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import text
from app.core.db import Session
from app.core import scenarist
logging.basicConfig(level=logging.WARNING)
MSK = timezone(timedelta(hours=3))
HOURS = [14, 16, 18, 20, 22]

async def main():
    uid = 1
    async with Session() as s:
        await s.execute(text(
            "DELETE FROM scheduled_posts WHERE user_id=:u AND status IN ('pending','failed')"),
            {"u": uid})
        await s.commit()
        acc = (await s.execute(text(
            "SELECT id FROM threads_accounts WHERE user_id=:u AND expires_at>now() "
            "ORDER BY id DESC LIMIT 1"), {"u": uid})).first()[0]
        traw = (await s.execute(text(
            "SELECT topics FROM autocontent_settings WHERE user_id=:u"), {"u": uid})).first()[0]
        topics = [t.strip() for t in traw.splitlines() if t.strip()]
        goal = (await s.execute(text(
            "SELECT coalesce(goal,'') FROM autocontent_settings WHERE user_id=:u"),
            {"u": uid})).first()[0]
        total = (await s.execute(text(
            "SELECT count(*) FROM scheduled_posts WHERE user_id=:u"), {"u": uid})).first()[0]
        rrows = (await s.execute(text(
            "SELECT text FROM scheduled_posts WHERE user_id=:u ORDER BY id DESC LIMIT 8"),
            {"u": uid})).all()
        recent = [r[0] for r in rrows if r[0]]
        profile = await scenarist.get_voice(s, uid)
    now = datetime.now(MSK)
    for i, hour in enumerate(HOURS):
        topic = topics[(total + i) % len(topics)]
        out = await scenarist.generate_post(profile, topic, recent=recent[-6:],
                                            goal=scenarist.GOALS.get(goal))
        hooks = out.get("hooks", [])
        hook = hooks[0]["text"] if hooks else ""
        txt = scenarist.trim_post(hook + chr(10) + chr(10) + out.get("body", ""))
        recent.append(txt)
        run_at = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if run_at < now:
            run_at = run_at + timedelta(days=1)
        async with Session() as s:
            await s.execute(text(
                "INSERT INTO scheduled_posts (user_id, threads_account_id, text, run_at, status) "
                "VALUES (:u,:a,:t,:r,'pending')"),
                {"u": uid, "a": acc, "t": txt, "r": run_at})
            await s.commit()
        print("+", run_at.strftime("%d.%m %H:%M"), "|", topic[:35], "|", hook[:50])

asyncio.run(main())
