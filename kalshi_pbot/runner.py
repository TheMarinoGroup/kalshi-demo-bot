"""Main paper-bot loop: discover → evaluate → risk → paper-match / execute."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime

import structlog

from kalshi_pbot.config import Settings
from kalshi_pbot.execution import ExecutionEngine, flatten_intent
from kalshi_pbot.hud_state import MidHistory, build_snapshot
from kalshi_pbot.kalshi_client import KalshiClient, KalshiRestClient, MockKalshiClient
from kalshi_pbot.market_data import (
    MarketUniverse,
    OrderBookStore,
    parse_cfb_tick,
    parse_public_trade,
)
from kalshi_pbot.metrics import compute_metrics, emit_metrics
from kalshi_pbot.paper_matcher import PaperMatcher
from kalshi_pbot.portfolio import Portfolio
from kalshi_pbot.risk_engine import (
    RiskEngine,
    recycle_ready,
    seconds_since_close,
    should_abort_unpaired,
    unpaired_abort_reason,
)
from kalshi_pbot.strategy.maker import MakerStrategy
from kalshi_pbot.strategy.pair_arb import PairArbStrategy
from kalshi_pbot.tape import JsonlTape
from kalshi_pbot.types import IntentKind, MarketWindow, OrderBook, Outcome, QuoteIntent

log = structlog.get_logger(__name__)


class PaperBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if settings.mock:
            self.client: KalshiClient | MockKalshiClient = MockKalshiClient(settings)
            rest: KalshiRestClient | None = None
        else:
            if settings.live_submit and not settings.has_credentials():
                raise RuntimeError(
                    "Demo submit requires KALSHI_API_KEY_ID and a private key. "
                    "Use --dry-run or --mock, or set credentials."
                )
            self.client = KalshiClient(settings)
            rest = self.client.rest if settings.has_credentials() else None
            if not settings.has_credentials():
                log.warning(
                    "public_data_only",
                    hint="prod REST events/books need no key; WS needs demo or read-only prod keys",
                )

        self.tape = JsonlTape(settings.tape_path)
        self.matcher = PaperMatcher(
            latency_ms=settings.latency_ms,
        )
        self.portfolio = Portfolio(settings)
        self.risk = RiskEngine(settings)
        self.execution = ExecutionEngine(
            settings, self.portfolio, rest, matcher=self.matcher, tape=self.tape
        )
        self.universe = MarketUniverse(settings, self.client)
        self.books = OrderBookStore()
        self.maker = MakerStrategy(settings)
        self.pair_arb = PairArbStrategy(settings)
        self._stop = asyncio.Event()
        self._last_tob_ms = 0
        self._last_wipe = 0
        self.mid_history = MidHistory()
        self.hud_hub = None
        self.cfb: dict[str, object] = {}

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        log.info(
            "bot_start",
            env=self.settings.env,
            data_rest=self.settings.resolved_data_rest,
            order_rest=self.settings.resolved_order_rest,
            ws=self.settings.resolved_ws_url,
            dry_run=self.settings.dry_run,
            paper_tape=self.settings.paper_tape,
            latency_ms=self.settings.latency_ms,
            live_submit=self.settings.live_submit,
            mock=isinstance(self.client, MockKalshiClient),
            series=list(self.settings.series_tickers),
            bankroll=str(self.settings.bankroll),
            max_open=str(self.settings.max_open_notional),
            daily_kill=str(self.settings.daily_loss_limit),
            onesided=str(self.settings.max_onesided),
            max_windows=self.settings.max_windows,
            max_unpaired_age_s=self.settings.max_unpaired_age_seconds,
            soft_onesided=str(self.settings.soft_onesided),
            last_seconds=self.settings.last_seconds,
            min_edge=str(self.settings.min_edge),
            quote_mode=self.settings.quote_mode,
            improve_ticks=self.settings.improve_ticks,
            only_quote_underround=self.settings.only_quote_underround,
            clip=str(self.settings.clip),
            settle_recycle_s=self.settings.effective_settle_recycle_seconds,
            settle_rare_tail=self.settings.settle_rare_tail,
            hud=self.settings.hud,
            min_window_minutes=self.settings.min_window_minutes,
            discover_seconds=self.settings.discover_seconds,
        )
        try:
            self.universe.refresh()
            self.universe.hydrate_books(self.books)
        except Exception:
            log.exception("startup_discover_failed")
        last_discover = datetime.now(UTC).timestamp()

        hud_task: asyncio.Task[None] | None = None
        if self.settings.hud:
            from kalshi_pbot.hud_server import HudHub, serve_hud

            self.hud_hub = HudHub()
            self.hud_hub.publish(build_snapshot(self, self.mid_history))
            hud_task = asyncio.create_task(
                serve_hud(self, self.hud_hub, self.mid_history), name="kalshi-hud"
            )

        ws = getattr(self.client, "ws", None)
        if ws is not None and self.settings.has_credentials() and not self.settings.mock:
            ws.add_handler(self._on_ws)
            await ws.connect()
            await self._resubscribe()

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

        if hud_task is not None:
            hud_task.cancel()
        if ws is not None:
            await ws.close()
        self.client.close()
        log.info("bot_stop")

    async def _resubscribe(self) -> None:
        ws = getattr(self.client, "ws", None)
        if ws is None:
            return
        tickers = list(self.universe.markets)
        if tickers:
            await ws.subscribe(
                ["orderbook_delta", "trade"],
                market_tickers=tickers,
                extra={"use_yes_price": False},
            )
        await ws.subscribe(["ticker", "market_lifecycle_v2"])
        cfb = ["cfbenchmarks_value"]
        if self.settings.cfb_5hz:
            cfb.append("cfbenchmarks_value_5hz")
        await ws.subscribe(cfb, extra={"index_ids": self.settings.cfb_index_ids()})
        if self.settings.live_submit:
            await ws.subscribe(["fill", "market_positions", "user_orders"])
        log.info(
            "ws_subscribed",
            tickers=tickers,
            channels=["orderbook_delta", "trade", "ticker", "market_lifecycle_v2", *cfb],
            index_ids=self.settings.cfb_index_ids(),
        )

    def _on_ws(self, message: dict) -> None:
        kind = message.get("type")
        if kind in {"orderbook_snapshot", "orderbook_delta"}:
            touch = self.books.handle_ws(message)
            if touch is not None and touch.old_size != touch.new_size:
                body = message.get("msg") or message
                self.matcher.enqueue_size_change(
                    ticker=touch.ticker,
                    outcome=touch.outcome,
                    price=touch.price,
                    old_size=touch.old_size,
                    new_size=touch.new_size,
                    ts_ms=_ws_ts_ms(body),
                )
        elif kind == "trade":
            trade = parse_public_trade(message)
            if trade:
                self.matcher.enqueue_trade(trade)
                if self.tape:
                    self.tape.write(
                        "trade",
                        ticker=trade.market_ticker,
                        yes_price=trade.yes_price,
                        count=trade.count,
                        taker=trade.taker_outcome.value,
                    )
        elif kind in {"ticker", "market_lifecycle_v2"}:
            body = message.get("msg") or {}
            if kind == "market_lifecycle_v2":
                self._apply_lifecycle_result(body)
            if self.tape:
                self.tape.write(str(kind), **{k: body[k] for k in list(body)[:8]})
            log.info("ws_lifecycle" if kind == "market_lifecycle_v2" else "ws_ticker", type=kind)
        elif kind in {"cfbenchmarks_value", "cfbenchmarks_value_5hz"}:
            tick = parse_cfb_tick(message)
            if tick:
                self.cfb[tick.index_id] = tick
            if tick and self.tape:
                self.tape.write(
                    "cfb",
                    index_id=tick.index_id,
                    value=tick.value,
                    avg_60s=tick.avg_60s,
                    hz="5" if kind.endswith("5hz") else "1",
                )
        elif kind == "fill" and self.settings.live_submit:
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
        now_ms = int(now.timestamp() * 1000)
        self.portfolio.reset_day_if_needed(now)
        self._drain_matcher(now_ms)
        self._recycle_settled(now)
        self._maybe_tob(now_ms)

        books = {
            t: b
            for t, b in (
                (m.ticker, self.books.get(m.ticker)) for m in self.universe.markets.values()
            )
            if b
        }
        snapshot = self.portfolio.snapshot(books)
        self.risk.maybe_trip_limits(snapshot)
        if self.risk.kill_active:
            self.portfolio.kill_active = True
            self.portfolio.kill_reason = self.risk.kill_reason
            self.execution.cancel_all()
            snapshot = self.portfolio.snapshot(books)

        submitted: list[QuoteIntent] = []

        for market in self.universe.flatten_only(now=now):
            flat = self._flatten_window(market, snapshot, now, reason="last_60s_flatten_unpaired")
            if flat:
                submitted.append(flat)

        aged_tickers: set[str] = set()
        for market in self._aged_unpaired_markets(snapshot, now):
            aged_tickers.add(market.ticker)
            pos = snapshot.positions.get(market.ticker)
            reason = (
                unpaired_abort_reason(pos, self.settings, now)
                if pos
                else "unpaired_age_abort"
            )
            flat = self._flatten_window(market, snapshot, now, reason=reason)
            if flat:
                submitted.append(flat)
            snapshot = self.portfolio.snapshot(books)

        if self.risk.kill_active:
            emit_metrics(compute_metrics(self.settings, self.portfolio, snapshot))
            self._publish_hud(now)
            return submitted

        for market in self.universe.tradable(now=now):
            if market.ticker in aged_tickers:
                continue
            book = self.books.get(market.ticker)
            if book is None:
                continue
            intents = self._decide(market, book, snapshot, now=now)
            for intent in intents:
                if intent.kind is IntentKind.FLATTEN:
                    self.execution.cancel_market(
                        market.ticker, market.event_ticker, intent.reason or "unpaired_abort"
                    )
                elif self._already_quoting(intent):
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
                self.execution.submit(intent, book=book)
                submitted.append(intent)
                snapshot = self.portfolio.snapshot(books)

        emit_metrics(compute_metrics(self.settings, self.portfolio, self.portfolio.snapshot(books)))
        self._publish_hud(now)
        return submitted

    def _publish_hud(self, now: datetime) -> None:
        if self.hud_hub is not None:
            self.hud_hub.publish(build_snapshot(self, self.mid_history, now))

    def _apply_lifecycle_result(self, body: dict) -> None:
        ticker = str(body.get("market_ticker") or body.get("ticker") or "")
        if not ticker:
            return
        raw = str(body.get("result") or "").lower()
        result = Outcome.YES if raw == "yes" else Outcome.NO if raw == "no" else None
        existing = self.universe.settling.get(ticker) or self.universe.markets.get(ticker)
        if existing is None or result is None:
            return
        updated = replace(
            existing,
            result=result,
            status=str(body.get("status") or existing.status),
        )
        if ticker in self.universe.settling:
            self.universe.settling[ticker] = updated
        if ticker in self.universe.markets:
            self.universe.markets[ticker] = updated

    def _recycle_settled(self, now: datetime) -> None:
        tail = self.settings.settle_rare_tail_seconds
        for market in list(self.universe.due_for_recycle(now)):
            if market.result is None:
                if recycle_ready(market.close_time, tail, now):
                    log.warning(
                        "settle_rare_tail_no_result",
                        ticker=market.ticker,
                        since_close_s=seconds_since_close(market.close_time, now),
                        expected_expiration=market.expected_expiration.isoformat()
                        if market.expected_expiration
                        else None,
                        note="expected_expiration is not settle-lock",
                    )
                continue
            self.execution.cancel_market(market.ticker, market.event_ticker, "settlement_recycle")
            self.portfolio.apply_settlement(
                market.ticker,
                market.result,
                close_time=market.close_time,
                now=now,
                settlement_ts=market.settlement_ts,
            )
            self.universe.mark_recycled(market.ticker)
            if self.tape:
                self.tape.write(
                    "settlement",
                    ticker=market.ticker,
                    result=market.result.value,
                    recycle_s=seconds_since_close(market.close_time, now),
                    close_to_settlement_s=(
                        (market.settlement_ts - market.close_time).total_seconds()
                        if market.settlement_ts
                        else None
                    ),
                )

    def _drain_matcher(self, now_ms: int) -> None:
        fills = self.matcher.drain(now_ms)
        for paper in fills:
            fill = self.matcher.to_portfolio_fill(paper)
            self.portfolio.apply_fill(fill)
            if self.tape:
                self.tape.write(
                    "paper_fill",
                    order_id=paper.order_id,
                    ticker=paper.market_ticker,
                    outcome=paper.outcome.value,
                    price=paper.price,
                    count=paper.count,
                    fee=paper.fee,
                    notional=paper.price * paper.count,
                    latency_ms=paper.latency_ms,
                    reason=paper.reason,
                )
        if self.matcher.ambiguous_wipes > self._last_wipe:
            for event in self.matcher.wipe_events[self._last_wipe :]:
                if self.tape:
                    self.tape.write("ambiguous_wipe", **event)
            self._last_wipe = self.matcher.ambiguous_wipes

    def _maybe_tob(self, now_ms: int) -> None:
        if now_ms - self._last_tob_ms < self.settings.tob_heartbeat_ms:
            return
        self._last_tob_ms = now_ms
        for market in self.universe.markets.values():
            book = self.books.get(market.ticker)
            if book is None:
                continue
            tob = book.tob()
            log.info(
                "tob",
                ticker=tob.ticker,
                yes_bid=str(tob.yes_bid),
                yes_ask=str(tob.yes_ask),
                no_bid=str(tob.no_bid),
                no_ask=str(tob.no_ask),
                mid=str(tob.mid_yes),
                spread=str(tob.spread_yes),
            )
            if self.tape:
                self.tape.write(
                    "tob",
                    ticker=tob.ticker,
                    yes_bid=tob.yes_bid,
                    yes_ask=tob.yes_ask,
                    no_bid=tob.no_bid,
                    no_ask=tob.no_ask,
                    mid_yes=tob.mid_yes,
                    spread_yes=tob.spread_yes,
                    seq=tob.seq,
                )
            self.mid_history.push(tob.ticker, tob.mid_yes, tob.spread_yes, now_ms)

    def _decide(
        self,
        market: MarketWindow,
        book: OrderBook,
        snapshot,
        now: datetime | None = None,
    ) -> list[QuoteIntent]:
        pos = snapshot.positions.get(market.ticker)
        if pos and pos.unpaired_outcome is not None:
            return self.maker.evaluate(market, book, snapshot, now=now)

        pair_quotes = self.pair_arb.evaluate(market, book, snapshot)
        if pair_quotes:
            return pair_quotes
        return self.maker.evaluate(market, book, snapshot, now=now)

    def _aged_unpaired_markets(self, snapshot, now: datetime) -> list[MarketWindow]:
        aged: list[MarketWindow] = []
        for market in self.universe.markets.values():
            pos = snapshot.positions.get(market.ticker)
            if pos is not None and should_abort_unpaired(pos, self.settings, now):
                aged.append(market)
        return aged

    def _already_quoting(self, intent: QuoteIntent) -> bool:
        for order in self.portfolio.resting_for(intent.market_ticker):
            if order.outcome is intent.outcome:
                return True
        return False

    def _flatten_window(
        self,
        market: MarketWindow,
        snapshot,
        now: datetime,
        *,
        reason: str = "last_60s_flatten_unpaired",
    ) -> QuoteIntent | None:
        cancel_reason = "last_60s_cancel" if reason.startswith("last_60s") else reason
        self.execution.cancel_market(market.ticker, market.event_ticker, cancel_reason)
        pos = snapshot.positions.get(market.ticker)
        book = self.books.get(market.ticker)
        if pos is None or book is None or pos.unpaired_qty <= 0:
            return None
        outcome = pos.unpaired_outcome
        if outcome is None:
            return None
        bid = book.best_yes_bid() if outcome.value == "yes" else book.best_no_bid()
        if bid is None:
            log.warning("flatten_no_bid", ticker=market.ticker, outcome=outcome.value)
            return None
        intent = flatten_intent(
            market.ticker,
            market.event_ticker,
            outcome,
            pos.unpaired_qty,
            bid,
        )
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
            reason=reason,
        )
        decision = self.risk.evaluate(intent, snapshot, close_time=market.close_time, now=now)
        if decision.allowed:
            self.execution.submit(intent, book=book)
            return intent
        log.info("flatten_blocked", detail=decision.detail)
        return None


def _ws_ts_ms(body: dict) -> int:
    if body.get("ts_ms"):
        return int(body["ts_ms"])
    return int(time.time() * 1000)


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
