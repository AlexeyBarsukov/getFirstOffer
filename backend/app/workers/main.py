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
from app.monitoring import TelegramAlerter
from app.notifiers.base import Notifier
from app.notifiers.telegram import TelegramNotifier
from app.sources.portals import Portal
from app.sources.registry import build_portals
from app.workers.catalog import sync_catalog_now, sync_catalogs
from app.workers.fetcher import check_subscription_now, fetch_job
from app.workers.notifier import send_notifications
from app.workers.tracker import track_vacancies

logger = structlog.get_logger(__name__)

# Когда опрашивать площадку (параметры arq.cron)
FETCH_SCHEDULES: dict[Portal, dict[str, Any]] = {
    # Каждую минуту в :00
    Portal.HH: {"second": 0},
    # Каждые 2 минуты в :10 — вакансий на Хабре в десятки раз меньше
    Portal.HABR: {"minute": set(range(0, 60, 2)), "second": 10},
    # Каждые 2 минуты в :40 — один запрос на подписку, лимит API 120 в минуту
    Portal.SUPERJOB: {"minute": set(range(0, 60, 2)), "second": 40},
    # Каждые 5 минут в :50 — лента целиком (10 запросов), вакансий в день — десятки
    Portal.GETMATCH: {"minute": set(range(1, 60, 5)), "second": 50},
    # Раз в 30 минут: API отвечает ~15 секунд на запрос, IT-вакансий там мало
    Portal.TRUDVSEM: {"minute": {3, 33}, "second": 0, "timeout": 20 * 60},
}


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    setup_logging(settings.log_level, json=settings.app_env == "production")
    engine = make_engine(settings.database_url)
    ctx["engine"] = engine
    ctx["session_factory"] = make_session_factory(engine)
    ctx["portals"] = build_portals(settings)

    notifiers: dict[ChannelType, Notifier] = {}
    bot: Bot | None = None
    if settings.telegram_bot_token:
        bot = Bot(settings.telegram_bot_token.get_secret_value())
        ctx["bot"] = bot
        notifiers[ChannelType.TELEGRAM] = TelegramNotifier(bot)
    else:
        logger.warning("telegram_disabled", reason="TELEGRAM_BOT_TOKEN не задан")
    ctx["notifiers"] = notifiers
    ctx["alerter"] = TelegramAlerter(bot, settings.admin_chat_id)


async def shutdown(ctx: dict[str, Any]) -> None:
    for portal in ctx["portals"].values():
        await portal.aclose()
    if bot := ctx.get("bot"):
        await bot.session.close()
    await ctx["engine"].dispose()


class WorkerSettings:
    # keep_result=0: задачи ставятся с фиксированным job_id («check-subscription-<id>»,
    # «send-notifications-now»), и сохранённый результат занимал бы этот id ещё час
    functions: ClassVar[list[Any]] = [
        # Проверка на всех площадках подряд; «Работа России» отвечает медленно
        func(check_subscription_now, keep_result=0, timeout=20 * 60),
        func(send_notifications, keep_result=0),
        func(sync_catalog_now, keep_result=0, timeout=15 * 60),
    ]
    cron_jobs: ClassVar[list[CronJob]] = [
        # Поиск новых — у каждой площадки своё расписание
        *(
            cron(
                fetch_job(portal),
                run_at_startup=True,
                **{"timeout": 5 * 60, **FETCH_SCHEDULES[portal]},
            )
            for portal in get_settings().enabled_portals
            if portal in FETCH_SCHEDULES
        ),
        # Каждую минуту в :30 — страховочная отправка (повторы после ошибок)
        cron(send_notifications, second=30, timeout=5 * 60),
        # Раз в 30 минут — изменения уже найденных вакансий
        cron(track_vacancies, minute={5, 35}, second=15, timeout=20 * 60),
        # Раз в 2 часа — полная выдача для подборок (/catalog)
        cron(sync_catalogs, hour=set(range(0, 24, 2)), minute=20, second=0, timeout=30 * 60),
    ]
    on_startup = startup
    on_shutdown = shutdown
    # Ключ здоровья для `arq ... --check` (healthcheck контейнера)
    health_check_interval = 60
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
