"""Account-scoped UX V3 settings screens and direct-input FSM flows."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.ux import (
    escape_html,
    escape_truncated,
    navigation_row,
    show_ui_screen,
)
from app.core.accounts import ThreadsAccountService
from app.core.db import Session
from app.core.ux import UXService, normalize_text_list

router = Router()


def _last_int(value: str) -> int | None:
    try:
        return int(value.rsplit(":", 1)[-1])
    except (AttributeError, TypeError, ValueError):
        return None


class StyleInput(StatesGroup):
    value = State()
    confirm = State()
    examples = State()


class TopicInput(StatesGroup):
    value = State()


class KeywordInput(StatesGroup):
    value = State()


async def _selected(telegram_id: int):
    async with Session() as session:
        accounts = ThreadsAccountService(session)
        user_id = await accounts.user_id_for_telegram(telegram_id)
        account = (
            await accounts.selected_account(user_id)
            if user_id is not None else None
        )
        if account is not None:
            await accounts.ensure_settings(user_id, account.id)
        await session.commit()
    return user_id, account


async def _settings(telegram_id: int):
    user_id, account = await _selected(telegram_id)
    if account is None:
        return user_id, None, None
    async with Session() as session:
        value = await UXService(session).account_settings(user_id, account.id)
        await session.commit()
    return user_id, account, value


def _style_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить стиль", callback_data="ux:style:edit")],
        [InlineKeyboardButton(text="📝 Добавить примеры", callback_data="ux:examples:add")],
        [InlineKeyboardButton(text="👀 Посмотреть примеры", callback_data="ux:examples:view")],
        [InlineKeyboardButton(text="🗑 Очистить примеры", callback_data="ux:examples:clear")],
        navigation_row("ux:settings"),
    ])


async def _show_style(target: Message, telegram_id: int) -> None:
    _, account, settings = await _settings(telegram_id)
    if account is None or settings is None:
        await show_ui_screen(
            target,
            "🎙 <b>Стиль общения</b>\n\nСначала подключите Threads-аккаунт.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[navigation_row("home")]),
        )
        return
    style, style_shortened = escape_truncated(
        settings.manual_style or "Пока не описан",
        2500,
    )
    style_note = (
        "\nСтиль сокращён только на этом экране."
        if style_shortened else ""
    )
    await show_ui_screen(
        target,
        "🎙 <b>Стиль общения</b>\n\n"
        "Здесь можно объяснить Автопилоту, как должен звучать ваш аккаунт.\n\n"
        f"<b>Аккаунт: @{escape_html(settings.username)}</b>\n\n"
        f"Текущий стиль:\n<b>{style}</b>{style_note}\n\n"
        f"Примеров сохранено: <b>{len(settings.style_examples)}</b>",
        reply_markup=_style_keyboard(),
    )


async def _remember_prompt(state: FSMContext, prompt: Message) -> None:
    await state.update_data(
        transient_chat_id=prompt.chat.id,
        transient_message_id=prompt.message_id,
    )


async def _cleanup_prompt(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    chat_id = data.get("transient_chat_id")
    message_id = data.get("transient_message_id")
    if chat_id is not None and message_id is not None:
        try:
            await message.bot.delete_message(chat_id, message_id)
        except Exception:
            pass


async def _account_still_selected(
    message: Message,
    state: FSMContext,
) -> tuple[int | None, object | None, dict]:
    data = await state.get_data()
    user_id, account = await _selected(message.from_user.id)
    if account is None or account.id != data.get("account_id"):
        await _cleanup_prompt(message, state)
        await state.clear()
        await message.answer(
            "Активный аккаунт изменился. Откройте настройку заново."
        )
        return user_id, None, data
    return user_id, account, data


@router.callback_query(F.data == "ux:style")
async def cb_style(cb: CallbackQuery):
    await _show_style(cb.message, cb.from_user.id)
    await cb.answer()


@router.callback_query(F.data == "ux:style:edit")
async def cb_style_edit(cb: CallbackQuery, state: FSMContext):
    _, account = await _selected(cb.from_user.id)
    if account is None:
        await cb.answer("Подключите Threads-аккаунт", show_alert=True)
        return
    await _cleanup_prompt(cb.message, state)
    await state.set_state(StyleInput.value)
    await state.set_data({"account_id": account.id})
    prompt = await cb.message.answer(
        "Опишите своими словами, как должен писать ваш аккаунт.\n\n"
        "Например: Пиши просто и разговорно. Без канцелярита. "
        "Короткие абзацы. Не используй длинное тире.\n\n"
        "Для отмены: /cancel"
    )
    await _remember_prompt(state, prompt)
    await cb.answer()


@router.message(StyleInput.value, Command("cancel"))
@router.message(StyleInput.confirm, Command("cancel"))
@router.message(StyleInput.examples, Command("cancel"))
@router.message(TopicInput.value, Command("cancel"))
@router.message(KeywordInput.value, Command("cancel"))
async def cancel_settings_input(message: Message, state: FSMContext):
    current = await state.get_state()
    await _cleanup_prompt(message, state)
    await state.clear()
    if current in {
        StyleInput.value.state,
        StyleInput.confirm.state,
        StyleInput.examples.state,
    }:
        await _show_style(message, message.from_user.id)
    elif current == TopicInput.value.state:
        if not await _show_topics(message, message.from_user.id):
            await message.answer("Аккаунт больше недоступен.")
    elif current == KeywordInput.value.state:
        if not await _show_keywords(message, message.from_user.id):
            await message.answer("Аккаунт больше недоступен.")
    else:
        await message.answer("Ввод отменён. Настройки не изменены.")


@router.message(StyleInput.value)
async def style_value(message: Message, state: FSMContext):
    _, account, _ = await _account_still_selected(message, state)
    if account is None:
        return
    value = (message.text or "").strip()
    if not 10 <= len(value) <= 1500:
        await message.answer("Опишите стиль текстом длиной от 10 до 1500 символов.")
        return
    await state.set_state(StyleInput.confirm)
    await state.update_data(draft=value)
    preview, shortened = escape_truncated(value, 2500)
    await show_ui_screen(
        message,
        "🎙 <b>Предпросмотр стиля</b>\n\n"
        f"<b>{preview}</b>"
        + ("\n\nТекст сокращён только в предпросмотре." if shortened else "")
        + "\n\nСохранить этот стиль?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Сохранить", callback_data="ux:style:save")],
            [InlineKeyboardButton(text="✏️ Изменить", callback_data="ux:style:edit")],
            [InlineKeyboardButton(text="Отменить", callback_data="ux:style:cancel")],
        ]),
    )


@router.callback_query(F.data == "ux:style:save")
async def cb_style_save(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_id, account = await _selected(cb.from_user.id)
    if account is None or account.id != data.get("account_id"):
        await state.clear()
        await cb.answer("Активный аккаунт изменился", show_alert=True)
        return
    async with Session() as session:
        saved = await UXService(session).save_manual_style(
            user_id, account.id, str(data.get("draft") or "")
        )
        if saved:
            await session.commit()
        else:
            await session.rollback()
    await _cleanup_prompt(cb.message, state)
    await state.clear()
    if not saved:
        await cb.answer("Стиль не сохранён", show_alert=True)
        return
    await _show_style(cb.message, cb.from_user.id)
    await cb.answer("Сохранено")


@router.callback_query(F.data == "ux:style:cancel")
async def cb_style_cancel(cb: CallbackQuery, state: FSMContext):
    await _cleanup_prompt(cb.message, state)
    await state.clear()
    await _show_style(cb.message, cb.from_user.id)
    await cb.answer("Отменено")


@router.callback_query(F.data == "ux:examples:add")
async def cb_examples_add(cb: CallbackQuery, state: FSMContext):
    _, account, settings = await _settings(cb.from_user.id)
    if account is None or settings is None:
        await cb.answer("Подключите Threads-аккаунт", show_alert=True)
        return
    await state.set_state(StyleInput.examples)
    await state.set_data({
        "account_id": account.id,
        "examples": list(settings.style_examples),
    })
    prompt = await cb.message.answer(
        "Пришлите 1-10 постов, стиль которых вам нравится, каждый отдельным "
        "сообщением. Автопилот будет учитывать их как примеры подачи и не "
        "будет копировать дословно.\n\nКогда закончите: /done. Отмена: /cancel."
    )
    await _remember_prompt(state, prompt)
    await cb.answer()


@router.message(StyleInput.examples, Command("done"))
async def examples_done(message: Message, state: FSMContext):
    user_id, account, data = await _account_still_selected(message, state)
    if account is None:
        return
    examples = data.get("examples") or []
    if not examples:
        await message.answer("Сначала пришлите хотя бы один пример.")
        return
    async with Session() as session:
        saved = await UXService(session).save_style_examples(
            user_id, account.id, examples
        )
        if saved:
            await session.commit()
        else:
            await session.rollback()
    if not saved:
        await message.answer(
            "Не удалось сохранить примеры. Откройте настройку заново."
        )
        return
    await _cleanup_prompt(message, state)
    await state.clear()
    await _show_style(message, message.from_user.id)


@router.message(StyleInput.examples)
async def example_value(message: Message, state: FSMContext):
    _, account, data = await _account_still_selected(message, state)
    if account is None:
        return
    examples = list(data.get("examples") or [])
    value = (message.text or "").strip()
    if not value:
        await message.answer("Пришлите текстовый пример.")
        return
    if len(examples) >= 10:
        await message.answer("Сохранено 10 примеров. Завершите ввод командой /done.")
        return
    examples.append(value[:1000])
    await state.update_data(examples=examples)
    await message.answer(f"Принято: {len(examples)} из 10.")


@router.callback_query(F.data == "ux:examples:view")
async def cb_examples_view(cb: CallbackQuery):
    _, _, settings = await _settings(cb.from_user.id)
    if settings is None:
        await cb.answer("Подключите Threads-аккаунт", show_alert=True)
        return
    if not settings.style_examples:
        text_value = (
            "📝 <b>Примеров пока нет</b>\n\n"
            "Добавьте несколько своих удачных постов или примеров подачи."
        )
    else:
        lines = ["📝 <b>Примеры стиля</b>"]
        for index, item in enumerate(settings.style_examples, 1):
            preview, shortened = escape_truncated(item, 260)
            lines.extend([
                "",
                f"<b>{index}.</b> {preview}{'…' if shortened else ''}",
            ])
        text_value = "\n".join(lines)
    await show_ui_screen(
        cb.message,
        text_value,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[navigation_row("ux:style")]),
    )
    await cb.answer()


@router.callback_query(F.data == "ux:examples:clear")
async def cb_examples_clear(cb: CallbackQuery):
    _, account = await _selected(cb.from_user.id)
    if account is None:
        await cb.answer("Аккаунт не выбран", show_alert=True)
        return
    await show_ui_screen(
        cb.message,
        "⚠️ <b>Очистить примеры?</b>\n\n"
        f"Аккаунт: <b>@{escape_html(account.username or account.id)}</b>\n\n"
        "Стиль и остальные настройки сохранятся.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🗑 Очистить",
                callback_data=f"ux:examples:clear_confirm:{account.id}",
            )],
            [InlineKeyboardButton(text="Отменить", callback_data="ux:style")],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("ux:examples:clear_confirm:"))
async def cb_examples_clear_confirm(cb: CallbackQuery):
    expected_id = _last_int(cb.data)
    user_id, account = await _selected(cb.from_user.id)
    if account is None or account.id != expected_id:
        await cb.answer("Активный аккаунт изменился", show_alert=True)
        return
    async with Session() as session:
        saved = await UXService(session).save_style_examples(
            user_id, account.id, []
        )
        if saved:
            await session.commit()
        else:
            await session.rollback()
    if not saved:
        await cb.answer("Примеры не изменены", show_alert=True)
        return
    await _show_style(cb.message, cb.from_user.id)
    await cb.answer("Примеры очищены")


def _list_screen(title: str, username: str, items: list[str], empty: str) -> str:
    lines = [title, "", f"<b>Аккаунт: @{escape_html(username)}</b>", ""]
    if items:
        for item in items:
            shown, shortened = escape_truncated(item, 180)
            lines.append(f"• {shown}{'…' if shortened else ''}")
    else:
        lines.append(empty)
    return "\n".join(lines)


def _edit_list_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="➕ Добавить", callback_data=f"ux:{prefix}:add"
        )],
        [InlineKeyboardButton(text="✏️ Изменить", callback_data=f"ux:{prefix}:edit")],
        [InlineKeyboardButton(text="🗑 Очистить", callback_data=f"ux:{prefix}:clear")],
        navigation_row("ux:settings"),
    ])


async def _show_topics(target: Message, telegram_id: int) -> bool:
    _, _, settings = await _settings(telegram_id)
    if settings is None:
        return False
    await show_ui_screen(
        target,
        _list_screen(
            "🎯 <b>Темы аккаунта</b>",
            settings.username,
            settings.topics,
            "Темы пока не добавлены. Добавьте направления, о которых "
            "должен писать Автопилот.",
        ),
        reply_markup=_edit_list_keyboard("topics"),
    )
    return True


async def _show_keywords(target: Message, telegram_id: int) -> bool:
    _, _, settings = await _settings(telegram_id)
    if settings is None:
        return False
    await show_ui_screen(
        target,
        _list_screen(
            "🔑 <b>Ключевые слова Radar</b>",
            settings.username,
            settings.radar_keywords,
            "Ключевые слова пока не заданы. Radar не знает, какие "
            "обсуждения искать.",
        ) + (
            "\n\nТемы управляют контентом, а эти слова используются "
            "только для поиска Radar."
        ),
        reply_markup=_edit_list_keyboard("keywords"),
    )
    return True


@router.callback_query(F.data == "ux:topics")
async def cb_topics(cb: CallbackQuery):
    if not await _show_topics(cb.message, cb.from_user.id):
        await cb.answer("Подключите Threads-аккаунт", show_alert=True)
        return
    await cb.answer()


@router.callback_query(F.data.in_({"ux:topics:add", "ux:topics:edit"}))
async def cb_topics_edit(cb: CallbackQuery, state: FSMContext):
    _, account, settings = await _settings(cb.from_user.id)
    if account is None or settings is None:
        await cb.answer("Аккаунт не выбран", show_alert=True)
        return
    append = cb.data.endswith(":add")
    await state.set_state(TopicInput.value)
    await state.set_data({
        "account_id": account.id,
        "append": append,
        "existing": list(settings.topics) if append else [],
    })
    prompt = await cb.message.answer(
        ("Добавьте новые темы" if append else "Введите новый список тем")
        + " через запятую или каждую с новой строки. До 20 тем.\n"
        "Для отмены: /cancel"
    )
    await _remember_prompt(state, prompt)
    await cb.answer()


@router.message(TopicInput.value)
async def topic_value(message: Message, state: FSMContext):
    user_id, account, data = await _account_still_selected(message, state)
    if account is None:
        return
    incoming = normalize_text_list(message.text or "", limit=20)
    if not incoming:
        await message.answer("Укажите хотя бы одну тему.")
        return
    topics = normalize_text_list(
        [*(data.get("existing") or []), *incoming],
        limit=20,
    )
    async with Session() as session:
        saved = await UXService(session).save_topics(
            user_id, account.id, topics
        )
        if saved:
            await session.commit()
        else:
            await session.rollback()
    if not saved:
        await message.answer(
            "Не удалось сохранить темы. Откройте настройку заново."
        )
        return
    await _cleanup_prompt(message, state)
    await state.clear()
    await show_ui_screen(
        message,
        _list_screen("🎯 <b>Темы аккаунта</b>", account.username or str(account.id), topics, ""),
        reply_markup=_edit_list_keyboard("topics"),
    )


@router.callback_query(F.data == "ux:topics:clear")
async def cb_topics_clear(cb: CallbackQuery):
    _, account = await _selected(cb.from_user.id)
    if account is None:
        await cb.answer("Аккаунт не выбран", show_alert=True)
        return
    await show_ui_screen(
        cb.message,
        "⚠️ <b>Очистить темы аккаунта?</b>\n\n"
        f"Аккаунт: <b>@{escape_html(account.username or account.id)}</b>\n\n"
        "Автопилот потеряет ориентиры для новых публикаций.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🗑 Очистить",
                callback_data=f"ux:topics:clear_confirm:{account.id}",
            )],
            [InlineKeyboardButton(text="Отменить", callback_data="ux:topics")],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("ux:topics:clear_confirm:"))
async def cb_topics_clear_confirm(cb: CallbackQuery):
    expected_id = _last_int(cb.data)
    user_id, account = await _selected(cb.from_user.id)
    if account is None or account.id != expected_id:
        await cb.answer("Активный аккаунт изменился", show_alert=True)
        return
    async with Session() as session:
        saved = await UXService(session).save_topics(
            user_id, account.id, []
        )
        if saved:
            await session.commit()
        else:
            await session.rollback()
    if not saved:
        await cb.answer("Темы не изменены", show_alert=True)
        return
    await cb_topics(cb)


@router.callback_query(F.data == "ux:keywords")
async def cb_keywords(cb: CallbackQuery):
    if not await _show_keywords(cb.message, cb.from_user.id):
        await cb.answer("Подключите Threads-аккаунт", show_alert=True)
        return
    await cb.answer()


@router.callback_query(F.data.in_({"ux:keywords:add", "ux:keywords:edit"}))
async def cb_keywords_edit(cb: CallbackQuery, state: FSMContext):
    _, account, settings = await _settings(cb.from_user.id)
    if account is None or settings is None:
        await cb.answer("Аккаунт не выбран", show_alert=True)
        return
    append = cb.data.endswith(":add")
    await state.set_state(KeywordInput.value)
    await state.set_data({
        "account_id": account.id,
        "append": append,
        "existing": list(settings.radar_keywords) if append else [],
    })
    prompt = await cb.message.answer(
        (
            "Добавьте до 10 ключевых слов через запятую.\n"
            if append else
            "Введите новый список: до 10 ключевых слов через запятую.\n"
        )
        + "Например: Threads, продвижение, маркетинг, CPA.\n"
        "Для отмены: /cancel"
    )
    await _remember_prompt(state, prompt)
    await cb.answer()


@router.message(KeywordInput.value)
async def keyword_value(message: Message, state: FSMContext):
    user_id, account, data = await _account_still_selected(message, state)
    if account is None:
        return
    incoming = normalize_text_list(message.text or "", limit=10)
    if not incoming:
        await message.answer("Укажите хотя бы одно ключевое слово.")
        return
    keywords = normalize_text_list(
        [*(data.get("existing") or []), *incoming],
        limit=10,
    )
    async with Session() as session:
        saved = await UXService(session).save_radar_keywords(
            user_id, account.id, keywords
        )
        if saved:
            await session.commit()
        else:
            await session.rollback()
    if not saved:
        await message.answer(
            "Не удалось сохранить ключевые слова. Откройте настройку заново."
        )
        return
    await _cleanup_prompt(message, state)
    await state.clear()
    await show_ui_screen(
        message,
        _list_screen(
            "🔑 <b>Ключевые слова Radar</b>",
            account.username or str(account.id), keywords, "",
        ),
        reply_markup=_edit_list_keyboard("keywords"),
    )


@router.callback_query(F.data == "ux:keywords:clear")
async def cb_keywords_clear(cb: CallbackQuery):
    _, account = await _selected(cb.from_user.id)
    if account is None:
        await cb.answer("Аккаунт не выбран", show_alert=True)
        return
    await show_ui_screen(
        cb.message,
        "⚠️ <b>Очистить ключевые слова?</b>\n\n"
        f"Аккаунт: <b>@{escape_html(account.username or account.id)}</b>\n\n"
        "Radar перестанет искать новые обсуждения до следующей настройки.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🗑 Очистить",
                callback_data=f"ux:keywords:clear_confirm:{account.id}",
            )],
            [InlineKeyboardButton(text="Отменить", callback_data="ux:keywords")],
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("ux:keywords:clear_confirm:"))
async def cb_keywords_clear_confirm(cb: CallbackQuery):
    expected_id = _last_int(cb.data)
    user_id, account = await _selected(cb.from_user.id)
    if account is None or account.id != expected_id:
        await cb.answer("Активный аккаунт изменился", show_alert=True)
        return
    async with Session() as session:
        saved = await UXService(session).save_radar_keywords(
            user_id, account.id, []
        )
        if saved:
            await session.commit()
        else:
            await session.rollback()
    if not saved:
        await cb.answer("Ключевые слова не изменены", show_alert=True)
        return
    await cb_keywords(cb)


@router.callback_query(F.data == "ux:notifications")
async def cb_notifications(cb: CallbackQuery):
    _, _, settings = await _settings(cb.from_user.id)
    if settings is None:
        await cb.answer("Аккаунт не выбран", show_alert=True)
        return
    enabled = settings.publish_notifications_enabled
    await show_ui_screen(
        cb.message,
        "🔔 <b>Уведомления</b>\n\n"
        f"<b>Аккаунт: @{escape_html(settings.username)}</b>\n\n"
        "Уведомлять об опубликованных постах: "
        f"<b>{'Включено' if enabled else 'Выключено'}</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="Выключить" if enabled else "Включить",
                callback_data=(
                    f"ux:notifications:set:{settings.threads_account_id}:"
                    f"{0 if enabled else 1}"
                ),
            )],
            navigation_row("ux:settings"),
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("ux:notifications:set:"))
async def cb_notifications_set(cb: CallbackQuery):
    parts = cb.data.split(":")
    try:
        expected_id = int(parts[-2])
    except (IndexError, ValueError):
        await cb.answer("Настройка устарела. Откройте экран заново.", show_alert=True)
        return
    enabled = parts[-1] == "1"
    user_id, account = await _selected(cb.from_user.id)
    if account is None or account.id != expected_id:
        await cb.answer("Активный аккаунт изменился", show_alert=True)
        return
    async with Session() as session:
        saved = await UXService(session).set_publish_notifications(
            user_id, account.id, enabled
        )
        if saved:
            await session.commit()
        else:
            await session.rollback()
    if not saved:
        await cb.answer("Настройка не изменена", show_alert=True)
        return
    await cb_notifications(cb)
