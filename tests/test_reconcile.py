from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from kalshi_pbot.config import Settings
from kalshi_pbot.execution import ExecutionEngine
from kalshi_pbot.hud_state import MidHistory, build_snapshot
from kalshi_pbot.kalshi_client import PAGINATE_MAX_PAGES
from kalshi_pbot.portfolio import Portfolio
from kalshi_pbot.reconcile import (
    SOURCE_EXCHANGE_SYNC,
    SOURCE_PAPER_LOCAL,
    STATUS_NOT_READY,
    STATUS_READY,
    STATUS_SYNCING,
    Reconciler,
    may_cancel_orphans,
)
from kalshi_pbot.risk_engine import RiskEngine, classify_kill
from kalshi_pbot.runner import PaperBot
from kalshi_pbot.tape import JsonlTape
from kalshi_pbot.types import IntentKind, Liquidity, Outcome, QuoteIntent, RejectReason, TimeInForce
from tests.conftest import empty_snapshot
from tests.test_kalshi_client import _client


class FakeRest:
    def __init__(
        self,
        positions: list[dict] | None = None,
        orders: list[dict] | None = None,
        *,
        fills: list[dict] | None = None,
        settlements: list[dict] | None = None,
        error: Exception | None = None,
        fail_orders: bool = False,
        fail_fills: bool = False,
        fail_settlements: bool = False,
        cancel_status: int = 200,
    ) -> None:
        self.positions = positions if positions is not None else []
        self.orders = orders if orders is not None else []
        self.fills = fills if fills is not None else []
        self.settlements = settlements if settlements is not None else []
        self.error = error
        self.fail_orders = fail_orders
        self.fail_fills = fail_fills
        self.fail_settlements = fail_settlements
        self.cancel_status = cancel_status
        self.cancelled: list[tuple[str, str]] = []
        self.created_orders: list[dict] = []
        self.fill_min_ts: int | None = None
        self.settlement_min_ts: int | None = None

    def list_market_positions(self) -> list[dict]:
        if self.error:
            raise self.error
        return list(self.positions)

    def list_resting_orders(self) -> list[dict]:
        if self.error:
            raise self.error
        if self.fail_orders:
            raise RuntimeError("orders endpoint failed")
        return list(self.orders)

    def list_fills_since(self, min_ts: int) -> list[dict]:
        if self.error:
            raise self.error
        if self.fail_fills:
            raise RuntimeError("fills endpoint failed")
        self.fill_min_ts = min_ts
        return list(self.fills)

    def list_settlements_since(self, min_ts: int) -> list[dict]:
        if self.error:
            raise self.error
        if self.fail_settlements:
            raise RuntimeError("settlements endpoint failed")
        self.settlement_min_ts = min_ts
        return list(self.settlements)

    def cancel_order(self, order_id: str, market_ticker: str) -> httpx.Response:
        self.cancelled.append((order_id, market_ticker))
        return httpx.Response(self.cancel_status, json={"order_id": order_id})

    def create_order(self, body: dict) -> httpx.Response:
        self.created_orders.append(body)
        raise AssertionError("create_order must not run on paper/view-only reconcile")


def _settings(**kwargs: object) -> Settings:
    defaults: dict[str, object] = {
        "dry_run": True,
        "paper_tape": True,
        "mock": False,
        "series": "KXBTC15M",
    }
    defaults.update(kwargs)
    return Settings(**defaults)


def _yes_position(
    ticker: str = "KXBTC15M-T",
    qty: str = "20.00",
    exposure: str = "10.0000",
    *,
    realized: str = "0.0000",
    fees: str = "0.0000",
    last_updated_ts: str = "2026-09-14T12:00:00Z",
) -> dict:
    return {
        "ticker": ticker,
        "event_ticker": ticker,
        "position_fp": qty,
        "market_exposure_dollars": exposure,
        "realized_pnl_dollars": realized,
        "fees_paid_dollars": fees,
        "last_updated_ts": last_updated_ts,
        "total_traded_dollars": exposure,
        "exchange_index": 0,
    }


