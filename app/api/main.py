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
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from sqlalchemy import text

from app.core.accounts import (
    AccountBusyError,
    AccountNotFoundError,
    ThreadsAccountService,
    safe_threads_id,
)
from app.core.config import PLANS, settings
from app.core.credits import topup
from app.core.crypto import encrypt_token
from app.core.db import Session
from app.core.meta_callbacks import InvalidSignedRequest, verify_signed_request
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

    async with Session() as session:
        oauth_state = (
            await session.execute(
                text("""
                    DELETE FROM oauth_states
                    WHERE state = :state
                      AND created_at > now() - interval '30 minutes'
                    RETURNING user_id, action,
                              expected_threads_account_id
                """),
                {"state": state},
            )
        ).mappings().first()
        if not oauth_state:
            return HTMLResponse(
                "<h3>Ссылка протухла. Вернись в бот и начни подключение заново.</h3>",
                status_code=400,
            )
        await session.commit()

    try:
        short = await exchange_code(code)
        long_token = await to_long_lived(short["access_token"])
        me = await get_me(long_token["access_token"])
    except Exception as error:
        log.warning(
            "threads oauth exchange failed error_type=%s",
            type(error).__name__,
        )
        return HTMLResponse(
            "<h3>Threads не отдал токен. Запусти подключение заново из бота.</h3>",
            status_code=502,
        )

    user_id = int(oauth_state["user_id"])
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=int(long_token["expires_in"])
    )
    async with Session() as session:
        service = ThreadsAccountService(session)
        try:
            outcome = await service.apply_oauth_connection(
                user_id,
                action=oauth_state["action"],
                expected_account_id=oauth_state[
                    "expected_threads_account_id"
                ],
                threads_user_id=str(me["id"]),
                username=me.get("username"),
                access_token_enc=encrypt_token(long_token["access_token"]),
                expires_at=expires_at,
            )
        except AccountNotFoundError:
            await session.rollback()
            return HTMLResponse(
                "<h3>Аккаунт для переподключения не найден. Вернись в бот.</h3>",
                status_code=400,
            )
        telegram = (
            await session.execute(
                text("SELECT telegram_id FROM users WHERE id = :user_id"),
                {"user_id": user_id},
            )
        ).first()
        await session.commit()

    username = outcome.username or me.get("username") or "Threads"
    keyboard = None
    if outcome.status == "connected_new":
        message = (
            f"✅ Threads подключён\n\nАккаунт: @{username}\n\n"
            "Сделать его активным?"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🚀 Настроить за 2 минуты",
                callback_data=f"ob:start:{outcome.account_id}",
            )],
            [InlineKeyboardButton(
                text="✅ Сделать активным",
                callback_data=f"cab:select:{outcome.account_id}",
            )],
            [InlineKeyboardButton(
                text="Оставить текущий",
                callback_data="cab:accounts",
            )],
            [InlineKeyboardButton(
                text="🆕 Настроить с нуля",
                callback_data=f"cab:setup_default:{outcome.account_id}",
            )],
            [InlineKeyboardButton(
                text="📋 Скопировать настройки",
                callback_data=f"cab:copy_settings:{outcome.account_id}",
            )],
        ])
    elif outcome.status == "refreshed":
        message = (
            "✅ Этот аккаунт уже подключён. Авторизация обновлена.\n\n"
            f"Аккаунт: @{username}"
        )
    elif outcome.status == "reconnect_mismatch":
        log.warning(
            "threads oauth reconnect mismatch user=%s expected_account=%s "
            "returned_threads=%s",
            user_id,
            oauth_state["expected_threads_account_id"],
            safe_threads_id(str(me["id"])),
        )
        expected = outcome.expected_username or "ожидаемый аккаунт"
        returned = outcome.returned_username or "другой аккаунт"
        message = (
            "⚠️ Авторизован другой Threads-аккаунт.\n\n"
            f"Ожидался: @{expected}\nПолучен: @{returned}"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="➕ Подключить как новый",
                callback_data="cab:connect",
            )],
            [InlineKeyboardButton(
                text="Отмена",
                callback_data="cab:accounts",
            )],
        ])
    else:
        log.warning(
            "threads oauth ownership conflict user=%s threads=%s",
            user_id,
            safe_threads_id(str(me["id"])),
        )
        message = (
            "❌ Этот Threads-аккаунт уже связан с другим пользователем. "
            "Автоматический перенос запрещён."
        )

    if telegram:
        try:
            await bot.send_message(
                telegram[0],
                message,
                reply_markup=keyboard,
            )
        except Exception as error:
            log.warning(
                "oauth Telegram notification failed user=%s error_type=%s",
                user_id,
                type(error).__name__,
            )
    if outcome.status == "ownership_conflict":
        return HTMLResponse(
            "<h3>Этот Threads-аккаунт уже подключён другим пользователем.</h3>",
            status_code=409,
        )
    if outcome.status == "reconnect_mismatch":
        return HTMLResponse(
            "<h3>Авторизован другой аккаунт. Вернитесь в бот и выберите действие.</h3>",
            status_code=409,
        )
    return HTMLResponse(
        "<h3>Готово. Авторизация обновлена, вернитесь в бот.</h3>"
    )


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

