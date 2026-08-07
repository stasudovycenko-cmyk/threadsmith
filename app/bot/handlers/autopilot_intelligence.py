"""Russian account-scoped explanation and decision history screens."""

from __future__ import annotations

import logging
from datetime import timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.ux import escape_html, navigation_row, show_ui_screen
from app.core.accounts import ThreadsAccountService
from app.core.autopilot_intelligence.localization import (
    ACTION_LABELS,
    STATUS_LABELS,
    reason_message,
)
from app.core.autopilot_intelligence.models import ActionType, DecisionRun
from app.core.autopilot_intelligence.repository import DecisionRepository
from app.core.autopilot_intelligence.service import AutopilotIntelligenceService
from app.core.db import Session

router = Router()
log = logging.getLogger("autopilot_intelligence_bot")
HISTORY_PAGE_SIZE = 5

async def _selected_account(telegram_id: int):
    async with Session() as session:
        accounts = ThreadsAccountService(session)
        user_id = await accounts.user_id_for_telegram(telegram_id)
        account = (
            await accounts.selected_account(user_id)
            if user_id is not None else None
        )
        await session.commit()
    return user_id, account


def _safe_target(action: ActionType, account_id: int):
    return {
        ActionType.RECONNECT_ACCOUNT: (
            "Переподключить аккаунт", f"cab:reconnect:{account_id}"
        ),
        ActionType.OPEN_BALANCE: ("Открыть баланс", "balance"),
        ActionType.OPEN_QUEUE: ("Открыть очередь", f"ap:queue:{account_id}"),
        ActionType.OPEN_RECOVERY: (
            "Открыть историю публикаций", f"ac:history:{account_id}"
        ),
        ActionType.OPEN_RADAR: ("Открыть найденные обсуждения", "rd:ready"),
        ActionType.OPEN_NEURO: ("Открыть комментарии", "nc:pending"),
        ActionType.OPEN_ANALYTICS: ("Открыть аналитику", "an:menu"),
        ActionType.OPEN_SCHEDULE: (
            "Открыть настройки Автопилота", f"ac:menu:{account_id}"
        ),
    }.get(action)


def render_explanation(run: DecisionRun) -> str:
    result = run.result
    lines = [
        "💡 <b>Почему Автопилот так решил</b>",
        "",
        f"Состояние аккаунта: <b>{result.health_score}/100</b>",
        f"Статус: <b>{STATUS_LABELS[result.status]}</b>",
    ]
    positive = [
        code for code in result.reason_codes
        if code not in set(result.blockers) | set(result.warnings)
    ]
    if positive:
        lines.extend(["", "<b>Что хорошо</b>"])
        lines.extend(f"• {reason_message(code)}" for code in positive[:5])
    if result.blockers:
        lines.extend(["", "<b>Что мешает работе</b>"])
        lines.extend(
            f"• {reason_message(code)}" for code in result.blockers
        )
    if result.warnings:
        lines.extend(["", "<b>Что стоит улучшить</b>"])
        lines.extend(
            f"• {reason_message(code)}" for code in result.warnings
        )
    lines.extend([
        "",
        "<b>Что делать сейчас</b>",
        f"Следующий шаг: {ACTION_LABELS[result.next_recommended_action]}",
        "",
        "Автопилот ничего не изменил автоматически.",
    ])
    return "\n".join(lines)


def explanation_keyboard(run: DecisionRun) -> InlineKeyboardMarkup:
    rows = []
    target = _safe_target(run.result.safe_action, run.threads_account_id)
    if target:
        label, callback_data = target
        rows.append([InlineKeyboardButton(
            text=label,
            callback_data=callback_data,
        )])
    rows.extend([
        [InlineKeyboardButton(
            text="История рекомендаций",
            callback_data="intel:history:0",
        )],
        navigation_row("home"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "intel:why")
async def cb_why(cb: CallbackQuery):
    user_id, account = await _selected_account(cb.from_user.id)
    if user_id is None or account is None:
        await cb.answer("Подключите Threads-аккаунт", show_alert=True)
        return
    async with Session() as session:
        service = AutopilotIntelligenceService(session)
        try:
            run = await service.history.latest(user_id, account.id)
            if run is None:
                run = await service.evaluate_account(user_id, account.id)
            await session.commit()
        except Exception as error:
            await session.rollback()
            log.warning(
                "autopilot explanation unavailable user=%s account=%s "
                "error_type=%s",
                user_id,
                account.id,
                type(error).__name__,
            )
            await cb.message.answer(
                "Рекомендация пока недоступна. Публикации и настройки не изменились.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    navigation_row("home")
                ]),
            )
            await cb.answer()
            return
    await show_ui_screen(
        cb.message,
        render_explanation(run),
        reply_markup=explanation_keyboard(run),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("intel:history:"))
async def cb_history(cb: CallbackQuery):
    try:
        page = max(0, int(cb.data.rsplit(":", 1)[-1]))
    except (AttributeError, TypeError, ValueError):
        page = 0
    user_id, account = await _selected_account(cb.from_user.id)
    if user_id is None or account is None:
        await cb.answer("Подключите Threads-аккаунт", show_alert=True)
        return
    async with Session() as session:
        runs = await DecisionRepository(session).history(
            user_id,
            account.id,
            limit=HISTORY_PAGE_SIZE + 1,
            offset=page * HISTORY_PAGE_SIZE,
        )
    visible = runs[:HISTORY_PAGE_SIZE]
    lines = ["🕘 История рекомендаций", ""]
    if not visible:
        lines.append("История пока пуста.")
    for run in visible:
        created = run.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        lines.extend([
            created.strftime("%d.%m %H:%M"),
            f"{run.result.health_score} из 100 · {run.result.human_message}",
            "",
        ])
    rows = []
    navigation = []
    if page:
        navigation.append(InlineKeyboardButton(
            text="Назад", callback_data=f"intel:history:{page - 1}"
        ))
    if len(runs) > HISTORY_PAGE_SIZE:
        navigation.append(InlineKeyboardButton(
            text="Дальше", callback_data=f"intel:history:{page + 1}"
        ))
    if navigation:
        rows.append(navigation)
    rows.append(navigation_row("intel:why"))
    await show_ui_screen(
        cb.message,
        escape_html("\n".join(lines).rstrip()).replace(
            "🕘 История рекомендаций",
            "🕘 <b>История рекомендаций</b>",
            1,
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await cb.answer()
