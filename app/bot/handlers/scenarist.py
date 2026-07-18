"""
Модуль 2 в интерфейсе бота.

Паттерн списания кредитов: списали -> дёрнули LLM -> упало? вернули.
Списывать после генерации нельзя: два параллельных запроса от юзера
с балансом на один - и мы раздали генерацию бесплатно.

"Ещё 3 варианта": input прошлой генерации лежит в generations, по
callback c id перегенерируем. Состояние не держим в памяти - переживает
рестарт бота.
"""
import json
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from sqlalchemy import text

from app.core import credits, scenarist
from app.core.config import CREDIT_COSTS
from app.core.db import Session
from app.core.llm import LLMError

log = logging.getLogger("scenarist_bot")
router = Router()


class Voice(StatesGroup):
    collecting = State()


class Gen(StatesGroup):
    topic = State()


class Rewrite(StatesGroup):
    source = State()


class Thread(StatesGroup):
    topic = State()


def scenarist_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Пост по теме", callback_data="sc:gen"),
         InlineKeyboardButton(text="🔁 Рерайт", callback_data="sc:rw")],
        [InlineKeyboardButton(text="🧵 Ветка", callback_data="sc:thread"),
         InlineKeyboardButton(text="🎙 Мой голос", callback_data="sc:voice")],
    ])


async def _uid(tg_id: int) -> int | None:
    async with Session() as s:
        row = (await s.execute(text(
            "SELECT id FROM users WHERE telegram_id = :tg"
        ), {"tg": tg_id})).first()
    return row[0] if row else None


async def _require_voice(cb: CallbackQuery, uid: int) -> dict | None:
    async with Session() as s:
        profile = await scenarist.get_voice(s, uid)
    if not profile:
        await cb.message.answer(
            "Сначала обучи бота своему голосу - жми «🎙 Мой голос» "
            "и кидай 5-10 своих постов."
        )
        await cb.answer()
        return None
    return profile


def _render(gen: dict) -> str:
    lines = ["Варианты первой строки:\n"]
    for i, h in enumerate(gen["hooks"], 1):
        lines.append(f"{i}. [{h['type']}] {h['text']}\n")
    lines.append(f"\nТело поста:\n\n{gen['body']}")
    return "".join(lines)


def _more_kb(gen_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Ещё 3 варианта", callback_data=f"sc:more:{gen_id}")
    ]])


@router.message(Command("scenarist"))
async def cmd_scenarist(msg: Message):
    await msg.answer("Сценарист. Что делаем:", reply_markup=scenarist_kb())


@router.callback_query(F.data == "sc:menu")
async def cb_menu(cb: CallbackQuery):
    await cb.message.answer("Сценарист. Что делаем:", reply_markup=scenarist_kb())
    await cb.answer()


# ---------- онбординг голоса ----------

@router.callback_query(F.data == "sc:voice")
async def cb_voice(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Voice.collecting)
    await state.update_data(posts=[])
    await cb.message.answer(
        "Кидай свои посты, каждый отдельным сообщением, 5-10 штук.\n"
        "Чем разнообразнее, тем точнее профиль. Когда всё - жми /done"
    )
    await cb.answer()


@router.message(Voice.collecting, Command("done"))
async def voice_done(msg: Message, state: FSMContext):
    data = await state.get_data()
    posts = data.get("posts", [])
    if len(posts) < 3:
        await msg.answer(f"Мало ({len(posts)}). Минимум 3, лучше 5-10.")
        return
    await state.clear()

    uid = await _uid(msg.from_user.id)
    cost = CREDIT_COSTS["voice_onboarding"]
    async with Session() as s:
        try:
            await credits.spend(s, uid, cost, "voice_onboarding")
            await s.commit()
        except credits.NotEnoughCredits:
            await msg.answer("Не хватает кредитов. /start -> Тарифы")
            return

    await msg.answer("Разбираю голос, полминуты...")
    try:
        async with Session() as s:
            profile = await scenarist.build_voice_profile(s, uid, posts)
            await s.commit()
    except LLMError:
        async with Session() as s:
            await credits.topup(s, uid, cost, "refund_voice_fail")
            await s.commit()
        await msg.answer("Генерация упала, кредиты вернул. Попробуй ещё раз.")
        return

    taboo = ", ".join(profile.get("taboo", [])[:4])
    await msg.answer(
        f"✅ Голос сохранён.\n\nМанера: {profile.get('tone', '-')}\n"
        f"Табу: {taboo}\n\nТеперь генерация пишет как ты.",
        reply_markup=scenarist_kb(),
    )


@router.message(Voice.collecting)
async def voice_collect(msg: Message, state: FSMContext):
    data = await state.get_data()
    posts = data.get("posts", [])
    posts.append(msg.text or "")
    await state.update_data(posts=posts)
    await msg.answer(f"Принял ({len(posts)}). Ещё или /done")


