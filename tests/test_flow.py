"""Сквозные тесты: настоящие хендлеры и БД, Telegram API заменён заглушкой."""
import datetime as dt

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import GetChatMember, GetMe, SendDocument, SendMessage
from aiogram.types import (
    CallbackQuery, Chat, ChatMemberLeft, ChatMemberMember, ChatMemberOwner,
    Message, Update, User,
)

from bot.admin import build_admin_router
from bot.config import Config
from bot.db import Database
from bot.handlers import build_user_router
from bot.manager import BotManager
from bot.texts import DEFAULT_LANGUAGES, DEFAULTS, Texts

NOW = dt.datetime.now(dt.timezone.utc)
ADMIN_ID = 999


class FakeSession(BaseSession):
    """Запоминает исходящие вызовы; подписчики задаются множеством user_id."""

    def __init__(self):
        super().__init__()
        self.sent: list = []
        self.subscribers: set[int] = set()
        self.fail_membership = False

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        if isinstance(method, GetChatMember):
            if self.fail_membership:
                raise RuntimeError("telegram is down")
            user = User(id=method.user_id, is_bot=False, first_name="u")
            if method.user_id == bot.id:
                return ChatMemberOwner(user=user, is_anonymous=False)
            if method.user_id in self.subscribers:
                return ChatMemberMember(user=user)
            return ChatMemberLeft(user=user)
        if isinstance(method, GetMe):
            return User(id=bot.id, is_bot=True, first_name="b", username="new_bot")
        if isinstance(method, (SendMessage, SendDocument)):
            return Message(message_id=1, date=NOW, chat=Chat(id=method.chat_id, type="private"))
        return True

    def texts(self) -> list[str]:
        return [m.text for m in self.sent if isinstance(m, SendMessage)]


class Client:
    def __init__(self, dp: Dispatcher, bot: Bot, data: dict):
        self.dp, self.bot, self.data, self.n = dp, bot, data, 0

    @property
    def session(self) -> FakeSession:
        return self.bot.session

    def _user(self, uid):
        return User(id=uid, is_bot=False, first_name="Test", username=f"user{uid}")

    async def message(self, uid: int, text: str):
        self.n += 1
        msg = Message(message_id=self.n, date=NOW, chat=Chat(id=uid, type="private"),
                      from_user=self._user(uid), text=text)
        await self.dp.feed_update(self.bot, Update(update_id=self.n, message=msg), **self.data)

    async def click(self, uid: int, data: str):
        self.n += 1
        msg = Message(message_id=self.n, date=NOW, chat=Chat(id=uid, type="private"), text="x")
        cb = CallbackQuery(id=str(self.n), from_user=self._user(uid), chat_instance="c",
                           message=msg, data=data)
        await self.dp.feed_update(self.bot, Update(update_id=self.n, callback_query=cb), **self.data)


def make_bot(bot_id: int) -> Bot:
    return Bot(f"{bot_id}:{'A' * 35}", session=FakeSession(),
               default=DefaultBotProperties(parse_mode="HTML"))


