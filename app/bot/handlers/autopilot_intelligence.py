"""Russian account-scoped explanation and decision history screens."""

from __future__ import annotations

import logging
from datetime import timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.ux import navigation_row
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

_COMPONENT_LABELS = {
    "token": ("Подключение", 20),
    "credits": ("Баланс", 15),
    "queue": ("Очередь", 20),
    "analytics": ("Статистика", 15),
    "radar": ("Поиск обсуждений", 10),
    "neuro": ("Комментарии", 10),
    "publishing": ("Публикация", 10),
}


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
        "🧭 Почему Автопилот так решил",
        "",
        f"Состояние: {STATUS_LABELS[result.status]}",
        f"Оценка: {result.health_score} из 100",
        "",
        "Из чего складывается оценка:",
    ]
    breakdown = result.health_breakdown.model_dump()
    for key, (label, maximum) in _COMPONENT_LABELS.items():
        lines.append(f"• {label}: {breakdown[key]} из {maximum}")
    lines.extend(["", "Причины:"])
    lines.extend(
        f"• {reason_message(code)}" for code in result.reason_codes[:8]
    )
    if result.blockers:
        lines.extend(["", "Что мешает работе:"])
        lines.extend(
            f"• {reason_message(code)}" for code in result.blockers
        )
    if result.warnings:
        lines.extend(["", "На что обратить внимание:"])
        lines.extend(
            f"• {reason_message(code)}" for code in result.warnings
        )
    lines.extend([
        "",
        "Следующий шаг:",
        ACTION_LABELS[result.next_recommended_action],
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
            text="История решений",
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
    await cb.message.answer(
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
    lines = ["🕘 История решений", ""]
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
    await cb.message.answer(
        "\n".join(lines).rstrip(),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await cb.answer()
