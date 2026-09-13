"""Kalshi demo REST + WebSocket client (V2 Trade API).

Auth is RSA-PSS over `timestamp + METHOD + path` (path from URL root,
no query string). WebSocket handshake signs GET /trade-api/ws/v2.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
import structlog
import websockets
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from kalshi_pbot.config import Settings
from kalshi_pbot.types import D, MarketWindow, Outcome, SeriesMeta

log = structlog.get_logger(__name__)

WS_SIGN_PATH = "/trade-api/ws/v2"
JsonDict = dict[str, Any]
WsHandler = Callable[[JsonDict], Awaitable[None] | None]


def load_private_key(settings: Settings) -> Any:
    if settings.private_key:
        pem = settings.private_key.encode("utf-8")
    elif settings.private_key_path:
        pem = Path(settings.private_key_path).read_bytes()
    else:
        raise RuntimeError("No Kalshi private key configured")
    return serialization.load_pem_private_key(pem, password=None, backend=default_backend())


def sign_request(private_key: Any, timestamp: str, method: str, path: str) -> str:
    path_without_query = path.split("?", 1)[0]
    message = f"{timestamp}{method}{path_without_query}".encode()
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def _parse_dt(value: str | None) -> datetime:
    if not value:
        raise ValueError("missing timestamp")
    # Kalshi emits RFC3339; Python 3.11 accepts offset, not always trailing Z.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_dt_opt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return _parse_dt(str(value))
    except ValueError:
        return None


def _parse_result(raw: JsonDict) -> Outcome | None:
    value = str(raw.get("result") or raw.get("settlement_result") or "").lower()
    if value == "yes":
        return Outcome.YES
    if value == "no":
        return Outcome.NO
    return None


def market_from_api(
    raw: JsonDict, series_ticker: str, series: SeriesMeta | None = None
) -> MarketWindow:
    status = str(raw.get("status") or "")
    return MarketWindow(
        ticker=raw["ticker"],
        event_ticker=raw.get("event_ticker") or raw["ticker"],
        series_ticker=raw.get("series_ticker") or series_ticker,
        title=raw.get("title") or raw.get("yes_sub_title") or raw["ticker"],
        status=status,
        open_time=_parse_dt(raw.get("open_time")),
        close_time=_parse_dt(raw.get("close_time")),
        yes_sub_title=raw.get("yes_sub_title") or "",
        no_sub_title=raw.get("no_sub_title") or "",
        fee_type=(series.fee_type if series else raw.get("fee_type") or "quadratic"),
        fee_multiplier=D(series.fee_multiplier if series else raw.get("fee_multiplier") or 1),
        expected_expiration=_parse_dt_opt(
            raw.get("expected_expiration_time") or raw.get("expected_expiration")
        ),
        settlement_ts=_parse_dt_opt(raw.get("settlement_ts") or raw.get("settled_time")),
        result=_parse_result(raw),
    )


class MarketSource(Protocol):
    def get_series(self, series_ticker: str) -> SeriesMeta: ...
    def list_open_markets(self, series_ticker: str) -> list[MarketWindow]: ...
    def list_events(self, series_ticker: str, status: str) -> list[MarketWindow]: ...
    def get_orderbook(self, ticker: str) -> JsonDict: ...


class KalshiRestClient:
    def __init__(
        self,
        settings: Settings,
        *,
        base_url: str | None = None,
        private_key: Any | None = None,
        purpose: str = "data",
    ) -> None:
        self.settings = settings
        self.base_url = (base_url or settings.resolved_data_rest).rstrip("/")
        self.purpose = purpose
        self._key = private_key
        self._http = httpx.Client(timeout=settings.http_timeout)

    def close(self) -> None:
        self._http.close()

    def _ensure_key(self) -> Any:
        if self._key is None:
            self._key = load_private_key(self.settings)
        return self._key

    def _headers(self, method: str, url: str, authenticated: bool) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if not authenticated:
            return headers
        timestamp = str(int(time.time() * 1000))
        sign_path = urlparse(url).path
        signature = sign_request(self._ensure_key(), timestamp, method, sign_path)
        headers.update(
            {
                "KALSHI-ACCESS-KEY": self.settings.api_key_id,
                "KALSHI-ACCESS-SIGNATURE": signature,
                "KALSHI-ACCESS-TIMESTAMP": timestamp,
            }
        )
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: JsonDict | None = None,
        authenticated: bool = True,
    ) -> httpx.Response:
        url = self.base_url + path
        headers = self._headers(method, url, authenticated)
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        response = self._http.request(
            method, url, params=params, json=json_body, headers=headers
        )
        if response.status_code >= 400:
            log.warning(
                "kalshi_http_error",
                method=method,
                path=path,
                status=response.status_code,
                body=response.text[:500],
            )
        return response

    def get_json(self, path: str, **kwargs: Any) -> JsonDict:
        response = self.request("GET", path, **kwargs)
        response.raise_for_status()
        return response.json()

    def get_exchange_status(self) -> JsonDict:
        return self.get_json("/exchange/status", authenticated=False)

    def get_series(self, series_ticker: str) -> SeriesMeta:
        data = self.get_json(f"/series/{series_ticker}", authenticated=False)
        series = data.get("series") or data
        return SeriesMeta(
            ticker=series.get("ticker") or series_ticker,
            fee_type=series.get("fee_type") or "quadratic",
            fee_multiplier=D(series.get("fee_multiplier") or 1),
            title=series.get("title") or "",
        )

    def list_events(self, series_ticker: str, status: str) -> list[MarketWindow]:
        """Events-first discovery. Rollover uses status=unopened, not markets."""
        series = self.get_series(series_ticker)
        markets: list[MarketWindow] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {
                "series_ticker": series_ticker,
                "status": status,
                "with_nested_markets": "true",
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor
            data = self.get_json("/events", params=params, authenticated=False)
            for event in data.get("events") or []:
                event_ticker = str(event.get("event_ticker") or "")
                nested = event.get("markets") or []
                if not nested:
                    continue
                for raw in nested:
                    raw.setdefault("event_ticker", event_ticker)
                    raw.setdefault("series_ticker", series_ticker)
                    try:
                        markets.append(market_from_api(raw, series_ticker, series))
                    except (KeyError, ValueError) as exc:
                        log.warning("skip_event_market", ticker=raw.get("ticker"), error=str(exc))
            cursor = data.get("cursor") or None
            if not cursor:
                break
        markets.sort(key=lambda m: m.open_time)
        return markets

    def list_open_markets(self, series_ticker: str) -> list[MarketWindow]:
        # Event status=open is often the *previous* window (determined).
        # The live clip is nested under status=unopened with market status=active.
        now = datetime.now(UTC)
        found = self.list_events(series_ticker, "open") + self.list_events(
            series_ticker, "unopened"
        )
        live = [m for m in found if _is_live_window(m, now)]
        live.sort(key=lambda m: m.close_time)
        return live

    def get_orderbook(self, ticker: str) -> JsonDict:
        return self.get_json(f"/markets/{ticker}/orderbook", authenticated=False)

    def get_balance(self) -> JsonDict:
        return self.get_json("/portfolio/balance")

    def get_positions(self) -> JsonDict:
        return self.get_json("/portfolio/positions")

    def get_fills(self, limit: int = 100) -> JsonDict:
        return self.get_json("/portfolio/fills", params={"limit": limit})

    def get_resting_orders(self) -> JsonDict:
        return self.get_json("/portfolio/orders", params={"status": "resting"})

    def create_order(self, body: JsonDict) -> httpx.Response:
        if self.settings.paper_tape or self.settings.dry_run:
            raise RuntimeError(
                "paper-tape / dry-run: POST /portfolio/events/orders is disabled"
            )
        if self.purpose != "order":
            raise RuntimeError("create_order must use the demo order REST client")
        return self.request("POST", "/portfolio/events/orders", json_body=body)

    def cancel_order(self, order_id: str, market_ticker: str) -> httpx.Response:
        return self.request(
            "DELETE",
            f"/portfolio/events/orders/{order_id}",
            params={"market_ticker": market_ticker},
        )

    def cancel_all_orders(self) -> httpx.Response:
        return self.request("DELETE", "/portfolio/events/orders")


_DEAD_STATUSES = {"closed", "settled", "finalized", "determined"}


def _is_live_window(market: MarketWindow, now: datetime) -> bool:
    if market.status in _DEAD_STATUSES:
        return False
    return market.open_time <= now < market.close_time


class KalshiWebSocket:
    def __init__(self, settings: Settings, *, private_key: Any | None = None) -> None:
        self.settings = settings
        self._key = private_key
        self._ws: Any = None
        self._msg_id = 1
        self._handlers: list[WsHandler] = []
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def add_handler(self, handler: WsHandler) -> None:
        self._handlers.append(handler)

    def _auth_headers(self) -> dict[str, str]:
        if self._key is None:
            self._key = load_private_key(self.settings)
        timestamp = str(int(time.time() * 1000))
        signature = sign_request(self._key, timestamp, "GET", WS_SIGN_PATH)
        return {
            "KALSHI-ACCESS-KEY": self.settings.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
        }

    async def connect(self) -> None:
        headers = self._auth_headers()
        self._ws = await websockets.connect(
            self.settings.resolved_ws_url,
            additional_headers=headers,
        )
        log.info("ws_connected", url=self.settings.resolved_ws_url)
        self._stop.clear()
        self._task = asyncio.create_task(self._read_loop(), name="kalshi-ws")

    async def close(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def subscribe(
        self,
        channels: list[str],
        *,
        market_tickers: list[str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        params: dict[str, Any] = {"channels": channels}
        if market_tickers:
            params["market_tickers"] = market_tickers
        if extra:
            params.update(extra)
        await self._send({"id": self._next_id(), "cmd": "subscribe", "params": params})

    async def update_subscription(
        self,
        sid: int,
        action: str,
        market_tickers: list[str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        params: dict[str, Any] = {"sids": [sid], "action": action}
        if market_tickers:
            params["market_tickers"] = market_tickers
        if extra:
            params.update(extra)
        await self._send({"id": self._next_id(), "cmd": "update_subscription", "params": params})

    async def _send(self, payload: JsonDict) -> None:
        if self._ws is None:
            raise RuntimeError("WebSocket is not connected")
        await self._ws.send(json.dumps(payload))

    def _next_id(self) -> int:
        mid = self._msg_id
        self._msg_id += 1
        return mid

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("ws_bad_json", raw=str(raw)[:200])
                    continue
                for handler in self._handlers:
                    result = handler(msg)
                    if asyncio.iscoroutine(result):
                        await result
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("ws_read_error")

    async def run_forever(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            try:
                await self.connect()
                delay = 1.0
                if self._task:
                    await self._task
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ws_reconnect", delay=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)


class KalshiClient:
    """Facade: public prod data REST + optional auth WS + demo order REST."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        key = load_private_key(settings) if settings.has_credentials() else None
        self.data = KalshiRestClient(
            settings, base_url=settings.resolved_data_rest, purpose="data"
        )
        self.rest = KalshiRestClient(
            settings,
            base_url=settings.resolved_order_rest,
            private_key=key,
            purpose="order",
        )
        self.ws = KalshiWebSocket(settings, private_key=key) if key is not None else None

    def close(self) -> None:
        self.data.close()
        self.rest.close()

    def get_series(self, series_ticker: str) -> SeriesMeta:
        return self.data.get_series(series_ticker)

    def list_open_markets(self, series_ticker: str) -> list[MarketWindow]:
        return self.data.list_open_markets(series_ticker)

    def list_events(self, series_ticker: str, status: str) -> list[MarketWindow]:
        return self.data.list_events(series_ticker, status)

    def get_orderbook(self, ticker: str) -> JsonDict:
        return self.data.get_orderbook(ticker)


