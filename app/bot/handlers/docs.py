"""Документы (политика, условия, удаление данных) внутри бота, RU и EN."""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

router = Router()
BASE = "https://threadsmith.pro"
NL = chr(10)

RU = (
"📄 Документы ThreadSmith" + NL + NL +
"Что мы храним:" + NL +
"- твой Telegram ID" + NL +
"- зашифрованный токен Threads (получен через официальный OAuth Meta)" + NL +
"- твои темы, настройки голоса и посты" + NL +
"- записи об оплатах (данные карты к нам не попадают)" + NL + NL +
"Зачем: только чтобы писать посты твоим голосом, публиковать их в твой "
"Threads и отвечать на комментарии по твоим правилам." + NL + NL +
"Кому передаём: Meta (Threads API), Anthropic (генерация текста), "
"Supabase (база в ЕС), Telegram. Мы не продаём данные и не отдаём их "
"в рекламу." + NL + NL +
"Твои права: отключить Threads в любой момент — токен удаляется сразу. "
"Полное удаление данных — напиши на support@threadsmith.pro, сотрём за 30 дней." + NL + NL +
"Правила: подключай только свои аккаунты, не используй бот для спама "
"и контента, нарушающего правила Threads. Автор постов — ты, "
"ответственность за них тоже твоя."
)

EN = (
"📄 ThreadSmith Documents" + NL + NL +
"What we store:" + NL +
"- your Telegram ID" + NL +
"- encrypted Threads access token (via official Meta OAuth)" + NL +
"- your topics, voice settings and posts" + NL +
"- payment records (card data never reaches our servers)" + NL + NL +
"Why: only to generate posts in your style, publish them to your Threads "
"account and reply to comments according to your rules." + NL + NL +
"Third parties: Meta (Threads API), Anthropic (text generation), "
"Supabase (EU database), Telegram. We never sell your data." + NL + NL +
"Your rights: disconnect Threads anytime, the token is deleted immediately. "
"For full deletion write to support@threadsmith.pro, done within 30 days." + NL + NL +
"Rules: connect only accounts you own, no spam, no content violating "
"Threads guidelines. You are the author and owner of published content."
)


def kb(lang="ru"):
    other = ("🇬🇧 English", "docs:en") if lang == "ru" else ("🇷🇺 Русский", "docs:ru")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=other[0], callback_data=other[1])],
        [InlineKeyboardButton(text="Privacy Policy", url=BASE + "/privacy")],
        [InlineKeyboardButton(text="Terms of Service", url=BASE + "/terms")],
        [InlineKeyboardButton(text="Data Deletion", url=BASE + "/data-deletion")],
        [InlineKeyboardButton(text="🏠 Главная", callback_data="home")],
    ])


@router.message(Command("docs"))
async def cmd_docs(msg: Message):
    await msg.answer(RU, reply_markup=kb("ru"))


@router.callback_query(F.data == "docs:menu")
async def cb_docs(cb: CallbackQuery):
    await cb.message.answer(RU, reply_markup=kb("ru"))
    await cb.answer()


@router.callback_query(F.data == "docs:ru")
async def cb_ru(cb: CallbackQuery):
    await cb.message.edit_text(RU, reply_markup=kb("ru"))
    await cb.answer()


@router.callback_query(F.data == "docs:en")
async def cb_en(cb: CallbackQuery):
    await cb.message.edit_text(EN, reply_markup=kb("en"))
    await cb.answer()
