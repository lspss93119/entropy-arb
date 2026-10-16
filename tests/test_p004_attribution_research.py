"""P0-04 synthetic economics: characterize matched fill edge vs all-leg cashflow.

No venue clients, credentials, network, orders, or trading-code modifications.
These are illustrative executions, not historical fills or live PnL.
"""
from dataclasses import dataclass
import pytest


@dataclass(frozen=True)
class Leg:
    side: str
    qty: float
    px: float
    fee_bps: float = 0.0

    def cashflow(self):
        fee = self.qty * self.px * self.fee_bps / 10000
        return (self.qty * self.px if self.side == "sell" else -self.qty * self.px) - fee


def current_engine_matched_edge(buy_qty, sell_qty, buy_px, sell_px,
                                buy_fee_bps=0, sell_fee_bps=0):
    """Formula used by main engine.py for paired fills, excluding extra hedge."""
    if min(buy_qty, sell_qty) <= 0 or buy_px is None or sell_px is None:
        return 0.0
    matched = min(buy_qty, sell_qty)
    return matched * (sell_px * (1 - sell_fee_bps/10000)
                      - buy_px * (1 + buy_fee_bps/10000))


def complete_cashflow(*legs):
    """Cashflow after illustrative repair trades; valid for flat final inventory."""
    assert abs(sum(x.qty * (1 if x.side == "buy" else -1) for x in legs)) < 1e-9
    return sum(x.cashflow() for x in legs)


@pytest.mark.parametrize("name,buy_qty,sell_qty,buy_px,sell_px,repair,expected_edge,actual_net", [
    ("both_full_with_slippage", 1, 1, 100.10, 100.12, None, 0.02, 0.02),
    ("buy_zero_sell_full_then_repair", 0, 1, None, 100.20, Leg("buy",1,100.35), 0.00, -0.15),
    ("buy_partial_sell_full_then_repair", 0.4, 1, 100.00, 100.20, Leg("buy",0.6,100.50), 0.08, -0.10),
    ("both_full_adverse_exit_prices", 1, 1, 100.30, 100.10, None, -0.20, -0.20),
])
def test_synthetic_cases(name,buy_qty,sell_qty,buy_px,sell_px,repair,expected_edge,actual_net):
    matched = current_engine_matched_edge(buy_qty,sell_qty,buy_px,sell_px)
    assert matched == pytest.approx(expected_edge,abs=1e-9)
    legs=[]
    if buy_qty:
        legs.append(Leg("buy",buy_qty,buy_px))
    if sell_qty:
        legs.append(Leg("sell",sell_qty,sell_px))
    if repair:
        legs.append(repair)
    net=complete_cashflow(*legs)
    assert net == pytest.approx(actual_net,abs=1e-9)
    print(f"P0-04 {name}: matched_edge={matched:+.4f} complete_flat_cashflow={net:+.4f}")
    if repair:
        assert matched != pytest.approx(net,abs=1e-9)


def test_fees_reduce_both_expected_and_realized_edge():
    matched=current_engine_matched_edge(1,1,100,100.2,5,5)
    net=complete_cashflow(Leg("buy",1,100,5),Leg("sell",1,100.2,5))
    assert matched == pytest.approx(net)
    assert matched == pytest.approx(0.0999)
