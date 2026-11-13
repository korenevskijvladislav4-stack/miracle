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

    def user_client(source: str, bot_id: int, language: str | None = None) -> Client:
        dp = Dispatcher()
        dp.include_router(build_user_router())
        return Client(dp, make_bot(bot_id), dict(
            db=db, texts=texts, source=source, language=language or source.lower(),
            channel_id=f"@ch_{source.lower()}", channel_url=f"https://t.me/ch_{source.lower()}"))

    yield db, texts, user_client
    await db.close()


async def test_happy_path(env):
    db, texts, user_client = env
    pt = user_client("PT", 9001)

    await pt.message(1, "/start partner_123")  # deep link: t.me/bot?start=partner_123
    assert pt.session.texts() == [DEFAULTS["pt"]["sub_prompt"]]
    buttons = pt.session.sent[-1].reply_markup.inline_keyboard
    assert buttons[0][0].url == "https://t.me/ch_pt"
    assert buttons[0][0].text == DEFAULTS["pt"]["btn_subscribe"]

    await pt.click(1, "check")
    assert pt.session.texts()[-1] == DEFAULTS["pt"]["not_subscribed"]
    assert await db.get_participant(1) is None

    await pt.message(1, "RG-A7K3X")  # код до подтверждения подписки не принимается
    assert pt.session.texts()[-1] == DEFAULTS["pt"]["sub_prompt"]

    pt.session.subscribers.add(1)
    await pt.click(1, "check")
    assert pt.session.texts()[-1] == DEFAULTS["pt"]["subscribed"]
    assert await db.get_participant(1) is None  # без кода участия ещё нет

    await pt.message(1, "/start instagram")  # повторный вход: шаг кода, метка не меняется
    assert pt.session.texts()[-1] == DEFAULTS["pt"]["subscribed"]
    await pt.message(1, "это не код")
    assert pt.session.texts()[-1] == DEFAULTS["pt"]["code_invalid"]
    assert await db.get_participant(1) is None

    await pt.message(1, " rg-a7k3x ")
    assert pt.session.texts()[-1] == DEFAULTS["pt"]["registered"]
    p = await db.get_participant(1)
    assert (p["code"], p["ref"]) == ("RG-A7K3X", "partner_123")
    assert (p["source_channel"], p["bot_language"], p["username"]) == ("PT", "pt", "user1")
    assert (p["subscription_status"], p["participation_status"]) == ("subscribed", "active")

    await pt.click(1, "check")  # повторное нажатие
    await pt.message(1, "/start")
    assert pt.session.texts()[-2:] == [DEFAULTS["pt"]["already"]] * 2
    assert len(await db.list_participants()) == 1


async def test_one_participation_across_bots(env):
    db, texts, user_client = env
    ru, en = user_client("RU", 9001), user_client("EN", 9002)
    ru.session.subscribers.add(5)
    en.session.subscribers.add(5)

    await ru.message(5, "/start")
    await ru.click(5, "check")
    await ru.message(5, "CODE1")
    assert ru.session.texts()[-1] == DEFAULTS["ru"]["registered"]

    await en.message(5, "/start")  # другой бот — «уже участвуешь» на языке этого бота
    await en.click(5, "check")
    await en.message(5, "CODE2")
    assert en.session.texts() == [DEFAULTS["en"]["already"]] * 3

    assert [p["source_channel"] for p in await db.list_participants()] == ["RU"]


async def test_technical_error_does_not_register(env):
    db, texts, user_client = env
    es = user_client("ES", 9001)
    es.session.fail_membership = True
    await es.message(7, "/start")
    await es.click(7, "check")
    assert es.session.texts()[-1] == DEFAULTS["es"]["error"]
    assert await db.get_participant(7) is None

    es.session.fail_membership = False
    es.session.subscribers.add(7)
    await es.click(7, "check")
    await es.message(7, "CODE7")
    assert (await db.get_participant(7))["source_channel"] == "ES"


async def test_stats_and_pools(env):
    db, texts, user_client = env
    ru, pt = user_client("RU", 9001), user_client("PT", 9002)
    for uid, ref in ((1, "insta"), (2, "insta"), (3, "")):
        ru.session.subscribers.add(uid)
        await ru.message(uid, f"/start {ref}".strip())
    for uid in (1, 3):
        await ru.click(uid, "check")
        await ru.message(uid, f"CODE{uid}")
    pt.session.subscribers.add(10)
    await pt.message(10, "/start lk")
    await pt.message(10, "/start <b>bad ref</b>")  # недопустимая метка игнорируется
    await pt.click(10, "check")
    await pt.message(10, "CODE10")

    s = await db.stats()
    assert s["participants"] == {"RU": 2, "PT": 1}
    assert s["started"] == {"RU": 3, "PT": 1}
    assert [p["telegram_id"] for p in await db.list_participants("PT")] == [10]
    assert [tuple(r) for r in await db.ref_stats()] == [
        ("PT", "lk", 1, 1),
        ("RU", "insta", 2, 1),
        ("RU", "", 1, 1),
    ]


