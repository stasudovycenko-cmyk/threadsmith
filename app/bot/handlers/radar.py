"""Account-scoped Radar Telegram UI."""

import json
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import text

from app.core import credits, radar
from app.core.accounts import ThreadsAccountService, authorization_status
from app.core.ai_cost import AIUsageContext
from app.core.config import CREDIT_COSTS
from app.core.crypto import decrypt_token
from app.core.db import Session
from app.core.llm import LLMError, LLMGuardError

log = logging.getLogger("radar_bot")
router = Router()


class Niche(StatesGroup):
    setup = State()


def radar_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Запустить поиск", callback_data="rd:search")],
        [
            InlineKeyboardButton(text="Ниша и keywords", callback_data="rd:niche"),
            InlineKeyboardButton(text="История", callback_data="rd:history"),
        ],
        [InlineKeyboardButton(text="Мои посты", callback_data="rd:my")],
        [InlineKeyboardButton(text="Главная", callback_data="home")],
    ])


async def _selected(telegram_id: int):
    async with Session() as session:
        service = ThreadsAccountService(session)
        user_id = await service.user_id_for_telegram(telegram_id)
        account = (
            await service.selected_credentials(user_id)
            if user_id is not None
            else None
        )
        if account:
            await service.ensure_settings(user_id, account.id)
        await session.commit()
    return user_id, account


async def _require_selected(cb: CallbackQuery):
    user_id, account = await _selected(cb.from_user.id)
    if account is None:
        await cb.answer("Подключите Threads-аккаунт", show_alert=True)
        return user_id, None
    if authorization_status(account) == "EXPIRED":
        await cb.answer("Авторизация Threads истекла", show_alert=True)
        return user_id, None
    return user_id, account


@router.callback_query(F.data == "rd:menu")
async def cb_menu(cb: CallbackQuery):
    user_id, account = await _require_selected(cb)
    if account is None:
        return
    async with Session() as session:
        row = (await session.execute(text("""
            SELECT setting.niche, setting.keywords, setting.language,
                   (SELECT count(*) FROM radar_candidates candidate
                    WHERE candidate.user_id = :user_id
                      AND candidate.threads_account_id = :account_id
                      AND candidate.status = 'ready'),
                   (SELECT max(final_score) FROM radar_candidates candidate
                    WHERE candidate.user_id = :user_id
                      AND candidate.threads_account_id = :account_id
                      AND candidate.status = 'ready'),
                   (SELECT status FROM radar_search_runs run
                    WHERE run.user_id = :user_id
                      AND run.threads_account_id = :account_id
                    ORDER BY started_at DESC LIMIT 1)
            FROM radar_settings setting
            WHERE setting.user_id = :user_id
              AND setting.threads_account_id = :account_id
        """), {"user_id": user_id, "account_id": account.id})).first()
    niche, keywords, language, ready_count, best_score, last_status = row
    keyword_text = ", ".join(keywords or []) or "не заданы"
    await cb.message.answer(
        f"Radar для @{account.username or account.id}\n"
        f"Статус: {'готов' if keywords else 'нужны keywords'}\n"
        f"Ниша: {niche or 'не задана'}\n"
        f"Keywords: {keyword_text}\n"
        f"Язык: {language}\n"
        f"Готовых кандидатов: {ready_count}\n"
        f"Лучший score: {best_score if best_score is not None else '-'}\n"
        f"Последний поиск: {last_status or '-'}",
        reply_markup=radar_kb(),
    )
    await cb.answer()


@router.callback_query(F.data == "rd:niche")
async def cb_niche(cb: CallbackQuery, state: FSMContext):
    _, account = await _require_selected(cb)
    if account is None:
        return
    await state.set_state(Niche.setup)
    await state.update_data(account_id=account.id)
    await cb.message.answer(
        "Укажите нишу и keywords через запятую.\n"
        "Пример: AI для бизнеса, автоматизация, нейросети, контент"
    )
    await cb.answer()


