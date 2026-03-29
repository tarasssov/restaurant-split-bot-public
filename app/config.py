import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    bot_token: str
    tip_percent: float


def load_config() -> Config:
    # Явно читаем .env из корня проекта
    load_dotenv(".env")

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is not set in .env")

    tip_percent = float(os.getenv("TIP_PERCENT", "10"))

    return Config(bot_token=token, tip_percent=tip_percent)
