import logging
import re

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .db import Database
from .texts import Texts

log = logging.getLogger(__name__)

MEMBER_STATUSES = {"member", "administrator", "creator"}
REF_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")  # допустимый параметр deep link в Telegram
CODE_RE = re.compile(r"\S{3,32}")


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


def parse_ref(args: str | None) -> str | None:
    """Метка источника из ссылки t.me/bot?start=<метка>."""
    return args if args and REF_RE.fullmatch(args) else None


def parse_code(text: str | None) -> str | None:
    """Код с лендинга. Содержимое не проверяется — только то, что это похоже на код."""
    code = (text or "").strip()
    if code.startswith("/") or not CODE_RE.fullmatch(code):
        return None
    return code.upper()


def build_user_router() -> Router:
    """Роутер бота розыгрыша. Создаётся заново для каждого бота (свой Dispatcher).

    Шаги: подписка на канал -> код с лендинга -> регистрация.
    Язык (language) — настройка самого бота, пользователь его не выбирает.
    """
    router = Router()
    router.message.filter(F.chat.type == "private")

    def ask_subscription(message: Message, texts: Texts, language: str, channel_url: str):
        return message.answer(
            texts.get(language, "sub_prompt"),
            reply_markup=subscribe_kb(texts, language, channel_url),
        )

    @router.message(CommandStart())
    async def on_start(
        message: Message,
        command: CommandObject,
        db: Database,
        texts: Texts,
        source: str,
        language: str,
        channel_url: str,
    ) -> None:
        try:
            user = message.from_user
            await db.touch_user(user.id, source, user.username, parse_ref(command.args))
            if await db.get_participant(user.id):
                # Участвует (в этом или в другом боте) — повторная регистрация не создаётся
                await message.answer(texts.get(language, "already"))
            elif (await db.get_user(user.id, source))["awaiting_code"]:
                await message.answer(texts.get(language, "subscribed"))
            else:
                await ask_subscription(message, texts, language, channel_url)
        except Exception:
            log.exception("start failed (pool %s)", source)
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
            await db.touch_user(user.id, source, user.username)
            if await db.get_participant(user.id):
                await callback.message.answer(texts.get(language, "already"))
            elif not await is_subscribed(bot, channel_id, user.id):
                await callback.message.answer(
                    texts.get(language, "not_subscribed"),
                    reply_markup=subscribe_kb(texts, language, channel_url),
                )
            else:
                await db.set_awaiting_code(user.id, source)
                await callback.message.answer(texts.get(language, "subscribed"))
        except Exception:
            log.exception("subscription check failed (pool %s)", source)
            await callback.message.answer(
                texts.get(language, "error"),
                reply_markup=subscribe_kb(texts, language, channel_url),
            )
        await callback.answer()

    @router.message()
    async def on_message(
        message: Message, db: Database, texts: Texts, source: str, language: str, channel_url: str
    ) -> None:
        """Любое сообщение, кроме /start: это либо код, либо повтор текущего шага."""
        try:
            user = message.from_user
            await db.touch_user(user.id, source, user.username)
            if await db.get_participant(user.id):
                await message.answer(texts.get(language, "already"))
                return
            row = await db.get_user(user.id, source)
            if not row["awaiting_code"]:
                await ask_subscription(message, texts, language, channel_url)
                return
            code = parse_code(message.text)
            if not code:
                await message.answer(texts.get(language, "code_invalid"))
            elif await db.register(
                user.id, user.username, user.full_name, language, source, code, row["ref"]
            ):
                await message.answer(texts.get(language, "registered"))
            else:  # параллельная регистрация в другом боте успела раньше
                await message.answer(texts.get(language, "already"))
        except Exception:
            # При ошибке участие не создаётся: register() — последняя операция
            log.exception("message handling failed (pool %s)", source)
            await message.answer(texts.get(language, "error"))

    return router