# ---------- генерация поста ----------

@router.callback_query(F.data == "sc:gen")
async def cb_gen(cb: CallbackQuery, state: FSMContext):
    uid = await _uid(cb.from_user.id)
    if not await _require_voice(cb, uid):
        return
    await state.set_state(Gen.topic)
    await cb.message.answer(
        "Тема поста? Можно добавить пост-референс после темы с новой строки - "
        "украду механику."
    )
    await cb.answer()


@router.message(Gen.topic)
async def gen_topic(msg: Message, state: FSMContext):
    await state.clear()
    parts = (msg.text or "").split("\n", 1)
    topic = parts[0].strip()
    reference = parts[1].strip() if len(parts) > 1 else None
    await _run_generation(
        msg, gen_type="generate_post",
        inp={"topic": topic, "reference": reference},
    )


# ---------- рерайт ----------

@router.callback_query(F.data == "sc:rw")
async def cb_rw(cb: CallbackQuery, state: FSMContext):
    uid = await _uid(cb.from_user.id)
    if not await _require_voice(cb, uid):
        return
    await state.set_state(Rewrite.source)
    await cb.message.answer("Кидай пост - чужой или свой старый. Перепишу твоим голосом.")
    await cb.answer()


@router.message(Rewrite.source)
async def rw_source(msg: Message, state: FSMContext):
    await state.clear()
    await _run_generation(
        msg, gen_type="rewrite", inp={"source": msg.text or ""},
    )


# ---------- ветка ----------

@router.callback_query(F.data == "sc:thread")
async def cb_thread(cb: CallbackQuery, state: FSMContext):
    uid = await _uid(cb.from_user.id)
    if not await _require_voice(cb, uid):
        return
    await state.set_state(Thread.topic)
    await cb.message.answer("Тема ветки?")
    await cb.answer()


@router.message(Thread.topic)
async def thread_topic(msg: Message, state: FSMContext):
    await state.clear()
    await _run_generation(
        msg, gen_type="generate_thread", inp={"topic": msg.text or ""},
    )


# ---------- ещё 3 варианта ----------

@router.callback_query(F.data.startswith("sc:more:"))
async def cb_more(cb: CallbackQuery):
    gen_id = int(cb.data.rsplit(":", 1)[1])
    uid = await _uid(cb.from_user.id)
    async with Session() as s:
        row = (await s.execute(text("""
            SELECT type, input FROM generations
            WHERE id = :id AND user_id = :uid
        """), {"id": gen_id, "uid": uid})).first()
    if not row:
        await cb.answer("Не нашёл исходник", show_alert=True)
        return
    gen_type, inp = row[0], row[1]
    if isinstance(inp, str):
        inp = json.loads(inp)
    await cb.answer("Генерю ещё...")
    await _run_generation(cb.message, gen_type=gen_type, inp=inp,
                          override_user_tg=cb.from_user.id)


# ---------- общий раннер ----------

async def _run_generation(msg: Message, gen_type: str, inp: dict,
                          override_user_tg: int | None = None):
    tg_id = override_user_tg or msg.from_user.id
    uid = await _uid(tg_id)
    cost = CREDIT_COSTS[gen_type]

    async with Session() as s:
        profile = await scenarist.get_voice(s, uid)
        try:
            await credits.spend(s, uid, cost, gen_type)
            await s.commit()
        except credits.NotEnoughCredits:
            await msg.answer("Не хватает кредитов. /start -> Тарифы")
            return

    await msg.answer("Пишу...")
    try:
        if gen_type == "generate_post":
            out = await scenarist.generate_post(profile, inp["topic"], inp.get("reference"))
        elif gen_type == "rewrite":
            out = await scenarist.rewrite_post(profile, inp["source"])
        else:
            out = await scenarist.generate_thread(profile, inp["topic"])
    except (LLMError, KeyError):
        log.exception("generation failed type=%s uid=%s", gen_type, uid)
        async with Session() as s:
            await credits.topup(s, uid, cost, f"refund_{gen_type}")
            await s.commit()
        await msg.answer("Генерация упала, кредиты вернул. Попробуй ещё раз.")
        return

    async with Session() as s:
        gen_id = await scenarist.log_generation(s, uid, gen_type, inp, out, cost)
        await s.commit()

    if gen_type == "generate_thread":
        posts = out.get("posts", [])
        for i, p in enumerate(posts, 1):
            await msg.answer(f"Пост {i}/{len(posts)}:\n\n{p}")
        await msg.answer("Ветка готова.", reply_markup=_more_kb(gen_id))
    else:
        await msg.answer(_render(out), reply_markup=_more_kb(gen_id))