def _resting_order(
    ticker: str = "KXBTC15M-T",
    *,
    order_id: str = "ord-1",
    outcome: str = "no",
    remaining: str = "20.00",
    yes_px: str = "0.5200",
    no_px: str = "0.4800",
) -> dict:
    return {
        "order_id": order_id,
        "client_order_id": f"cli-{order_id}",
        "ticker": ticker,
        "outcome_side": outcome,
        "book_side": "ask" if outcome == "no" else "bid",
        "status": "resting",
        "yes_price_dollars": yes_px,
        "no_price_dollars": no_px,
        "remaining_count_fp": remaining,
        "fill_count_fp": "0.00",
        "initial_count_fp": remaining,
        "taker_fees_dollars": "0",
        "maker_fees_dollars": "0",
        "taker_fill_cost_dollars": "0",
        "maker_fill_cost_dollars": "0",
        "user_id": "u",
        "type": "limit",
    }


def _fill(
    ticker: str = "KXBTC15M-T",
    *,
    fill_id: str = "fill-1",
    outcome: str = "yes",
    count: str = "20.00",
    yes_px: str = "0.5000",
    no_px: str = "0.5000",
    fee: str = "0.2500",
    created: str = "2026-09-14T12:00:00Z",
    is_taker: bool = False,
) -> dict:
    return {
        "fill_id": fill_id,
        "trade_id": fill_id,
        "order_id": f"ord-{fill_id}",
        "ticker": ticker,
        "market_ticker": ticker,
        "outcome_side": outcome,
        "book_side": "bid" if outcome == "yes" else "ask",
        "count_fp": count,
        "yes_price_dollars": yes_px,
        "no_price_dollars": no_px,
        "is_taker": is_taker,
        "fee_cost": fee,
        "created_time": created,
        "exchange_index": 0,
    }


def _settlement(
    ticker: str = "KXBTC15M-SETTLED",
    *,
    result: str = "no",
    yes_count: str = "20.00",
    yes_cost: str = "10.0000",
    no_count: str = "0.00",
    no_cost: str = "0.0000",
    revenue_cents: int = 0,
    fee: str = "0.0000",
    settled_time: str = "2026-09-14T12:05:00Z",
) -> dict:
    return {
        "ticker": ticker,
        "event_ticker": ticker,
        "exchange_index": 0,
        "market_result": result,
        "yes_count_fp": yes_count,
        "yes_total_cost_dollars": yes_cost,
        "no_count_fp": no_count,
        "no_total_cost_dollars": no_cost,
        "revenue": revenue_cents,
        "fee_cost": fee,
        "settled_time": settled_time,
    }


def _reconciler(settings: Settings, rest: FakeRest | None, **kwargs: object) -> Reconciler:
    portfolio = kwargs.pop("portfolio", Portfolio(settings))
    rec = Reconciler(
        settings,
        rest=rest,
        portfolio=portfolio,  # type: ignore[arg-type]
        tape=kwargs.get("tape"),  # type: ignore[arg-type]
        execution=kwargs.get("execution"),  # type: ignore[arg-type]
        universe=kwargs.get("universe"),
        mock=bool(kwargs.get("mock", False)),
    )
    return rec


def _entry_intent(ticker: str = "KXBTC15M-T") -> QuoteIntent:
    return QuoteIntent(
        market_ticker=ticker,
        event_ticker=ticker,
        outcome=Outcome.YES,
        price=Decimal("0.50"),
        count=Decimal("20"),
        liquidity=Liquidity.MAKER,
        tif=TimeInForce.GTC,
        post_only=True,
        kind=IntentKind.ENTRY,
    )


def _flatten_intent(ticker: str = "KXBTC15M-T") -> QuoteIntent:
    return QuoteIntent(
        market_ticker=ticker,
        event_ticker=ticker,
        outcome=Outcome.YES,
        price=Decimal("0.48"),
        count=Decimal("20"),
        liquidity=Liquidity.TAKER,
        tif=TimeInForce.IOC,
        post_only=False,
        reduce_only=True,
        sell=True,
        kind=IntentKind.FLATTEN,
    )


def _paper_bot(**kwargs: object) -> PaperBot:
    defaults: dict[str, object] = {
        "mock": True,
        "dry_run": True,
        "paper_tape": True,
        "series": "KXBTC15M",
    }
    defaults.update(kwargs)
    bot = PaperBot(Settings(**defaults))
    bot.universe.refresh()
    bot.universe.hydrate_books(bot.books)
    return bot


