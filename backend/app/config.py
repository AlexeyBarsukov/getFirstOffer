from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.sources.portals import Portal

# .env лежит в корне репозитория (на уровень выше backend/)
ROOT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["local", "test", "production"] = "local"
    log_level: str = "INFO"

    database_url: str = (
        "postgresql+asyncpg://getfirstoffer:getfirstoffer@localhost:5433/getfirstoffer"
    )
    redis_url: str = "redis://localhost:6380/0"

    hh_api_base_url: str = "https://api.hh.ru"
    # User-Agent для всех площадок (hh.ru требует его с контактом)
    hh_user_agent: str = Field(min_length=1)
    # Client ID / Secret с dev.hh.ru — нужны только чтобы один раз получить hh_app_token
    hh_client_id: str | None = None
    hh_client_secret: SecretStr | None = None
    hh_app_token: SecretStr | None = None
    # Минимальный интервал между запросами к api.hh.ru, секунды
    hh_min_request_interval: float = 0.5
    parser_enabled: bool = False
    # Источник вакансий: auto — API, а при 403 сайт (RSS + страница поиска);
    # api — только API; rss — только сайт
    hh_source: Literal["auto", "api", "rss"] = "auto"
    # Площадки через запятую (app.sources.portals.Portal): hh,habr,superjob,...
    portals: str = "hh"

    # Ключ приложения SuperJob (Secret key на api.superjob.ru); без него SuperJob выключен
    superjob_api_key: SecretStr | None = None

    telegram_bot_token: SecretStr | None = None
    # Куда слать алерты о проблемах сервиса (chat_id в Telegram)
    admin_chat_id: int | None = None

    smtp_host: str = "smtp.yandex.ru"
    smtp_port: int = 465
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None

    jwt_secret: SecretStr = SecretStr("change-me")

    # --- Часть 2: черновики откликов (2part.md) ---
    drafts_enabled: bool = False
    # Кому слать черновики (chat_id в Telegram); пусто — ADMIN_CHAT_ID.
    # Профиль кандидата один, поэтому и получатель один
    drafts_chat_id: int | None = None
    # Карточки с оценкой ниже не присылаем (они видны в /drafts как отсеянные)
    drafts_min_score: int = 50
    # Профиль кандидата; пусто — candidate.toml в корне репозитория
    candidate_profile_path: Path | None = None
    # Ollama: на Windows из контейнера — http://host.docker.internal:11434
    llm_base_url: str = "http://localhost:11434"
    llm_model: str = "qwen3:8b"
    llm_temperature: float = 0.7

    @property
    def drafts_recipient(self) -> int | None:
        return self.drafts_chat_id or self.admin_chat_id

    @property
    def enabled_portals(self) -> list[Portal]:
        return [Portal(p.strip()) for p in self.portals.split(",") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
