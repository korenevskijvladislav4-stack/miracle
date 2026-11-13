import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    admin_bot_token: str
    admin_ids: frozenset[int]
    db_path: str


def parse_channel_id(raw: str) -> int | str:
    """'@name' остаётся строкой, '-100123' становится числом."""
    raw = raw.strip()
    return int(raw) if raw.lstrip("-").isdigit() else raw


def load_config() -> Config:
    load_dotenv()
    token = os.getenv("ADMIN_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Не задан ADMIN_BOT_TOKEN в .env")
    admin_ids = frozenset(
        int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
    )
    if not admin_ids:
        raise RuntimeError("Не задан ADMIN_IDS в .env")
    return Config(token, admin_ids, os.getenv("DB_PATH", "data/miracle_glow.db"))
