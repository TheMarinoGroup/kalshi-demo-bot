from __future__ import annotations

from decimal import Decimal

from kalshi_pbot.fees import (
    arb_taker_eligible,
    fee_drag,
    maker_fee,
    maker_pair_viable,
    pair_cost,
    pair_edge,
)


def test_maker_fee_zero_on_quadratic_series() -> None:
    assert maker_fee(Decimal("0.50"), Decimal("40"), fee_type="quadratic") == Decimal("0")


def test_maker_fee_nonzero_when_series_enables_it() -> None:
    fee = maker_fee(
        Decimal("0.50"),
        Decimal("1"),
        fee_type="quadratic_with_maker_fees",
    )
    assert fee == Decimal("0.004375")


def test_maker_pair_viable_when_sum_below_one() -> None:
    assert maker_pair_viable(
        Decimal("0.48"), Decimal("0.49"), Decimal("10"), min_edge=Decimal("0.02")
    )
    assert not maker_pair_viable(
        Decimal("0.50"), Decimal("0.50"), Decimal("10"), min_edge=Decimal("0.02")
    )


def test_taker_pair_edge_negative_near_mid() -> None:
    edge = pair_edge(
        Decimal("0.50"),
        Decimal("0.50"),
        Decimal("1"),
        yes_is_taker=True,
        no_is_taker=True,
    )
    assert edge < 0
    # 1 - 1.00 - 0.035 = -0.035
    assert edge == Decimal("-0.035000")


def test_fee_drag_maker_quadratic_pending_confirm() -> None:
    drag = fee_drag(Decimal("0.50"), Decimal("10"), is_taker=False, fee_type="quadratic")
    assert drag.charged == Decimal("0")
    assert drag.assumed_maker == Decimal("0")
    assert drag.pending_demo_confirm is True


def test_fee_drag_taker_not_pending() -> None:
    drag = fee_drag(Decimal("0.50"), Decimal("1"), is_taker=True)
    assert drag.charged == Decimal("0.017500")
    assert drag.pending_demo_confirm is False


def test_arb_taker_eligible_is_after_fee_ask_lock_not_underround() -> None:
    # Typical soft underround (bid_sum 0.95): taking both asks is 1.05 + ~3.5¢ > 1.
    assert not arb_taker_eligible(Decimal("0.53"), Decimal("0.52"))
    _premium, fees = pair_cost(
        Decimal("0.53"),
        Decimal("0.52"),
        Decimal("1"),
        yes_is_taker=True,
        no_is_taker=True,
    )
    assert Decimal("1.05") + fees > 1
    # Crossed book: ask_sum 0.60 + taker fees still < 1.
    assert arb_taker_eligible(Decimal("0.30"), Decimal("0.30"))
    _p2, fees2 = pair_cost(
        Decimal("0.30"),
        Decimal("0.30"),
        Decimal("1"),
        yes_is_taker=True,
        no_is_taker=True,
    )
    assert Decimal("0.60") + fees2 < 1
