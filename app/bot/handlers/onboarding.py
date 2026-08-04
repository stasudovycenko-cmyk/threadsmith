"""Resumable account-scoped first-run setup."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.ux import navigation_row
from app.core.accounts import ThreadsAccountService
from app.core.db import Session
from app.core.ux import UXService
from app.schemas.ux import OnboardingProgress

router = Router()
TOTAL_STEPS = 9

GOALS = {
    "reach": "охваты",
    "engagement": "вовлечение",
}
SCHEDULES = {
    "morning": ("09:00,12:00", "Утро: 09:00 и 12:00"),
    "balanced": ("09:00,15:00,19:00", "В течение дня: 09:00, 15:00, 19:00"),
    "evening": ("18:00,21:00", "Вечер: 18:00 и 21:00"),
}
STYLES = {
    "expert": "Экспертно и по делу",
    "friendly": "Дружелюбно и просто",
    "own_voice": "Использовать мой профиль голоса",
}


class OnboardingInput(StatesGroup):
    topic = State()


def _keyboard(rows, account_id: int, step: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=label, callback_data=callback)]
        for label, callback in rows
    ]
    controls = []
    if step > 1:
        controls.append(InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=f"ob:back:{account_id}:{step - 1}",
        ))
    controls.append(InlineKeyboardButton(
        text="Продолжить позже",
        callback_data=f"ob:pause:{account_id}",
    ))
    buttons.append(controls)
    buttons.append([InlineKeyboardButton(
        text="Пропустить настройку",
        callback_data=f"ob:skip:{account_id}",
    )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def render_onboarding_step(
    progress: OnboardingProgress,
    username: str,
) -> tuple[str, InlineKeyboardMarkup]:
    step = max(1, min(TOTAL_STEPS, progress.current_step))
    account_id = progress.threads_account_id
    header = (
        f"Настройка @{username}\n"
        f"Шаг {step} из {TOTAL_STEPS}\n\n"
    )
    if step == 1:
        return (
            header
            + "Тематика аккаунта\n\n"
            "Напишите тему и ключевые направления через запятую.\n"
            "Пример: маркетинг, личный бренд, продажи.\n\n"
            "Для отмены ввода используйте /cancel.",
            _keyboard([], account_id, step),
        )
    if step == 2:
        return (
            header
            + "Главная цель\n\n"
            "Цель помогает выбирать стратегию постов. Для старта рекомендуем охваты.",
            _keyboard([
                ("✅ Больше охватов", f"ob:set:{account_id}:goal:reach"),
                ("Больше реакций", f"ob:set:{account_id}:goal:engagement"),
            ], account_id, step),
        )
    if step == 3:
        return (
            header
            + "Количество постов\n\n"
            "Начните с двух постов в день: этого достаточно для регулярности.",
            _keyboard([
                ("1 пост в день", f"ob:set:{account_id}:daily:1"),
                ("✅ 2 поста в день", f"ob:set:{account_id}:daily:2"),
                ("3 поста в день", f"ob:set:{account_id}:daily:3"),
            ], account_id, step),
        )
    if step == 4:
        return (
            header
            + "Время публикации\n\n"
            "Выберите базовое расписание. Позже его можно изменить точно.",
            _keyboard([
                ("Утро", f"ob:set:{account_id}:schedule:morning"),
                ("✅ В течение дня", f"ob:set:{account_id}:schedule:balanced"),
                ("Вечер", f"ob:set:{account_id}:schedule:evening"),
            ], account_id, step),
        )
    if step == 5:
        return (
            header
            + "Стиль текста\n\n"
            "Выберите ориентир. Обученный профиль голоса всегда имеет приоритет.",
            _keyboard([
                ("Экспертно и по делу", f"ob:set:{account_id}:style:expert"),
                ("Дружелюбно и просто", f"ob:set:{account_id}:style:friendly"),
                ("✅ Мой профиль голоса", f"ob:set:{account_id}:style:own_voice"),
            ], account_id, step),
        )
    if step == 6:
        return (
            header
            + "Автопилот\n\n"
            "Он создаёт и публикует посты по расписанию. Его можно остановить в любой момент.",
            _keyboard([
                ("Включить Автопилот", f"ob:set:{account_id}:autopilot:on"),
                ("✅ Пока оставить выключенным", f"ob:set:{account_id}:autopilot:off"),
            ], account_id, step),
        )
    if step == 7:
        return (
            header
            + "Radar\n\n"
            "Radar ищет публичные обсуждения по вашей теме. Сам он ничего не публикует.",
            _keyboard([
                ("Использовать Radar", f"ob:set:{account_id}:radar:on"),
                ("Настрою позже", f"ob:set:{account_id}:radar:off"),
            ], account_id, step),
        )
    if step == 8:
        return (
            header
            + "Neuro\n\n"
            "Безопасный режим сначала показывает комментарий вам. Автоматическая публикация здесь не включается.",
            _keyboard([
                ("✅ Сначала спрашивать меня", f"ob:set:{account_id}:neuro:approve"),
                ("Пока выключить", f"ob:set:{account_id}:neuro:off"),
            ], account_id, step),
        )
    data = progress.data
    summary = [
        header + "Финальная проверка",
        "",
        f"Тема: {data.get('topic') or 'не задана'}",
        f"Цель: {data.get('goal_label') or 'не задана'}",
        f"Постов в день: {data.get('daily_limit') or 'не задано'}",
        f"Расписание: {data.get('schedule_label') or 'не задано'}",
        f"Стиль: {data.get('style_label') or 'по профилю голоса'}",
        f"Автопилот: {'включён' if data.get('autopilot') else 'выключен'}",
        f"Radar: {'использовать' if data.get('radar') else 'позже'}",
        "Neuro: " + (
            "сначала спрашивать"
            if data.get("neuro") == "approve"
            else "выключен"
        ),
    ]
    return (
        "\n".join(summary),
        InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🚀 Запустить работу",
                callback_data=f"ob:finish:{account_id}",
            )],
            [InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"ob:back:{account_id}:8",
            )],
            [InlineKeyboardButton(
                text="Продолжить позже",
                callback_data=f"ob:pause:{account_id}",
            )],
        ]),
    )


async def _owned_scope(telegram_id: int, account_id: int | None):
    if account_id is None:
        return None, None
    async with Session() as session:
        accounts = ThreadsAccountService(session)
        user_id = await accounts.user_id_for_telegram(telegram_id)
        account = (
            await accounts.get_owned(user_id, account_id)
            if user_id is not None
            else None
        )
    return user_id, account


async def _show(
    target: Message,
    progress: OnboardingProgress,
    username: str,
    state: FSMContext,
) -> None:
    if progress.current_step == 1:
        await state.set_state(OnboardingInput.topic)
        await state.update_data(account_id=progress.threads_account_id)
    else:
        await state.clear()
    text_value, keyboard = render_onboarding_step(progress, username)
    await target.answer(text_value, reply_markup=keyboard)


@router.callback_query(F.data.startswith("ob:start:"))
@router.callback_query(F.data.startswith("ob:resume:"))
async def cb_start(cb: CallbackQuery, state: FSMContext):
    try:
        account_id = int(cb.data.rsplit(":", 1)[-1])
    except (TypeError, ValueError):
        await cb.answer("Некорректный аккаунт", show_alert=True)
        return
    async with Session() as session:
        accounts = ThreadsAccountService(session)
        user_id = await accounts.user_id_for_telegram(cb.from_user.id)
        account = (
            await accounts.select_account(user_id, account_id)
            if user_id is not None
            else None
        )
        if account is None:
            await cb.answer("Аккаунт не найден", show_alert=True)
            return
        await accounts.ensure_settings(user_id, account_id)
        ux = UXService(session)
        progress = await ux.start_onboarding(user_id, account_id)
        await session.commit()
    if progress is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    await _show(cb.message, progress, account.username or str(account.id), state)
    await cb.answer()


@router.message(OnboardingInput.topic, Command("cancel"))
async def cancel_topic(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Ввод отменён. Прогресс сохранён.")


@router.message(OnboardingInput.topic)
async def save_topic(msg: Message, state: FSMContext):
    topic = (msg.text or "").strip()
    if len(topic) < 3:
        await msg.answer(
            "Напишите тему длиной от трёх символов.\n"
            "Пример: маркетинг, личный бренд. Для отмены: /cancel"
        )
        return
    data = await state.get_data()
    account_id = data.get("account_id")
    if not isinstance(account_id, int):
        await state.clear()
        await msg.answer("Сессия настройки завершилась. Откройте мастер заново.")
        return
    user_id, account = await _owned_scope(msg.from_user.id, account_id)
    if account is None:
        await state.clear()
        await msg.answer("Аккаунт изменился. Откройте настройку заново.")
        return
    async with Session() as session:
        ux = UXService(session)
        if not await ux.save_topic(user_id, account_id, topic):
            await session.rollback()
            await msg.answer("Не удалось сохранить тему. Попробуйте ещё раз.")
            return
        progress = await ux.update_onboarding(
            user_id,
            account_id,
            step=2,
            values={"topic": topic[:300]},
        )
        await session.commit()
    await _show(msg, progress, account.username or str(account.id), state)


@router.callback_query(F.data.startswith("ob:set:"))
async def cb_set_value(cb: CallbackQuery, state: FSMContext):
    try:
        _, _, account_raw, key, value = cb.data.split(":", 4)
        account_id = int(account_raw)
    except (TypeError, ValueError):
        await cb.answer("Некорректная команда", show_alert=True)
        return
    user_id, account = await _owned_scope(cb.from_user.id, account_id)
    if account is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    steps = {
        "goal": 3,
        "daily": 4,
        "schedule": 5,
        "style": 6,
        "autopilot": 7,
        "radar": 8,
        "neuro": 9,
    }
    if key not in steps:
        await cb.answer("Неизвестный шаг", show_alert=True)
        return
    values = {}
    async with Session() as session:
        ux = UXService(session)
        ok = True
        if key == "goal" and value in GOALS:
            label = GOALS[value]
            ok = await ux.save_autopilot_settings(
                user_id, account_id, goal=label
            )
            values = {"goal": value, "goal_label": label}
        elif key == "daily" and value in {"1", "2", "3"}:
            ok = await ux.save_autopilot_settings(
                user_id, account_id, posts_per_day=int(value)
            )
            values = {"daily_limit": int(value)}
        elif key == "schedule" and value in SCHEDULES:
            slots, label = SCHEDULES[value]
            ok = await ux.save_autopilot_settings(
                user_id, account_id, slots=slots
            )
            values = {"schedule": value, "schedule_label": label}
        elif key == "style" and value in STYLES:
            ok = await ux.save_style(user_id, account_id, value)
            values = {"style": value, "style_label": STYLES[value]}
        elif key == "autopilot" and value in {"on", "off"}:
            enabled = value == "on"
            ok = await ux.save_autopilot_settings(
                user_id, account_id, active=enabled
            )
            values = {"autopilot": enabled}
        elif key == "radar" and value in {"on", "off"}:
            values = {"radar": value == "on"}
        elif key == "neuro" and value in {"approve", "off"}:
            ok = await ux.save_neuro_mode(
                user_id,
                account_id,
                active=value == "approve",
                mode="approve",
            )
            values = {"neuro": value}
        else:
            await cb.answer("Недопустимое значение", show_alert=True)
            return
        if not ok:
            await session.rollback()
            await cb.answer("Настройки аккаунта не найдены", show_alert=True)
            return
        progress = await ux.update_onboarding(
            user_id,
            account_id,
            step=steps[key],
            values=values,
        )
        if progress is None:
            await session.rollback()
            await cb.answer("Откройте мастер настройки заново", show_alert=True)
            return
        await session.commit()
    await _show(cb.message, progress, account.username or str(account.id), state)
    await cb.answer("Сохранено")


@router.callback_query(F.data.startswith("ob:back:"))
async def cb_back(cb: CallbackQuery, state: FSMContext):
    try:
        _, _, account_raw, step_raw = cb.data.split(":", 3)
        account_id, step = int(account_raw), int(step_raw)
    except (TypeError, ValueError):
        await cb.answer("Некорректная команда", show_alert=True)
        return
    if not 1 <= step <= 9:
        await cb.answer("Некорректный шаг", show_alert=True)
        return
    user_id, account = await _owned_scope(cb.from_user.id, account_id)
    if account is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    async with Session() as session:
        progress = await UXService(session).update_onboarding(
            user_id, account_id, step=step
        )
        if progress is None:
            await session.rollback()
            await cb.answer("Откройте мастер настройки заново", show_alert=True)
            return
        await session.commit()
    await _show(cb.message, progress, account.username or str(account.id), state)
    await cb.answer()


@router.callback_query(F.data.startswith("ob:pause:"))
async def cb_pause(cb: CallbackQuery, state: FSMContext):
    try:
        account_id = int(cb.data.rsplit(":", 1)[-1])
    except (AttributeError, TypeError, ValueError):
        await cb.answer("Некорректная команда", show_alert=True)
        return
    _, account = await _owned_scope(cb.from_user.id, account_id)
    if account is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    await state.clear()
    await cb.message.answer(
        "Прогресс сохранён. Продолжить можно из карточки аккаунта.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            navigation_row("cab:accounts")
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("ob:skip:"))
async def cb_skip(cb: CallbackQuery, state: FSMContext):
    try:
        account_id = int(cb.data.rsplit(":", 1)[-1])
    except (AttributeError, TypeError, ValueError):
        await cb.answer("Некорректная команда", show_alert=True)
        return
    user_id, account = await _owned_scope(cb.from_user.id, account_id)
    if account is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    async with Session() as session:
        progress = await UXService(session).update_onboarding(
            user_id,
            account_id,
            step=0,
            status="skipped",
        )
        if progress is None:
            await session.rollback()
            await cb.answer("Откройте мастер настройки заново", show_alert=True)
            return
        await session.commit()
    await state.clear()
    await cb.message.answer(
        "Настройка пропущена. Существующие параметры не изменены.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Главная", callback_data="home")]
        ]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("ob:finish:"))
async def cb_finish(cb: CallbackQuery, state: FSMContext):
    try:
        account_id = int(cb.data.rsplit(":", 1)[-1])
    except (AttributeError, TypeError, ValueError):
        await cb.answer("Некорректная команда", show_alert=True)
        return
    user_id, account = await _owned_scope(cb.from_user.id, account_id)
    if account is None:
        await cb.answer("Аккаунт не найден", show_alert=True)
        return
    async with Session() as session:
        progress = await UXService(session).update_onboarding(
            user_id,
            account_id,
            step=9,
            status="completed",
        )
        if progress is None:
            await session.rollback()
            await cb.answer("Откройте мастер настройки заново", show_alert=True)
            return
        await session.commit()
    await state.clear()
    await cb.message.answer(
        f"Готово. @{account.username or account.id} настроен.\n\n"
        "Проверьте главный экран и очередь перед первой автоматической публикацией.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Открыть главный экран", callback_data="home")]
        ]),
    )
    await cb.answer("Настройка завершена")
