from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
    Reconciler,
    may_cancel_orphans,
)
from kalshi_pbot.runner import PaperBot
from kalshi_pbot.tape import JsonlTape
from kalshi_pbot.types import Liquidity, Outcome, QuoteIntent
from tests.test_kalshi_client import _client


class FakeRest:
    def __init__(
        self,
        positions: list[dict] | None = None,
        orders: list[dict] | None = None,
        *,
        error: Exception | None = None,
        fail_orders: bool = False,
        cancel_status: int = 200,
    ) -> None:
        self.positions = positions if positions is not None else []
        self.orders = orders if orders is not None else []
        self.error = error
        self.fail_orders = fail_orders
        self.cancel_status = cancel_status
        self.cancelled: list[tuple[str, str]] = []

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

    def cancel_order(self, order_id: str, market_ticker: str) -> httpx.Response:
        self.cancelled.append((order_id, market_ticker))
        return httpx.Response(self.cancel_status, json={"order_id": order_id})


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
) -> dict:
    return {
        "ticker": ticker,
        "event_ticker": ticker,
        "position_fp": qty,
        "market_exposure_dollars": exposure,
        "realized_pnl_dollars": "0.0000",
        "fees_paid_dollars": "0.0000",
        "last_updated_ts": "2026-09-14T12:00:00Z",
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


def test_hud_shows_reconciling_then_ready() -> None:
    bot = PaperBot(Settings(mock=True, dry_run=True, paper_tape=True, series="KXBTC15M"))
    bot.universe.refresh()
    bot.universe.hydrate_books(bot.books)
    before = build_snapshot(bot, MidHistory())
    assert before["reconcile"]["ready_to_trade"] is False
    assert before["reconcile"]["status"] in {"RECONCILING", "NOT READY"}
    assert before["gate"]["new_risk_allowed"] is False
    assert before["gate"]["ready_to_trade"] is False
    bot.step()
    after = build_snapshot(bot, MidHistory())
    assert after["reconcile"]["ready_to_trade"] is True
    assert after["reconcile"]["status"] == STATUS_READY
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
