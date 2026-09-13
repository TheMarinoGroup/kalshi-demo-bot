"""Main paper-bot loop: discover → evaluate → risk → execute."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog

from kalshi_pbot.config import Settings
from kalshi_pbot.execution import ExecutionEngine, flatten_intent
from kalshi_pbot.kalshi_client import KalshiClient, KalshiRestClient, MockKalshiClient
from kalshi_pbot.market_data import MarketUniverse, OrderBookStore
from kalshi_pbot.metrics import compute_metrics, emit_metrics
from kalshi_pbot.portfolio import Portfolio
from kalshi_pbot.risk_engine import RiskEngine
from kalshi_pbot.strategy.maker import MakerStrategy
from kalshi_pbot.strategy.pair_arb import PairArbStrategy
from kalshi_pbot.types import IntentKind, MarketWindow, OrderBook, QuoteIntent

log = structlog.get_logger(__name__)


class PaperBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if settings.mock:
            self.client: KalshiClient | MockKalshiClient = MockKalshiClient(settings)
            rest: KalshiRestClient | None = None
        else:
            if not settings.dry_run and not settings.has_credentials():
                raise RuntimeError(
                    "Demo submit requires KALSHI_API_KEY_ID and a private key. "
                    "Use --dry-run or --mock, or set credentials."
                )
            self.client = KalshiClient(settings)
            rest = self.client.rest if settings.has_credentials() else None
            if not settings.has_credentials():
                log.warning(
                    "public_data_only",
                    hint="dry-run against demo REST books; WS/fills need API keys",
                )

        self.portfolio = Portfolio(settings)
        self.risk = RiskEngine(settings)
        self.execution = ExecutionEngine(settings, self.portfolio, rest)
        self.universe = MarketUniverse(settings, self.client)
        self.books = OrderBookStore()
        self.maker = MakerStrategy(settings)
        self.pair_arb = PairArbStrategy(settings)
        self._stop = asyncio.Event()
        self._ob_sid: int | None = None

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        log.info(
            "bot_start",
            env=self.settings.env,
            rest=self.settings.resolved_rest_base,
            dry_run=self.settings.dry_run,
            mock=isinstance(self.client, MockKalshiClient),
            series=list(self.settings.series_tickers),
            bankroll=str(self.settings.bankroll),
            max_open=str(self.settings.max_open_notional),
            daily_kill=str(self.settings.daily_loss_limit),
            onesided=str(self.settings.max_onesided),
            clip=str(self.settings.clip),
        )
        self.universe.refresh()
        self.universe.hydrate_books(self.books)

        ws = getattr(self.client, "ws", None)
        if ws is not None and self.settings.has_credentials() and not self.settings.mock:
            ws.add_handler(self._on_ws)
            await ws.connect()
            await self._resubscribe()

        last_discover = 0.0
        while not self._stop.is_set():
            now = datetime.now(UTC)
            if now.timestamp() - last_discover >= self.settings.discover_seconds:
                prev = set(self.universe.markets)
                self.universe.refresh(now=now)
                self.universe.hydrate_books(self.books)
                if set(self.universe.markets) != prev and ws is not None and ws._ws is not None:
                    await self._resubscribe()
                last_discover = now.timestamp()
            self.step(now=now)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.loop_seconds)
            except TimeoutError:
                continue

        if ws is not None:
            await ws.close()
        self.client.close()
        log.info("bot_stop")

    async def _resubscribe(self) -> None:
        ws = getattr(self.client, "ws", None)
        if ws is None:
            return
        tickers = list(self.universe.markets)
        await ws.subscribe(
            ["ticker", "market_lifecycle_v2", "fill", "market_positions", "user_orders"],
        )
        if tickers:
            await ws.subscribe(
                ["orderbook_delta"],
                market_tickers=tickers,
                extra={"use_yes_price": False},
            )
        log.info("ws_subscribed", tickers=tickers)

    def _on_ws(self, message: dict) -> None:
        kind = message.get("type")
        if kind in {"orderbook_snapshot", "orderbook_delta"}:
            self.books.handle_ws(message)
        elif kind == "fill":
            self._handle_fill_msg(message.get("msg") or {})
        elif kind == "error":
            log.warning("ws_error", msg=message.get("msg"))

    def _handle_fill_msg(self, body: dict) -> None:
        from kalshi_pbot.types import D, Fill, Outcome

        try:
            outcome_raw = str(body.get("outcome_side") or body.get("side") or "yes").lower()
            outcome = Outcome.YES if outcome_raw in {"yes", "bid"} else Outcome.NO
            ticker = body.get("ticker") or body.get("market_ticker")
            fill = Fill(
                fill_id=str(body.get("fill_id") or body.get("trade_id") or body.get("order_id")),
                order_id=str(body.get("order_id") or ""),
                market_ticker=str(ticker),
                event_ticker=str(body.get("event_ticker") or ticker),
                outcome=outcome,
                price=D(
                    body.get("price_dollars")
                    or body.get("yes_price_dollars")
                    or body.get("price")
                    or 0
                ),
                count=D(body.get("count_fp") or body.get("count") or 0),
                fee=D(body.get("fee_dollars") or body.get("fee_cost") or 0),
                is_taker=bool(body.get("is_taker") or body.get("liquidity") == "taker"),
                ts_ms=int(body.get("ts_ms") or 0),
            )
            if fill.count > 0 and fill.price > 0:
                self.portfolio.apply_fill(fill)
        except Exception:
            log.exception("fill_parse_failed", body=str(body)[:300])

    def step(self, now: datetime | None = None) -> list[QuoteIntent]:
        now = now or datetime.now(UTC)
        self.portfolio.reset_day_if_needed(now)
        books = {
            t: b
            for t, b in (
                (m.ticker, self.books.get(m.ticker)) for m in self.universe.markets.values()
            )
            if b
        }
        snapshot = self.portfolio.snapshot(books)
        self.risk.maybe_trip_daily(snapshot)
        if self.risk.kill_active:
            self.portfolio.kill_active = True
            self.portfolio.kill_reason = self.risk.kill_reason
            self.execution.cancel_all()
            snapshot = self.portfolio.snapshot(books)

        submitted: list[QuoteIntent] = []

        for market in self.universe.flatten_only(now=now):
            self._flatten_window(market, snapshot, now)

        if self.risk.kill_active:
            emit_metrics(compute_metrics(self.settings, self.portfolio, snapshot))
            return submitted

        for market in self.universe.tradable(now=now):
            book = self.books.get(market.ticker)
            if book is None:
                continue
            intents = self._decide(market, book, snapshot)
            for intent in intents:
                # Skip if we already have a resting order on this outcome.
                if self._already_quoting(intent):
                    continue
                decision = self.risk.evaluate(
                    intent, snapshot, close_time=market.close_time, now=now
                )
                if not decision.allowed:
                    log.info(
                        "risk_reject",
                        reason=decision.reason.value,
                        detail=decision.detail,
                        ticker=intent.market_ticker,
                        outcome=intent.outcome.value,
                        kind=intent.kind.value,
                    )
                    continue
                self.execution.submit(intent)
                submitted.append(intent)
                snapshot = self.portfolio.snapshot(books)

        emit_metrics(compute_metrics(self.settings, self.portfolio, self.portfolio.snapshot(books)))
        return submitted

    def _decide(
        self,
        market: MarketWindow,
        book: OrderBook,
        snapshot,
    ) -> list[QuoteIntent]:
        pos = snapshot.positions.get(market.ticker)
        if pos and pos.unpaired_outcome is not None:
            return self.maker.evaluate(market, book, snapshot)

        pair_quotes = self.pair_arb.evaluate(market, book, snapshot)
        if pair_quotes:
            return pair_quotes
        return self.maker.evaluate(market, book, snapshot)

    def _already_quoting(self, intent: QuoteIntent) -> bool:
        for order in self.portfolio.resting_for(intent.market_ticker):
            if order.outcome is intent.outcome:
                return True
        return False

    def _flatten_window(self, market: MarketWindow, snapshot, now: datetime) -> None:
        self.execution.cancel_market(market.ticker, market.event_ticker, "last_60s_cancel")
        pos = snapshot.positions.get(market.ticker)
        book = self.books.get(market.ticker)
        if pos is None or book is None or pos.unpaired_qty <= 0:
            return
        outcome = pos.unpaired_outcome
        if outcome is None:
            return
        # Hitting our own bid to dump the unpaired side would add taker risk;
        # only flatten if a bid exists on that outcome.
        bid = book.best_yes_bid() if outcome.value == "yes" else book.best_no_bid()
        if bid is None:
            log.warning("flatten_no_bid", ticker=market.ticker, outcome=outcome.value)
            return
        intent = flatten_intent(
            market.ticker,
            market.event_ticker,
            outcome,
            pos.unpaired_qty,
            bid,
        )
        # Flattening sells the unpaired outcome. V2 ask on YES sells YES;
        # for a long NO we buy YES / sell NO via the YES bid... treat as
        # reduce-only taker at the outcome bid (give the inventory away).
        intent = QuoteIntent(
            market_ticker=intent.market_ticker,
            event_ticker=intent.event_ticker,
            outcome=intent.outcome,
            price=intent.price,
            count=intent.count,
            liquidity=intent.liquidity,
            tif=intent.tif,
            post_only=False,
            reduce_only=True,
            sell=True,
            kind=IntentKind.FLATTEN,
            reason="last_60s_flatten_unpaired",
        )
        decision = self.risk.evaluate(intent, snapshot, close_time=market.close_time, now=now)
        if decision.allowed:
            self.execution.submit(intent)
        else:
            log.info("flatten_blocked", detail=decision.detail)


def run_bot(settings: Settings) -> None:
    bot = PaperBot(settings)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        bot.stop()


def discover_once(settings: Settings) -> list[MarketWindow]:
    client: KalshiClient | MockKalshiClient
    if settings.mock:
        client = MockKalshiClient(settings)
    else:
        client = KalshiClient(settings)
    try:
        universe = MarketUniverse(settings, client)
        return universe.refresh()
    finally:
        client.close()
