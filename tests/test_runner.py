from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from kalshi_pbot.config import Settings
from kalshi_pbot.runner import PaperBot
from kalshi_pbot.types import Fill, IntentKind, Liquidity, Outcome


def test_mock_bot_discovers_and_dry_runs_quotes() -> None:
    settings = Settings(mock=True, dry_run=True, series="KXBTC15M", loop_seconds=0.01)
    bot = PaperBot(settings)
    bot.universe.refresh()
    bot.universe.hydrate_books(bot.books)
    assert bot.universe.markets
    ticker = next(iter(bot.universe.markets))
    assert ticker.startswith("KXBTC15M")
    book = bot.books.get(ticker)
    assert book is not None
    assert book.best_yes_bid() is not None

    submitted = bot.step()
    assert submitted
    assert all(q.post_only or q.liquidity is Liquidity.TAKER for q in submitted)
    assert bot.execution.dry_run_orders
    assert bot.matcher.orders
    assert bot.portfolio.orders_submitted == len(submitted)
    assert bot.settings.paper_tape is True
    assert bot.settings.live_submit is False
    # Risk Desk utilization is logged via metrics; Option B open cap is $25
    snap = bot.portfolio.snapshot({ticker: book})
    assert snap.open_notional <= settings.max_open_notional
    entries = [q for q in submitted if q.kind is IntentKind.ENTRY]
    assert entries
    assert all("pair" not in (q.reason or "") for q in entries)
    by_ticker: dict[str, list] = {}
    for q in entries:
        by_ticker.setdefault(q.market_ticker, []).append(q)
    for quotes in by_ticker.values():
        assert len(quotes) == 1


def test_discover_once_mock() -> None:
    from kalshi_pbot.runner import discover_once

    markets = discover_once(Settings(mock=True, series="KXBTC15M,KXETH15M"))
    series = {m.series_ticker for m in markets}
    assert "KXBTC15M" in series
    assert len(markets) <= 2


