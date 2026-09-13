"""Environment and Risk Desk v1 configuration.

Demo/paper is the only default. Production URLs are refused unless an
operator sets both KALSHI_ENV=production and KALSHI_ALLOW_PRODUCTION=1.
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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KALSHI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["demo", "production"] = "demo"
    allow_production: bool = False
    rest_base: str | None = None
    ws_url: str | None = None

    api_key_id: str = ""
    private_key_path: str = ""
    private_key: str = ""

    bankroll: Decimal = REF_BANKROLL
    dry_run: bool = True
    mock: bool = False

    series: str = ",".join(DEFAULT_SERIES)
    clip_dollars: Decimal = DEFAULT_CLIP
    quote_mode: QuoteMode = "one_sided"
    min_edge: Decimal = Decimal("0.02")
    taker_pair_arb: bool = False
    tick_size: Decimal = Decimal("0.01")
    improve_ticks: int = 0
    loop_seconds: float = 1.0
    discover_seconds: float = 15.0
    last_seconds: int = LAST_SECONDS_NO_RISK
    max_windows: int = MAX_CONCURRENT_WINDOWS

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
        if self.env == "production" and not self.allow_production:
            raise ValueError(
                "Production trading is disabled. This bot defaults to the Kalshi "
                "demo API. Set KALSHI_ALLOW_PRODUCTION=1 only if you intend live risk."
            )
        if self.rest_base:
            self._assert_url_safe(self.rest_base)
        if self.ws_url:
            self._assert_url_safe(self.ws_url)
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
    def resolved_rest_base(self) -> str:
        if self.rest_base:
            self._assert_url_safe(self.rest_base)
            return self.rest_base.rstrip("/")
        return PROD_REST if self.env == "production" else DEMO_REST

    @cached_property
    def resolved_ws_url(self) -> str:
        if self.ws_url:
            self._assert_url_safe(self.ws_url)
            return self.ws_url
        return PROD_WS if self.env == "production" else DEMO_WS

    def _assert_url_safe(self, url: str) -> None:
        lowered = url.lower()
        prod_hosts = ("external-api.kalshi.com", "api.elections.kalshi.com")
        if any(host in lowered for host in prod_hosts) and not self.allow_production:
            raise ValueError(
                f"Refusing production host {url!r} without KALSHI_ALLOW_PRODUCTION=1"
            )

    def has_credentials(self) -> bool:
        return bool(self.api_key_id and (self.private_key_path or self.private_key))


def load_settings(**overrides: object) -> Settings:
    return Settings(**overrides)
