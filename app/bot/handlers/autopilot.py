"""Manual scheduling, queue controls, and reply-rule Telegram UI."""
import logging
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from sqlalchemy import text

from app.core.accounts import ThreadsAccountService
from app.core.autopost_status import (
    AutopostStatusService,
    render_clear_result,
    render_queue_summary,
    render_rebuild_result,
    resolve_timezone,
)
from app.core.db import Session

log = logging.getLogger("autopilot_bot")
router = Router()


def render_clear_confirmation(summary) -> str:
    account_name = f"@{summary.account.username or summary.account.id}"
    return (
        "⚠️ Очистить очередь Автопилота?\n\n"
        f"Аккаунт: {account_name}\n"
        f"Будет удалено постов: {len(summary.posts)}\n\n"
        "Потраченные на генерацию кредиты не возвращаются.\n\n"
        "Опубликованные посты и история останутся. Другие аккаунты "
        "не будут затронуты."
    )

class Schedule(StatesGroup):
    body = State()
    link = State()
    when = State()


class Rule(StatesGroup):
    keyword = State()
    response = State()


def ap_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Запланировать пост", callback_data="ap:new")],
        [InlineKeyboardButton(text="📋 Очередь", callback_data="ap:queue"),
         InlineKeyboardButton(text="🤖 Автоответы", callback_data="ap:rules")],
        [InlineKeyboardButton(text="✨ Автопилот", callback_data="ac:menu")],
        [InlineKeyboardButton(text="🏠 Главная", callback_data="home")],
    ])


async def _uid_and_acc(tg_id: int):
    async with Session() as s:
        service = ThreadsAccountService(s)
        user_id = await service.user_id_for_telegram(tg_id)
        if user_id is None:
            return None, None
        account = await service.selected_account(user_id)
        await s.commit()
    return user_id, account.id if account else None


@router.callback_query(F.data == "ap:menu")
async def cb_menu(cb: CallbackQuery):
    await cb.message.answer("Автопилот:", reply_markup=ap_kb())
    await cb.answer()


# ---------- планирование поста ----------

@router.callback_query(F.data == "ap:new")
async def cb_new(cb: CallbackQuery, state: FSMContext):
    uid, acc = await _uid_and_acc(cb.from_user.id)
    if not acc:
        await cb.message.answer("Сначала подключите Threads-аккаунт.")
        await cb.answer()
        return
    async with Session() as session:
        timezone_name = (await session.execute(text("""
            SELECT timezone FROM autocontent_settings
            WHERE user_id = :uid AND threads_account_id = :account_id
        """), {"uid": uid, "account_id": acc})).scalar_one_or_none()
    await state.set_state(Schedule.body)
    await state.update_data(
        account_id=acc,
        timezone=timezone_name or "Europe/Moscow",
    )
    await cb.message.answer(
        "Введите текст поста до 500 символов.\nДля отмены: /cancel"
    )
    await cb.answer()


