"""
Публичный API-процесс. Две задачи:
1. /oauth/threads/callback - ловим редирект от Meta, меняем code на
   long-lived токен, шифруем, сохраняем.
2. /webhooks/robokassa - ResultURL. Проверка подписи, активация подписки,
   начисление кредитов, уведомление юзера в бот.

Уведомления в Telegram шлём отдельным Bot-инстансом - процессы бота и
API независимы, общего состояния нет.
"""
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from aiogram import Bot
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import text

from app.core.config import PLANS, settings
from app.core.credits import topup
from app.core.crypto import encrypt_token
from app.core.db import Session
from app.core.robokassa import verify_result
from app.core.threads_api import auth_link, exchange_code, get_me, to_long_lived

log = logging.getLogger("api")
app = FastAPI()
bot = Bot(settings.BOT_TOKEN)


@app.get("/oauth/threads/callback")
async def threads_callback(request: Request):
    code = request.query_params.get("code")
    state = request.query_params.get("state")

    if not code or not state:
        return HTMLResponse(
            "<h3>Ошибка авторизации. Вернись в бот и попробуй ещё раз.</h3>",
            status_code=400,
        )

    try:
        state = str(UUID(state))
    except (ValueError, TypeError):
        return HTMLResponse(
            "<h3>Некорректная ссылка авторизации.</h3>",
            status_code=400,
        )

    async with Session() as s:
        row = (await s.execute(text("""
            DELETE FROM oauth_states
            WHERE state = :st AND created_at > now() - interval '30 minutes'
            RETURNING user_id
        """), {"st": state})).first()
        if not row:
            return HTMLResponse("<h3>Ссылка протухла. Вернись в бот, нажми «Подключить» заново.</h3>")
        user_id = row[0]

        try:
            short = await exchange_code(code)
            long = await to_long_lived(short["access_token"])
            me = await get_me(long["access_token"])
        except Exception:
            log.exception("threads oauth failed")
            return HTMLResponse("<h3>Threads не отдал токен. Попробуй заново из бота.</h3>")

        expires = datetime.now(timezone.utc) + timedelta(seconds=long["expires_in"])
        await s.execute(text("""
            INSERT INTO threads_accounts
                (user_id, threads_user_id, username, access_token_enc, expires_at)
            VALUES (:uid, :tid, :un, :tok, :exp)
            ON CONFLICT (threads_user_id) DO UPDATE SET
                user_id = :uid, username = :un,
                access_token_enc = :tok, expires_at = :exp
        """), {
            "uid": user_id, "tid": me["id"], "un": me.get("username"),
            "tok": encrypt_token(long["access_token"]), "exp": expires,
        })
        tg = (await s.execute(text(
            "SELECT telegram_id FROM users WHERE id = :uid"
        ), {"uid": user_id})).first()
        await s.commit()

    if tg:
        try:
            await bot.send_message(
                tg[0], f"✅ Threads подключён: @{me.get('username')}"
            )
        except Exception:
            pass
    return HTMLResponse("<h3>Готово. Аккаунт подключён - возвращайся в бот.</h3>")


@app.post("/webhooks/robokassa")
async def robokassa_result(request: Request):
    form = await request.form()
    out_sum = form.get("OutSum", "")
    inv_id = form.get("InvId", "")
    signature = form.get("SignatureValue", "")
    shp = {k: v for k, v in form.items() if k.startswith("shp_")}

    if not verify_result(out_sum, inv_id, signature, shp):
        log.warning("robokassa: bad signature inv=%s", inv_id)
        return PlainTextResponse("bad sign", status_code=400)

    async with Session() as s:
        # идемпотентность: paid ставим только если был pending.
        # Робокасса ретраит вебхуки - без этого начислим кредиты дважды.
        row = (await s.execute(text("""
            UPDATE payments SET status = 'paid', paid_at = now()
            WHERE inv_id = :inv AND status = 'pending'
            RETURNING user_id, plan
        """), {"inv": int(inv_id)})).first()

        if row:
            user_id, plan_code = row
            plan = PLANS[plan_code]
            await s.execute(text("""
                INSERT INTO subscriptions (user_id, plan, status, renews_at)
                VALUES (:uid, :plan, 'active', now() + interval '1 month')
                ON CONFLICT (user_id) DO UPDATE SET
                    plan = :plan, status = 'active',
                    renews_at = now() + interval '1 month'
            """), {"uid": user_id, "plan": plan_code})
            await topup(s, user_id, plan["credits"], f"plan_{plan_code}")
            tg = (await s.execute(text(
                "SELECT telegram_id FROM users WHERE id = :uid"
            ), {"uid": user_id})).first()
            await s.commit()
            if tg:
                try:
                    await bot.send_message(
                        tg[0],
                        f"✅ Оплата прошла. Тариф {plan['title']} активен, "
                        f"+{plan['credits']} кредитов.",
                    )
                except Exception:
                    pass
        else:
            await s.commit()  # дубль вебхука - молча ок

    return PlainTextResponse(f"OK{inv_id}")


