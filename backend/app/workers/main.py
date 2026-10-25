"""Воркер arq. Запуск: cd backend && .venv/bin/arq app.workers.main.WorkerSettings"""

from typing import Any, ClassVar

import structlog
from aiogram import Bot
from arq import cron, func
from arq.connections import RedisSettings
from arq.cron import CronJob

from app.config import get_settings
from app.db.models import ChannelType
from app.db.session import make_engine, make_session_factory
from app.logging import setup_logging
from app.notifiers.base import Notifier
from app.notifiers.telegram import TelegramNotifier
from app.sources.hh_api import HHApiSource, HHClient
from app.workers.fetcher import check_subscription_now, fetch_new_vacancies
from app.workers.notifier import send_notifications

logger = structlog.get_logger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    setup_logging(settings.log_level, json=settings.app_env == "production")
    engine = make_engine(settings.database_url)
    hh_client = HHClient.from_settings(settings)
    ctx["engine"] = engine
    ctx["session_factory"] = make_session_factory(engine)
    ctx["hh_client"] = hh_client
    ctx["source"] = HHApiSource(hh_client)

    notifiers: dict[ChannelType, Notifier] = {}
    if settings.telegram_bot_token:
        bot = Bot(settings.telegram_bot_token.get_secret_value())
        ctx["bot"] = bot
        notifiers[ChannelType.TELEGRAM] = TelegramNotifier(bot)
    else:
        logger.warning("telegram_disabled", reason="TELEGRAM_BOT_TOKEN не задан")
    ctx["notifiers"] = notifiers


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["hh_client"].aclose()
    if bot := ctx.get("bot"):
        await bot.session.close()
    await ctx["engine"].dispose()


class WorkerSettings:
    # keep_result=0: задачи ставятся с фиксированным job_id («check-subscription-<id>»,
    # «send-notifications-now»), и сохранённый результат занимал бы этот id ещё час
    functions: ClassVar[list[Any]] = [
        func(check_subscription_now, keep_result=0),
        func(send_notifications, keep_result=0),
    ]
    cron_jobs: ClassVar[list[CronJob]] = [
        # Каждую минуту в :00 — поиск, в :30 — страховочная отправка (повторы после ошибок)
        cron(fetch_new_vacancies, second=0, run_at_startup=True, timeout=5 * 60),
        cron(send_notifications, second=30, timeout=5 * 60),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