def test_empty_exchange_snapshot_marks_paper_ready() -> None:
    settings = _settings()
    rec = _reconciler(settings, FakeRest([], []))
    state = rec.attempt()
    assert state.ready_to_trade is True
    assert state.status == STATUS_READY
    assert state.source == SOURCE_PAPER_LOCAL
    assert rec.portfolio.ready_to_trade is True
    snap = rec.portfolio.snapshot()
    assert snap.open_notional == 0
    assert snap.unpaired_notional == 0
    assert snap.resting == ()


def test_open_position_rebuilds_onesided_and_open() -> None:
    settings = _settings()
    rec = _reconciler(settings, FakeRest([_yes_position()], []))
    rec.attempt()
    assert rec.state.source == SOURCE_EXCHANGE_SYNC
    pos = rec.portfolio.positions["KXBTC15M-T"]
    assert pos.yes_qty == Decimal("20.00")
    assert pos.no_qty == 0
    assert pos.unpaired_outcome is Outcome.YES
    snap = rec.portfolio.snapshot()
    assert snap.unpaired_notional == Decimal("10.0000")
    assert snap.open_notional == Decimal("10.0000")
    assert "KXBTC15M-T" in snap.window_ids


def test_resting_orders_restored_into_portfolio() -> None:
    settings = _settings()
    rec = _reconciler(settings, FakeRest([], [_resting_order()]))
    rec.attempt()
    assert rec.state.source == SOURCE_EXCHANGE_SYNC
    order = rec.portfolio.resting["ord-1"]
    assert order.outcome is Outcome.NO
    assert order.price == Decimal("0.4800")
    assert order.remaining == Decimal("20.00")
    snap = rec.portfolio.snapshot()
    assert snap.open_notional == Decimal("9.6000")  # 0.48 * 20
    assert rec.state.resting_count == 1


def test_api_error_fail_closed_no_quotes() -> None:
    settings = _settings()
    err = httpx.HTTPStatusError(
        "401",
        request=httpx.Request("GET", "https://api.test/portfolio/positions"),
        response=httpx.Response(401, json={"error": "unauthorized"}),
    )
    rec = _reconciler(settings, FakeRest(error=err))
    engine = ExecutionEngine(settings, rec.portfolio)
    rec.execution = engine
    state = rec.attempt()
    assert state.ready_to_trade is False
    assert state.status == STATUS_NOT_READY
    assert "401" in state.error or "HTTPStatusError" in state.error
    assert rec.portfolio.positions == {}
    assert rec.portfolio.resting == {}
    assert rec.portfolio.ready_to_trade is False
    assert engine.ready_to_trade is False
    result = engine.submit(
        QuoteIntent(
            market_ticker="KXBTC15M-T",
            event_ticker="KXBTC15M-T",
            outcome=Outcome.YES,
            price=Decimal("0.48"),
            count=Decimal("20"),
            liquidity=Liquidity.MAKER,
        )
    )
    assert result["error"] == "not_ready"
    assert engine.dry_run_orders == []
    hud = rec.state.as_hud(cancel_orphans=False)
    assert hud["hard_hold"] is True
    assert hud["book_verified"] is False
    flat = engine.submit(_flatten_intent())
    assert flat.get("error") != "not_ready"
    assert engine.dry_run_orders


def test_partial_orders_failure_fail_closed() -> None:
    settings = _settings()
    rec = _reconciler(settings, FakeRest([_yes_position()], fail_orders=True))
    state = rec.attempt()
    assert state.status == STATUS_NOT_READY
    assert rec.portfolio.positions == {}
    assert rec.portfolio.ready_to_trade is False
    assert state.next_retry_ts is not None


def test_backoff_skips_until_retry_ts() -> None:
    settings = _settings()
    rec = _reconciler(settings, FakeRest(error=RuntimeError("network down")))
    first = rec.attempt(datetime(2026, 9, 14, 12, 0, tzinfo=UTC))
    assert first.status == STATUS_NOT_READY
    retry_at = first.next_retry_ts
    assert retry_at is not None
    skipped = rec.maybe_attempt(retry_at - timedelta(seconds=1))
    assert skipped.attempts == 1
    rec.rest = FakeRest([], [])  # type: ignore[assignment]
    later = rec.maybe_attempt(retry_at)
    assert later.ready_to_trade is True
    assert later.attempts == 2