@pytest_asyncio.fixture
async def env(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.connect(DEFAULT_LANGUAGES)
    texts = Texts(db)
    await texts.load()

    def user_client(source: str, bot_id: int) -> Client:
        dp = Dispatcher()
        dp.include_router(build_user_router())
        return Client(dp, make_bot(bot_id), dict(
            db=db, texts=texts, source=source,
            channel_id=f"@ch_{source.lower()}", channel_url=f"https://t.me/ch_{source.lower()}"))

    yield db, texts, user_client
    await db.close()


async def test_happy_path(env):
    db, texts, user_client = env
    pt = user_client("PT", 9001)

    await pt.message(1, "/start")
    assert pt.session.texts()[-1].startswith("Choose your language")

    await pt.click(1, "lang:ru")  # язык интерфейса не влияет на пул
    assert pt.session.texts()[-1] == DEFAULTS["ru"]["sub_prompt"]

    await pt.click(1, "check")
    assert pt.session.texts()[-1] == DEFAULTS["ru"]["not_subscribed"]
    assert await db.get_participant(1) is None

    pt.session.subscribers.add(1)
    await pt.click(1, "check")
    assert pt.session.texts()[-1] == DEFAULTS["ru"]["registered"]
    p = await db.get_participant(1)
    assert (p["source_channel"], p["bot_language"], p["username"]) == ("PT", "ru", "user1")
    assert (p["subscription_status"], p["participation_status"]) == ("subscribed", "active")

    await pt.click(1, "check")  # повторное нажатие
    await pt.message(1, "/start")
    assert pt.session.texts()[-2:] == [DEFAULTS["ru"]["already"]] * 2
    assert len(await db.list_participants()) == 1


async def test_one_participation_across_bots(env):
    db, texts, user_client = env
    ru, en = user_client("RU", 9001), user_client("EN", 9002)
    ru.session.subscribers.add(5)
    en.session.subscribers.add(5)

    await ru.message(5, "/start")
    await ru.click(5, "lang:es")
    await ru.click(5, "check")

    await en.message(5, "/start")  # другой бот — сразу «уже участвуешь»
    assert en.session.texts() == [DEFAULTS["es"]["already"]]
    await en.click(5, "lang:en")
    await en.click(5, "check")
    assert en.session.texts()[-2:] == [DEFAULTS["en"]["already"]] * 2

    assert [p["source_channel"] for p in await db.list_participants()] == ["RU"]


async def test_technical_error_does_not_register(env):
    db, texts, user_client = env
    es = user_client("ES", 9001)
    es.session.fail_membership = True
    await es.message(7, "/start")
    await es.click(7, "lang:pt")
    await es.click(7, "check")
    assert es.session.texts()[-1] == DEFAULTS["pt"]["error"]
    assert await db.get_participant(7) is None

    es.session.fail_membership = False
    es.session.subscribers.add(7)
    await es.click(7, "check")
    assert (await db.get_participant(7))["source_channel"] == "ES"


async def test_stats_and_pools(env):
    db, texts, user_client = env
    ru, pt = user_client("RU", 9001), user_client("PT", 9002)
    for uid in (1, 2, 3):
        ru.session.subscribers.add(uid)
        await ru.message(uid, "/start")
        await ru.click(uid, "lang:ru")
    await ru.click(1, "check")
    await ru.click(2, "check")
    pt.session.subscribers.add(10)
    await pt.message(10, "/start")
    await pt.click(10, "lang:en")
    await pt.click(10, "check")

    s = await db.stats()
    assert s["participants"] == {"RU": 2, "PT": 1}
    assert s["started"] == {"RU": 3, "PT": 1}
    assert s["languages"] == {"ru": 2, "en": 1}
    assert [p["telegram_id"] for p in await db.list_participants("PT")] == [10]


async def test_admin_panel(env, monkeypatch):
    db, texts, user_client = env
    manager = BotManager(db, texts)
    started = []

    async def fake_start(row):
        started.append(row["source"])

    monkeypatch.setattr(manager, "start", fake_start)
    # Проверочные Bot(token) в админке тоже не должны ходить в сеть
    monkeypatch.setattr("bot.admin.Bot", lambda token: make_bot(int(token.split(":")[0])))

    cfg = Config("1:x", frozenset({ADMIN_ID}), "")
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_admin_router(cfg))
    admin = Client(dp, make_bot(100), dict(db=db, texts=texts, manager=manager))

    # посторонний не получает доступа
    await admin.message(5, "/admin")
    assert "Нет доступа" in admin.session.texts()[-1]

    # создание бота
    await admin.message(ADMIN_ID, "/admin")
    await admin.click(ADMIN_ID, "bot_add")
    await admin.message(ADMIN_ID, "de")
    await admin.message(ADMIN_ID, f"555:{'B' * 35}")
    await admin.message(ADMIN_ID, "@royal_de")
    await admin.message(ADMIN_ID, "https://t.me/royal_de")
    row = await db.get_bot("DE")
    assert (row["username"], row["channel_id"], row["channel_url"]) == (
        "new_bot", "@royal_de", "https://t.me/royal_de")
    assert started == ["DE"]
    assert "создан и запущен" in admin.session.texts()[-1]
    assert "⚠️" not in admin.session.texts()[-1]

    # правка текста сразу видна в боте розыгрыша
    await admin.click(ADMIN_ID, "te:ru:registered")
    await admin.message(ADMIN_ID, "Новый текст ✨")
    assert texts.get("ru", "registered") == "Новый текст ✨"
    ru = user_client("RU", 9001)
    ru.session.subscribers.add(1)
    await ru.message(1, "/start")
    await ru.click(1, "lang:ru")
    await ru.click(1, "check")
    assert ru.session.texts()[-1] == "Новый текст ✨"

    # правки переживают перезапуск, сброс возвращает стандартный текст
    reloaded = Texts(db)
    await reloaded.load()
    assert reloaded.get("ru", "registered") == "Новый текст ✨"
    await admin.click(ADMIN_ID, "tr:ru:registered")
    assert texts.get("ru", "registered") == DEFAULTS["ru"]["registered"]

    # новый язык: появляется в выборе, тексты — английские, пока не переведены
    await admin.click(ADMIN_ID, "lang_add")
    await admin.message(ADMIN_ID, "de")
    await admin.message(ADMIN_ID, "🇩🇪 Deutsch")
    assert texts.languages["de"] == "🇩🇪 Deutsch"
    assert texts.get("de", "already") == DEFAULTS["en"]["already"]

    # статистика и экспорт
    await admin.click(ADMIN_ID, "stats")
    await admin.click(ADMIN_ID, "exp:*")
    docs = [m for m in admin.session.sent if isinstance(m, SendDocument)]
    assert len(docs) == 1 and b"telegram_id;username" in docs[0].document.data