async def test_run_does_not_burst_discover_after_startup() -> None:
    """discover_seconds is 15s; the loop must not refresh again immediately."""
    settings = Settings(
        mock=True,
        dry_run=True,
        series="KXBTC15M",
        loop_seconds=0.01,
        discover_seconds=15.0,
    )
    bot = PaperBot(settings)
    calls = {"n": 0}
    original = bot.universe.refresh

    def counted(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    bot.universe.refresh = counted  # type: ignore[method-assign]

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        bot.stop()

    await asyncio.gather(bot.run(), stop_soon())
    assert calls["n"] == 1


def _yes_fill(ticker: str, *, count: str = "20", price: str = "0.50") -> Fill:
    return Fill(
        fill_id="f1",
        order_id="o1",
        market_ticker=ticker,
        event_ticker=ticker,
        outcome=Outcome.YES,
        price=Decimal(price),
        count=Decimal(count),
        fee=Decimal("0"),
        is_taker=False,
        ts_ms=int(datetime.now(UTC).timestamp() * 1000),
    )


def test_runner_skips_new_onesided_when_unpaired_exists() -> None:
    settings = Settings(mock=True, dry_run=True, series="KXBTC15M,KXETH15M", max_windows=2)
    bot = PaperBot(settings)
    bot.universe.refresh()
    bot.universe.hydrate_books(bot.books)
    btc = next(t for t in bot.universe.markets if t.startswith("KXBTC15M"))
    eth = next(t for t in bot.universe.markets if t.startswith("KXETH15M"))
    bot.portfolio.apply_fill(_yes_fill(btc))

    submitted = bot.step()
    eth_entries = [
        q
        for q in submitted
        if q.market_ticker == eth and q.kind is IntentKind.ENTRY
    ]
    assert eth_entries == []
    btc_quotes = [q for q in submitted if q.market_ticker == btc]
    assert btc_quotes
    assert all(q.kind in {IntentKind.COMPLETE_PAIR, IntentKind.FLATTEN} for q in btc_quotes)


def test_runner_age_aborts_unpaired() -> None:
    settings = Settings(
        mock=True,
        dry_run=True,
        series="KXBTC15M",
        max_unpaired_age_seconds=45,
        soft_onesided=Decimal("30"),
    )
    bot = PaperBot(settings)
    bot.universe.refresh()
    bot.universe.hydrate_books(bot.books)
    ticker = next(iter(bot.universe.markets))
    bot.portfolio.apply_fill(_yes_fill(ticker))
    pos = bot.portfolio.positions[ticker]
    pos.unpaired_since = datetime.now(UTC) - timedelta(seconds=46)

    submitted = bot.step()
    flats = [q for q in submitted if q.kind is IntentKind.FLATTEN]
    assert flats
    assert all(q.reason == "unpaired_age_abort" for q in flats)
    assert pos.unpaired_qty > 0


def test_runner_soft_onesided_aborts_without_kill() -> None:
    settings = Settings(
        mock=True,
        dry_run=True,
        series="KXBTC15M",
        bankroll=Decimal("1000"),
        clip_dollars=Decimal("10"),
    )
    bot = PaperBot(settings)
    bot.universe.refresh()
    bot.universe.hydrate_books(bot.books)
    ticker = next(iter(bot.universe.markets))
    bot.portfolio.apply_fill(_yes_fill(ticker, count="30", price="0.50"))  # $15
    pos = bot.portfolio.positions[ticker]
    pos.unpaired_since = datetime.now(UTC)

    submitted = bot.step()
    flats = [q for q in submitted if q.kind is IntentKind.FLATTEN]
    assert flats
    assert all(q.reason == "unpaired_soft_abort" for q in flats)
    assert not bot.risk.kill_active


def _keepalive_closed() -> ConnectionClosedError:
    return ConnectionClosedError(None, Close(1011, "keepalive ping timeout"))


class _FlakyWs:
    """Closed on first subscribe (rollover send-on-dead), then healthy after reconnect."""

    def __init__(self) -> None:
        self.subscribe_calls = 0
        self.reconnect_calls = 0
        self.channels: list[list[str]] = []
        self._ws = object()

    async def subscribe(self, channels: list[str], **kwargs: object) -> None:
        del kwargs
        self.subscribe_calls += 1
        self.channels.append(list(channels))
        if self.reconnect_calls == 0:
            raise _keepalive_closed()

    async def reconnect(self) -> None:
        self.reconnect_calls += 1

    async def close(self) -> None:
        return None


async def test_resubscribe_reconnects_after_closed_ws_and_continues() -> None:
    """Universe rollover send-on-closed must reconnect+resubscribe, not kill the desk."""
    bot = PaperBot(Settings(mock=True, dry_run=True, series="KXBTC15M"))
    bot.universe.refresh()
    assert bot.universe.markets
    fake = _FlakyWs()
    bot.client.ws = fake  # type: ignore[attr-defined]

    await bot._resubscribe()

    assert fake.reconnect_calls == 1
    assert fake.subscribe_calls > 1
    assert any("ticker" in ch for ch in fake.channels)


async def test_resubscribe_continues_if_reconnect_also_fails() -> None:
    class _DeadWs:
        async def subscribe(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            raise _keepalive_closed()

        async def reconnect(self) -> None:
            raise _keepalive_closed()

    bot = PaperBot(Settings(mock=True, dry_run=True, series="KXBTC15M"))
    bot.client.ws = _DeadWs()  # type: ignore[attr-defined]
    await bot._resubscribe()


async def test_run_survives_closed_ws_during_universe_rollover() -> None:
    """Main loop discover → _resubscribe on a dead socket must not end the process."""
    settings = Settings(
        mock=True,
        dry_run=True,
        series="KXBTC15M",
        loop_seconds=0.01,
        discover_seconds=0.03,
    )
    bot = PaperBot(settings)
    fake = _FlakyWs()
    bot.client.ws = fake  # type: ignore[attr-defined]

    original = bot.universe.refresh
    calls = {"n": 0}

    def refresh_and_change(*args, **kwargs):
        markets = original(*args, **kwargs)
        calls["n"] += 1
        # After startup refresh, change the ticker set so the loop resubscribes.
        if calls["n"] >= 2 and bot.universe.markets:
            extra = next(iter(bot.universe.markets.values()))
            bot.universe.markets[f"{extra.ticker}-ROLL"] = extra
        return markets

    bot.universe.refresh = refresh_and_change  # type: ignore[method-assign]

    async def stop_soon() -> None:
        await asyncio.sleep(0.12)
        bot.stop()

    await asyncio.gather(bot.run(), stop_soon())
    assert calls["n"] >= 2
    assert fake.reconnect_calls == 1
    assert fake.subscribe_calls > 1
