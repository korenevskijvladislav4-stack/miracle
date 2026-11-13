import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .db import Database
from .texts import Texts

log = logging.getLogger(__name__)

MEMBER_STATUSES = {"member", "administrator", "creator"}


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
    """Роутер бота розыгрыша. Создаётся заново для каждого бота (свой Dispatcher).

    Язык (language) — настройка самого бота, пользователь его не выбирает.
    """
    router = Router()
    router.message.filter(F.chat.type == "private")

    @router.message()
    async def on_message(
        message: Message, db: Database, texts: Texts, source: str, language: str, channel_url: str
    ) -> None:
        """/start и любое другое сообщение: показать текущий шаг."""
        try:
            user = message.from_user
            await db.touch_user(user.id, source, user.username)
            if await db.get_participant(user.id):
                # Участвует (в этом или в другом боте) — повторная регистрация не создаётся
                await message.answer(texts.get(language, "already"))
            else:
                await message.answer(
                    texts.get(language, "sub_prompt"),
                    reply_markup=subscribe_kb(texts, language, channel_url),
                )
        except Exception:
            log.exception("message handling failed (pool %s)", source)
            await message.answer(texts.get(language, "error"))

    @router.callback_query(F.data == "check")
    async def on_check(
        callback: CallbackQuery,
        bot: Bot,
        db: Database,
        texts: Texts,
        source: str,
        language: str,
        channel_id: int | str,
        channel_url: str,
    ) -> None:
        user = callback.from_user
        try:
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

    return router
