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
from app.sources.auto import AutoSource
from app.sources.base import FullSearchSource
from app.sources.details import (
    ApiDetailsSource,
    AutoDetailsSource,
    PageDetailsSource,
    VacancyDetailsSource,
)
from app.sources.hh_api import HHApiSource, HHClient
from app.sources.hh_rss import HHRssSource
from app.sources.hh_search_page import HHSearchPageSource
from app.sources.site import SiteSource
from app.workers.catalog import sync_catalog_now, sync_catalogs
from app.workers.fetcher import check_subscription_now, fetch_new_vacancies
from app.workers.notifier import send_notifications
from app.workers.tracker import track_vacancies

logger = structlog.get_logger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    setup_logging(settings.log_level, json=settings.app_env == "production")
    engine = make_engine(settings.database_url)
    hh_client = HHClient.from_settings(settings)
    rss = HHRssSource.from_settings(settings)
    search_page = HHSearchPageSource(settings.hh_user_agent)
    ctx["engine"] = engine
    ctx["session_factory"] = make_session_factory(engine)
    ctx["hh_client"] = hh_client
    ctx["rss"] = rss
    ctx["search_page"] = search_page
    api = HHApiSource(hh_client)
    site = SiteSource(rss, search_page)
    sources: dict[str, FullSearchSource] = {"api": api, "rss": site, "auto": AutoSource(api, site)}
    ctx["source"] = sources[settings.hh_source]
    logger.info("vacancy_source", source=settings.hh_source)

    # Для трекера изменений: API, а при 403 — страница вакансии (RSS тут не помогает)
    page = PageDetailsSource(settings.hh_user_agent)
    ctx["page_source"] = page
    api_details = ApiDetailsSource(hh_client)
    details_sources: dict[str, VacancyDetailsSource] = {
        "api": api_details,
        "rss": page,
        "auto": AutoDetailsSource(api_details, page),
    }
    ctx["details_source"] = details_sources[settings.hh_source]

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
    await ctx["hh_client"].aclose()
    await ctx["rss"].aclose()
    await ctx["search_page"].aclose()
    await ctx["page_source"].aclose()
    if bot := ctx.get("bot"):
        await bot.session.close()
    await ctx["engine"].dispose()


class WorkerSettings:
    # keep_result=0: задачи ставятся с фиксированным job_id («check-subscription-<id>»,
    # «send-notifications-now»), и сохранённый результат занимал бы этот id ещё час
    functions: ClassVar[list[Any]] = [
        func(check_subscription_now, keep_result=0),
        func(send_notifications, keep_result=0),
        func(sync_catalog_now, keep_result=0, timeout=15 * 60),
    ]
    cron_jobs: ClassVar[list[CronJob]] = [
        # Каждую минуту в :00 — поиск, в :30 — страховочная отправка (повторы после ошибок)
        cron(fetch_new_vacancies, second=0, run_at_startup=True, timeout=5 * 60),
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
