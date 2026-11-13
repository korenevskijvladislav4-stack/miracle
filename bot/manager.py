import asyncio
import contextlib
import logging
from dataclasses import dataclass

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from .config import parse_channel_id
from .db import Database
from .handlers import build_user_router
from .texts import Texts

log = logging.getLogger(__name__)


@dataclass
class RunningBot:
    bot: Bot
    dispatcher: Dispatcher
    task: asyncio.Task


class BotManager:
    """Запускает и останавливает ботов розыгрыша на лету (без перезапуска процесса)."""

    def __init__(self, db: Database, texts: Texts):
        self.db = db
        self.texts = texts
        self.running: dict[str, RunningBot] = {}

    def bot(self, source: str) -> Bot | None:
        item = self.running.get(source)
        return item.bot if item else None

    def is_running(self, source: str) -> bool:
        item = self.running.get(source)
        return bool(item) and not item.task.done()

    async def start(self, row) -> None:
        source = row["source"]
        await self.stop(source)
        bot = Bot(row["token"], default=DefaultBotProperties(parse_mode="HTML"))
        dp = Dispatcher()
        dp.include_router(build_user_router())
        task = asyncio.create_task(
            dp.start_polling(
                bot,
                handle_signals=False,
                db=self.db,
                texts=self.texts,
                source=source,
                channel_id=parse_channel_id(row["channel_id"]),
                channel_url=row["channel_url"],
            ),
            name=f"bot-{source}",
        )
        task.add_done_callback(lambda t: self._on_done(source, t))
        self.running[source] = RunningBot(bot, dp, task)
        log.info("bot %s started", source)

    @staticmethod
    def _on_done(source: str, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            log.error("bot %s stopped with error: %r", source, task.exception())

    async def stop(self, source: str) -> None:
        item = self.running.pop(source, None)
        if not item:
            return
        try:
            await item.dispatcher.stop_polling()
        except RuntimeError:  # поллинг ещё не начался или уже упал
            item.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await item.task
        await item.bot.session.close()
        log.info("bot %s stopped", source)

    async def start_all(self) -> None:
        for row in await self.db.list_bots():
            if row["is_active"]:
                await self.start(row)

    async def stop_all(self) -> None:
        for source in list(self.running):
            await self.stop(source)
