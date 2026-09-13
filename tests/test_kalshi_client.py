from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from kalshi_pbot.config import Settings
from kalshi_pbot.kalshi_client import (
    HTTP_MAX_ATTEMPTS,
    HTTP_RETRY_CAP_SECONDS,
    KalshiRestClient,
    parse_retry_after,
    retry_delay_seconds,
)
from kalshi_pbot.market_data import MarketUniverse


def _settings() -> Settings:
    return Settings(
        dry_run=True,
        paper_tape=True,
        data_rest="https://api.test/trade-api/v2",
        discover_series_delay=0,
    )


def _client(handler, sleeps: list[float] | None = None) -> KalshiRestClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return KalshiRestClient(
        _settings(),
        base_url="https://api.test/trade-api/v2",
        http=http,
        sleep=(sleeps.append if sleeps is not None else (lambda _: None)),
    )


def test_parse_retry_after_seconds() -> None:
    response = httpx.Response(429, headers={"Retry-After": "3"})
    assert parse_retry_after(response) == 3.0


def test_parse_retry_after_http_date() -> None:
    when = datetime.now(UTC) + timedelta(seconds=12)
    response = httpx.Response(429, headers={"Retry-After": format_datetime(when, usegmt=True)})
    delay = parse_retry_after(response)
    assert delay is not None
    assert 10 <= delay <= 13


def test_retry_delay_honors_retry_after_and_cap() -> None:
    assert retry_delay_seconds(0, 7.0) == 7.0
    assert retry_delay_seconds(0, 999.0) == HTTP_RETRY_CAP_SECONDS
    delay = retry_delay_seconds(0, None)
    assert 1.0 <= delay <= 2.0


def test_get_json_retries_429_then_succeeds() -> None:
    hits = {"n": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        assert request.url.path.endswith("/events")
        assert "KALSHI-ACCESS-KEY" not in request.headers
        if hits["n"] == 1:
            return httpx.Response(
                429,
                json={"error": "too_many_requests"},
                headers={"Retry-After": "2"},
            )
        return httpx.Response(200, json={"events": [], "cursor": None})

    client = _client(handler, sleeps)
    try:
        data = client.get_json("/events", params={"status": "open"}, authenticated=False)
    finally:
        client.close()

    assert data == {"events": [], "cursor": None}
    assert hits["n"] == 2
    assert sleeps == [2.0]


def test_get_json_retries_503_then_succeeds() -> None:
    hits = {"n": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        hits["n"] += 1
        if hits["n"] == 1:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json={"ok": True})

    client = _client(handler, sleeps)
    try:
        data = client.get_json("/exchange/status", authenticated=False)
    finally:
        client.close()

    assert data == {"ok": True}
    assert hits["n"] == 2
    assert len(sleeps) == 1
    assert 1.0 <= sleeps[0] <= 2.0


def test_get_json_exhausted_429_raises() -> None:
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        hits["n"] += 1
        return httpx.Response(429, json={"error": "too_many_requests"})

    client = _client(handler)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            client.get_json("/events", authenticated=False)
    finally:
        client.close()

    assert hits["n"] == HTTP_MAX_ATTEMPTS


def test_universe_refresh_survives_exhausted_429() -> None:
    """HUD/bot startup must not crash if discovery still 429s after retries."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(429, json={"error": "too_many_requests"})

    rest = _client(handler)
    settings = Settings(
        dry_run=True,
        paper_tape=True,
        series="KXBTC15M",
        discover_series_delay=0,
        data_rest="https://api.test/trade-api/v2",
    )
    try:
        chosen = MarketUniverse(settings, rest).refresh()
    finally:
        rest.close()
    assert chosen == []
