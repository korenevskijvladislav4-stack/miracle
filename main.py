import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from bot.admin import build_admin_router
from bot.config import load_config
from bot.db import Database
from bot.manager import BotManager
from bot.texts import DEFAULT_LANGUAGES, Texts


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()

    db = Database(cfg.db_path)
    await db.connect(DEFAULT_LANGUAGES)
    texts = Texts(db)
    await texts.load()

    manager = BotManager(db, texts)
    await manager.start_all()

    admin_bot = Bot(cfg.admin_bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(build_admin_router(cfg))
    try:
        await dp.start_polling(admin_bot, db=db, texts=texts, manager=manager)
    finally:
        await manager.stop_all()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