def test_paper_local_rebuilds_from_tape(tmp_path) -> None:
    tape_path = tmp_path / "tape.jsonl"
    settings = _settings(tape_path=str(tape_path), mock=True)
    tape = JsonlTape(tape_path)
    tape.write(
        "paper_fill",
        order_id="paper-1",
        ticker="KXBTC15M-MOCK",
        event_ticker="KXBTC15M-MOCK",
        outcome="yes",
        price="0.50",
        count="20",
        fee="0",
    )
    tape.write(
        "quote",
        order_id="paper-2",
        ticker="KXBTC15M-MOCK",
        outcome="no",
        price="0.48",
        count="20",
        post_only=True,
    )
    rec = _reconciler(settings, None, tape=tape, mock=True)
    rec.universe = SimpleNamespace(
        markets={"KXBTC15M-MOCK": SimpleNamespace(event_ticker="KXBTC15M-MOCK")},
        settling={},
    )
    rec.attempt()
    assert rec.state.source == SOURCE_PAPER_LOCAL
    assert rec.state.ready_to_trade is True
    pos = rec.portfolio.positions["KXBTC15M-MOCK"]
    assert pos.yes_qty == Decimal("20")
    assert rec.portfolio.snapshot().unpaired_notional == Decimal("10.00")
    assert rec.portfolio.resting["paper-2"].remaining == Decimal("20")
    assert rec.state.paper_fills_restored == 1
    assert rec.state.paper_quotes_restored == 1


def test_exchange_occupied_skips_tape(tmp_path) -> None:
    tape_path = tmp_path / "tape.jsonl"
    settings = _settings(tape_path=str(tape_path))
    tape = JsonlTape(tape_path)
    tape.write(
        "paper_fill",
        order_id="paper-1",
        ticker="KXBTC15M-T",
        outcome="yes",
        price="0.40",
        count="10",
        fee="0",
    )
    rec = _reconciler(settings, FakeRest([_yes_position()], []), tape=tape)
    rec.attempt()
    assert rec.state.source == SOURCE_EXCHANGE_SYNC
    pos = rec.portfolio.positions["KXBTC15M-T"]
    assert pos.yes_qty == Decimal("20.00")
    assert rec.state.paper_fills_restored == 0


def test_cancel_orphans_default_off() -> None:
    settings = _settings(cancel_orphans=False, paper_tape=False, dry_run=False)
    rest = FakeRest([], [_resting_order()])
    rec = _reconciler(settings, rest)
    rec.attempt()
    assert rest.cancelled == []
    assert "ord-1" in rec.portfolio.resting
    assert rec.state.orphan_count == 1
    assert rec.state.cancelled_orphans == 0


def test_cancel_orphans_demo_submit_only() -> None:
    settings = _settings(cancel_orphans=True, paper_tape=False, dry_run=False)
    assert settings.live_submit is True
    rest = FakeRest([], [_resting_order()])
    rec = _reconciler(settings, rest)
    rec.attempt()
    assert rest.cancelled == [("ord-1", "KXBTC15M-T")]
    assert rec.portfolio.resting == {}
    assert rec.state.cancelled_orphans == 1
    assert rec.state.ready_to_trade is True


def test_cancel_orphans_never_on_production() -> None:
    settings = Settings(
        env="production",
        allow_production=True,
        cancel_orphans=True,
        paper_tape=False,
        dry_run=False,
        order_rest="https://external-api.kalshi.com/trade-api/v2",
    )
    assert may_cancel_orphans(settings) is False
    rest = FakeRest([], [_resting_order()])
    rec = _reconciler(settings, rest)
    rec.attempt()
    assert rest.cancelled == []
    assert "ord-1" in rec.portfolio.resting


def test_paper_bot_step_refuses_quotes_until_ready() -> None:
    bot = PaperBot(Settings(mock=True, dry_run=True, paper_tape=True, series="KXBTC15M"))
    bot.universe.refresh()
    bot.universe.hydrate_books(bot.books)
    bot.reconcile.state.next_retry_ts = datetime.now(UTC) + timedelta(hours=1)
    bot.portfolio.ready_to_trade = False
    bot.execution.ready_to_trade = False
    assert bot.step() == []
    assert bot.execution.dry_run_orders == []
    bot.reconcile.state.next_retry_ts = None
    submitted = bot.step()
    assert submitted
    assert all(q.kind is not None for q in submitted)


