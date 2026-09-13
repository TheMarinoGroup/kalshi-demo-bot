from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from kalshi_pbot.config import Settings
from kalshi_pbot.kalshi_client import MockKalshiClient
from kalshi_pbot.market_data import (
    MarketUniverse,
    OrderBookStore,
    book_from_rest,
    parse_public_trade,
)
from kalshi_pbot.types import MarketWindow, Outcome
from kalshi_pbot.windows import WindowStore


def test_book_from_rest_implied_ask() -> None:
    payload = {
        "orderbook_fp": {
            "yes_dollars": [["0.4200", "13.00"]],
            "no_dollars": [["0.5600", "17.00"]],
        }
    }
    book = book_from_rest("T", payload)
    assert book.best_yes_bid() == Decimal("0.4200")
    assert book.implied_yes_ask() == Decimal("0.4400")
    assert book.spread_yes() == Decimal("0.0200")


def test_orderbook_delta_and_seq() -> None:
    store = OrderBookStore()
    store.apply_snapshot(
        {
            "seq": 1,
            "msg": {
                "market_ticker": "T",
                "yes_dollars_fp": [["0.40", "10.00"]],
                "no_dollars_fp": [["0.50", "5.00"]],
            },
        }
    )
    store.apply_delta(
        {
            "seq": 2,
            "msg": {
                "market_ticker": "T",
                "side": "yes",
                "price_dollars": "0.41",
                "delta_fp": "8.00",
            },
        }
    )
    book = store.get("T")
    assert book is not None
    assert book.best_yes_bid() == Decimal("0.41")
    store.apply_delta(
        {
            "seq": 3,
            "msg": {
                "market_ticker": "T",
                "side": "yes",
                "price_dollars": "0.41",
                "delta_fp": "-8.00",
            },
        }
    )
    assert store.get("T").best_yes_bid() == Decimal("0.40")


def test_universe_discovers_mock_kxbtc15m() -> None:
    settings = Settings(mock=True, dry_run=True, series="KXBTC15M")
    universe = MarketUniverse(settings, MockKalshiClient(settings))
    markets = universe.refresh()
    assert markets
    assert markets[0].series_ticker == "KXBTC15M"
    assert markets[0].ticker.startswith("KXBTC15M")


def test_rollover_drops_closed_and_caps_windows() -> None:
    now = datetime(2026, 9, 13, 20, 0, tzinfo=UTC)

    class Fake:
        def list_events(self, series_ticker: str, status: str) -> list[MarketWindow]:
            if status == "unopened":
                return [
                    MarketWindow(
                        ticker=f"{series_ticker}-NEXT",
                        event_ticker=f"{series_ticker}-NEXT",
                        series_ticker=series_ticker,
                        title="next",
                        status="initialized",
                        open_time=now + timedelta(minutes=10),
                        close_time=now + timedelta(minutes=25),
                    )
                ]
            return self.list_open_markets(series_ticker)

        def list_open_markets(self, series_ticker: str) -> list[MarketWindow]:
            return [
                MarketWindow(
                    ticker=f"{series_ticker}-OLD",
                    event_ticker=f"{series_ticker}-OLD",
                    series_ticker=series_ticker,
                    title="old",
                    status="active",
                    open_time=now - timedelta(minutes=20),
                    close_time=now - timedelta(seconds=1),
                ),
                MarketWindow(
                    ticker=f"{series_ticker}-A",
                    event_ticker=f"{series_ticker}-A",
                    series_ticker=series_ticker,
                    title="a",
                    status="active",
                    open_time=now - timedelta(minutes=5),
                    close_time=now + timedelta(minutes=5),
                ),
                MarketWindow(
                    ticker=f"{series_ticker}-B",
                    event_ticker=f"{series_ticker}-B",
                    series_ticker=series_ticker,
                    title="b",
                    status="active",
                    open_time=now - timedelta(minutes=1),
                    close_time=now + timedelta(minutes=12),
                ),
            ]

    settings = Settings(series="KXBTC15M,KXETH15M", dry_run=True, max_windows=2)
    universe = MarketUniverse(settings, Fake())  # type: ignore[arg-type]
    chosen = universe.refresh(now=now)
    assert len(chosen) == 2
    assert all(m.close_time > now for m in chosen)
    # Soonest close first
    assert chosen[0].close_time <= chosen[1].close_time
    assert {m.series_ticker for m in chosen} == {"KXBTC15M", "KXETH15M"}
    assert any(m.ticker.endswith("-NEXT") for m in universe.upcoming)
    assert universe.store.path.exists()


def test_unopened_active_market_is_selected_live() -> None:
    """Kalshi leaves the current 15m clip under events?status=unopened."""
    now = datetime(2026, 9, 13, 20, 50, tzinfo=UTC)

    class Fake:
        def list_events(self, series_ticker: str, status: str) -> list[MarketWindow]:
            if status == "open":
                return [
                    MarketWindow(
                        ticker=f"{series_ticker}-DONE",
                        event_ticker=f"{series_ticker}-DONE",
                        series_ticker=series_ticker,
                        title="done",
                        status="determined",
                        open_time=now - timedelta(minutes=20),
                        close_time=now - timedelta(minutes=5),
                    )
                ]
            return [
                MarketWindow(
                    ticker=f"{series_ticker}-LIVE",
                    event_ticker=f"{series_ticker}-LIVE",
                    series_ticker=series_ticker,
                    title="live",
                    status="active",
                    open_time=now - timedelta(minutes=5),
                    close_time=now + timedelta(minutes=10),
                )
            ]

        def list_open_markets(self, series_ticker: str) -> list[MarketWindow]:
            return []

    settings = Settings(series="KXBTC15M", dry_run=True, max_windows=2)
    universe = MarketUniverse(settings, Fake())  # type: ignore[arg-type]
    chosen = universe.refresh(now=now)
    assert [m.ticker for m in chosen] == ["KXBTC15M-LIVE"]


def test_events_first_persists_windows(tmp_path) -> None:
    settings = Settings(
        mock=True,
        dry_run=True,
        series="KXBTC15M",
        windows_path=str(tmp_path / "windows.json"),
    )
    universe = MarketUniverse(settings, MockKalshiClient(settings))
    universe.refresh()
    store = WindowStore(tmp_path / "windows.json")
    tickers = {w.ticker for w in store.known()}
    assert any(t.startswith("KXBTC15M") for t in tickers)


def test_parse_public_trade() -> None:
    trade = parse_public_trade(
        {
            "type": "trade",
            "msg": {
                "market_ticker": "KXBTC15M-T",
                "yes_price_dollars": "0.44",
                "count_fp": "3.00",
                "taker_side": "no",
                "trade_id": "x",
                "ts_ms": 1,
            },
        }
    )
    assert trade is not None
    assert trade.taker_outcome is Outcome.NO
    assert trade.yes_price == Decimal("0.44")
    assert trade.no_price == Decimal("0.56")
