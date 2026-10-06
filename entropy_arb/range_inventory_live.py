"""Thin Range planner and atomic independent state; never submits an order."""
from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from typing import Mapping, Optional

from .book import ArbPlan, OrderBook, floor_step, plan_arb
from .range_inventory import DEFAULT_PARAMS, RangeInventoryCore, RangeSignal, signal_action

log = logging.getLogger("engine")
STRATEGY_VERSION = "range-inventory-v1"
MAX_SIGNAL_AGE_SEC = 15.0
CANARY_PARAMS = replace(DEFAULT_PARAMS, long_cap_usd=1500.0,
                        short_cap_usd=1500.0, hard_cap_usd=1500.0,
                        max_adjust_usd=53.0)
EPS = 1e-9


class RangeStateError(RuntimeError):
    """Untrusted inventory, state or settlement; caller must persist HALT."""


@dataclass(frozen=True)
class RangePlan:
    plan: ArbPlan
    direction: str
    action: str
    signal: RangeSignal
    current_inventory_usd: float
    requested_adjustment_usd: float
    depth_used_fraction: float
    reference_price: float


def finite(value, label):
    if isinstance(value, bool):
        raise RangeStateError(f"{label} must be numeric, not bool")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise RangeStateError(f"{label} must be numeric") from exc
    if not math.isfinite(out):
        raise RangeStateError(f"{label} must be finite")
    return out


