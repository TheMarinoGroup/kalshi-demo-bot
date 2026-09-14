from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from kalshi_pbot.config import Settings
from kalshi_pbot.kalshi_client import (
    HTTP_MAX_ATTEMPTS,
    HTTP_RETRY_CAP_SECONDS,
    KalshiRestClient,
    KalshiWebSocket,
    is_ws_disconnect,
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


def _keepalive_closed() -> ConnectionClosedError:
    return ConnectionClosedError(None, Close(1011, "keepalive ping timeout"))


class _FakeSocket:
    """Looks OPEN until send/read. drop_send / drop_read simulate a dead keepalive."""

    def __init__(self, *, drop_send: bool = False, drop_read: bool = False) -> None:
        self.sent: list[dict] = []
        self.drop_send = drop_send
        self.drop_read = drop_read
        self.close_code = None
        self.closed = False
        self._hold = asyncio.Event()

    async def send(self, data: str) -> None:
        if self.drop_send:
            raise _keepalive_closed()
        self.sent.append(json.loads(data))

    async def close(self) -> None:
        self.closed = True
        self.close_code = 1000
        self._hold.set()

    def __aiter__(self) -> _FakeSocket:
        return self

    async def __anext__(self) -> str:
        if self.drop_read:
            raise _keepalive_closed()
        await self._hold.wait()
        raise StopAsyncIteration


def test_is_ws_disconnect_matches_keepalive_and_send_on_closed() -> None:
    assert is_ws_disconnect(_keepalive_closed())
    assert is_ws_disconnect(RuntimeError("WebSocket is not connected"))
    assert not is_ws_disconnect(ValueError("nope"))


async def test_subscribe_after_reconnect_on_closed_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    live = _FakeSocket()
    connects = {"n": 0}

    async def fake_connect(*args: object, **kwargs: object) -> _FakeSocket:
        del args, kwargs
        connects["n"] += 1
        return live

    ws = KalshiWebSocket(
        Settings(dry_run=True, paper_tape=True, ws_url="wss://example.test/trade-api/ws/v2"),
        private_key=object(),
    )
    ws._auth_headers = lambda: {}  # type: ignore[method-assign]
    ws._ws = _FakeSocket(drop_send=True)
    monkeypatch.setattr("kalshi_pbot.kalshi_client.websockets.connect", fake_connect)

    with pytest.raises(ConnectionClosedError):
        await ws.subscribe(["ticker"])

    await ws.reconnect()
    await ws.subscribe(["ticker"])

    assert connects["n"] == 1
    assert live.sent
    assert live.sent[0]["cmd"] == "subscribe"
    assert live.sent[0]["params"]["channels"] == ["ticker"]
    await ws.close()


async def test_run_forever_reconnects_after_keepalive_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sockets: list[_FakeSocket] = []

    async def fake_connect(*args: object, **kwargs: object) -> _FakeSocket:
        del args, kwargs
        sock = _FakeSocket(drop_read=len(sockets) == 0)
        sockets.append(sock)
        return sock

    ws = KalshiWebSocket(
        Settings(dry_run=True, paper_tape=True, ws_url="wss://example.test/trade-api/ws/v2"),
        private_key=object(),
    )
    ws._auth_headers = lambda: {}  # type: ignore[method-assign]
    monkeypatch.setattr("kalshi_pbot.kalshi_client.websockets.connect", fake_connect)

    subscribed = {"n": 0}

    async def on_connect() -> None:
        subscribed["n"] += 1
        await ws.subscribe(["ticker"])

    ws.add_on_connect(on_connect)
    task = asyncio.create_task(ws.run_forever(), name="test-ws-forever")
    try:
        for _ in range(80):
            if subscribed["n"] >= 2 and len(sockets) >= 2:
                break
            await asyncio.sleep(0.02)
        assert subscribed["n"] >= 2
        assert len(sockets) >= 2
        assert any(sock.sent for sock in sockets)
    finally:
        await ws.close()
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
