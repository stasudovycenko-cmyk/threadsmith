"""Account-scoped Radar Telegram UI."""

import json
import logging

from aiogram import F, Router
from aiogram.filters import Command
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
from app.bot.ux import (
    escape_html,
    format_local_time,
    format_number,
    navigation_row,
    render_error,
    show_ui_screen,
)

log = logging.getLogger("radar_bot")
router = Router()


class Niche(StatesGroup):
    setup = State()


def radar_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Найти посты сейчас", callback_data="rd:search")],
        [
            InlineKeyboardButton(text="📋 Подходящие посты", callback_data="rd:ready"),
            InlineKeyboardButton(text="🕘 История поиска", callback_data="rd:history"),
        ],
        [
            InlineKeyboardButton(text="🔑 Ключевые слова", callback_data="rd:niche"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="rd:settings"),
        ],
        [InlineKeyboardButton(text="ℹ️ Как это работает", callback_data="help:radar")],
        navigation_row("home"),
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
                   coalesce(content.timezone, 'Europe/Moscow'),
                   (SELECT count(*) FROM radar_candidates candidate
                    WHERE candidate.user_id = :user_id
                      AND candidate.threads_account_id = :account_id
                      AND candidate.status = 'ready'),
                   (SELECT count(*) FROM radar_candidates candidate
                    WHERE candidate.user_id = :user_id
                      AND candidate.threads_account_id = :account_id
                      AND candidate.status IN ('discovered', 'scoring')),
                   (SELECT final_score FROM radar_candidates candidate
                    WHERE candidate.user_id = :user_id
                      AND candidate.threads_account_id = :account_id
                      AND candidate.status = 'ready'
                    ORDER BY final_score DESC NULLS LAST,
                             discovered_at DESC LIMIT 1),
                   (SELECT status FROM radar_search_runs run
                    WHERE run.user_id = :user_id
                      AND run.threads_account_id = :account_id
                    ORDER BY started_at DESC LIMIT 1),
                   (SELECT started_at FROM radar_search_runs run
                    WHERE run.user_id = :user_id
                      AND run.threads_account_id = :account_id
                    ORDER BY started_at DESC LIMIT 1),
                   (SELECT results_seen FROM radar_search_runs run
                    WHERE run.user_id = :user_id
                      AND run.threads_account_id = :account_id
                    ORDER BY started_at DESC LIMIT 1)
            FROM radar_settings setting
            LEFT JOIN autocontent_settings content
              ON content.user_id = setting.user_id
             AND content.threads_account_id = setting.threads_account_id
            WHERE setting.user_id = :user_id
              AND setting.threads_account_id = :account_id
        """), {"user_id": user_id, "account_id": account.id})).first()
    (
        niche, keywords, language, timezone_name, ready_count, waiting_count,
        best_score, last_status, last_search_at, results_seen,
    ) = row
    keyword_text = ", ".join(keywords or []) or "не заданы"
    status_labels = {
        "success": "🟢 Поиск завершён",
        "running": "🟡 Идёт поиск",
        "permission_denied": "🔴 Нужны разрешения Threads",
        "failed": "🟡 Последний поиск не завершён",
    }
    lines = [
        "🔎 <b>Radar</b>",
        "",
        f"<b>Аккаунт: @{escape_html(account.username or account.id)}</b>",
        "Статус: <b>"
        + escape_html(status_labels.get(
            last_status, "⚪ Первый поиск ещё не запускался"
        ))
        + "</b>",
        "",
        "Ищет публичные обсуждения по вашей теме, где можно оставить "
        "полезный комментарий и получить новые просмотры.",
        "",
        "Последний поиск: " + (
            format_local_time(last_search_at, timezone_name)
            if last_search_at
            else "ещё не запускался"
        ),
        f"Просмотрено результатов: {results_seen or 0}",
        f"Подходящих постов: {ready_count or 0}",
        f"Ждут оценки: {waiting_count or 0}",
        "Лучшая оценка: "
        + (str(best_score) if best_score is not None else "нет данных"),
        "",
        f"Тема: <b>{escape_html(niche or 'не задана')}</b>",
        f"Ключевые слова: <b>{escape_html(keyword_text)}</b>",
        "Язык: " + escape_html(
            "русский" if language == "ru" else (language or "не указан")
        ),
    ]
    await show_ui_screen(
        cb.message,
        "\n".join(lines),
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
        f"🔑 Ключевые слова для @{account.username or account.id}\n\n"
        "Укажите тему и ключевые слова через запятую.\n"
        "Пример: AI для бизнеса, автоматизация, нейросети, контент.\n\n"
        "Для отмены используйте /cancel."
    )
    await cb.answer()


@router.message(Niche.setup, Command("cancel"))
async def cancel_niche(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Ввод отменён. Настройки не изменены.")


@router.message(Niche.setup)
async def niche_setup(msg: Message, state: FSMContext):
    parts = [part.strip() for part in (msg.text or "").split(",") if part.strip()]
    if not parts:
        await msg.answer(
            "Не удалось распознать тему.\n"
            "Формат: тема, ключевое слово 1, ключевое слово 2.\n"
            "Для отмены: /cancel"
        )
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
        f"Тема: {niche}\nКлючевые слова: {', '.join(keywords)}\n\n"
        "Сохранено. Следующий поиск будет использовать новые слова.",
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
        await cb.message.answer(render_error("threads_temporary"))
        return
    if summary.status == "permission_denied":
        await cb.message.answer(
            render_error("permission_denied"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔄 Переподключить аккаунт",
                    callback_data=f"cab:reconnect:{account.id}",
                )],
                navigation_row("rd:menu"),
            ]),
        )
        return
    if summary.status == "failed":
        message = {
            "NO_KEYWORDS": (
                "Сначала добавьте ключевые слова для выбранного аккаунта."
            ),
            "SEARCH_QUOTA_EXHAUSTED": (
                "Лимит поиска на сегодня исчерпан. Новый поиск будет доступен завтра."
            ),
        }.get(summary.error_code, render_error("threads_temporary"))
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
        f"Просмотрено результатов: {summary.results_seen}\n"
        f"Подходящих постов: {summary.candidates_saved}\n"
        f"Не подошли: {summary.filtered}\n"
        f"Уже были найдены раньше: {summary.duplicates}"
    )
    if best:
        author, body, score, reason, permalink = best
        await cb.message.answer(
            f"Лучший подходящий пост: @{author or 'автор'}\n"
            f"Оценка: {score}/100\n"
            f"Почему: {reason or '-'}\n\n"
            f"{body[:500]}\n\n{permalink or ''}"
        )


@router.callback_query(F.data == "rd:ready")
async def cb_ready(cb: CallbackQuery):
    user_id, account = await _require_selected(cb)
    if account is None:
        return
    async with Session() as session:
        rows = (await session.execute(text("""
            SELECT author_username, post_text, final_score, permalink
            FROM radar_candidates
            WHERE user_id = :user_id
              AND threads_account_id = :account_id
              AND status = 'ready'
            ORDER BY final_score DESC NULLS LAST, discovered_at DESC
            LIMIT 10
        """), {"user_id": user_id, "account_id": account.id})).all()
    if not rows:
        message = (
            f"📋 Подходящие посты\n\nАккаунт: @{account.username or account.id}\n\n"
            "Пока подходящих постов не найдено. Попробуйте добавить более "
            "широкие ключевые слова."
        )
    else:
        lines = ["📋 Подходящие посты", "", f"Аккаунт: @{account.username or account.id}"]
        for index, (author, body, score, permalink) in enumerate(rows, 1):
            lines.extend([
                "",
                f"{index}. @{author or 'автор'} · Оценка {score or 0}",
                " ".join(body.split())[:180],
                permalink or "",
            ])
        message = "\n".join(lines).rstrip()
    await cb.message.answer(message, reply_markup=radar_kb())
    await cb.answer()


@router.callback_query(F.data == "rd:settings")
async def cb_settings(cb: CallbackQuery):
    user_id, account = await _require_selected(cb)
    if account is None:
        return
    async with Session() as session:
        row = (await session.execute(text("""
            SELECT niche, keywords, language, max_age_hours
            FROM radar_settings
            WHERE user_id = :user_id
              AND threads_account_id = :account_id
        """), {"user_id": user_id, "account_id": account.id})).first()
    if row is None:
        await cb.answer("Настройки не найдены", show_alert=True)
        return
    niche, keywords, language, max_age_hours = row
    language_label = {"ru": "русский", "en": "английский", "any": "любой"}.get(
        language, "не указан"
    )
    await cb.message.answer(
        "⚙️ Настройки Radar\n\n"
        f"Аккаунт: @{account.username or account.id}\n"
        f"Тема: {niche or 'не задана'}\n"
        f"Ключевые слова: {', '.join(keywords or []) or 'не заданы'}\n"
        f"Язык: {language_label}\n"
        f"Возраст публикаций: до {max_age_hours} ч.\n\n"
        "Изменить тему и ключевые слова можно отдельной кнопкой.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🔑 Изменить ключевые слова",
                callback_data="rd:niche",
            )],
            navigation_row("rd:menu"),
        ]),
    )
    await cb.answer()


@router.callback_query(F.data == "rd:history")
async def cb_history(cb: CallbackQuery):
    user_id, account = await _require_selected(cb)
    if account is None:
        return
    async with Session() as session:
        rows = (await session.execute(text("""
            SELECT run.status, run.results_seen, run.candidates_saved,
                   filtered_count, duplicate_count, started_at, error_code
            FROM radar_search_runs run
            WHERE run.user_id = :user_id
              AND run.threads_account_id = :account_id
            ORDER BY run.started_at DESC LIMIT 8
        """), {"user_id": user_id, "account_id": account.id})).all()
        timezone_name = (await session.execute(text("""
            SELECT coalesce(timezone, 'Europe/Moscow')
            FROM autocontent_settings
            WHERE user_id = :user_id
              AND threads_account_id = :account_id
        """), {"user_id": user_id, "account_id": account.id})).scalar_one_or_none()
    if not rows:
        await cb.message.answer("История поиска пока пуста.")
    else:
        lines = ["🕘 История поиска", "", f"Аккаунт: @{account.username or account.id}"]
        status_labels = {
            "success": "Завершён",
            "permission_denied": "Недостаточно разрешений",
            "failed": "Не завершён",
            "running": "Выполняется",
        }
        for status, seen, saved, filtered, duplicates, started_at, error in rows:
            lines.extend([
                "",
                f"{format_local_time(started_at, timezone_name or 'Europe/Moscow')} · "
                f"{status_labels.get(status, 'Не завершён')}",
                f"Просмотрено: {seen} · Найдено: {saved}",
                f"Не подошли: {filtered} · Повторы: {duplicates}",
            ])
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
                f"{body[:70]}\nПросмотры: {format_number(metrics.get('views'))}, "
                f"реакции: {format_number(metrics.get('likes'))}, "
                f"ответы: {format_number(metrics.get('replies'))}"
            )
        await cb.message.answer("\n\n".join(lines))
    await cb.answer()