def test_hud_shows_syncing_then_ready() -> None:
    bot = _paper_bot()
    before = build_snapshot(bot, MidHistory())
    assert before["reconcile"]["ready_to_trade"] is False
    assert before["reconcile"]["status"] == STATUS_SYNCING
    assert before["reconcile"]["book_verified"] is False
    assert before["reconcile"]["hard_hold"] is False
    assert before["gate"]["new_risk_allowed"] is False
    assert before["gate"]["ready_to_trade"] is False
    bot.step()
    after = build_snapshot(bot, MidHistory())
    assert after["reconcile"]["ready_to_trade"] is True
    assert after["reconcile"]["status"] == STATUS_READY
    assert after["reconcile"]["book_verified"] is True
    assert after["reconcile"]["hard_hold"] is False
    assert after["reconcile"]["source"] == SOURCE_PAPER_LOCAL


def test_list_market_positions_paginates() -> None:
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        assert request.url.path.endswith("/portfolio/positions")
        if hits["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "market_positions": [_yes_position("KXBTC15M-A")],
                    "event_positions": [],
                    "cursor": "page-2",
                },
            )
        return httpx.Response(
            200,
            json={
                "market_positions": [_yes_position("KXBTC15M-B")],
                "event_positions": [],
                "cursor": None,
            },
        )

    client = _client(handler)
    client._headers = lambda method, url, authenticated: {"Accept": "application/json"}  # type: ignore[method-assign]
    try:
        rows = client.list_market_positions()
    finally:
        client.close()
    assert [row["ticker"] for row in rows] == ["KXBTC15M-A", "KXBTC15M-B"]
    assert hits["n"] == 2


def test_list_resting_orders_missing_key_is_partial() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"cursor": None})

    client = _client(handler)
    client._headers = lambda method, url, authenticated: {"Accept": "application/json"}  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="missing orders"):
            client.list_resting_orders()
    finally:
        client.close()


def test_list_fills_paginates_and_missing_key_fails() -> None:
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        assert request.url.path.endswith("/portfolio/fills")
        assert request.url.params.get("min_ts") == "1000"
        if hits["n"] == 1:
            return httpx.Response(
                200,
                json={"fills": [_fill(fill_id="a")], "cursor": "page-2"},
            )
        return httpx.Response(200, json={"fills": [_fill(fill_id="b")], "cursor": None})

    client = _client(handler)
    client._headers = lambda method, url, authenticated: {"Accept": "application/json"}  # type: ignore[method-assign]
    try:
        rows = client.list_fills_since(1000)
    finally:
        client.close()
    assert [row["fill_id"] for row in rows] == ["a", "b"]

    def missing(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"cursor": None})

    client = _client(missing)
    client._headers = lambda method, url, authenticated: {"Accept": "application/json"}  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="missing fills"):
            client.list_fills_since(1000)
    finally:
        client.close()


def test_list_settlements_missing_key_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"cursor": None})

    client = _client(handler)
    client._headers = lambda method, url, authenticated: {"Accept": "application/json"}  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="missing settlements"):
            client.list_settlements_since(1000)
    finally:
        client.close()


def test_pagination_overflow_fail_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={"market_positions": [_yes_position()], "event_positions": [], "cursor": "more"},
        )

    client = _client(handler)
    client._headers = lambda method, url, authenticated: {"Accept": "application/json"}  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="pagination exceeded"):
            client.list_market_positions()
    finally:
        client.close()
    # sanity: cap is finite
    assert PAGINATE_MAX_PAGES == 50


