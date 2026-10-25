"""Telegram-бот. Запуск: cd backend && .venv/bin/python -m app.bots.telegram"""

import asyncio
from datetime import UTC, datetime

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand, LinkPreviewOptions
from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from app.bots.telegram.handlers import router
from app.bots.telegram.middlewares import DbSessionMiddleware
from app.config import get_settings
from app.db.session import make_engine, make_session_factory
from app.logging import setup_logging
from app.monitoring import STALE_AFTER, TelegramAlerter, check_heartbeat
from app.services.areas import AreaIndex
from app.sources.hh_api import HHClient

logger = structlog.get_logger(__name__)

COMMANDS = [
    BotCommand(command="list", description="Мои подписки"),
    BotCommand(command="new", description="Новая подписка"),
    BotCommand(command="help", description="Справка"),
    BotCommand(command="cancel", description="Отменить ввод"),
]


class ArqScheduler:
    """Ставит внеочередную проверку подписки в очередь воркера."""

    def __init__(self, arq: ArqRedis) -> None:
        self._arq = arq

    async def schedule_check(self, subscription_id: int) -> None:
        try:
            # Один job_id на подписку: несколько быстрых изменений подряд — одна проверка
            await self._arq.enqueue_job(
                "check_subscription_now",
                subscription_id,
                _job_id=f"check-subscription-{subscription_id}",
            )
        except Exception:
            # Не страшно: плановый цикл воркера проверит подписку в течение минуты
            logger.exception("schedule_check_failed", subscription_id=subscription_id)


async def monitor_worker(arq: ArqRedis, alerter: TelegramAlerter) -> None:
    """Раз в минуту проверяет, что воркер жив. Первая проверка — после STALE_AFTER,
    чтобы воркер успел стартовать вместе с ботом."""
    await asyncio.sleep(STALE_AFTER.total_seconds())
    while True:
        try:
            await check_heartbeat(arq, alerter, datetime.now(UTC))
        except Exception:
            logger.exception("monitor_failed")
        await asyncio.sleep(60)


async def load_areas() -> AreaIndex:
    try:
        async with HHClient.from_settings(get_settings()) as client:
            return AreaIndex(await client.get_areas())
    except Exception:
        logger.exception("areas_load_failed")
        return AreaIndex.fallback()


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, json=settings.app_env == "production")
    if not settings.telegram_bot_token:
        raise SystemExit("Заполни TELEGRAM_BOT_TOKEN в .env")

    bot = Bot(
        settings.telegram_bot_token.get_secret_value(),
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview=LinkPreviewOptions(is_disabled=True),
        ),
    )
    engine = make_engine(settings.database_url)
    arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    dispatcher = Dispatcher(storage=RedisStorage.from_url(settings.redis_url))
    dispatcher.update.outer_middleware(DbSessionMiddleware())
    dispatcher.include_router(router)
    # Доступны в middleware и обработчиках по имени аргумента
    dispatcher["session_factory"] = make_session_factory(engine)
    dispatcher["areas"] = await load_areas()
    dispatcher["scheduler"] = ArqScheduler(arq)

    monitor = asyncio.create_task(monitor_worker(arq, TelegramAlerter(bot, settings.admin_chat_id)))
    try:
        await bot.set_my_commands(COMMANDS)
        logger.info("bot_started")
        await dispatcher.start_polling(bot)
    finally:
        monitor.cancel()
        await dispatcher.storage.close()
        await arq.aclose()
        await engine.dispose()
        await bot.session.close()
