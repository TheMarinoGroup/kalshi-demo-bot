"""Shared domain types for the paper bot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal


class Outcome(StrEnum):
    YES = "yes"
    NO = "no"


class BookSide(StrEnum):
    BID = "bid"
    ASK = "ask"


class IntentKind(StrEnum):
    ENTRY = "entry"
    COMPLETE_PAIR = "complete_pair"
    FLATTEN = "flatten"
    CANCEL = "cancel"


class TimeInForce(StrEnum):
    GTC = "good_till_canceled"
    IOC = "immediate_or_cancel"
    FOK = "fill_or_kill"


class Liquidity(StrEnum):
    MAKER = "maker"
    TAKER = "taker"


class RejectReason(StrEnum):
    OK = "ok"
    KILL_SWITCH = "kill_switch"
    DAILY_LOSS = "daily_loss"
    LAST_SECONDS = "last_seconds"
    PER_FILL = "per_fill"
    OPEN_NOTIONAL = "open_notional"
    CONCURRENT_WINDOWS = "concurrent_windows"
    ONESIDED_CAP = "onesided_cap"
    INVALID = "invalid"


QuoteMode = Literal["one_sided", "two_sided"]


def D(value: object) -> Decimal:
    """Parse a Decimal from int/float/str without binary float surprises."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class SeriesMeta:
    ticker: str
    fee_type: str = "quadratic"
    fee_multiplier: Decimal = Decimal("1")
    title: str = ""


@dataclass(frozen=True)
class MarketWindow:
    ticker: str
    event_ticker: str
    series_ticker: str
    title: str
    status: str
    open_time: datetime
    close_time: datetime
    yes_sub_title: str = ""
    no_sub_title: str = ""
    fee_type: str = "quadratic"
    fee_multiplier: Decimal = Decimal("1")
    # Informational only. expected_expiration (~close+300s) is NOT settle-lock.
    expected_expiration: datetime | None = None
    settlement_ts: datetime | None = None
    result: Outcome | None = None
    floor_strike: Decimal | None = None

    @property
    def window_id(self) -> str:
        return self.event_ticker or self.ticker


@dataclass(frozen=True)
class PriceLevel:
    price: Decimal
    size: Decimal


@dataclass
class OrderBook:
    ticker: str
    yes_bids: list[PriceLevel] = field(default_factory=list)
    no_bids: list[PriceLevel] = field(default_factory=list)
    seq: int = 0

    def best_yes_bid(self) -> Decimal | None:
        return self.yes_bids[-1].price if self.yes_bids else None

    def best_no_bid(self) -> Decimal | None:
        return self.no_bids[-1].price if self.no_bids else None

    def implied_yes_ask(self) -> Decimal | None:
        no_bid = self.best_no_bid()
        return (Decimal("1") - no_bid) if no_bid is not None else None

    def implied_no_ask(self) -> Decimal | None:
        yes_bid = self.best_yes_bid()
        return (Decimal("1") - yes_bid) if yes_bid is not None else None

    def mid_yes(self) -> Decimal | None:
        bid = self.best_yes_bid()
        ask = self.implied_yes_ask()
        if bid is None or ask is None:
            return None
        return (bid + ask) / Decimal("2")

    def spread_yes(self) -> Decimal | None:
        bid = self.best_yes_bid()
        ask = self.implied_yes_ask()
        if bid is None or ask is None:
            return None
        return ask - bid

    def size_at(self, outcome: Outcome, price: Decimal) -> Decimal:
        levels = self.yes_bids if outcome is Outcome.YES else self.no_bids
        for lvl in levels:
            if lvl.price == price:
                return lvl.size
        return Decimal("0")

    def best_yes_bid_size(self) -> Decimal | None:
        return self.yes_bids[-1].size if self.yes_bids else None

    def best_no_bid_size(self) -> Decimal | None:
        return self.no_bids[-1].size if self.no_bids else None

    def bid_sum(self) -> Decimal | None:
        yes = self.best_yes_bid()
        no = self.best_no_bid()
        if yes is None or no is None:
            return None
        return yes + no

    def ask_sum(self) -> Decimal | None:
        yes = self.implied_yes_ask()
        no = self.implied_no_ask()
        if yes is None or no is None:
            return None
        return yes + no

    def tob(self) -> TopOfBook:
        return TopOfBook(
            ticker=self.ticker,
            yes_bid=self.best_yes_bid(),
            yes_ask=self.implied_yes_ask(),
            no_bid=self.best_no_bid(),
            no_ask=self.implied_no_ask(),
            mid_yes=self.mid_yes(),
            spread_yes=self.spread_yes(),
            seq=self.seq,
        )


@dataclass(frozen=True)
class QuoteIntent:
    """A desired order. Prices are outcome-leg prices (pay this for YES or NO)."""

    market_ticker: str
    event_ticker: str
    outcome: Outcome
    price: Decimal
    count: Decimal
    liquidity: Liquidity = Liquidity.MAKER
    tif: TimeInForce = TimeInForce.GTC
    post_only: bool = True
    reduce_only: bool = False
    sell: bool = False
    kind: IntentKind = IntentKind.ENTRY
    reason: str = ""
    client_order_id: str | None = None

    @property
    def notional(self) -> Decimal:
        return (self.price * self.count).quantize(Decimal("0.0001"))

    def yes_leg_price(self) -> Decimal:
        """V2 events API quotes the YES book: bid=buy YES, ask=sell YES."""
        if self.outcome is Outcome.YES:
            return self.price
        return (Decimal("1") - self.price).quantize(Decimal("0.0001"))

    def book_side(self) -> BookSide:
        # V2 events API is YES-leg: bid = buy YES, ask = sell YES.
        buying_yes = (self.outcome is Outcome.YES and not self.sell) or (
            self.outcome is Outcome.NO and self.sell
        )
        return BookSide.BID if buying_yes else BookSide.ASK


