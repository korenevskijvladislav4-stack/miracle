import os
import sqlite3
from datetime import datetime, timedelta, timezone

import aiosqlite

SCHEMA = """
-- Боты розыгрыша. source — код пула (RU / EN / PT / ES / ...).
CREATE TABLE IF NOT EXISTS bots (
    source      TEXT PRIMARY KEY,
    token       TEXT NOT NULL UNIQUE,
    username    TEXT,
    channel_id  TEXT NOT NULL,
    channel_url TEXT NOT NULL,
    language    TEXT NOT NULL DEFAULT 'en',
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

-- Языки (наборы фраз). Каждый бот работает на одном языке.
CREATE TABLE IF NOT EXISTS languages (
    code     TEXT PRIMARY KEY,
    title    TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
);

-- Переопределения текстов (то, что отредактировано в админке)
CREATE TABLE IF NOT EXISTS texts (
    language TEXT NOT NULL,
    key      TEXT NOT NULL,
    value    TEXT NOT NULL,
    PRIMARY KEY (language, key)
);

-- Все, кто нажал Start в каком-либо боте (воронка)
CREATE TABLE IF NOT EXISTS users (
    telegram_id    INTEGER NOT NULL,
    source_channel TEXT    NOT NULL,
    username       TEXT,
    language       TEXT,
    started_at     TEXT    NOT NULL,
    PRIMARY KEY (telegram_id, source_channel)
);

-- Участники. PRIMARY KEY по telegram_id гарантирует
-- «1 Telegram ID = 1 участие» сразу для всех ботов.
CREATE TABLE IF NOT EXISTS participants (
    telegram_id             INTEGER PRIMARY KEY,
    username                TEXT,
    full_name               TEXT,
    bot_language            TEXT NOT NULL,
    source_channel          TEXT NOT NULL,
    subscription_status     TEXT NOT NULL DEFAULT 'subscribed',
    participation_status    TEXT NOT NULL DEFAULT 'active',
    registered_at           TEXT NOT NULL,
    subscription_checked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_participants_source ON participants (source_channel);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self, default_languages: dict[str, str] | None = None) -> None:
        folder = os.path.dirname(self.path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.executescript(SCHEMA)
        columns = {row["name"] for row in await self._all("PRAGMA table_info(bots)")}
        if "language" not in columns:  # база, созданная до появления языка бота
            await self.conn.execute("ALTER TABLE bots ADD COLUMN language TEXT NOT NULL DEFAULT 'en'")
        if default_languages and not await self.list_languages():
            for position, (code, title) in enumerate(default_languages.items()):
                await self.conn.execute(
                    "INSERT INTO languages (code, title, position) VALUES (?, ?, ?)",
                    (code, title, position),
                )
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def _all(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(sql, params)
        return await cur.fetchall()

    async def _one(self, sql: str, params: tuple = ()) -> aiosqlite.Row | None:
        cur = await self.conn.execute(sql, params)
        return await cur.fetchone()

    async def _write(self, sql: str, params: tuple = ()) -> None:
        await self.conn.execute(sql, params)
        await self.conn.commit()

    # --- bots ------------------------------------------------------------

    async def list_bots(self) -> list[aiosqlite.Row]:
        return await self._all("SELECT * FROM bots ORDER BY created_at")

    async def get_bot(self, source: str) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM bots WHERE source = ?", (source,))

    async def add_bot(
        self,
        source: str,
        token: str,
        username: str | None,
        channel_id: str,
        channel_url: str,
        language: str,
    ) -> bool:
        """False — бот с таким кодом пула или токеном уже есть."""
        try:
            await self._write(
                "INSERT INTO bots "
                "(source, token, username, channel_id, channel_url, language, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source, token, username, channel_id, channel_url, language, _now()),
            )
            return True
        except sqlite3.IntegrityError:
            await self.conn.rollback()
            return False

    async def update_bot_channel(self, source: str, channel_id: str, channel_url: str) -> None:
        await self._write(
            "UPDATE bots SET channel_id = ?, channel_url = ? WHERE source = ?",
            (channel_id, channel_url, source),
        )

    async def set_bot_language(self, source: str, language: str) -> None:
        await self._write("UPDATE bots SET language = ? WHERE source = ?", (language, source))

    async def set_bot_active(self, source: str, active: bool) -> None:
        await self._write("UPDATE bots SET is_active = ? WHERE source = ?", (int(active), source))

    async def delete_bot(self, source: str) -> None:
        await self._write("DELETE FROM bots WHERE source = ?", (source,))

    # --- languages & texts -----------------------------------------------

    async def list_languages(self) -> list[aiosqlite.Row]:
        return await self._all("SELECT * FROM languages ORDER BY position, code")

    async def add_language(self, code: str, title: str) -> bool:
        try:
            await self._write(
                "INSERT INTO languages (code, title, position) "
                "VALUES (?, ?, (SELECT COALESCE(MAX(position), 0) + 1 FROM languages))",
                (code, title),
            )
            return True
        except sqlite3.IntegrityError:
            await self.conn.rollback()
            return False

    async def delete_language(self, code: str) -> None:
        await self.conn.execute("DELETE FROM languages WHERE code = ?", (code,))
        await self.conn.execute("DELETE FROM texts WHERE language = ?", (code,))
        await self.conn.commit()

    async def list_texts(self) -> list[aiosqlite.Row]:
        return await self._all("SELECT * FROM texts")

    async def set_text(self, language: str, key: str, value: str) -> None:
        await self._write(
            "INSERT INTO texts (language, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT (language, key) DO UPDATE SET value = excluded.value",
            (language, key, value),
        )

    async def delete_text(self, language: str, key: str) -> None:
        await self._write("DELETE FROM texts WHERE language = ? AND key = ?", (language, key))

    # --- users -----------------------------------------------------------

    async def touch_user(self, telegram_id: int, source: str, username: str | None) -> None:
        await self._write(
            """INSERT INTO users (telegram_id, source_channel, username, started_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT (telegram_id, source_channel) DO UPDATE SET username = excluded.username""",
            (telegram_id, source, username, _now()),
        )

    # --- participants ----------------------------------------------------

    async def get_participant(self, telegram_id: int) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM participants WHERE telegram_id = ?", (telegram_id,))

    async def register(
        self,
        telegram_id: int,
        username: str | None,
        full_name: str | None,
        language: str,
        source: str,
    ) -> bool:
        """True — участник создан; False — этот Telegram ID уже участвует."""
        try:
            await self._write(
                """INSERT INTO participants
                   (telegram_id, username, full_name, bot_language, source_channel,
                    registered_at, subscription_checked_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (telegram_id, username, full_name, language, source, _now(), _now()),
            )
            return True
        except sqlite3.IntegrityError:
            await self.conn.rollback()
            return False

    async def set_subscription_status(self, telegram_id: int, status: str) -> None:
        await self._write(
            "UPDATE participants SET subscription_status = ?, subscription_checked_at = ? "
            "WHERE telegram_id = ?",
            (status, _now(), telegram_id),
        )

    async def list_participants(self, source: str | None = None) -> list[aiosqlite.Row]:
        if source:
            return await self._all(
                "SELECT * FROM participants WHERE source_channel = ? ORDER BY registered_at",
                (source,),
            )
        return await self._all("SELECT * FROM participants ORDER BY source_channel, registered_at")

    # --- stats -----------------------------------------------------------

    async def stats(self) -> dict[str, dict[str, int]]:
        async def grouped(sql: str, params: tuple = ()) -> dict[str, int]:
            return {row[0]: row[1] for row in await self._all(sql, params)}

        day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        return {
            "started": await grouped(
                "SELECT source_channel, COUNT(*) FROM users GROUP BY source_channel"
            ),
            "participants": await grouped(
                "SELECT source_channel, COUNT(*) FROM participants GROUP BY source_channel"
            ),
            "last_24h": await grouped(
                "SELECT source_channel, COUNT(*) FROM participants "
                "WHERE registered_at >= ? GROUP BY source_channel",
                (day_ago,),
            ),
            "unsubscribed": await grouped(
                "SELECT source_channel, COUNT(*) FROM participants "
                "WHERE subscription_status != 'subscribed' GROUP BY source_channel"
            ),
        }
