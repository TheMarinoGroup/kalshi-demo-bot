"""Order placement / cancel with dry-run default and V2 events API."""

from __future__ import annotations

import uuid
from decimal import Decimal

import structlog

from kalshi_pbot.config import Settings
from kalshi_pbot.kalshi_client import KalshiRestClient
from kalshi_pbot.portfolio import Portfolio
from kalshi_pbot.types import (
    CancelIntent,
    IntentKind,
    Liquidity,
    Outcome,
    QuoteIntent,
    RestingOrder,
    TimeInForce,
)

log = structlog.get_logger(__name__)


def format_price(price: Decimal) -> str:
    return f"{price.quantize(Decimal('0.0001')):.4f}"


def format_count(count: Decimal) -> str:
    return f"{count.quantize(Decimal('0.01')):.2f}"


def build_order_body(intent: QuoteIntent) -> dict[str, object]:
    client_order_id = intent.client_order_id or str(uuid.uuid4())
    body: dict[str, object] = {
        "ticker": intent.market_ticker,
        "side": intent.book_side().value,
        "count": format_count(intent.count),
        "price": format_price(intent.yes_leg_price()),
        "time_in_force": intent.tif.value,
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": client_order_id,
        "post_only": bool(intent.post_only and intent.liquidity is Liquidity.MAKER),
        "reduce_only": intent.reduce_only,
    }
    return body


class ExecutionEngine:
    def __init__(
        self,
        settings: Settings,
        portfolio: Portfolio,
        rest: KalshiRestClient | None = None,
    ) -> None:
        self.settings = settings
        self.portfolio = portfolio
        self.rest = rest
        self.dry_run_orders: list[dict[str, object]] = []

    @property
    def live_submit(self) -> bool:
        return (not self.settings.dry_run) and self.rest is not None

    def submit(self, intent: QuoteIntent) -> dict[str, object]:
        body = build_order_body(intent)
        self.portfolio.orders_submitted += 1
        if not self.live_submit:
            fake_id = f"dry-{body['client_order_id']}"
            self.dry_run_orders.append(body)
            self.portfolio.upsert_resting(
                RestingOrder(
                    order_id=fake_id,
                    client_order_id=str(body["client_order_id"]),
                    market_ticker=intent.market_ticker,
                    event_ticker=intent.event_ticker,
                    outcome=intent.outcome,
                    price=intent.price,
                    remaining=intent.count,
                    post_only=bool(body["post_only"]),
                )
            )
            log.info(
                "dry_run_order",
                **{k: str(v) for k, v in body.items()},
                outcome=intent.outcome.value,
                kind=intent.kind.value,
                reason=intent.reason,
                notional=str(intent.notional),
            )
            return {
                "order_id": fake_id,
                "client_order_id": body["client_order_id"],
                "dry_run": True,
            }

        assert self.rest is not None
        response = self.rest.create_order(body)
        payload = {}
        try:
            payload = response.json()
        except Exception:
            payload = {"text": response.text}
        if response.status_code == 201:
            order_id = str(payload.get("order_id") or body["client_order_id"])
            remaining = Decimal(str(payload.get("remaining_count") or intent.count))
            if remaining > 0:
                self.portfolio.upsert_resting(
                    RestingOrder(
                        order_id=order_id,
                        client_order_id=str(body["client_order_id"]),
                        market_ticker=intent.market_ticker,
                        event_ticker=intent.event_ticker,
                        outcome=intent.outcome,
                        price=intent.price,
                        remaining=remaining,
                        post_only=bool(body["post_only"]),
                    )
                )
            log.info(
                "order_submitted",
                order_id=order_id,
                ticker=intent.market_ticker,
                outcome=intent.outcome.value,
                remaining=str(remaining),
                fill_count=str(payload.get("fill_count", "0")),
                fee=str(payload.get("average_fee_paid", "")),
            )
        else:
            log.warning(
                "order_rejected",
                status=response.status_code,
                body=str(payload)[:400],
                ticker=intent.market_ticker,
            )
        payload["dry_run"] = False
        return payload

    def cancel(self, intent: CancelIntent) -> dict[str, object]:
        if intent.cancel_all:
            return self.cancel_all()
        if not intent.order_id:
            return {"ok": False, "error": "missing order_id"}
        if not self.live_submit:
            dropped = self.portfolio.drop_resting(intent.order_id)
            log.info(
                "dry_run_cancel",
                order_id=intent.order_id,
                ticker=intent.market_ticker,
                reason=intent.reason,
                found=dropped is not None,
            )
            return {"order_id": intent.order_id, "dry_run": True, "reduced_by": "all"}

        assert self.rest is not None
        response = self.rest.cancel_order(intent.order_id, intent.market_ticker)
        if response.status_code == 200:
            self.portfolio.drop_resting(intent.order_id)
        log.info("order_cancel", order_id=intent.order_id, status=response.status_code)
        try:
            return response.json()
        except Exception:
            return {"status": response.status_code}

    def cancel_all(self) -> dict[str, object]:
        ids = list(self.portfolio.resting)
        if not self.live_submit:
            for oid in ids:
                self.portfolio.drop_resting(oid)
            log.info("dry_run_cancel_all", count=len(ids))
            return {"dry_run": True, "cancelled": len(ids)}
        assert self.rest is not None
        response = self.rest.cancel_all_orders()
        if response.status_code in {200, 204}:
            for oid in ids:
                self.portfolio.drop_resting(oid)
        log.info("cancel_all", status=response.status_code, local=len(ids))
        return {"status": response.status_code, "cancelled": len(ids)}

    def cancel_market(self, ticker: str, event_ticker: str, reason: str) -> None:
        for order in list(self.portfolio.resting_for(ticker)):
            self.cancel(
                CancelIntent(
                    market_ticker=ticker,
                    event_ticker=event_ticker,
                    order_id=order.order_id,
                    client_order_id=order.client_order_id,
                    reason=reason,
                )
            )


def flatten_intent(
    ticker: str,
    event_ticker: str,
    outcome: Outcome,
    qty: Decimal,
    price: Decimal,
) -> QuoteIntent:
    """Reduce-only IOC on the unpaired side. Price is the outcome bid we hit."""
    return QuoteIntent(
        market_ticker=ticker,
        event_ticker=event_ticker,
        outcome=outcome,
        price=price,
        count=qty,
        liquidity=Liquidity.TAKER,
        tif=TimeInForce.IOC,
        post_only=False,
        reduce_only=True,
        sell=True,
        kind=IntentKind.FLATTEN,
        reason="flatten_unpaired",
    )
