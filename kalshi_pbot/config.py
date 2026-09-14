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

from kalshi_pbot.series import MIN_WINDOW_MINUTES, filter_series
from kalshi_pbot.types import D, QuoteMode

DEMO_REST = "https://external-api.demo.kalshi.co/trade-api/v2"
DEMO_WS = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
PROD_REST = "https://external-api.kalshi.com/trade-api/v2"
PROD_WS = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"

# Risk Desk fractions (locked). Paper-v2 Option B reference bankroll is $500:
# hard open $25 / onesided $15 / daily kill $10. Soft abort is mandatory
# because daily kill = 1× clip. Fractions still rescale with KALSHI_BANKROLL.
REF_BANKROLL = Decimal("500")
OPEN_NOTIONAL_FRAC = Decimal("0.05")  # $25 at $500
DAILY_LOSS_FRAC = Decimal("0.02")  # $10 at $500
ONESIDED_FRAC = Decimal("0.03")  # $15 at $500
MAX_CONCURRENT_WINDOWS = 1
LAST_SECONDS_NO_RISK = 60
# Paper-v2: no new risk earlier than the Risk Desk 60s floor.
PAPER_V2_LAST_SECONDS = 120
# Soft abort: flatten unpaired before the hard onesided kill ($15 at $500).
# 0 disables age abort only when daily kill > clip (not Option B).
DEFAULT_MAX_UNPAIRED_AGE_SECONDS = 45
# Soft onesided flatten / no-grow. Hard kill stays ONESIDED_FRAC ($15 at $500).
DEFAULT_SOFT_ONESIDED = Decimal("10")
CLIP_MIN = Decimal("10")
CLIP_MAX = Decimal("30")
DEFAULT_CLIP = Decimal("10")
DEFAULT_MIN_EDGE = Decimal("0.04")
DEFAULT_SERIES = ("KXBTC15M",)
# Measured close→settlement_ts on public REST, N=8000 finalized
# KXBTC15M+KXETH15M: p50≈7s, p90≈12s, p99≈59s. ~99% settle within 60s.
# market.expected_expiration (~close+300s) is NOT actual settlement latency —
# do not use it as settle-lock. Paper recycle targets the 60–90s band.
SETTLE_P50_S = 7
SETTLE_P90_S = 12
SETTLE_P99_S = 59
SETTLE_SAMPLE_N = 8000
SETTLE_RECYCLE_SECONDS = 75
SETTLE_RECYCLE_BAND = (60, 90)
SETTLE_RARE_TAIL_SECONDS = 300
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
    # Crypto Up/Down 15m+ only. 5-minute (and shorter) series are refused.
    min_window_minutes: int = MIN_WINDOW_MINUTES
    hud: bool = False
    hud_host: str = "0.0.0.0"
    hud_port: int = 8080
    clip_dollars: Decimal = DEFAULT_CLIP
    quote_mode: QuoteMode = "one_sided"
    min_edge: Decimal = DEFAULT_MIN_EDGE
    taker_pair_arb: bool = False
    tick_size: Decimal = Decimal("0.01")
    improve_ticks: int = 0
    latency_ms: int = 150
    loop_seconds: float = 0.05
    discover_seconds: float = 15.0
    # Pause between series during events-first discovery so we do not burst
    # Pause between series if more than one is configured.
    discover_series_delay: float = 0.4
    tob_heartbeat_ms: int = 100
    last_seconds: int = PAPER_V2_LAST_SECONDS
    # Paper capital velocity: recycle after close + this many seconds.
    # Default 75s sits in the 60–90s band (covers ~p99). Not expected_expiration.
    settle_recycle_seconds: int = SETTLE_RECYCLE_SECONDS
    # Conservative rare-tail lock (~expected_expiration horizon). Off by default.
    settle_rare_tail: bool = False
    settle_rare_tail_seconds: int = SETTLE_RARE_TAIL_SECONDS
    max_windows: int = MAX_CONCURRENT_WINDOWS
    # Soft preference: flatten unpaired after this many seconds (not a Risk Desk kill).
    max_unpaired_age_seconds: int = DEFAULT_MAX_UNPAIRED_AGE_SECONDS
    # Soft preference: flatten / refuse growth above this notional (hard kill stays 3%).
    soft_onesided: Decimal = DEFAULT_SOFT_ONESIDED
    # Soft preference: skip new ENTRY unless bid_sum ≤ 1 − min_edge (Regime B).
    only_quote_underround: bool = False
    cfb_5hz: bool = False
    windows_path: str = "data/windows.json"
    tape_path: str = "data/tape.jsonl"
    # Daily-loss / manual kill persist so a HUD watchdog restart cannot clear the latch.
    kill_latch_path: str = "data/kill-latch.json"
    # Cancel leftover resting orders on watched series after reconcile.
    # Default safe (off). Honored only for --demo-submit on demo hosts.
    # Production never auto-cancels, even if this is true.
    cancel_orphans: bool = False

    log_level: str = "INFO"
    log_json: bool = False

    http_timeout: float = 15.0

    @field_validator(
        "bankroll", "clip_dollars", "min_edge", "tick_size", "soft_onesided", mode="before"
    )
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
        if self.settle_recycle_seconds <= 0:
            raise ValueError("settle_recycle_seconds must be positive")
        if self.settle_rare_tail_seconds <= 0:
            raise ValueError("settle_rare_tail_seconds must be positive")
        if self.max_unpaired_age_seconds < 0:
            raise ValueError("max_unpaired_age_seconds must be >= 0")
        if self.soft_onesided < 0:
            raise ValueError("soft_onesided must be >= 0")
        if self.daily_loss_limit <= self.clip and (
            self.soft_onesided <= 0 or self.max_unpaired_age_seconds <= 0
        ):
            raise ValueError(
                "daily kill <= clip requires soft_onesided > 0 and "
                "max_unpaired_age_seconds > 0 (soft abort is mandatory)"
            )
        if self.last_seconds < LAST_SECONDS_NO_RISK:
            raise ValueError(
                f"last_seconds must be >= {LAST_SECONDS_NO_RISK} (Risk Desk v1 floor)"
            )
        if self.improve_ticks < 0:
            raise ValueError("improve_ticks must be >= 0")
        if self.max_windows < 1:
            raise ValueError("max_windows must be >= 1")
        if self.min_window_minutes < MIN_WINDOW_MINUTES:
            raise ValueError(
                f"min_window_minutes must be >= {MIN_WINDOW_MINUTES} "
                "(15m+ crypto Up/Down only; 5-minute markets are not supported)"
            )
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
        kept = filter_series(items, self.min_window_minutes)
        return kept or DEFAULT_SERIES

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
    def effective_settle_recycle_seconds(self) -> int:
        """Post-close wait before paper recycle. Never derived from expected_expiration."""
        if self.settle_rare_tail:
            return self.settle_rare_tail_seconds
        return self.settle_recycle_seconds

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
