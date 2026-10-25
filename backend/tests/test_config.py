import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HH_USER_AGENT", "getFirstOffer/0.1 (test@example.com)")
    monkeypatch.setenv("PARSER_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")

    settings = Settings(_env_file=None)

    assert settings.hh_user_agent == "getFirstOffer/0.1 (test@example.com)"
    assert settings.parser_enabled is True
    assert settings.telegram_bot_token is not None
    assert settings.telegram_bot_token.get_secret_value() == "123:abc"
    assert settings.hh_app_token is None


def test_hh_user_agent_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HH_USER_AGENT", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
