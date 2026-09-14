"""Kelly size clamp for paper-v2-tight / Dig7.

Caps any increasing-risk size at ``kelly_max`` of bankroll (default 0.25).
This is a hard fraction ceiling, not a full Kelly sizer: it does not compute
p*, Edge, or Family D / path-dependent scale. MM lane only.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from kalshi_pbot.types import D

DEFAULT_KELLY_MAX = Decimal("0.25")


def kelly_fraction(notional: Decimal, bankroll: Decimal) -> Decimal:
    bankroll = D(bankroll)
    if bankroll <= 0:
        return Decimal("0")
    return D(notional) / bankroll


def kelly_cap_notional(bankroll: Decimal, kelly_max: Decimal) -> Decimal:
    return (D(bankroll) * D(kelly_max)).quantize(Decimal("0.0001"))


def over_kelly_max(notional: Decimal, bankroll: Decimal, kelly_max: Decimal) -> bool:
    """True when size fraction is strictly above kelly_max (Kelly ≤ kelly_max)."""
    return kelly_fraction(notional, bankroll) > D(kelly_max)


def count_within_kelly(
    count: Decimal,
    price: Decimal,
    bankroll: Decimal,
    kelly_max: Decimal,
) -> Decimal:
    """Clip contract count so notional does not exceed kelly_max × bankroll."""
    count = D(count)
    price = D(price)
    if count <= 0 or price <= 0:
        return Decimal("0")
    cap_notional = kelly_cap_notional(bankroll, kelly_max)
    cap = (cap_notional / price).to_integral_value(rounding=ROUND_DOWN)
    if cap <= 0:
        return Decimal("0")
    return min(count, cap)