@router.message(Schedule.body, Command("cancel"))
@router.message(Schedule.link, Command("cancel"))
@router.message(Schedule.when, Command("cancel"))
async def cancel_schedule(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Планирование отменено. Пост не добавлен в очередь.")


@router.message(Schedule.body)
async def sch_body(msg: Message, state: FSMContext):
    body = (msg.text or "").strip()
    if len(body) > 500:
        await msg.answer(
            f"Длина: {len(body)} символов. Лимит Threads: 500. "
            "Сократите текст."
        )
        return
    await state.update_data(body=body)
    await state.set_state(Schedule.link)
    await msg.answer(
        "Отправьте ссылку: она будет опубликована первым комментарием.\n"
        "Если ссылки нет, отправьте «-». Для отмены: /cancel"
    )


@router.message(Schedule.link)
async def sch_link(msg: Message, state: FSMContext):
    raw = (msg.text or "").strip()
    link = None if raw == "-" else raw
    if link and not link.startswith("http"):
        await msg.answer("Ссылка должна начинаться с http. Или «-» если без неё.")
        return
    await state.update_data(link=link)
    await state.set_state(Schedule.when)
    data = await state.get_data()
    await msg.answer(
        "Когда опубликовать? Формат: ДД.ММ ЧЧ:ММ.\n"
        f"Часовой пояс: {data.get('timezone', 'Europe/Moscow')}.\n"
        "Также можно написать «сейчас». Для отмены: /cancel"
    )


@router.message(Schedule.when)
async def sch_when(msg: Message, state: FSMContext):
    raw = (msg.text or "").strip().lower()
    state_data = await state.get_data()
    timezone_name = state_data.get("timezone") or "Europe/Moscow"
    local_timezone = resolve_timezone(timezone_name)
    now = datetime.now(local_timezone)
    if raw in ("сейчас", "now"):
        run_at = now
    else:
        try:
            dt = datetime.strptime(raw, "%d.%m %H:%M").replace(
                year=now.year,
                tzinfo=local_timezone,
            )
            if dt < now - timedelta(minutes=5):
                dt = dt.replace(year=now.year + 1)  # 05.01 в декабре = январь следующего
            run_at = dt
        except ValueError:
            await msg.answer("Не понял. Формат: 15.07 09:30. Или «сейчас»")
            return

    data = state_data
    await state.clear()
    uid, selected_account_id = await _uid_and_acc(msg.from_user.id)
    acc = data.get("account_id")
    if selected_account_id != acc:
        await msg.answer(
            "Активный аккаунт изменился. Откройте планирование заново."
        )
        return
    async with Session() as ownership_session:
        owned = await ThreadsAccountService(
            ownership_session
        ).get_owned(uid, acc)
    if owned is None or owned.connection_status != "connected":
        await msg.answer("Threads-аккаунт не подключён.")
        return

    async with Session() as s:
        await s.execute(text("""
            INSERT INTO scheduled_posts (
              user_id, threads_account_id, text, link, run_at,
              content_metadata
            )
            VALUES (
              :uid, :acc, :body, :link, :run,
              '{"source":"manual"}'::jsonb
            )
        """), {"uid": uid, "acc": acc, "body": data["body"],
               "link": data.get("link"), "run": run_at})
        await s.commit()

    when_str = "прямо сейчас (в ближайшую минуту)" if raw in ("сейчас", "now") \
        else run_at.strftime("%d.%m %H:%M") + f" ({timezone_name})"
    await msg.answer(f"В очереди. Публикация: {when_str}", reply_markup=ap_kb())


# ---------- очередь ----------

@router.callback_query(F.data == "ap:queue")
@router.callback_query(F.data.startswith("ap:queue:"))
async def cb_queue(cb: CallbackQuery):
    uid, latest_account_id = await _uid_and_acc(cb.from_user.id)
    try:
        account_id = int(cb.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        account_id = latest_account_id
    if uid is None or account_id is None:
        await cb.answer("Threads-аккаунт не найден", show_alert=True)
        return
    async with Session() as s:
        service = AutopostStatusService(s)
        summary = await service.queue_summary(uid, account_id)
        accounts = await service.list_accounts(uid)
        if summary is None:
            await cb.answer("Аккаунт не найден", show_alert=True)
            return
        rows = (await s.execute(text("""
            SELECT
              post.id, post.text, post.run_at, post.status,
              (
                post.content_metadata ->> 'source' = 'autocontent'
                OR EXISTS (
                  SELECT 1 FROM autopost_runs run
                  WHERE run.scheduled_post_id = post.id
                )
              ) AS is_auto
            FROM scheduled_posts post
            WHERE post.user_id = :uid
              AND post.threads_account_id = :account_id
              AND post.status IN ('pending', 'publishing')
            ORDER BY post.run_at, post.id
            LIMIT 10
        """), {"uid": uid, "account_id": account_id})).all()
    kb = []
    if len(accounts) > 1:
        for account in accounts:
            label = f"@{account.username or account.id}"
            if account.id == account_id:
                label = "✅ " + label
            kb.append([InlineKeyboardButton(
                text=label,
                callback_data=f"ap:queue:{account.id}",
            )])
    kb.extend([
        [InlineKeyboardButton(
            text="🔄 Перестроить очередь",
            callback_data=f"ap:q_rebuild:{account_id}",
        )],
        [InlineKeyboardButton(
            text="🗑 Очистить очередь",
            callback_data=f"ap:q_clear:{account_id}",
        )],
    ])
    tz = resolve_timezone(summary.settings.timezone)
    for pid, body, run_at, status, is_auto in rows:
        preview = " ".join(body.split())[:30]
        when = run_at.astimezone(tz).strftime("%d.%m %H:%M")
        source = "Авто" if is_auto else "Вручную"
        kb.append([InlineKeyboardButton(
            text=f"{source} · {when} · {preview}",
            callback_data=f"ap:view:{pid}:{account_id}",
        )])
    kb.extend([
        [InlineKeyboardButton(
            text="🔄 Обновить",
            callback_data=f"ap:queue:{account_id}",
        )],
        [InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=f"ac:menu:{account_id}",
        )],
    ])
    await cb.message.answer(
        render_queue_summary(summary),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("ap:q_rebuild:"))
async def cb_queue_rebuild(cb: CallbackQuery):
    uid, _ = await _uid_and_acc(cb.from_user.id)
    account_id = int(cb.data.rsplit(":", 1)[1])
    await cb.answer("Перестраиваю очередь...")
    async with Session() as session:
        service = AutopostStatusService(session)
        try:
            result = await service.rebuild_queue(uid, account_id)
            status = await service.get_status(uid, account_id)
            await session.commit()
        except Exception as error:
            await session.rollback()
            log.error(
                "queue rebuild failed uid=%s account=%s error_type=%s",
                uid,
                account_id,
                type(error).__name__,
            )
            await cb.message.answer("Не удалось перестроить очередь.")
            return
    await cb.message.answer(
        render_rebuild_result(result, status.settings.timezone),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="📋 К очереди",
                callback_data=f"ap:queue:{account_id}",
            )
        ]]),
    )