@router.message(Niche.setup)
async def niche_setup(msg: Message, state: FSMContext):
    parts = [part.strip() for part in (msg.text or "").split(",") if part.strip()]
    if not parts:
        await msg.answer("Формат: ниша, keyword 1, keyword 2")
        return
    state_data = await state.get_data()
    await state.clear()
    user_id, account = await _selected(msg.from_user.id)
    expected_account_id = state_data.get("account_id")
    if account is None or account.id != expected_account_id:
        await msg.answer("Выбранный аккаунт изменился. Откройте Radar заново.")
        return
    niche = parts[0][:200]
    keywords = [value[:100] for value in (parts[1:] or [parts[0]])][:10]
    async with Session() as session:
        updated = (await session.execute(text("""
            UPDATE radar_settings
            SET niche = :niche, keywords = :keywords, updated_at = now()
            WHERE user_id = :user_id AND threads_account_id = :account_id
            RETURNING threads_account_id
        """), {
            "niche": niche, "keywords": keywords,
            "user_id": user_id, "account_id": account.id,
        })).first()
        if updated:
            await session.execute(text("""
                INSERT INTO user_niches (user_id, niche, keywords)
                VALUES (:user_id, :niche, :keywords)
                ON CONFLICT (user_id) DO UPDATE
                SET niche = excluded.niche, keywords = excluded.keywords
            """), {
                "user_id": user_id, "niche": niche, "keywords": keywords,
            })
        await session.commit()
    await msg.answer(
        f"Аккаунт: @{account.username or account.id}\n"
        f"Ниша: {niche}\nKeywords: {', '.join(keywords)}",
        reply_markup=radar_kb(),
    )


@router.callback_query(F.data.in_({"rd:search", "rd:top"}))
async def cb_search(cb: CallbackQuery):
    user_id, account = await _require_selected(cb)
    if account is None:
        return
    await cb.answer("Ищу и оцениваю...")
    try:
        async with Session() as session:
            summary = await radar.discover_account_posts(
                session,
                user_id=user_id,
                account_id=account.id,
                token=decrypt_token(account.access_token_enc),
            )
            if summary.status == "success":
                await radar.semantic_score_candidates(
                    session, user_id=user_id, account_id=account.id
                )
            await session.commit()
    except Exception as error:
        log.warning(
            "manual radar failed user=%s account=%s error_type=%s",
            user_id, account.id, type(error).__name__,
        )
        await cb.message.answer("Поиск временно не завершён. Попробуйте позже.")
        return
    if summary.status == "permission_denied":
        await cb.message.answer(
            "Threads не дал разрешение на keyword search. "
            "Проверьте App Review и переподключите аккаунт."
        )
        return
    if summary.status == "failed":
        message = {
            "NO_KEYWORDS": "Сначала задайте keywords для выбранного аккаунта.",
            "SEARCH_QUOTA_EXHAUSTED": "Суточная квота поиска исчерпана.",
        }.get(summary.error_code, "Threads search временно не завершён.")
        await cb.message.answer(message)
        return
    async with Session() as session:
        best = (await session.execute(text("""
            SELECT author_username, post_text, final_score,
                   score_reason, permalink
            FROM radar_candidates
            WHERE user_id = :user_id AND threads_account_id = :account_id
              AND status = 'ready'
            ORDER BY final_score DESC, discovered_at DESC LIMIT 1
        """), {"user_id": user_id, "account_id": account.id})).first()
    await cb.message.answer(
        f"Поиск завершён для @{account.username or account.id}.\n"
        f"Результатов API: {summary.results_seen}\n"
        f"Сохранено: {summary.candidates_saved}\n"
        f"Отфильтровано: {summary.filtered}\n"
        f"Дублей: {summary.duplicates}"
    )
    if best:
        author, body, score, reason, permalink = best
        await cb.message.answer(
            f"Лучший кандидат: @{author or 'unknown'}\n"
            f"Оценка: {score}/100\n"
            f"Почему: {reason or '-'}\n\n"
            f"{body[:500]}\n\n{permalink or ''}"
        )