async def _read_threads_meta_payload(request: Request) -> dict:
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
async def threads_deauthorize_callback(request: Request):
    if request.method == "GET":
        return {
            "status": "ok",
            "endpoint": "threads_deauthorize",
        }

    payload = await _read_threads_meta_payload(request)
    try:
        verified = verify_signed_request(
            payload.get("signed_request", ""),
            settings.THREADS_APP_SECRET,
        )
    except InvalidSignedRequest:
        return JSONResponse(
            status_code=400,
            content={"success": False, "error": "invalid_signed_request"},
        )
    threads_user_id = str(verified["user_id"])
    async with Session() as session:
        row = (
            await session.execute(
                text("""
                    SELECT id, user_id
                    FROM threads_accounts
                    WHERE threads_user_id = :threads_user_id
                    FOR UPDATE
                """),
                {"threads_user_id": threads_user_id},
            )
        ).first()
        if row:
            try:
                await ThreadsAccountService(session).disconnect(
                    int(row[1]),
                    int(row[0]),
                )
            except AccountNotFoundError:
                pass
        await session.commit()
    return JSONResponse(status_code=200, content={"success": True})


@app.api_route(
    "/oauth/threads/data-deletion",
    methods=["GET", "POST"],
)
async def threads_data_deletion_callback(request: Request):
    if request.method == "GET":
        confirmation_code = request.query_params.get("code")

        if not confirmation_code:
            return {"status": "ready"}
        async with Session() as session:
            row = (
                await session.execute(
                    text("""
                        SELECT status, requested_at, completed_at
                        FROM threads_data_deletion_requests
                        WHERE confirmation_code = :confirmation_code
                    """),
                    {"confirmation_code": confirmation_code},
                )
            ).mappings().first()
        if row is None:
            return JSONResponse(
                status_code=404,
                content={"status": "not_found"},
            )
        return {
            "status": row["status"],
            "confirmation_code": confirmation_code,
        }

    payload = await _read_threads_meta_payload(request)
    try:
        verified = verify_signed_request(
            payload.get("signed_request", ""),
            settings.THREADS_APP_SECRET,
        )
    except InvalidSignedRequest:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_signed_request"},
        )
    threads_user_id = str(verified["user_id"])
    async with Session() as session:
        service = ThreadsAccountService(session)
        row = (
            await session.execute(
                text("""
                    SELECT id, user_id
                    FROM threads_accounts
                    WHERE threads_user_id = :threads_user_id
                    FOR UPDATE
                """),
                {"threads_user_id": threads_user_id},
            )
        ).first()
        if row:
            try:
                await service.delete_account_data(int(row[1]), int(row[0]))
            except AccountBusyError:
                await session.rollback()
                return JSONResponse(
                    status_code=409,
                    content={"error": "publication_in_progress"},
                )
        confirmation_code = await service.record_deletion_request(
            threads_user_id,
            status="completed",
        )
        await session.commit()

    base_url = settings.PUBLIC_BASE_URL.rstrip("/") or "https://threadsmith.pro"
    return JSONResponse(
        status_code=200,
        content={
            "url": (
                f"{base_url}/oauth/threads/"
                f"data-deletion?code={confirmation_code}"
            ),
            "confirmation_code": confirmation_code,
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
    log.info("threads oauth start accepted")

    return RedirectResponse(url=url, status_code=302)

from app.api.legal import router as legal_router
app.include_router(legal_router)