@router.callback_query(F.data.startswith("ap:q_clear:"))
async def cb_queue_clear_confirm(cb: CallbackQuery):
    account_id = int(cb.data.rsplit(":", 1)[1])
    uid, _ = await _uid_and_acc(cb.from_user.id)
    async with Session() as session:
        summary = await AutopostStatusService(session).queue_summary(
            uid, account_id
        )
    if summary is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    await cb.message.answer(
        render_clear_confirmation(summary),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🗑 Очистить и оставить включённым",
                callback_data=f"ap:q_clear_keep:{account_id}",
            )],
            [InlineKeyboardButton(
                text="⏸ Очистить и выключить Автопилот",
                callback_data=f"ap:q_clear_disable:{account_id}",
            )],
            [InlineKeyboardButton(
                text="⬅️ Отмена",
                callback_data=f"ap:queue:{account_id}",
            )],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("ap:q_clear_keep:"))
@router.callback_query(F.data.startswith("ap:q_clear_disable:"))
async def cb_queue_clear(cb: CallbackQuery):
    uid, _ = await _uid_and_acc(cb.from_user.id)
    account_id = int(cb.data.rsplit(":", 1)[1])
    disable = cb.data.startswith("ap:q_clear_disable:")
    await cb.answer("Очищаю очередь...")
    async with Session() as session:
        try:
            result = await AutopostStatusService(session).clear_queue(
                uid,
                account_id,
                disable_autoposting=disable,
            )
            await session.commit()
        except Exception as error:
            await session.rollback()
            log.error(
                "queue clear failed uid=%s account=%s error_type=%s",
                uid,
                account_id,
                type(error).__name__,
            )
            await cb.message.answer("Не удалось очистить очередь.")
            return
    await cb.message.answer(
        render_clear_result(result),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="📋 К очереди",
                callback_data=f"ap:queue:{account_id}",
            )
        ]]),
    )


@router.callback_query(F.data.startswith("ap:del:"))
async def cb_del(cb: CallbackQuery):
    parts = cb.data.split(":")
    pid = int(parts[2])
    callback_account_id = int(parts[3]) if len(parts) > 3 else None
    uid, latest_account_id = await _uid_and_acc(cb.from_user.id)
    account_id = callback_account_id or latest_account_id
    async with Session() as s:
        service = AutopostStatusService(s)
        await service.lock_queue(uid, account_id)
        removable = (await s.execute(text("""
            SELECT id
            FROM scheduled_posts
            WHERE id = :pid
              AND user_id = :uid
              AND threads_account_id = :account_id
              AND status = 'pending'
            FOR UPDATE SKIP LOCKED
        """), {
            "pid": pid,
            "uid": uid,
            "account_id": account_id,
        })).first()
        if removable:
            await s.execute(text("""
                UPDATE autopost_runs
                SET status = 'skipped',
                    finished_at = now(),
                    error_code = NULL,
                    safe_error_message = 'Пропущено: пост снят из очереди'
                WHERE scheduled_post_id = :pid
                  AND status = 'pending'
            """), {"pid": pid})
        row = (await s.execute(text("""
            DELETE FROM scheduled_posts
            WHERE id = :pid
              AND user_id = :uid
              AND threads_account_id = :account_id
              AND status = 'pending'
              AND :removable
            RETURNING id
        """), {
            "pid": pid,
            "uid": uid,
            "account_id": account_id,
            "removable": bool(removable),
        })).first()
        await s.commit()
    await cb.answer(
        "Пост снят с очереди" if row else "Пост уже публикуется",
        show_alert=not row,
    )


# ---------- правила автоответов ----------

@router.callback_query(F.data == "ap:rules")
async def cb_rules(cb: CallbackQuery):
    uid, _ = await _uid_and_acc(cb.from_user.id)
    async with Session() as s:
        rules = (await s.execute(text("""
            SELECT id, keyword, active FROM reply_rules WHERE user_id = :uid
            ORDER BY id
        """), {"uid": uid})).all()
    kb = [[InlineKeyboardButton(text="➕ Новое правило", callback_data="ap:rule_new")]]
    for rid, kw, active in rules:
        mark = "🟢" if active else "⚪"
        kb.append([
            InlineKeyboardButton(text=f"{mark} «{kw}»", callback_data=f"ap:rule_t:{rid}"),
            InlineKeyboardButton(text="🗑", callback_data=f"ap:rule_d:{rid}"),
        ])
    await cb.message.answer(
        "Автоответы: слово в комменте -> ответ в ветке.\n"
        "(в личку Threads API не умеет - DM у них нет)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
    )
    await cb.answer()


