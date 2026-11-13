import logging

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, User

from .db import Database
from .texts import Texts

log = logging.getLogger(__name__)

MEMBER_STATUSES = {"member", "administrator", "creator"}


def language_kb(texts: Texts) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=title, callback_data=f"lang:{code}")]
            for code, title in texts.languages.items()
        ]
    )


def subscribe_kb(texts: Texts, language: str, channel_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.get(language, "btn_subscribe"), url=channel_url)],
            [InlineKeyboardButton(text=texts.get(language, "btn_check"), callback_data="check")],
        ]
    )


async def is_subscribed(bot: Bot, channel_id: int | str, user_id: int) -> bool:
    member = await bot.get_chat_member(channel_id, user_id)
    return member.status in MEMBER_STATUSES or (
        member.status == "restricted" and getattr(member, "is_member", False)
    )


def build_user_router() -> Router:
    """Роутер бота розыгрыша. Создаётся заново для каждого бота (свой Dispatcher)."""
    router = Router()
    router.message.filter(F.chat.type == "private")

    def fallback_language(texts: Texts, source: str) -> str:
        code = source.lower()
        return code if code in texts.languages else "en"

    async def show_entry(
        message: Message, user: User, db: Database, texts: Texts, source: str, channel_url: str
    ) -> None:
        """Показывает шаг, на котором находится пользователь."""
        await db.touch_user(user.id, source, user.username)
        participant = await db.get_participant(user.id)
        if participant:
            # Участвует (в этом или в другом боте) — повторная регистрация не создаётся
            await message.answer(texts.get(participant["bot_language"], "already"))
            return
        language = await db.get_language(user.id, source)
        if language in texts.languages:
            await message.answer(
                texts.get(language, "sub_prompt"),
                reply_markup=subscribe_kb(texts, language, channel_url),
            )
        else:
            await message.answer(texts.choose_prompt(), reply_markup=language_kb(texts))

    @router.message(CommandStart())
    async def on_start(
        message: Message, db: Database, texts: Texts, source: str, channel_url: str
    ) -> None:
        try:
            user = message.from_user
            await db.touch_user(user.id, source, user.username)
            participant = await db.get_participant(user.id)
            if participant:
                await message.answer(texts.get(participant["bot_language"], "already"))
            else:
                await message.answer(texts.choose_prompt(), reply_markup=language_kb(texts))
        except Exception:
            log.exception("start failed")
            await message.answer(texts.get(fallback_language(texts, source), "error"))

    @router.callback_query(F.data.startswith("lang:"))
    async def on_language(
        callback: CallbackQuery, db: Database, texts: Texts, source: str, channel_url: str
    ) -> None:
        language = callback.data.split(":", 1)[1]
        if language not in texts.languages:
            language = fallback_language(texts, source)
        try:
            user = callback.from_user
            await db.set_language(user.id, source, language)
            participant = await db.get_participant(user.id)
            if participant:
                await callback.message.answer(texts.get(language, "already"))
            else:
                await callback.message.answer(
                    texts.get(language, "sub_prompt"),
                    reply_markup=subscribe_kb(texts, language, channel_url),
                )
        except Exception:
            log.exception("language selection failed")
            await callback.message.answer(texts.get(language, "error"))
        await callback.answer()

    @router.callback_query(F.data == "check")
    async def on_check(
        callback: CallbackQuery,
        bot: Bot,
        db: Database,
        texts: Texts,
        source: str,
        channel_id: int | str,
        channel_url: str,
    ) -> None:
        user = callback.from_user
        language = fallback_language(texts, source)
        try:
            stored = await db.get_language(user.id, source)
            if stored in texts.languages:
                language = stored
            if await db.get_participant(user.id):
                await callback.message.answer(texts.get(language, "already"))
            elif not await is_subscribed(bot, channel_id, user.id):
                await callback.message.answer(
                    texts.get(language, "not_subscribed"),
                    reply_markup=subscribe_kb(texts, language, channel_url),
                )
            elif await db.register(user.id, user.username, user.full_name, language, source):
                await callback.message.answer(texts.get(language, "registered"))
            else:  # параллельная регистрация в другом боте успела раньше
                await callback.message.answer(texts.get(language, "already"))
        except Exception:
            # При ошибке участие не создаётся: register() — последняя операция
            log.exception("subscription check failed (pool %s)", source)
            await callback.message.answer(
                texts.get(language, "error"),
                reply_markup=subscribe_kb(texts, language, channel_url),
            )
        await callback.answer()

    @router.message()
    async def on_any_message(
        message: Message, db: Database, texts: Texts, source: str, channel_url: str
    ) -> None:
        try:
            await show_entry(message, message.from_user, db, texts, source, channel_url)
        except Exception:
            log.exception("message handling failed")
            await message.answer(texts.get(fallback_language(texts, source), "error"))

    return router
