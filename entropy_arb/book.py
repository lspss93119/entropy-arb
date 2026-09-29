"""Order book state and fee-aware arbitrage sizing.

One book class serves both feed protocols: zkLighter sends a snapshot plus
diffs (dict maintenance), Hyperliquid's l2Book sends full snapshots.
Freshness is connection-based (any inbound ws frame touches alive_ts): a quiet
market is not stale, only a dead feed is.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

Level = Tuple[float, float]


class OrderBook:
    _TELEMETRY_MAX_SAMPLES = 10_000

    def __init__(self) -> None:
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.ready = False
        self.last_update_ts = 0.0
        self.alive_ts = 0.0
        self.last_update_gap_ms: Optional[float] = None
        self.last_server_ts_ms: Optional[float] = None
        self.last_server_age_ms: Optional[float] = None
        self._feed_update_count = 0
        self._feed_gap_ms = deque(maxlen=self._TELEMETRY_MAX_SAMPLES)
        self._feed_age_ms = deque(maxlen=self._TELEMETRY_MAX_SAMPLES)

    def touch(self) -> None:
        self.alive_ts = time.time()

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.ready = False

    def _record_book_update(self, server_ts_ms: Optional[float] = None) -> None:
        """Record compact in-memory feed telemetry for the current window."""
        now = time.time()
        if self.last_update_ts:
            gap_ms = max(0.0, now - self.last_update_ts) * 1000.0
            self.last_update_gap_ms = gap_ms
            self._feed_gap_ms.append(gap_ms)
        self.last_update_ts = now
        self.alive_ts = now
        self._feed_update_count += 1

        self.last_server_ts_ms = None
        self.last_server_age_ms = None
        if server_ts_ms is not None:
            try:
                server_ts = float(server_ts_ms)
            except (TypeError, ValueError):
                server_ts = None
            if server_ts is not None:
                self.last_server_ts_ms = server_ts
                self.last_server_age_ms = max(0.0, now * 1000.0 - server_ts)
                self._feed_age_ms.append(self.last_server_age_ms)

    @staticmethod
    def _percentile(values, q: float) -> Optional[float]:
        ordered = sorted(values)
        if not ordered:
            return None
        position = (len(ordered) - 1) * q
        lower, upper = math.floor(position), math.ceil(position)
        if lower == upper:
            return ordered[lower]
        return (ordered[lower] * (upper - position)
                + ordered[upper] * (position - lower))

    def drain_feed_stats(self) -> dict:
        """Return and reset compact feed statistics since the last drain."""
        gaps = list(self._feed_gap_ms)
        ages = list(self._feed_age_ms)
        stats = {
            "update_count": self._feed_update_count,
            "gap_p50_ms": self._percentile(gaps, 0.50),
            "gap_p95_ms": self._percentile(gaps, 0.95),
            "age_p95_ms": self._percentile(ages, 0.95),
        }
        self._feed_update_count = 0
        self._feed_gap_ms.clear()
        self._feed_age_ms.clear()
        return stats

    def server_age_ms(self, now: Optional[float] = None) -> Optional[float]:
        """Return local signal time minus the latest exchange timestamp."""
        if self.last_server_ts_ms is None:
            return None
        now = time.time() if now is None else now
        return max(0.0, now * 1000.0 - self.last_server_ts_ms)

    # ---- zkLighter snapshot + diff ----
    def apply_lighter(self, ob: dict, snapshot: bool) -> None:
        if snapshot:
            self.bids.clear()
            self.asks.clear()
        for name, side in (("bids", self.bids), ("asks", self.asks)):
            for lvl in ob.get(name) or []:
                px, sz = float(lvl["price"]), float(lvl["size"])
                if sz <= 0:
                    side.pop(px, None)
                else:
                    side[px] = sz
        self.ready = True
        self._record_book_update()

    # ---- Hyperliquid full snapshot ----
    def apply_hl(self, levels: list,
                 server_ts_ms: Optional[float] = None) -> None:
        self.bids = {float(level["px"]): float(level["sz"])
                     for level in levels[0] if float(level["sz"]) > 0}
        self.asks = {float(level["px"]): float(level["sz"])
                     for level in levels[1] if float(level["sz"]) > 0}
        self.ready = True
        self._record_book_update(server_ts_ms)

    def sorted_bids(self) -> List[Level]:
        return sorted(self.bids.items(), key=lambda kv: -kv[0])

    def sorted_asks(self) -> List[Level]:
        return sorted(self.asks.items())

    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None

    def mid(self) -> Optional[float]:
        if not (self.bids and self.asks):
            return None
        return (max(self.bids) + min(self.asks)) / 2.0

    def is_fresh(self, max_age_sec: float) -> bool:
        return self.ready and bool(self.bids) and bool(self.asks) and (
            time.time() - self.alive_ts <= max_age_sec)


def floor_step(x: float, step: float) -> float:
    return round(math.floor(x / step + 1e-9) * step, 12)


def crossable_base(asks: List[Level], bids: List[Level], threshold: float,
                   buy_fee: float = 0.0, sell_fee: float = 0.0,
                   require_edge: bool = True) -> Tuple[float, float]:
    """Walk both books level by level and return (base qty, buy notional) that
    can be crossed while every marginal slice still clears fees + threshold."""
    qty = 0.0
    buy_notional = 0.0
    i = j = 0
    a_px = a_rem = 0.0
    b_px = b_rem = 0.0
    while True:
        if a_rem <= 0:
            if i >= len(asks):
                break
            a_px, a_rem = asks[i]
            i += 1
        if b_rem <= 0:
            if j >= len(bids):
                break
            b_px, b_rem = bids[j]
            j += 1
        if (require_edge and b_px * (1.0 - sell_fee)
                < a_px * (1.0 + buy_fee) * (1.0 + threshold)):
            break
        take = min(a_rem, b_rem)
        qty += take
        buy_notional += take * a_px
        a_rem -= take
        b_rem -= take
    return qty, buy_notional


def walk_depth(levels: List[Level], qty: float) -> Tuple[float, float]:
    remaining = qty
    notional = 0.0
    marginal_px = levels[0][0]
    for px, sz in levels:
        take = min(remaining, sz)
        notional += take * px
        marginal_px = px
        remaining -= take
        if remaining <= 1e-12:
            break
    return marginal_px, notional


@dataclass
class ArbPlan:
    qty: float
    buy_limit: float
    sell_limit: float
    buy_notional: float
    sell_notional: float
    q_max: float
    q_max_notional: float
    top_premium_bps: float
    marginal_premium_bps: float
    buy_fee: float
    sell_fee: float
    reduce_only: bool = False
    # Rolling reduce-only plans carry the exact lots selected by the
    # break-even depth walk. These defaults keep fixed-mode callers unchanged.
    lot_allocations: tuple = ()
    selected_lot_ids: tuple = ()
    expected_exit_capture_bps: Optional[float] = None
    exit_segments: tuple = ()

    @property
    def gross_edge_usd(self) -> float:
        return self.sell_notional - self.buy_notional

    @property
    def exp_edge_usd(self) -> float:
        return (self.sell_notional * (1.0 - self.sell_fee)
                - self.buy_notional * (1.0 + self.buy_fee))


def plan_arb(buy_book: OrderBook, sell_book: OrderBook, *, threshold_bps: float,
             buy_fee_bps: float, sell_fee_bps: float, take_fraction: float,
             cap_notional: float, min_base: float, min_notional: float,
             size_step: float, require_edge: bool = True):
    """Size a two-leg taker slice: buy on buy_book, sell on sell_book.

    A slice qualifies when the executable premium (sell bid over buy ask)
    clears both venues' taker fees plus threshold_bps. Returns
    (ArbPlan | None, reason).
    """
    asks = buy_book.sorted_asks()
    bids = sell_book.sorted_bids()
    if not asks or not bids:
        return None, "empty_book"
    threshold = threshold_bps / 1e4
    buy_fee = buy_fee_bps / 1e4
    sell_fee = sell_fee_bps / 1e4
    top_premium_bps = (bids[0][0] / asks[0][0] - 1.0) * 1e4
    if (require_edge and bids[0][0] * (1.0 - sell_fee)
            < asks[0][0] * (1.0 + buy_fee) * (1.0 + threshold)):
        return None, "no_edge"
    q_max, q_max_notional = crossable_base(
        asks, bids, threshold, buy_fee, sell_fee, require_edge=require_edge)
    if q_max <= 0:
        return None, "no_edge"
    target = min(q_max * take_fraction, cap_notional / asks[0][0])
    target = floor_step(target, size_step)
    if target < min_base:
        return None, "below_min_base"
    buy_limit, buy_notional = walk_depth(asks, target)
    sell_limit, sell_notional = walk_depth(bids, target)
    if buy_notional < min_notional or sell_notional < min_notional:
        return None, "below_min_notional"
    return ArbPlan(
        qty=target, buy_limit=buy_limit, sell_limit=sell_limit,
        buy_notional=buy_notional, sell_notional=sell_notional,
        q_max=q_max, q_max_notional=q_max_notional,
        top_premium_bps=top_premium_bps,
        marginal_premium_bps=(sell_limit / buy_limit - 1.0) * 1e4,
        buy_fee=buy_fee, sell_fee=sell_fee,
    ), "ok"


def plan_reduce_arb(buy_book: OrderBook, sell_book: OrderBook, *,
                    candidates, buy_fee_bps: float, sell_fee_bps: float,
                    take_fraction: float, cap_notional: float,
                    min_base: float, min_notional: float,
                    size_step: float):
    """Plan a reduce-only close without violating any selected lot's BE.

    ``candidates`` are the immutable records returned by
    :meth:`LotLedger.exit_candidates`. The walk uses current opposite-side
    depth, assigns each safe slice to the lot with the best expected capture,
    and stops as soon as the marginal depth cannot satisfy any open lot.
    Unlike :func:`plan_arb`, this planner never applies generic entry
    slippage: its returned limits are the BE-safe protective limits.
    """
    asks = buy_book.sorted_asks()
    bids = sell_book.sorted_bids()
    if not asks or not bids:
        return None, "empty_book"
    if not candidates:
        return None, "no_open_lots"
    if not (0.0 < float(take_fraction) <= 1.0):
        return None, "invalid_take_fraction"
    if cap_notional <= 0:
        return None, "no_headroom"

    buy_fee = float(buy_fee_bps) / 1e4
    sell_fee = float(sell_fee_bps) / 1e4
    remaining = {}
    normalized = []
    for candidate in candidates:
        try:
            lot_id = str(candidate["lot_id"])
            qty = float(candidate["open_qty"])
            entry_cash = float(candidate["entry_cash_per_base"])
            reference = float(candidate["reference_entry_notional_per_base"])
            required = float(candidate["required_exit_cash_per_base"])
        except (KeyError, TypeError, ValueError):
            return None, "invalid_lot_candidate"
        if (not lot_id or not all(math.isfinite(value) for value in
                                  (qty, entry_cash, reference, required))
                or qty <= 0 or reference <= 0):
            return None, "invalid_lot_candidate"
        remaining[lot_id] = qty
        normalized.append({
            "lot_id": lot_id, "open_qty": qty,
            "entry_cash_per_base": entry_cash,
            "reference_entry_notional_per_base": reference,
            "required_exit_cash_per_base": required,
        })

    i = j = 0
    a_px = b_px = 0.0
    a_rem = b_rem = 0.0
    q_max = 0.0
    q_max_notional = 0.0
    segments = []
    while True:
        if a_rem <= 0:
            if i >= len(asks):
                break
            a_px, a_rem = asks[i]
            i += 1
        if b_rem <= 0:
            if j >= len(bids):
                break
            b_px, b_rem = bids[j]
            j += 1
        exit_cash = b_px * (1.0 - sell_fee) - a_px * (1.0 + buy_fee)
        eligible = [candidate for candidate in normalized
                    if remaining[candidate["lot_id"]] > 1e-12
                    and candidate["required_exit_cash_per_base"]
                    <= exit_cash + 1e-10]
        if not eligible:
            break
        # Lower required cash means higher current capture. Keep this ordering
        # stable so the planner is deterministic for equal economics.
        eligible.sort(key=lambda candidate: (
            -(candidate["entry_cash_per_base"] + exit_cash)
            / candidate["reference_entry_notional_per_base"],
            candidate["lot_id"]))
        slice_qty = min(a_rem, b_rem)
        left = slice_qty
        for candidate in eligible:
            if left <= 1e-12:
                break
            lot_id = candidate["lot_id"]
            take = min(left, remaining[lot_id])
            if take <= 1e-12:
                continue
            segments.append({
                "lot_id": lot_id, "qty": take,
                "buy_px": a_px, "sell_px": b_px,
                "exit_cash_per_base": exit_cash,
                "entry_cash_per_base": candidate["entry_cash_per_base"],
                "reference_entry_notional_per_base": candidate[
                    "reference_entry_notional_per_base"],
            })
            remaining[lot_id] -= take
            left -= take
        used = slice_qty - left
        if used <= 1e-12:
            break
        q_max += used
        q_max_notional += used * a_px
        a_rem -= used
        b_rem -= used
        # If the current books have more depth than the remaining eligible
        # lots, there is no reason to inspect further levels.
        if left > 1e-12:
            break

    if q_max <= 0:
        return None, "no_break_even_depth"
    target = min(q_max * float(take_fraction),
                 float(cap_notional) / asks[0][0])
    target = floor_step(target, size_step)
    remaining_qty = sum(candidate["open_qty"] for candidate in normalized)
    terminal_qty = floor_step(remaining_qty, size_step)
    terminal_eligible = (
        remaining_qty >= float(min_base)
        and abs(terminal_qty - remaining_qty) <= 1e-9
        and q_max + 1e-12 >= remaining_qty
        and terminal_qty <= float(cap_notional) / asks[0][0] + 1e-12
    )
    if target < min_base:
        if not terminal_eligible:
            return None, "below_min_base"
        # The full candidate inventory is already covered by the
        # break-even-safe depth walk. Keep the normal sizing path above for
        # every non-terminal reduction, but allow this legal remainder to be
        # attempted as one reduce-only plan.
        target = terminal_qty

    def select_segments(target_qty):
        selected = []
        left = target_qty
        for segment in segments:
            if left <= 1e-12:
                break
            take = min(left, segment["qty"])
            if take > 1e-12:
                selected.append({**segment, "qty": take})
                left -= take
        return selected, left

    selected_segments, left = select_segments(target)
    if left > 1e-9:
        return None, "below_min_base"

    buy_notional = sum(s["qty"] * s["buy_px"] for s in selected_segments)
    sell_notional = sum(s["qty"] * s["sell_px"] for s in selected_segments)
    if buy_notional < min_notional or sell_notional < min_notional:
        # A fractional target can clear min_base while still being too small
        # for a venue's notional minimum. If the full remainder is already
        # BE-safe and executable, use it as the terminal reduction instead.
        if (target < terminal_qty and terminal_eligible):
            target = terminal_qty
            selected_segments, left = select_segments(target)
            if left > 1e-9:
                return None, "below_min_base"
            buy_notional = sum(
                s["qty"] * s["buy_px"] for s in selected_segments)
            sell_notional = sum(
                s["qty"] * s["sell_px"] for s in selected_segments)
        if buy_notional < min_notional or sell_notional < min_notional:
            return None, "below_min_notional"
    buy_limit = max(s["buy_px"] for s in selected_segments)
    sell_limit = min(s["sell_px"] for s in selected_segments)
    top_premium_bps = (bids[0][0] / asks[0][0] - 1.0) * 1e4
    capture_usd = sum(
        s["qty"] * (s["entry_cash_per_base"] + s["exit_cash_per_base"])
        for s in selected_segments)
    reference_notional = sum(
        s["qty"] * s["reference_entry_notional_per_base"]
        for s in selected_segments)
    expected_capture_bps = (capture_usd / reference_notional * 1e4
                            if reference_notional > 0 else None)
    allocation_by_lot = {}
    for segment in selected_segments:
        lot_id = segment["lot_id"]
        allocation_by_lot[lot_id] = (allocation_by_lot.get(lot_id, 0.0)
                                     + segment["qty"])
    allocations = tuple({"lot_id": lot_id, "qty": qty}
                        for lot_id, qty in allocation_by_lot.items())
    selected_ids = tuple(allocation["lot_id"] for allocation in allocations)
    return ArbPlan(
        qty=target, buy_limit=buy_limit, sell_limit=sell_limit,
        buy_notional=buy_notional, sell_notional=sell_notional,
        q_max=q_max, q_max_notional=q_max_notional,
        top_premium_bps=top_premium_bps,
        marginal_premium_bps=(sell_limit / buy_limit - 1.0) * 1e4,
        buy_fee=buy_fee, sell_fee=sell_fee, reduce_only=True,
        lot_allocations=allocations, selected_lot_ids=selected_ids,
        expected_exit_capture_bps=expected_capture_bps,
        exit_segments=tuple(selected_segments),
    ), "ok"
