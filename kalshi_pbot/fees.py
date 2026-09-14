"""Kalshi quadratic fee model and pair-arb edge math.

Official series fee_type / fee_multiplier come from GET /series/{ticker}.
Crypto 15m series (KXBTC15M, KXETH15M) are believed to be taker-only
`quadratic` (maker = $0). We still compute and log maker drag if the
series reports `quadratic_with_maker_fees`.

Taker fee peaks at mid: 0.07 * 0.5 * 0.5 = $0.0175 per contract (~1.75¢),
so a taker–taker pair costs ~3.5¢ / 3.5% of $1 notional. That is why
taker–taker pairing is almost never viable: you need
Py + Pn + fees < 1, i.e. Py+Pn ≲ 0.965 at the mid after fees.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from kalshi_pbot.types import D

CENTICENT = Decimal("0.0001")
MICRO = Decimal("0.000001")
TAKER_COEFF = Decimal("0.07")
MAKER_COEFF = Decimal("0.0175")
ONE = Decimal("1")


def _ceil(value: Decimal, quant: Decimal) -> Decimal:
    return value.quantize(quant, rounding=ROUND_CEILING)


def model_fee(
    price: Decimal,
    count: Decimal,
    *,
    coefficient: Decimal,
    multiplier: Decimal = ONE,
) -> Decimal:
    """ceil_6dp(M * coeff * C * P * (1-P))."""
    p = D(price)
    c = D(count)
    if p <= 0 or p >= 1 or c <= 0:
        return Decimal("0")
    raw = D(multiplier) * coefficient * c * p * (ONE - p)
    return _ceil(raw, MICRO)


def taker_fee(
    price: Decimal,
    count: Decimal,
    *,
    multiplier: Decimal = ONE,
) -> Decimal:
    return model_fee(price, count, coefficient=TAKER_COEFF, multiplier=multiplier)


def maker_fee(
    price: Decimal,
    count: Decimal,
    *,
    fee_type: str = "quadratic",
    multiplier: Decimal = ONE,
) -> Decimal:
    """Maker fee is $0 on vanilla quadratic series; still computed when enabled."""
    if fee_type in {"quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"}:
        coeff = MAKER_COEFF
        if fee_type == "quadratic_with_combo_maker_fees":
            coeff = MAKER_COEFF * Decimal("2")  # 0.5 vs 0.25 of taker
        return model_fee(price, count, coefficient=coeff, multiplier=multiplier)
    return Decimal("0")


def pair_cost(
    yes_price: Decimal,
    no_price: Decimal,
    count: Decimal,
    *,
    yes_is_taker: bool,
    no_is_taker: bool,
    fee_type: str = "quadratic",
    multiplier: Decimal = ONE,
) -> tuple[Decimal, Decimal]:
    """Return (gross_premium, fees) for buying `count` of YES and NO."""
    premium = (D(yes_price) + D(no_price)) * D(count)
    fees = Decimal("0")
    if yes_is_taker:
        fees += taker_fee(yes_price, count, multiplier=multiplier)
    else:
        fees += maker_fee(yes_price, count, fee_type=fee_type, multiplier=multiplier)
    if no_is_taker:
        fees += taker_fee(no_price, count, multiplier=multiplier)
    else:
        fees += maker_fee(no_price, count, fee_type=fee_type, multiplier=multiplier)
    return premium, fees


def pair_edge(
    yes_price: Decimal,
    no_price: Decimal,
    count: Decimal,
    *,
    yes_is_taker: bool,
    no_is_taker: bool,
    fee_type: str = "quadratic",
    multiplier: Decimal = ONE,
) -> Decimal:
    """Locked settlement edge: count * $1 − premium − fees."""
    premium, fees = pair_cost(
        yes_price,
        no_price,
        count,
        yes_is_taker=yes_is_taker,
        no_is_taker=no_is_taker,
        fee_type=fee_type,
        multiplier=multiplier,
    )
    return D(count) * ONE - premium - fees


def taker_pair_viable(
    yes_ask: Decimal,
    no_ask: Decimal,
    count: Decimal,
    *,
    min_edge: Decimal,
    fee_type: str = "quadratic",
    multiplier: Decimal = ONE,
) -> bool:
    return (
        pair_edge(
            yes_ask,
            no_ask,
            count,
            yes_is_taker=True,
            no_is_taker=True,
            fee_type=fee_type,
            multiplier=multiplier,
        )
        >= D(min_edge) * D(count)
    )


def arb_taker_eligible(
    yes_ask: Decimal,
    no_ask: Decimal,
    count: Decimal = ONE,
    *,
    fee_type: str = "quadratic",
    multiplier: Decimal = ONE,
) -> bool:
    """Dig4 Regime A: ``ask_sum + modeled_taker_fees / C < 1``.

    Equivalent to ``pair_edge > 0`` when both legs are taker. This is *not*
    maker underround (``bid_sum ≤ 1 − min_edge``) and does not use min_edge.
    """
    c = D(count)
    if c <= 0:
        return False
    _premium, fees = pair_cost(
        yes_ask,
        no_ask,
        c,
        yes_is_taker=True,
        no_is_taker=True,
        fee_type=fee_type,
        multiplier=multiplier,
    )
    ask_sum = D(yes_ask) + D(no_ask)
    return ask_sum + fees / c < ONE


@dataclass(frozen=True)
class FeeDrag:
    charged: Decimal
    assumed_maker: Decimal
    pending_demo_confirm: bool
    fee_type: str


def fee_drag(
    price: Decimal,
    count: Decimal,
    *,
    is_taker: bool,
    fee_type: str = "quadratic",
    multiplier: Decimal = ONE,
) -> FeeDrag:
    """Loggable fee for a fill. Maker $0 on quadratic is pending demo confirmation."""
    if is_taker:
        charged = taker_fee(price, count, multiplier=multiplier)
        return FeeDrag(charged, Decimal("0"), False, fee_type)
    assumed = maker_fee(price, count, fee_type=fee_type, multiplier=multiplier)
    pending = fee_type == "quadratic"
    return FeeDrag(assumed, assumed, pending, fee_type)


def maker_pair_viable(
    yes_bid: Decimal,
    no_bid: Decimal,
    count: Decimal,
    *,
    min_edge: Decimal,
    fee_type: str = "quadratic",
    multiplier: Decimal = ONE,
) -> bool:
    return (
        pair_edge(
            yes_bid,
            no_bid,
            count,
            yes_is_taker=False,
            no_is_taker=False,
            fee_type=fee_type,
            multiplier=multiplier,
        )
        >= D(min_edge) * D(count)
    )
