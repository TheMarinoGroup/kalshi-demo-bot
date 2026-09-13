from __future__ import annotations

from decimal import Decimal

from kalshi_pbot.fees import maker_fee, maker_pair_viable, pair_edge


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