@router.callback_query(F.data == "rd:history")
async def cb_history(cb: CallbackQuery):
    user_id, account = await _require_selected(cb)
    if account is None:
        return
    async with Session() as session:
        rows = (await session.execute(text("""
            SELECT status, results_seen, candidates_saved,
                   filtered_count, duplicate_count, started_at, error_code
            FROM radar_search_runs
            WHERE user_id = :user_id AND threads_account_id = :account_id
            ORDER BY started_at DESC LIMIT 8
        """), {"user_id": user_id, "account_id": account.id})).all()
    if not rows:
        await cb.message.answer("История поиска пока пуста.")
    else:
        lines = ["Последние поиски:"]
        for status, seen, saved, filtered, duplicates, started_at, error in rows:
            lines.append(
                f"{started_at:%d.%m %H:%M}: {status}, "
                f"seen={seen}, saved={saved}, filtered={filtered}, "
                f"duplicates={duplicates}{f', {error}' if error else ''}"
            )
        await cb.message.answer("\n".join(lines))
    await cb.answer()


@router.callback_query(F.data.startswith("rd:rz:"))
async def cb_razbor(cb: CallbackQuery):
    post_id = cb.data.rsplit(":", 1)[1]
    user_id, account = await _require_selected(cb)
    if account is None:
        return
    cost = CREDIT_COSTS["razbor"]
    async with Session() as session:
        try:
            await credits.spend(session, user_id, cost, "razbor")
            await session.commit()
        except credits.NotEnoughCredits:
            await cb.answer("Не хватает кредитов", show_alert=True)
            return
    await cb.answer("Разбираю...")
    try:
        async with Session() as session:
            result = await radar.razbor(
                session,
                post_id,
                usage_context=AIUsageContext(
                    user_id=user_id, threads_account_id=account.id
                ),
            )
            await session.commit()
    except (LLMError, LLMGuardError, ValueError):
        async with Session() as session:
            await credits.topup(session, user_id, cost, "refund_razbor")
            await session.commit()
        await cb.message.answer("Разбор не завершён, кредиты возвращены.")
        return
    await cb.message.answer(
        f"Хук [{result.get('hook_type')}]: {result.get('hook')}\n\n"
        f"Структура: {result.get('structure')}\n\n"
        f"Триггер: {result.get('trigger')}\n\n"
        f"Концовка: {result.get('ending')}\n\n"
        f"Как повторить: {result.get('how_to_repeat')}"
    )


@router.callback_query(F.data == "rd:my")
async def cb_my(cb: CallbackQuery):
    user_id, account = await _require_selected(cb)
    if account is None:
        return
    async with Session() as session:
        rows = (await session.execute(text("""
            SELECT post.text, snapshot.metrics_json
            FROM scheduled_posts post
            JOIN LATERAL (
              SELECT metrics_json FROM insights_snapshots insight
              WHERE insight.threads_post_id = post.threads_post_id
              ORDER BY snapshot_date DESC LIMIT 1
            ) snapshot ON true
            WHERE post.user_id = :user_id
              AND post.threads_account_id = :account_id
              AND post.status = 'done'
            ORDER BY post.run_at DESC LIMIT 5
        """), {"user_id": user_id, "account_id": account.id})).all()
    if not rows:
        await cb.message.answer("Для выбранного аккаунта метрик пока нет.")
    else:
        lines = [f"Последние посты @{account.username or account.id}:"]
        for body, metrics_json in rows:
            metrics = (
                metrics_json if isinstance(metrics_json, dict)
                else json.loads(metrics_json)
            )
            lines.append(
                f"{body[:70]}\nviews={metrics.get('views', 0)}, "
                f"likes={metrics.get('likes', 0)}, "
                f"replies={metrics.get('replies', 0)}"
            )
        await cb.message.answer("\n\n".join(lines))
    await cb.answer()