# ============================================================
# Meta Threads: callbacks for deauthorization and data deletion
# ============================================================

import secrets as _threads_secrets

from fastapi import Request as _ThreadsRequest
from fastapi.responses import JSONResponse as _ThreadsJSONResponse


async def _read_threads_meta_payload(request: _ThreadsRequest) -> dict:
    """Read JSON or form-urlencoded payload sent by Meta."""
    try:
        content_type = request.headers.get("content-type", "")

        if "application/json" in content_type:
            payload = await request.json()
            return payload if isinstance(payload, dict) else {}

        form = await request.form()
        return dict(form)

    except Exception:
        return {}


@app.api_route(
    "/oauth/threads/deauthorize",
    methods=["GET", "POST"],
)
async def threads_deauthorize_callback(request: _ThreadsRequest):
    if request.method == "GET":
        return {
            "status": "ok",
            "endpoint": "threads_deauthorize",
        }

    payload = await _read_threads_meta_payload(request)
    signed_request = payload.get("signed_request")

    # TODO:
    # После подключения полноценного удаления здесь нужно:
    # 1. Проверить signed_request с помощью секрета приложения Meta.
    # 2. Найти пользователя в базе.
    # 3. Удалить его Threads access token.
    # 4. Остановить его автоматизации.

    return _ThreadsJSONResponse(
        status_code=200,
        content={
            "success": True,
            "signed_request_received": bool(signed_request),
        },
    )


@app.api_route(
    "/oauth/threads/data-deletion",
    methods=["GET", "POST"],
)
async def threads_data_deletion_callback(request: _ThreadsRequest):
    if request.method == "GET":
        confirmation_code = request.query_params.get("code")

        return {
            "status": "completed" if confirmation_code else "ready",
            "confirmation_code": confirmation_code,
        }

    payload = await _read_threads_meta_payload(request)
    signed_request = payload.get("signed_request")

    confirmation_code = _threads_secrets.token_urlsafe(18)

    # TODO:
    # Здесь нужно будет выполнить настоящее удаление пользователя,
    # его Threads-токенов, настроек и связанных данных из базы.

    return _ThreadsJSONResponse(
        status_code=200,
        content={
            "url": (
                "https://threadsmith.pro/oauth/threads/"
                f"data-deletion?code={confirmation_code}"
            ),
            "confirmation_code": confirmation_code,
            "signed_request_received": bool(signed_request),
        },
    )


@app.get("/")
async def threadsmith_home():
    return HTMLResponse("""
    <!doctype html>
    <html lang="ru">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>ThreadSmith</title>
        <style>
            body {
                margin: 0;
                min-height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                background: #16161a;
                color: white;
                font-family: Arial, sans-serif;
                text-align: center;
            }
            .card {
                max-width: 520px;
                padding: 40px 28px;
            }
            h1 { color: #ff6a1a; font-size: 38px; }
            p { font-size: 18px; line-height: 1.5; color: #c7c7cc; }
        </style>
    </head>
    <body>
        <div class="card">
            <h1>ThreadSmith</h1>
            <p>Сервис работает, но авторизация Threads не была завершена.</p>
            <p>Вернитесь в Telegram и повторно нажмите «Подключить Threads».</p>
        </div>
    </body>
    </html>
    """)



@app.get("/oauth/threads/start")
async def threads_oauth_start(request: Request):
    state = request.query_params.get("state")

    if not state:
        return HTMLResponse(
            "<h3>Не передан state. Вернитесь в Telegram и попробуйте заново.</h3>",
            status_code=400,
        )

    try:
        state = str(UUID(state))
    except (ValueError, TypeError):
        return HTMLResponse(
            "<h3>Некорректная ссылка авторизации.</h3>",
            status_code=400,
        )

    async with Session() as s:
        row = (
            await s.execute(
                text("""
                    SELECT 1
                    FROM oauth_states
                    WHERE state = :state
                      AND created_at > now() - interval '30 minutes'
                """),
                {"state": state},
            )
        ).first()

    if not row:
        return HTMLResponse(
            "<h3>Ссылка устарела. Вернитесь в Telegram и нажмите «Подключить» заново.</h3>",
            status_code=400,
        )

    url = auth_link(state)
    log.info("threads oauth start state=%s redirect=%s", state, url)

    return RedirectResponse(url=url, status_code=302)

from app.api.legal import router as legal_router
app.include_router(legal_router)