class MockKalshiClient:
    """In-process market source so the bot can run without demo credentials."""

    def __init__(self, settings: Settings, *, now: datetime | None = None) -> None:
        from datetime import timedelta

        self.settings = settings
        self.ws = None
        now = now or datetime.now(UTC)
        self._markets: dict[str, MarketWindow] = {}
        for series in settings.series_tickers:
            ticker = f"{series}-MOCK"
            self._markets[series] = MarketWindow(
                ticker=ticker,
                event_ticker=ticker,
                series_ticker=series,
                title=f"{series} mock 15m Up/Down",
                status="active",
                open_time=now - timedelta(minutes=5),
                close_time=now + timedelta(minutes=10),
                fee_type="quadratic",
                fee_multiplier=Decimal("1"),
            )
        self._books: dict[str, JsonDict] = {
            m.ticker: {
                "orderbook_fp": {
                    "yes_dollars": [["0.4700", "80.00"], ["0.4800", "40.00"]],
                    "no_dollars": [["0.4700", "70.00"], ["0.4900", "25.00"]],
                }
            }
            for m in self._markets.values()
        }

    def close(self) -> None:
        return None

    def get_series(self, series_ticker: str) -> SeriesMeta:
        return SeriesMeta(ticker=series_ticker, fee_type="quadratic", title=f"{series_ticker} mock")

    def list_open_markets(self, series_ticker: str) -> list[MarketWindow]:
        market = self._markets.get(series_ticker)
        return [market] if market else []

    def list_events(self, series_ticker: str, status: str) -> list[MarketWindow]:
        from datetime import timedelta

        if status == "open":
            return self.list_open_markets(series_ticker)
        if status != "unopened":
            return []
        now = datetime.now(UTC)
        nxt = MarketWindow(
            ticker=f"{series_ticker}-MOCK-NEXT",
            event_ticker=f"{series_ticker}-MOCK-NEXT",
            series_ticker=series_ticker,
            title=f"{series_ticker} mock next",
            status="initialized",
            open_time=now + timedelta(minutes=10),
            close_time=now + timedelta(minutes=25),
        )
        return [nxt]

    def get_orderbook(self, ticker: str) -> JsonDict:
        return self._books.get(
            ticker,
            {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}},
        )
