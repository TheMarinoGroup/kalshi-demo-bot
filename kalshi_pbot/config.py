"""Environment and Risk Desk v1 configuration.

Two planes:

* **Data** — public prod REST (no key) for events/books. Research default.
* **Orders** — demo only. Paper-tape / matcher never POST. ``--demo-submit``
  is the only path that may hit ``POST /portfolio/events/orders``.

Production *order* hosts stay refused unless ``KALSHI_ALLOW_PRODUCTION=1``.
Read-only prod WebSocket is opt-in via ``KALSHI_ALLOW_PROD_WS=1``.
"""

from __future__ import annotations

from decimal import Decimal
from functools import cached_property
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from kalshi_pbot.types import D, QuoteMode

DEMO_REST = "https://external-api.demo.kalshi.co/trade-api/v2"
DEMO_WS = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
PROD_REST = "https://external-api.kalshi.com/trade-api/v2"
PROD_WS = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"

# Risk Desk v1 — locked fractions / dollars at the $1000 reference bankroll.
REF_BANKROLL = Decimal("1000")
OPEN_NOTIONAL_FRAC = Decimal("0.05")  # $50 at $1000
DAILY_LOSS_FRAC = Decimal("0.02")  # $20 at $1000
ONESIDED_FRAC = Decimal("0.03")  # $30 at $1000
MAX_CONCURRENT_WINDOWS = 2
LAST_SECONDS_NO_RISK = 60
CLIP_MIN = Decimal("10")
CLIP_MAX = Decimal("30")
DEFAULT_CLIP = Decimal("20")
DEFAULT_SERIES = ("KXBTC15M", "KXETH15M")
LATENCY_BUCKETS = (50, 150, 500)
PROD_HOSTS = (
    "external-api.kalshi.com",
    "external-api-ws.kalshi.com",
    "api.elections.kalshi.com",
)
CFB_INDEX = {"KXBTC15M": "BRTI", "KXETH15M": "ETHUSD_RTI"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KALSHI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["demo", "production"] = "demo"
    allow_production: bool = False
    allow_prod_ws: bool = False
    rest_base: str | None = None  # legacy alias for data_rest
    data_rest: str | None = None
    order_rest: str | None = None
    ws_url: str | None = None
    ws_env: Literal["demo", "production"] = "demo"

    api_key_id: str = ""
    private_key_path: str = ""
    private_key: str = ""

    bankroll: Decimal = REF_BANKROLL
    dry_run: bool = True
    paper_tape: bool = True
    mock: bool = False

    series: str = ",".join(DEFAULT_SERIES)
    clip_dollars: Decimal = DEFAULT_CLIP
    quote_mode: QuoteMode = "one_sided"
    min_edge: Decimal = Decimal("0.02")
    taker_pair_arb: bool = False
    tick_size: Decimal = Decimal("0.01")
    improve_ticks: int = 0
    latency_ms: int = 150
    loop_seconds: float = 0.05
    discover_seconds: float = 15.0
    tob_heartbeat_ms: int = 100
    last_seconds: int = LAST_SECONDS_NO_RISK
    max_windows: int = MAX_CONCURRENT_WINDOWS
    cfb_5hz: bool = False
    windows_path: str = "data/windows.json"
    tape_path: str = "data/tape.jsonl"

    log_level: str = "INFO"
    log_json: bool = False

    http_timeout: float = 15.0

    @field_validator("bankroll", "clip_dollars", "min_edge", "tick_size", mode="before")
    @classmethod
    def _decimalize(cls, value: object) -> Decimal:
        return D(value)

    @field_validator("series", mode="before")
    @classmethod
    def _series_str(cls, value: object) -> str:
        if isinstance(value, (list, tuple)):
            return ",".join(str(v) for v in value)
        return str(value)

    @model_validator(mode="after")
    def _safety(self) -> Settings:
        if self.bankroll <= 0:
            raise ValueError("bankroll must be positive")
        if self.latency_ms not in LATENCY_BUCKETS:
            raise ValueError(f"latency_ms must be one of {LATENCY_BUCKETS}")
        if self.env == "production" and not self.allow_production:
            raise ValueError(
                "Production *trading* is disabled. Public prod REST is the data "
                "plane default. Set KALSHI_ALLOW_PRODUCTION=1 only for live risk."
            )
        if self.order_rest:
            self._assert_order_url_safe(self.order_rest)
        if self.ws_url:
            self._assert_ws_url_safe(self.ws_url)
        if self.ws_env == "production" and not (self.allow_prod_ws or self.allow_production):
            raise ValueError("Read-only prod WebSocket requires KALSHI_ALLOW_PROD_WS=1")
        return self

    @cached_property
    def series_tickers(self) -> tuple[str, ...]:
        items = [s.strip().upper() for s in self.series.split(",") if s.strip()]
        return tuple(items) or DEFAULT_SERIES

    @cached_property
    def clip(self) -> Decimal:
        clip = self.clip_dollars
        if clip < CLIP_MIN:
            return CLIP_MIN
        if clip > CLIP_MAX:
            return CLIP_MAX
        return clip

    @cached_property
    def max_open_notional(self) -> Decimal:
        return (self.bankroll * OPEN_NOTIONAL_FRAC).quantize(Decimal("0.01"))

    @cached_property
    def daily_loss_limit(self) -> Decimal:
        return (self.bankroll * DAILY_LOSS_FRAC).quantize(Decimal("0.01"))

    @cached_property
    def max_onesided(self) -> Decimal:
        return (self.bankroll * ONESIDED_FRAC).quantize(Decimal("0.01"))

    @cached_property
    def resolved_data_rest(self) -> str:
        raw = self.data_rest or self.rest_base or PROD_REST
        return raw.rstrip("/")

    @cached_property
    def resolved_order_rest(self) -> str:
        raw = (self.order_rest or DEMO_REST).rstrip("/")
        self._assert_order_url_safe(raw)
        return raw

    @cached_property
    def resolved_rest_base(self) -> str:
        """Data-plane REST (public). Order POST uses resolved_order_rest."""
        return self.resolved_data_rest

    @cached_property
    def resolved_ws_url(self) -> str:
        if self.ws_url:
            self._assert_ws_url_safe(self.ws_url)
            return self.ws_url
        if self.ws_env == "production":
            return PROD_WS
        return DEMO_WS

    @property
    def live_submit(self) -> bool:
        return (not self.dry_run) and (not self.paper_tape)

    def _is_prod_host(self, url: str) -> bool:
        lowered = url.lower()
        return any(host in lowered for host in PROD_HOSTS)

    def _assert_order_url_safe(self, url: str) -> None:
        if self._is_prod_host(url) and not self.allow_production:
            raise ValueError(
                f"Refusing production order host {url!r} without KALSHI_ALLOW_PRODUCTION=1"
            )

    def _assert_ws_url_safe(self, url: str) -> None:
        if self._is_prod_host(url) and not (self.allow_prod_ws or self.allow_production):
            raise ValueError(
                f"Refusing production WebSocket {url!r} without KALSHI_ALLOW_PROD_WS=1"
            )

    def has_credentials(self) -> bool:
        return bool(self.api_key_id and (self.private_key_path or self.private_key))

    def cfb_index_ids(self) -> list[str]:
        ids: list[str] = []
        for series in self.series_tickers:
            idx = CFB_INDEX.get(series)
            if idx and idx not in ids:
                ids.append(idx)
        return ids or ["BRTI", "ETHUSD_RTI"]


def load_settings(**overrides: object) -> Settings:
    return Settings(**overrides)