@router.callback_query(F.data == "ap:rule_new")
async def cb_rule_new(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Rule.keyword)
    await cb.message.answer("Кодовое слово (например: ГАЙД):")
    await cb.answer()


@router.message(Rule.keyword)
async def rule_kw(msg: Message, state: FSMContext):
    await state.update_data(keyword=(msg.text or "").strip())
    await state.set_state(Rule.response)
    await msg.answer("Текст ответа (можно со ссылкой):")


@router.message(Rule.response)
async def rule_resp(msg: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    uid, _ = await _uid_and_acc(msg.from_user.id)
    async with Session() as s:
        await s.execute(text("""
            INSERT INTO reply_rules (user_id, keyword, response_text)
            VALUES (:uid, :kw, :resp)
        """), {"uid": uid, "kw": data["keyword"], "resp": msg.text or ""})
        await s.commit()
    await msg.answer(
        f"Правило живое: «{data['keyword']}» в комменте -> автоответ в ветке.",
        reply_markup=ap_kb(),
    )


@router.callback_query(F.data.startswith("ap:rule_t:"))
async def cb_rule_toggle(cb: CallbackQuery):
    rid = int(cb.data.rsplit(":", 1)[1])
    uid, _ = await _uid_and_acc(cb.from_user.id)
    async with Session() as s:
        await s.execute(text("""
            UPDATE reply_rules SET active = NOT active
            WHERE id = :rid AND user_id = :uid
        """), {"rid": rid, "uid": uid})
        await s.commit()
    await cb.answer("Переключил")


@router.callback_query(F.data.startswith("ap:rule_d:"))
async def cb_rule_del(cb: CallbackQuery):
    rid = int(cb.data.rsplit(":", 1)[1])
    uid, _ = await _uid_and_acc(cb.from_user.id)
    async with Session() as s:
        await s.execute(text(
            "DELETE FROM reply_rules WHERE id = :rid AND user_id = :uid"
        ), {"rid": rid, "uid": uid})
        await s.commit()
    await cb.answer("Удалил")


@router.callback_query(F.data.startswith("ap:view:"))
async def cb_view(cb: CallbackQuery):
    parts = cb.data.split(":")
    pid = int(parts[2])
    callback_account_id = int(parts[3]) if len(parts) > 3 else None
    uid, latest_account_id = await _uid_and_acc(cb.from_user.id)
    account_id = callback_account_id or latest_account_id
    async with Session() as s:
        row = (await s.execute(text("""
            SELECT post.text, post.run_at, post.status,
                   coalesce(ac.timezone, 'Europe/Moscow')
            FROM scheduled_posts post
            LEFT JOIN autocontent_settings ac
              ON ac.user_id = post.user_id
             AND ac.threads_account_id = post.threads_account_id
            WHERE post.id = :pid
              AND post.user_id = :uid
              AND post.threads_account_id = :account_id
        """), {
            "pid": pid,
            "uid": uid,
            "account_id": account_id,
        })).first()
    if not row:
        await cb.answer("Пост не найден", show_alert=True)
        return
    body, run_at, status, timezone_name = row
    when = run_at.astimezone(
        resolve_timezone(timezone_name)
    ).strftime("%d.%m %H:%M")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Опубликовать сейчас", callback_data=f"ap:now:{pid}")],
        [InlineKeyboardButton(
            text="❌ Снять",
            callback_data=f"ap:del:{pid}:{account_id}",
        )],
        [InlineKeyboardButton(
            text="📋 К очереди",
            callback_data=f"ap:queue:{account_id}",
        )],
    ])
    status_label = {
        "pending": "Ожидает",
        "publishing": "Публикуется",
        "done": "Опубликован",
        "failed": "Ошибка",
    }.get(status, "Статус обновлён")
    head = (
        "Пост на " + when + " · " + status_label + " · "
        + str(len(body)) + " симв."
    )
    await cb.message.answer(head + chr(10) + chr(10) + body, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("ap:now:"))
async def cb_now(cb: CallbackQuery):
    pid = int(cb.data.rsplit(":", 1)[1])
    uid, _ = await _uid_and_acc(cb.from_user.id)
    async with Session() as s:
        row = (await s.execute(text(
            "UPDATE scheduled_posts SET run_at = now() - interval '2 minutes' "
            "WHERE id = :pid AND user_id = :uid AND status = 'pending' RETURNING id"),
            {"pid": pid, "uid": uid})).first()
        await s.commit()
    await cb.answer("Уйдёт в ближайшую минуту 🚀" if row else "Уже публикуется",
                    show_alert=True)