class RangeInventoryLive:
    def __init__(self, path: str, *, symbol: str, hedge: str,
                 params=CANARY_PARAMS) -> None:
        self.path, self.symbol, self.hedge, self.params = path, symbol, hedge, params
        self.core = RangeInventoryCore(params=params, use_range_gate=True)
        self.loaded = False
        self.signal: Optional[RangeSignal] = None
        self._last_ingested = -1.0
        self.state = {
            "schema_version": 1, "strategy_version": STRATEGY_VERSION,
            "symbol": symbol, "hedge": hedge, "parameters": asdict(params),
            "max_signal_age_sec": MAX_SIGNAL_AGE_SEC,
            "current_target_usd": None, "current_direction": None,
            "entropy_qty": 0.0, "hedge_qty": 0.0,
            "latest_completed_minute_ts": None, "signal_timestamp": None,
            "signal_metadata": None, "consumed_minute_ts": None,
            "pending_intent": None, "mean_cost_per_base": None,
            "cumulative_realized_capture_usd": 0.0,
        }

    @property
    def signed_qty(self) -> float:
        return self.state["entropy_qty"]

    def expected_positions(self) -> dict:
        return {"entropy": self.state["entropy_qty"], "hedge": self.state["hedge_qty"]}

    def _validate(self, state) -> None:
        if not isinstance(state, dict) or set(state) != set(self.state):
            raise RangeStateError("invalid range state schema")
        for key in ("schema_version", "strategy_version", "symbol", "hedge",
                    "parameters", "max_signal_age_sec"):
            if state[key] != self.state[key]:
                raise RangeStateError(f"range state {key} mismatch")
        eq, hq = finite(state["entropy_qty"], "entropy_qty"), finite(state["hedge_qty"], "hedge_qty")
        if abs(eq + hq) > EPS:
            raise RangeStateError("unpaired range state")
        direction = "buy_entropy" if eq > 0 else "sell_entropy" if eq < 0 else None
        if state["current_direction"] != direction:
            raise RangeStateError("range direction mismatch")
        for key in ("current_target_usd", "mean_cost_per_base", "cumulative_realized_capture_usd"):
            if state[key] is not None:
                finite(state[key], key)
        target = state["current_target_usd"]
        if target is not None and abs(target) > self.params.hard_cap_usd + EPS:
            raise RangeStateError("target exceeds hard cap")
        for key in ("latest_completed_minute_ts", "signal_timestamp", "consumed_minute_ts"):
            if state[key] is not None and finite(state[key], key) < 0:
                raise RangeStateError(f"negative {key}")
        minute, stamp = state["latest_completed_minute_ts"], state["signal_timestamp"]
        if ((minute is None) != (stamp is None)
                or (minute is not None and (minute % 60 != 0 or stamp != minute + 60))):
            raise RangeStateError("invalid completed-minute timestamps")
        consumed = state["consumed_minute_ts"]
        if consumed is not None and (minute is None or consumed > minute or consumed % 60 != 0):
            raise RangeStateError("invalid consumed minute")
        metadata = state["signal_metadata"]
        if (metadata is None) != (minute is None):
            raise RangeStateError("missing range signal metadata")
        if metadata is not None:
            if not isinstance(metadata, dict) or set(metadata) != set(RangeSignal.__dataclass_fields__):
                raise RangeStateError("invalid range signal schema")
            if (metadata["minute_ts"] != minute or metadata["completed_ts"] != stamp
                    or metadata["target_usd"] != target):
                raise RangeStateError("range signal/state mismatch")
            for key in ("long_percentile", "short_percentile"):
                if metadata[key] is not None and not 0 <= finite(metadata[key], key) <= 1:
                    raise RangeStateError("invalid signal percentile")
            if metadata["range_4h_bps"] is not None and finite(metadata["range_4h_bps"], "range") < 0:
                raise RangeStateError("invalid signal range")
            if (metadata["range_gate_open"] not in (True, False, None)
                    or not isinstance(metadata["exposure_blocked"], bool)
                    or metadata["action"] not in ("none", "blocked", "long_build", "long_release", "short_build", "short_cover")):
                raise RangeStateError("invalid signal gate/action")
        if state["pending_intent"] is not None and not isinstance(state["pending_intent"], dict):
            raise RangeStateError("invalid in-flight intent")

    def validate_positions(self, positions: Mapping[str, float]) -> None:
        for key, expected in self.expected_positions().items():
            if key not in positions or abs(finite(positions[key], key) - expected) > EPS:
                raise RangeStateError(f"range position mismatch: {key} expected={expected}")
        if abs(sum(finite(positions[k], k) for k in ("entropy", "hedge"))) > EPS:
            raise RangeStateError("range net mismatch")

    def load_and_reconcile(self, positions: Mapping[str, float]) -> None:
        try:
            if os.path.exists(self.path):
                with open(self.path) as fh:
                    saved = json.load(fh)
                self._validate(saved)
                if saved["pending_intent"] is not None:
                    raise RangeStateError("unresolved in-flight range intent; operator review required")
                self.state = saved
            self.validate_positions(positions)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise RangeStateError(f"range state load failed: {exc}") from exc
        self.loaded = True

    def _commit(self, state: dict) -> None:
        self._validate(state)
        directory = os.path.dirname(os.path.abspath(self.path))
        name = None
        try:
            os.makedirs(directory, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as fh:
                name = fh.name
                json.dump(state, fh, sort_keys=True, allow_nan=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(name, self.path)
            name = None
            with open(self.path) as fh:
                if json.load(fh) != state:
                    raise RangeStateError("range state read-back mismatch")
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except (OSError, ValueError, TypeError) as exc:
            raise RangeStateError(f"cannot persist range state: {exc}") from exc
        finally:
            if name is not None:
                os.unlink(name)
        self.state = state

    def on_minute(self, row: Mapping, *, now: float) -> bool:
        if not self.loaded:
            raise RangeStateError("range state not reconciled")
        ts = finite(row.get("minute_ts"), "minute_ts")
        now = finite(now, "now")
        if ts < 0 or ts % 60 != 0:
            raise RangeStateError("minute timestamp must be on the minute grid")
        previous = self.state["latest_completed_minute_ts"]
        if (now < ts + 60 or ts <= self._last_ingested
                or (previous is not None and ts <= previous)):
            return False
        try:
            normalized = self.core._row(row)
            self.signal = self.core.on_row(normalized, inventory_usd=self.signed_qty * self.core._ref(normalized))
        except ValueError as exc:
            raise RangeStateError(str(exc)) from exc
        self._last_ingested = ts
        state = dict(self.state, current_target_usd=self.signal.target_usd,
                     latest_completed_minute_ts=ts, signal_timestamp=ts + 60,
                     signal_metadata=asdict(self.signal))
        self._commit(state)
        return True

    def signal_block(self, now: float, *, reserved=False) -> Optional[str]:
        if not self.loaded:
            return "state_not_reconciled"
        if self.signal is None:
            return "no_signal"
        age = now - self.signal.completed_ts
        if not math.isfinite(age) or age < 0 or age > MAX_SIGNAL_AGE_SEC:
            return "stale_signal"
        if self.state["pending_intent"] is not None and not reserved:
            return "inflight"
        if self.state["consumed_minute_ts"] == self.signal.minute_ts and not reserved:
            return "minute_consumed"
        if self.signal.target_usd is None:
            return "coverage"
        if self.signal.exposure_blocked:
            return "range_gate"
        return None

    def plan(self, *, now, entropy, hedge, step, min_base, min_notional,
             max_order_notional, staleness_sec):
        block = self.signal_block(now)
        if block:
            return None, block
        self.validate_positions({"entropy": entropy.position, "hedge": hedge.position})
        prices = [entropy.book.best_bid(), entropy.book.best_ask(),
                  hedge.book.best_bid(), hedge.book.best_ask()]
        if any(px is None or not math.isfinite(px) or px <= 0 for px in prices):
            return None, "empty_book"
        if prices[0] > prices[1] or prices[2] > prices[3]:
            return None, "crossed_book"
        if any(not v.book.ready or not math.isfinite(v.book.last_update_ts)
               or not 0 <= now - v.book.last_update_ts <= staleness_sec
               for v in (entropy, hedge)):
            return None, "stale_book"
        ref, risk_ref = sum(prices) / 4.0, max(prices)
        inv = self.signed_qty * ref
        target = self.signal.target_usd
        # Re-apply the exposure gate against actual current inventory / price.
        if self.signal.range_gate_open is not True and abs(target) > abs(inv) + EPS:
            target = math.copysign(min(abs(target), abs(inv)), self.signed_qty) if self.signed_qty else 0.0
        if self.signed_qty * target < 0:
            target = 0.0
        need = target / ref - self.signed_qty
        action = signal_action(inv, target)
        if abs(need) < 1e-12:
            return None, "at_target"
        reduce = self.signed_qty * need < 0
        if reduce:
            need = math.copysign(min(abs(need), abs(self.signed_qty)), need)
        buy, sell = (entropy, hedge) if need > 0 else (hedge, entropy)
        cap_qty = min(abs(need), self.params.max_adjust_usd / risk_ref,
                      max_order_notional / risk_ref)
        if not reduce:
            direction_cap = self.params.long_cap_usd if need > 0 else self.params.short_cap_usd
            for venue in (entropy, hedge):
                venue_ref = max(venue.book.best_bid(), venue.book.best_ask())
                cap_qty = min(cap_qty, max(0.0, min(venue.cap_usd, direction_cap,
                                                  self.params.hard_cap_usd) / venue_ref - abs(venue.position)))
        ask, bid = buy.book.best_ask(), sell.book.best_bid()
        depth = min(buy.book.asks[ask], sell.book.bids[bid])
        if not math.isfinite(depth) or depth <= 0:
            return None, "empty_depth"
        bb, sb = OrderBook(), OrderBook()
        bb.asks, sb.bids = {ask: buy.book.asks[ask]}, {bid: sell.book.bids[bid]}
        planned, reason = plan_arb(bb, sb, threshold_bps=0.0,
            buy_fee_bps=buy.fee_bps, sell_fee_bps=sell.fee_bps,
            take_fraction=self.params.depth_fraction,
            cap_notional=floor_step(cap_qty, step) * ask, size_step=step,
            min_base=min_base, min_notional=min_notional, require_edge=False)
        if planned is None:
            return None, reason
        planned = replace(planned, reduce_only=reduce)
        return RangePlan(planned, "buy_entropy" if need > 0 else "sell_entropy",
                         action, self.signal, inv, need * ref,
                         planned.qty / depth, ref), "ok"

    def reserve(self, result: RangePlan, *, now: float) -> None:
        block = self.signal_block(now)
        if block or result.signal != self.signal:
            raise RangeStateError(f"range reserve rejected: {block or 'signal changed'}")
        intent = {"minute_ts": result.signal.minute_ts,
                  "direction": result.direction, "qty": result.plan.qty,
                  "reduce_only": result.plan.reduce_only, "action": result.action,
                  "signal_age_ms": (now - result.signal.completed_ts) * 1000,
                  "depth_used_fraction": result.depth_used_fraction,
                  "requested_adjustment_usd": result.requested_adjustment_usd}
        self._commit(dict(self.state, consumed_minute_ts=result.signal.minute_ts,
                          pending_intent=intent))

    def abandon_unsent(self) -> None:
        """Only for a proven zero-order pre-submit rejection; minute stays spent."""
        self._commit(dict(self.state, pending_intent=None))

    def settle(self, positions: Mapping, fills: list[dict], *, unresolved=False) -> dict:
        intent = self.state["pending_intent"]
        if intent is None or unresolved:
            raise RangeStateError("range settlement unresolved or without intent")
        before = self.signed_qty
        expected = self.expected_positions()
        cash, priced = 0.0, True
        for fill in fills:
            venue, side = fill.get("venue"), fill.get("side")
            if venue not in expected or side not in ("buy", "sell"):
                raise RangeStateError("unknown range fill identity")
            qty = finite(fill.get("qty"), "fill qty")
            if qty < 0:
                raise RangeStateError("negative range fill")
            expected[venue] += qty if side == "buy" else -qty
            if qty <= 0:
                continue
            px = fill.get("avg_px")
            if px is None:
                priced = False
                continue
            px, fee = finite(px, "fill price"), finite(fill.get("fee_bps"), "fee") / 1e4
            if px <= 0 or fee < 0:
                raise RangeStateError("invalid range fill price/fee")
            cash += -qty * px * (1 + fee) if side == "buy" else qty * px * (1 - fee)
        for key in expected:
            if abs(finite(positions.get(key), key) - expected[key]) > EPS:
                raise RangeStateError("range actual-fill position mismatch")
        after = expected["entropy"]
        if abs(after + expected["hedge"]) > EPS:
            raise RangeStateError("range residual unresolved")
        if abs(after) < 1e-12:
            after = 0.0
        delta = after - before
        sign = 1 if intent["direction"] == "buy_entropy" else -1
        if delta * sign < -EPS or abs(delta) > intent["qty"] + EPS or before * after < 0:
            raise RangeStateError("range settlement exceeds intent or reverses direction")
        cost = self.state["mean_cost_per_base"]
        realized = 0.0
        cumulative = self.state["cumulative_realized_capture_usd"]
        if abs(delta) > 1e-12:
            if intent["reduce_only"]:
                if abs(after) > abs(before) + EPS:
                    raise RangeStateError("reduce increased inventory")
                realized = cash - cost * abs(delta) if priced and cost is not None else None
                cumulative = cumulative + realized if cumulative is not None and realized is not None else None
                if after == 0:
                    cost = None
            else:
                cost = ((abs(before) * (cost or 0.0) - cash) / abs(after)
                        if priced and (cost is not None or before == 0) else None)
        elif fills and abs(cash) > 1e-12:
            # Paired quantity may be unchanged after trimming an unpaired fill.
            # Keep the actual safety-hedge cash flow, not a guessed execution.
            realized = cash if priced else None
            cumulative = cumulative + realized if cumulative is not None and realized is not None else None
        direction = "buy_entropy" if after > 0 else "sell_entropy" if after < 0 else None
        self._commit(dict(self.state, entropy_qty=after, hedge_qty=-after,
                          current_direction=direction, mean_cost_per_base=cost,
                          cumulative_realized_capture_usd=cumulative,
                          pending_intent=None))
        if realized is None or not priced:
            log.warning("range realized capture unavailable: missing actual fill price")
        return {"paired_fill_qty": abs(delta), "realized_capture_usd": realized,
                "inventory_qty": after, "action": intent["action"]}
