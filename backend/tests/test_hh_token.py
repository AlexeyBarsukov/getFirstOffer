from urllib.parse import parse_qs

import httpx
import pytest

from app.sources.hh.token import fetch_app_token


async def test_fetch_app_token_uses_client_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/token"
        assert parse_qs(request.content.decode()) == {
            "grant_type": ["client_credentials"],
            "client_id": ["id"],
            "client_secret": ["secret"],
        }
        return httpx.Response(200, json={"access_token": "APPTOKEN", "token_type": "bearer"})

    token = await fetch_app_token("id", "secret", "ua", transport=httpx.MockTransport(handler))

    assert token == "APPTOKEN"


async def test_fetch_app_token_raises_on_error() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(400, json={"error": "invalid_client"}))

    with pytest.raises(RuntimeError, match="400"):
        await fetch_app_token("id", "bad", "ua", transport=transport)