def test_exchange_snapshot_rebuilds_daily_pnl_from_blotter() -> None:
    settings = _settings()
    now = datetime(2026, 9, 14, 12, 30, tzinfo=UTC)
    rest = FakeRest(
        [_yes_position(qty="20.00", exposure="10.0000", realized="-4.0000", fees="0.5000")],
        [],
        fills=[_fill(fee="0.2500", created="2026-09-14T12:00:00Z")],
        settlements=[
            _settlement(
                yes_count="20.00",
                yes_cost="10.0000",
                revenue_cents=0,
                fee="0.4000",
            )
        ],
    )
    rec = _reconciler(settings, rest)
    rec.attempt(now)
    snap = rec.portfolio.snapshot()
    assert rec.state.source == SOURCE_EXCHANGE_SYNC
    # Open-position realized_pnl is ignored; daily kill uses fills + settlements.
    assert rec.state.blotter_fills == 1
    assert rec.state.settlements_applied == 1
    assert snap.realized_pnl == Decimal("-10.0000")  # 0 revenue - $10 cost
    # Fill fee on the open ticker + settlement fee on the settled ticker (no fills there).
    assert snap.fees == Decimal("0.6500")
    assert snap.unrealized_pnl == Decimal("0")
    assert snap.daily_pnl == Decimal("-10.6500")
    assert snap.open_notional == Decimal("10.0000")
    assert snap.unpaired_notional == Decimal("10.0000")
    assert "KXBTC15M-T" in snap.window_ids
    assert rec.portfolio.kill_active is False
    assert rest.fill_min_ts == int(datetime(2026, 9, 14, tzinfo=UTC).timestamp())


def test_settled_same_day_loss_trips_daily_kill_on_restart() -> None:
    settings = _settings()
    now = datetime(2026, 9, 14, 12, 30, tzinfo=UTC)
    rec = _reconciler(
        settings,
        FakeRest(
            [],
            [],
            fills=[],
            settlements=[
                _settlement(
                    yes_count="20.00",
                    yes_cost="10.0000",
                    revenue_cents=0,
                    fee="0.5000",
                )
            ],
        ),
    )
    rec.attempt(now)
    assert rec.state.source == SOURCE_EXCHANGE_SYNC
    snap = rec.portfolio.snapshot()
    assert snap.realized_pnl == Decimal("-10.0000")
    assert snap.fees == Decimal("0.5000")  # no fills; settlement fee included
    assert snap.daily_pnl == Decimal("-10.5000")
    engine = RiskEngine(settings)
    assert engine.maybe_trip_limits(snap) is True
    assert classify_kill(engine.kill_reason) == "loss"


def test_bad_price_resting_fail_closed() -> None:
    settings = _settings()
    bad = _resting_order()
    bad["yes_price_dollars"] = "0"
    bad["no_price_dollars"] = "0"
    rec = _reconciler(settings, FakeRest([], [bad]))
    state = rec.attempt()
    assert state.ready_to_trade is False
    assert state.status == STATUS_NOT_READY
    hud = state.as_hud(cancel_orphans=False)
    assert hud["hard_hold"] is True
    assert hud["book_verified"] is False
    assert rec.portfolio.ready_to_trade is False
    assert rec.portfolio.resting == {}
    assert "unparseable resting price" in state.error


def test_fills_endpoint_failure_fail_closed() -> None:
    settings = _settings()
    rec = _reconciler(settings, FakeRest([_yes_position()], [], fail_fills=True))
    state = rec.attempt()
    assert state.status == STATUS_NOT_READY
    assert rec.portfolio.ready_to_trade is False
    assert "fills" in state.error.lower()


def test_unparseable_fill_fail_closed() -> None:
    settings = _settings()
    bad = _fill()
    bad["yes_price_dollars"] = "0"
    bad["no_price_dollars"] = "0"
    rec = _reconciler(settings, FakeRest([], [], fills=[bad]))
    state = rec.attempt(datetime(2026, 9, 14, 12, 30, tzinfo=UTC))
    assert state.status == STATUS_NOT_READY
    assert "unparseable fill price" in state.error


def test_rebuilt_state_still_enforces_option_b_caps() -> None:
    settings = _settings()
    rec = _reconciler(
        settings,
        FakeRest([_yes_position(qty="50.00", exposure="25.0000")], []),
    )
    rec.attempt()
    snap = rec.portfolio.snapshot()
    assert snap.ready_to_trade is True
    assert snap.open_notional == Decimal("25.0000")
    assert snap.unpaired_notional == Decimal("25.0000")
    engine = RiskEngine(settings)
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    close = now + timedelta(minutes=10)
    entry = engine.evaluate(_entry_intent(), snap, close_time=close, now=now)
    assert entry.allowed is False
    assert entry.reason in {
        RejectReason.OPEN_NOTIONAL,
        RejectReason.ONESIDED_CAP,
        RejectReason.UNPAIRED_EXISTS,
        RejectReason.KILL_SWITCH,
    }
    tiny = QuoteIntent(
        market_ticker="KXBTC15M-T",
        event_ticker="KXBTC15M-T",
        outcome=Outcome.YES,
        price=Decimal("0.50"),
        count=Decimal("10"),
        liquidity=Liquidity.MAKER,
        kind=IntentKind.ENTRY,
    )
    clip = engine.evaluate(tiny, empty_snapshot(settings), close_time=close, now=now)
    assert clip.allowed is False
    assert clip.reason is RejectReason.PER_FILL
    flatten = engine.evaluate(_flatten_intent(), snap, close_time=close, now=now)
    assert flatten.allowed is True


