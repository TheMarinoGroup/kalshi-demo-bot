from __future__ import annotations

import asyncio

from kalshi_pbot.config import Settings
from kalshi_pbot.runner import PaperBot
from kalshi_pbot.types import Liquidity


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
    # Risk Desk utilization is logged via metrics; open notional stays under $50
    snap = bot.portfolio.snapshot({ticker: book})
    assert snap.open_notional <= settings.max_open_notional


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