async def test_admin_panel(env, monkeypatch):
    db, texts, user_client = env
    manager = BotManager(db, texts)
    started = []

    async def fake_start(row):
        started.append((row["source"], row["language"]))

    monkeypatch.setattr(manager, "start", fake_start)
    monkeypatch.setattr(manager, "is_running", lambda source: True)
    monkeypatch.setattr("bot.admin.asyncio.sleep", lambda *_: _noop())
    # Проверочные Bot(token) в админке тоже не должны ходить в сеть
    monkeypatch.setattr("bot.admin.Bot", lambda token: make_bot(int(token.split(":")[0])))

    cfg = Config("1:x", frozenset({ADMIN_ID}), "")
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_admin_router(cfg))
    admin = Client(dp, make_bot(100), dict(db=db, texts=texts, manager=manager))

    # посторонний не получает доступа
    await admin.message(5, "/admin")
    assert "Нет доступа" in admin.session.texts()[-1]

    # создание бота: код → токен → язык → канал → ссылка
    await admin.message(ADMIN_ID, "/admin")
    await admin.click(ADMIN_ID, "bot_add")
    await admin.message(ADMIN_ID, "br")
    await admin.message(ADMIN_ID, f"555:{'B' * 35}")
    await admin.click(ADMIN_ID, "al:pt")
    await admin.message(ADMIN_ID, "@royal_br")
    await admin.message(ADMIN_ID, "https://t.me/royal_br")
    row = await db.get_bot("BR")
    assert (row["username"], row["channel_id"], row["channel_url"], row["language"]) == (
        "new_bot", "@royal_br", "https://t.me/royal_br", "pt")
    assert started == [("BR", "pt")]
    assert "создан и запущен" in admin.session.texts()[-1]
    assert "⚠️" not in admin.session.texts()[-1]

    # смена языка бота перезапускает его с новым языком
    await admin.click(ADMIN_ID, "bl:BR:es")
    assert (await db.get_bot("BR"))["language"] == "es"
    assert started[-1] == ("BR", "es")

    # правка текста сразу видна в боте на этом языке
    await admin.click(ADMIN_ID, "te:ru:registered")
    await admin.message(ADMIN_ID, "Новый текст ✨")
    assert texts.get("ru", "registered") == "Новый текст ✨"
    ru = user_client("RU", 9001)
    ru.session.subscribers.add(1)
    await ru.message(1, "/start promo")
    await ru.click(1, "check")
    await ru.message(1, "RG-1")
    assert ru.session.texts()[-1] == "Новый текст ✨"

    # правки переживают перезапуск, сброс возвращает стандартный текст
    reloaded = Texts(db)
    await reloaded.load()
    assert reloaded.get("ru", "registered") == "Новый текст ✨"
    await admin.click(ADMIN_ID, "tr:ru:registered")
    assert texts.get("ru", "registered") == DEFAULTS["ru"]["registered"]

    # новый язык: тексты английские, пока не переведены
    await admin.click(ADMIN_ID, "lang_add")
    await admin.message(ADMIN_ID, "de")
    await admin.message(ADMIN_ID, "🇩🇪 Deutsch")
    assert texts.languages["de"] == "🇩🇪 Deutsch"
    assert texts.get("de", "already") == DEFAULTS["en"]["already"]

    # язык, назначенный боту, удалить нельзя; свободный — можно
    await admin.click(ADMIN_ID, "lang_del_ok:es")
    assert "es" in texts.languages
    await admin.click(ADMIN_ID, "lang_del_ok:de")
    assert "de" not in texts.languages

    # статистика и экспорт
    await admin.click(ADMIN_ID, "stats")
    await admin.click(ADMIN_ID, "refs")
    await admin.click(ADMIN_ID, "bot:BR")
    await admin.click(ADMIN_ID, "exp:*")
    docs = [m for m in admin.session.sent if isinstance(m, SendDocument)]
    assert len(docs) == 1
    header, row = docs[0].document.data.decode("utf-8-sig").splitlines()[:2]
    assert header.endswith(";registered_at;code;ref")
    assert row.endswith(";RG-1;promo")


async def _noop():
    return None


async def test_migrates_database_without_bot_language(tmp_path):
    import sqlite3

    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE bots (source TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE, username TEXT, "
        "channel_id TEXT NOT NULL, channel_url TEXT NOT NULL, "
        "is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)"
    )
    old.execute("INSERT INTO bots VALUES ('RU', '1:x', 'b', '@c', 'https://t.me/c', 1, '2026-01-01')")
    old.commit()
    old.close()

    db = Database(path)
    await db.connect(DEFAULT_LANGUAGES)
    assert (await db.get_bot("RU"))["language"] == "en"
    await db.touch_user(1, "RU", "u", "ref1")  # новые колонки users/participants тоже на месте
    await db.set_awaiting_code(1, "RU")
    assert await db.register(1, "u", "U", "en", "RU", "CODE", "ref1")
    await db.close()