def test_fail_closed_hard_hold_never_silent_empty_book() -> None:
    bot = _paper_bot()
    rest = FakeRest(error=RuntimeError("network down"))
    bot.reconcile.mock = False
    bot.reconcile.rest = rest  # type: ignore[assignment]
    submitted = bot.step()
    assert all(q.kind is not IntentKind.ENTRY for q in submitted)
    assert all(q.kind is not IntentKind.COMPLETE_PAIR for q in submitted)
    hud = build_snapshot(bot, MidHistory())
    assert hud["reconcile"]["status"] == STATUS_NOT_READY
    assert hud["reconcile"]["ready_to_trade"] is False
    assert hud["reconcile"]["hard_hold"] is True
    assert hud["reconcile"]["book_verified"] is False
    assert hud["positions"] == []
    assert hud["gate"]["new_risk_allowed"] is False
    assert rest.created_orders == []


def test_rebuilt_over_soft_onesided_flattens_after_sync() -> None:
    bot = _paper_bot()
    ticker = next(iter(bot.universe.markets))
    rest = FakeRest([_yes_position(ticker, qty="40.00", exposure="20.0000")], [])
    bot.reconcile.mock = False
    bot.reconcile.rest = rest  # type: ignore[assignment]
    submitted = bot.step()
    assert bot.portfolio.ready_to_trade is True
    assert bot.reconcile.state.source == SOURCE_EXCHANGE_SYNC
    assert any(q.kind is IntentKind.FLATTEN for q in submitted)
    assert not any(q.kind is IntentKind.ENTRY for q in submitted)
    assert rest.created_orders == []


def test_rebuilt_aged_unpaired_still_aborts() -> None:
    bot = _paper_bot()
    ticker = next(iter(bot.universe.markets))
    now = datetime.now(UTC)
    aged = (now - timedelta(seconds=46)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rest = FakeRest(
        [
            _yes_position(
                ticker,
                qty="20.00",
                exposure="8.0000",
                last_updated_ts=aged,
            )
        ],
        [],
    )
    bot.reconcile.mock = False
    bot.reconcile.rest = rest  # type: ignore[assignment]
    submitted = bot.step(now=now)
    assert bot.portfolio.ready_to_trade is True
    pos = bot.portfolio.positions[ticker]
    assert pos.unpaired_since is not None
    assert any(q.kind is IntentKind.FLATTEN for q in submitted)
    assert not any(q.kind is IntentKind.ENTRY for q in submitted)


def test_reconcile_does_not_clear_persisted_daily_loss_latch() -> None:
    bot = _paper_bot(paper_tape=False)
    ticker = next(iter(bot.universe.markets))
    bot.risk.maybe_trip_daily(empty_snapshot(bot.settings, daily_pnl=Decimal("-21")))
    latch = Path(bot.settings.kill_latch_path)
    assert latch.is_file()
    reason = bot.risk.kill_reason
    assert classify_kill(reason) == "loss"
    rest = FakeRest([_yes_position(ticker, qty="40.00", exposure="20.0000")], [])
    bot.reconcile.mock = False
    bot.reconcile.rest = rest  # type: ignore[assignment]
    bot.step()
    assert latch.is_file()
    assert bot.risk.kill_active is True
    assert classify_kill(bot.risk.kill_reason) == "loss"
    assert bot.risk.kill_reason == reason


def test_paper_reconcile_never_posts_and_allow_production_stays_off() -> None:
    assert Settings().allow_production is False
    rest = FakeRest([], [])
    rec = _reconciler(_settings(), rest)
    rec.attempt()
    assert rest.created_orders == []
    bot = _paper_bot()
    assert bot.settings.allow_production is False
    assert bot.execution.live_submit is False
    bot.step()
    assert bot.settings.allow_production is False
    assert bot.execution.live_submit is False
