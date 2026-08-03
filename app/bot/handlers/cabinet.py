"""Telegram cabinet for account-scoped Threads connection management."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from sqlalchemy import text

from app.core.accounts import (
    AccountBusyError,
    AccountNotFoundError,
    ThreadsAccountService,
    authorization_label,
    authorization_status,
    ensure_aware,
    safe_threads_id,
)
from app.core.autopost_status import (
    AutopostStatusService,
    format_local_datetime,
)
from app.core.config import PLANS
from app.core.db import Session
from app.core.threads_api import auth_link

log = logging.getLogger("account_cabinet")
router = Router()


def _callback_account_id(data: str) -> int | None:
    try:
        return int(data.rsplit(":", 1)[1])
    except (AttributeError, TypeError, ValueError):
        return None


async def _user_id(telegram_id: int) -> int | None:
    async with Session() as session:
        return await ThreadsAccountService(
            session
        ).user_id_for_telegram(telegram_id)


def _account_name(account) -> str:
    return f"@{account.username or account.id}"


def _connect_keyboard(state: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Продолжить в Threads",
            url=auth_link(state),
        )
    ]])


async def _start_oauth(
    cb: CallbackQuery,
    *,
    action: str,
    expected_account_id: int | None = None,
) -> None:
    async with Session() as session:
        service = ThreadsAccountService(session)
        user_id = await service.user_id_for_telegram(cb.from_user.id)
        if user_id is None:
            await cb.answer("Сначала нажми /start", show_alert=True)
            return
        try:
            oauth = await service.create_oauth_state(
                user_id,
                action=action,
                expected_account_id=expected_account_id,
            )
        except AccountNotFoundError:
            await cb.answer("Аккаунт не найден", show_alert=True)
            return
        await session.commit()
    title = (
        "Переподключение Threads-аккаунта."
        if action == "reconnect"
        else "Подключение нового Threads-аккаунта."
    )
    await cb.message.answer(
        title + " Авторизуй нужный аккаунт и подтверди доступ.",
        reply_markup=_connect_keyboard(oauth.state),
    )
    await cb.answer()


@router.callback_query(F.data == "cab:menu")
async def cb_cabinet(cb: CallbackQuery):
    async with Session() as session:
        service = ThreadsAccountService(session)
        user_id = await service.user_id_for_telegram(cb.from_user.id)
        if user_id is None:
            await cb.answer("Сначала нажми /start", show_alert=True)
            return
        row = (
            await session.execute(
                text("""
                    SELECT
                      users.credits_balance,
                      coalesce(subscription.plan, 'free') AS plan
                    FROM users
                    LEFT JOIN subscriptions subscription
                      ON subscription.user_id = users.id
                    WHERE users.id = :user_id
                """),
                {"user_id": user_id},
            )
        ).mappings().first()
        accounts = await service.list_accounts(user_id)
        selected = await service.selected_account(user_id)
        await session.commit()
    plan_code = row["plan"] if row else "free"
    plan_title = PLANS.get(plan_code, PLANS["free"])["title"]
    username = cb.from_user.username
    lines = [
        "👤 ЛИЧНЫЙ КАБИНЕТ",
        "",
        f"Telegram: @{username}" if username else (
            f"Telegram ID: {cb.from_user.id}"
        ),
        f"Тариф: {plan_title}",
        f"Баланс: {int(row['credits_balance'] if row else 0):,} кредитов".replace(
            ",",
            " ",
        ),
        "Подключено Threads-аккаунтов: "
        + str(sum(
            account.connection_status == "connected"
            for account in accounts
        )),
        "",
        "Активный аккаунт:",
        (
            f"✅ {_account_name(selected)}"
            if selected
            else "Threads-аккаунт не подключён"
        ),
    ]
    buttons = [
        [InlineKeyboardButton(
            text="🔗 Мои Threads-аккаунты",
            callback_data="cab:accounts",
        )],
        [
            InlineKeyboardButton(text="⚡ Баланс", callback_data="balance"),
            InlineKeyboardButton(text="💳 Тариф", callback_data="plans"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")],
    ]
    await cb.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await cb.answer()


@router.callback_query(F.data == "cab:accounts")
@router.callback_query(F.data == "cab:accounts:refresh")
async def cb_accounts(cb: CallbackQuery):
    async with Session() as session:
        account_service = ThreadsAccountService(session)
        user_id = await account_service.user_id_for_telegram(
            cb.from_user.id
        )
        if user_id is None:
            await cb.answer("Сначала нажми /start", show_alert=True)
            return
        accounts = await account_service.list_accounts(user_id)
        selected = await account_service.selected_account(user_id)
        status_service = AutopostStatusService(session)
        summaries = []
        for account in accounts:
            status = await status_service.get_status(user_id, account.id)
            summaries.append((account, status))
        await session.commit()
    lines = ["🔗 МОИ THREADS-АККАУНТЫ", ""]
    buttons = []
    if not accounts:
        lines.extend([
            "Threads-аккаунт не подключён.",
            "",
            "Подключи аккаунт, чтобы использовать публикацию и аналитику.",
        ])
    for account, status in summaries:
        marker = "✅ " if selected and selected.id == account.id else ""
        if authorization_status(account) in {"EXPIRED", "ERROR"}:
            marker = "⚠️ "
        lines.extend([
            f"{marker}{_account_name(account)}",
            "Автопостинг: " + (
                "включён" if account.autoposting_enabled else "выключен"
            ),
            authorization_label(account),
        ])
        if status and status.next_run_at:
            lines.append(
                "Следующий пост: "
                + format_local_datetime(
                    status.next_run_at,
                    status.settings.timezone,
                ).lower()
            )
        lines.append("")
        buttons.append([InlineKeyboardButton(
            text=marker + _account_name(account),
            callback_data=f"cab:account:{account.id}",
        )])
    buttons.extend([
        [InlineKeyboardButton(
            text="➕ Подключить аккаунт",
            callback_data="cab:connect",
        )],
        [InlineKeyboardButton(
            text="🔄 Обновить",
            callback_data="cab:accounts:refresh",
        )],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="cab:menu")],
    ])
    await cb.message.answer(
        "\n".join(lines).rstrip(),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("cab:account:"))
async def cb_account(cb: CallbackQuery):
    account_id = _callback_account_id(cb.data)
    async with Session() as session:
        service = ThreadsAccountService(session)
        user_id = await service.user_id_for_telegram(cb.from_user.id)
        account = (
            await service.get_owned(user_id, account_id)
            if user_id is not None and account_id is not None
            else None
        )
        if account is None:
            await cb.answer("Аккаунт не найден", show_alert=True)
            return
        status = await AutopostStatusService(session).get_status(
            user_id,
            account_id,
        )
    current = datetime.now(timezone.utc)
    auth_state = authorization_status(account, now=current)
    remaining_days = max(
        0,
        (ensure_aware(account.expires_at) - current).days,
    )
    lines = [
        "🔗 THREADS-АККАУНТ",
        "",
        f"Аккаунт: {_account_name(account)}",
        f"Threads ID: {safe_threads_id(account.threads_user_id)}",
        "Статус: " + (
            "✅ Подключён"
            if account.connection_status == "connected"
            else "⚪ Отключён"
        ),
        f"Авторизация: {authorization_label(account, now=current)}",
        f"Действует до: {ensure_aware(account.expires_at):%d.%m.%Y}",
        f"Осталось: {remaining_days} дн.",
    ]
    if status:
        lines.extend([
            "Автопостинг: " + (
                "🟢 включён" if status.settings.enabled else "⚪ выключен"
            ),
            f"Постов в день: {status.settings.posts_per_day}",
            "Следующий пост: " + (
                format_local_datetime(
                    status.next_run_at,
                    status.settings.timezone,
                    now=current,
                ).lower()
                if status.next_run_at
                else "не запланирован"
            ),
            "Последняя публикация: " + (
                format_local_datetime(
                    status.last_success_at,
                    status.settings.timezone,
                    now=current,
                ).lower()
                if status.last_success_at
                else "нет"
            ),
        ])
    buttons = []
    if account.selected:
        buttons.append([InlineKeyboardButton(
            text="✅ Активный аккаунт",
            callback_data=f"cab:account:{account.id}",
        )])
    elif account.connection_status == "connected":
        buttons.append([InlineKeyboardButton(
            text="✅ Выбрать активным",
            callback_data=f"cab:select:{account.id}",
        )])
    if account.connection_status == "connected":
        buttons.extend([
            [InlineKeyboardButton(
                text="🚀 Автопилот",
                callback_data=f"cab:autopilot:{account.id}",
            )],
            [InlineKeyboardButton(
                text="📋 Очередь",
                callback_data=f"ap:queue:{account.id}",
            )],
            [InlineKeyboardButton(
                text=(
                    "⏸ Остановить автопостинг"
                    if account.autoposting_enabled
                    else "▶️ Включить автопостинг"
                ),
                callback_data=f"ac:toggle:{account.id}",
            )],
        ])
    if auth_state != "CONNECTED" or account.connection_status == "connected":
        buttons.append([InlineKeyboardButton(
            text="🔄 Переподключить",
            callback_data=f"cab:reconnect:{account.id}",
        )])
    if account.connection_status == "connected":
        buttons.append([InlineKeyboardButton(
            text="🗑 Отключить аккаунт",
            callback_data=f"cab:disconnect:{account.id}",
        )])
    buttons.extend([
        [InlineKeyboardButton(
            text="⚙️ Дополнительно",
            callback_data=f"cab:more:{account.id}",
        )],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="cab:accounts")],
    ])
    await cb.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("cab:select:"))
async def cb_select(cb: CallbackQuery):
    account_id = _callback_account_id(cb.data)
    async with Session() as session:
        service = ThreadsAccountService(session)
        user_id = await service.user_id_for_telegram(cb.from_user.id)
        account = (
            await service.select_account(user_id, account_id)
            if user_id is not None and account_id is not None
            else None
        )
        if account is None:
            await cb.answer("Аккаунт не найден", show_alert=True)
            return
        await session.commit()
    await cb.message.answer(
        f"✅ Активным выбран {_account_name(account)}"
    )
    await cb.answer()


@router.callback_query(F.data.startswith("cab:autopilot:"))
async def cb_open_autopilot(cb: CallbackQuery):
    account_id = _callback_account_id(cb.data)
    async with Session() as session:
        service = ThreadsAccountService(session)
        user_id = await service.user_id_for_telegram(cb.from_user.id)
        account = (
            await service.select_account(user_id, account_id)
            if user_id is not None and account_id is not None
            else None
        )
        if account is None:
            await cb.answer("Аккаунт не найден", show_alert=True)
            return
        await session.commit()
    await cb.message.answer(
        f"Активный аккаунт: {_account_name(account)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="🚀 Открыть Автопилот",
                callback_data="ap:menu",
            )
        ]]),
    )
    await cb.answer()


@router.callback_query(F.data == "cab:connect")
async def cb_connect(cb: CallbackQuery):
    await _start_oauth(cb, action="connect")


@router.callback_query(F.data.startswith("cab:reconnect:"))
async def cb_reconnect(cb: CallbackQuery):
    await _start_oauth(
        cb,
        action="reconnect",
        expected_account_id=_callback_account_id(cb.data),
    )


@router.callback_query(F.data.startswith("cab:disconnect:"))
async def cb_disconnect_confirm(cb: CallbackQuery):
    account_id = _callback_account_id(cb.data)
    async with Session() as session:
        service = ThreadsAccountService(session)
        user_id = await service.user_id_for_telegram(cb.from_user.id)
        account = (
            await service.get_owned(user_id, account_id)
            if user_id is not None and account_id is not None
            else None
        )
    if account is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    await cb.message.answer(
        f"⚠️ Отключить {_account_name(account)}?\n\n"
        "Будет остановлен автопостинг этого аккаунта.\n\n"
        "Будут удалены его будущие неопубликованные автопосты.\n\n"
        "Потраченные кредиты не возвращаются.\n\n"
        "Опубликованные посты и история останутся.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🗑 Да, отключить",
                callback_data=f"cab:disconnect_confirm:{account.id}",
            )],
            [InlineKeyboardButton(
                text="⬅️ Отмена",
                callback_data=f"cab:account:{account.id}",
            )],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("cab:disconnect_confirm:"))
async def cb_disconnect(cb: CallbackQuery):
    account_id = _callback_account_id(cb.data)
    async with Session() as session:
        service = ThreadsAccountService(session)
        user_id = await service.user_id_for_telegram(cb.from_user.id)
        try:
            result = await service.disconnect(user_id, account_id)
            await session.commit()
        except AccountNotFoundError:
            await session.rollback()
            await cb.answer("Аккаунт не найден", show_alert=True)
            return
    lines = [
        f"✅ {_account_name(result.account)} отключён.",
        f"Удалено будущих автопостов: {result.affected_posts}",
        "Возвращено кредитов: 0",
    ]
    if result.next_selected:
        lines.append(
            f"Активным выбран {_account_name(result.next_selected)}."
        )
    else:
        lines.append("Threads-аккаунт не подключён.")
    await cb.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="🔗 Мои Threads-аккаунты",
                callback_data="cab:accounts",
            )
        ]]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("cab:more:"))
async def cb_more(cb: CallbackQuery):
    account_id = _callback_account_id(cb.data)
    user_id = await _user_id(cb.from_user.id)
    async with Session() as session:
        account = (
            await ThreadsAccountService(session).get_owned(
                user_id,
                account_id,
            )
            if user_id is not None and account_id is not None
            else None
        )
    if account is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    await cb.message.answer(
        f"Дополнительные действия для {_account_name(account)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🧹 Удалить данные аккаунта",
                callback_data=f"cab:delete:{account.id}",
            )],
            [InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"cab:account:{account.id}",
            )],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("cab:delete:"))
async def cb_delete_confirm(cb: CallbackQuery):
    account_id = _callback_account_id(cb.data)
    user_id = await _user_id(cb.from_user.id)
    async with Session() as session:
        account = (
            await ThreadsAccountService(session).get_owned(
                user_id,
                account_id,
            )
            if user_id is not None and account_id is not None
            else None
        )
    if account is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    await cb.message.answer(
        f"⚠️ Удалить данные {_account_name(account)}?\n\n"
        "Будут удалены токен, настройки, очередь, Social Brain, "
        "история публикаций и связанные account-scoped данные.\n\n"
        "Тариф, кредиты и другие Threads-аккаунты останутся.\n\n"
        "Это действие необратимо.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🧹 Удалить навсегда",
                callback_data=f"cab:delete_confirm:{account.id}",
            )],
            [InlineKeyboardButton(
                text="Отмена",
                callback_data=f"cab:account:{account.id}",
            )],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("cab:delete_confirm:"))
async def cb_delete(cb: CallbackQuery):
    account_id = _callback_account_id(cb.data)
    async with Session() as session:
        service = ThreadsAccountService(session)
        user_id = await service.user_id_for_telegram(cb.from_user.id)
        try:
            result = await service.delete_account_data(
                user_id,
                account_id,
            )
            await session.commit()
        except AccountBusyError:
            await session.rollback()
            await cb.answer(
                "Сейчас идёт публикация. Повтори после её завершения.",
                show_alert=True,
            )
            return
        except AccountNotFoundError:
            await session.rollback()
            await cb.answer("Аккаунт не найден", show_alert=True)
            return
    lines = [
        f"✅ Данные {_account_name(result.account)} удалены.",
        f"Удалено публикаций: {result.affected_posts}",
    ]
    if result.next_selected:
        lines.append(
            f"Активным выбран {_account_name(result.next_selected)}."
        )
    else:
        lines.append("Threads-аккаунт не подключён.")
    await cb.message.answer("\n".join(lines))
    await cb.answer()


@router.callback_query(F.data.startswith("cab:setup_default:"))
async def cb_setup_default(cb: CallbackQuery):
    account_id = _callback_account_id(cb.data)
    async with Session() as session:
        service = ThreadsAccountService(session)
        user_id = await service.user_id_for_telegram(cb.from_user.id)
        ok = await service.ensure_settings(user_id, account_id)
        await session.commit()
    await cb.answer(
        "Настройки созданы" if ok else "Аккаунт не найден",
        show_alert=not ok,
    )


@router.callback_query(F.data.startswith("cab:copy_settings:"))
async def cb_copy_settings(cb: CallbackQuery):
    target_id = _callback_account_id(cb.data)
    async with Session() as session:
        service = ThreadsAccountService(session)
        user_id = await service.user_id_for_telegram(cb.from_user.id)
        target = await service.get_owned(user_id, target_id)
        accounts = await service.list_accounts(user_id)
    if target is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    sources = [account for account in accounts if account.id != target_id]
    if not sources:
        await cb.answer("Нет другого аккаунта для копирования", show_alert=True)
        return
    buttons = [[InlineKeyboardButton(
        text=_account_name(account),
        callback_data=f"cab:copy_from:{target_id}:{account.id}",
    )] for account in sources]
    buttons.append([InlineKeyboardButton(
        text="Отмена",
        callback_data=f"cab:account:{target_id}",
    )])
    await cb.message.answer(
        "Копировать настройки с:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("cab:copy_from:"))
async def cb_copy_from(cb: CallbackQuery):
    try:
        _, _, target_raw, source_raw = cb.data.split(":", 3)
        target_id = int(target_raw)
        source_id = int(source_raw)
    except (TypeError, ValueError):
        await cb.answer("Некорректная команда", show_alert=True)
        return
    async with Session() as session:
        service = ThreadsAccountService(session)
        user_id = await service.user_id_for_telegram(cb.from_user.id)
        ok = await service.copy_settings(
            user_id,
            source_id,
            target_id,
        )
        await session.commit()
    await cb.answer(
        "Настройки скопированы" if ok else "Аккаунт не найден",
        show_alert=not ok,
    )