@dataclass(frozen=True)
class CancelIntent:
    market_ticker: str
    event_ticker: str
    order_id: str | None = None
    client_order_id: str | None = None
    reason: str = ""
    cancel_all: bool = False


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: RejectReason
    detail: str = ""


@dataclass
class Position:
    market_ticker: str
    event_ticker: str
    yes_qty: Decimal = Decimal("0")
    no_qty: Decimal = Decimal("0")
    yes_cost: Decimal = Decimal("0")
    no_cost: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    fees: Decimal = Decimal("0")

    @property
    def paired_qty(self) -> Decimal:
        return min(self.yes_qty, self.no_qty)

    @property
    def unpaired_qty(self) -> Decimal:
        return abs(self.yes_qty - self.no_qty)

    @property
    def unpaired_outcome(self) -> Outcome | None:
        if self.yes_qty > self.no_qty:
            return Outcome.YES
        if self.no_qty > self.yes_qty:
            return Outcome.NO
        return None

    def avg_yes(self) -> Decimal | None:
        if self.yes_qty <= 0:
            return None
        return self.yes_cost / self.yes_qty

    def avg_no(self) -> Decimal | None:
        if self.no_qty <= 0:
            return None
        return self.no_cost / self.no_qty

    def cost_basis(self) -> Decimal:
        return self.yes_cost + self.no_cost

    def unpaired_notional(self) -> Decimal:
        extra_yes = self.yes_qty - self.paired_qty
        extra_no = self.no_qty - self.paired_qty
        yes_px = self.avg_yes() or Decimal("0")
        no_px = self.avg_no() or Decimal("0")
        return extra_yes * yes_px + extra_no * no_px

    def locked_pair_pnl(self) -> Decimal:
        """Certain PnL on the matched YES+NO quantity (ex-fees)."""
        paired = self.paired_qty
        if paired <= 0:
            return Decimal("0")
        yes_px = self.avg_yes() or Decimal("0")
        no_px = self.avg_no() or Decimal("0")
        return paired * (Decimal("1") - yes_px - no_px)

    def mark_unrealized(self, mid_yes: Decimal | None) -> Decimal:
        locked = self.locked_pair_pnl()
        if mid_yes is None:
            return locked
        extra_yes = self.yes_qty - self.paired_qty
        extra_no = self.no_qty - self.paired_qty
        yes_px = self.avg_yes() or Decimal("0")
        no_px = self.avg_no() or Decimal("0")
        mid_no = Decimal("1") - mid_yes
        unpaired = extra_yes * (mid_yes - yes_px) + extra_no * (mid_no - no_px)
        return locked + unpaired


@dataclass(frozen=True)
class RestingOrder:
    order_id: str
    client_order_id: str
    market_ticker: str
    event_ticker: str
    outcome: Outcome
    price: Decimal
    remaining: Decimal
    post_only: bool = True

    @property
    def reserved_notional(self) -> Decimal:
        return self.price * self.remaining


@dataclass(frozen=True)
class Fill:
    fill_id: str
    order_id: str
    market_ticker: str
    event_ticker: str
    outcome: Outcome
    price: Decimal
    count: Decimal
    fee: Decimal
    is_taker: bool
    ts_ms: int


@dataclass(frozen=True)
class PortfolioSnapshot:
    bankroll: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees: Decimal
    daily_pnl: Decimal
    open_notional: Decimal
    unpaired_notional: Decimal
    window_ids: frozenset[str]
    positions: dict[str, Position]
    resting: tuple[RestingOrder, ...]
    kill_active: bool
    kill_reason: str = ""


@dataclass(frozen=True)
class BotMetrics:
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    fees: Decimal
    daily_pnl: Decimal
    fill_count: int
    order_count: int
    fill_rate: Decimal
    incomplete_pair_notional: Decimal
    open_notional: Decimal
    open_notional_util: Decimal
    onesided_util: Decimal
    daily_loss_util: Decimal
    windows_used: int
    kill_active: bool
    dry_run: bool
    paper_tape: bool = True
    latency_ms: int = 150


@dataclass(frozen=True)
class TopOfBook:
    ticker: str
    yes_bid: Decimal | None
    yes_ask: Decimal | None
    no_bid: Decimal | None
    no_ask: Decimal | None
    mid_yes: Decimal | None
    spread_yes: Decimal | None
    seq: int = 0


@dataclass(frozen=True)
class PublicTrade:
    trade_id: str
    market_ticker: str
    yes_price: Decimal
    no_price: Decimal
    count: Decimal
    taker_outcome: Outcome
    ts_ms: int
    is_block: bool = False


@dataclass(frozen=True)
class PaperFill:
    order_id: str
    market_ticker: str
    event_ticker: str
    outcome: Outcome
    price: Decimal
    count: Decimal
    fee: Decimal
    ts_ms: int
    latency_ms: int
    reason: str


@dataclass(frozen=True)
class CFBTick:
    index_id: str
    value: Decimal
    avg_60s: Decimal | None
    settle_avg: Decimal | None
    received_at_ms: int
