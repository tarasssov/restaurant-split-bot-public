from __future__ import annotations

import asyncio
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from app.bot import setup_dispatcher
from app.config import load_config

log = logging.getLogger("webhook")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


async def run_webhook() -> None:
    cfg = load_config()
    bot = Bot(cfg.bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    setup_dispatcher(dp)

    host = _env("WEBHOOK_HOST", "127.0.0.1")
    port = int(_env("WEBHOOK_PORT", "8080"))
    path = _env("WEBHOOK_PATH", "/telegram/webhook")
    base_url = _env("WEBHOOK_BASE_URL")
    secret = _env("WEBHOOK_SECRET")

    if not base_url:
        raise RuntimeError("WEBHOOK_BASE_URL is not set (example: https://bot.example.com)")
    if not path.startswith("/"):
        raise RuntimeError("WEBHOOK_PATH must start with '/'")

    url = f"{base_url}{path}"

    async def on_startup(bot_obj: Bot) -> None:
        await bot_obj.set_webhook(url=url, secret_token=secret or None, drop_pending_updates=True)
        log.info("Webhook is set to %s", url)

    async def on_shutdown(bot_obj: Bot) -> None:
        await bot_obj.delete_webhook(drop_pending_updates=False)
        log.info("Webhook is deleted")

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    handler = SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret or None)
    handler.register(app, path=path)
    setup_application(app, dp, bot=bot)

    log.info("BOT STARTED: webhook on %s:%s%s", host, port, path)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    await asyncio.Event().wait()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_webhook())


if __name__ == "__main__":
    main()
