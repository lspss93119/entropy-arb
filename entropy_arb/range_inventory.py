"""Pure causal Range Inventory signal/target core; no execution or credentials."""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Mapping, Optional

@dataclass(frozen=True)
class FrozenRangeInventoryParams:
    long_window_minutes: int = 240
    short_window_minutes: int = 120
    min_coverage_pct: float = 80.0
    long_entry_pct: float = .30
    long_full_pct: float = .08
    long_release_pct: float = .55
    long_flat_pct: float = .93
    long_cap_usd: float = 8_500.0
    long_gamma: float = .4
    short_entry_pct: float = .80
    short_full_pct: float = .95
    short_release_pct: float = .08
    short_flat_pct: float = .02
    short_cap_usd: float = 8_500.0
    short_gamma: float = 1.0
    hard_cap_usd: float = 10_000.0
    max_adjust_usd: float = 300.0
    depth_fraction: float = .75
    min_trade_notional_usd: float = 10.0
    friction_bps_per_leg: float = .5
    max_signal_to_execution_gap_seconds: float = 120.0
    range_gate_window_minutes: int = 240
    range_gate_low_quantile: float = .10
    range_gate_high_quantile: float = .90
    range_gate_min_bps: float = 10.0


DEFAULT_PARAMS = FrozenRangeInventoryParams()



@dataclass(frozen=True)
class RangeSignal:
    minute_ts: float
    completed_ts: float
    long_percentile: Optional[float]
    short_percentile: Optional[float]
    range_4h_bps: Optional[float]
    range_gate_open: Optional[bool]
    target_usd: Optional[float]
    exposure_blocked: bool
    action: str


def signal_action(inventory_usd: float, target_usd: Optional[float]) -> str:
    if target_usd is None:
        return "blocked"
    if abs(target_usd - inventory_usd) <= 1e-9:
        return "none"
    if inventory_usd > 1e-9:
        return "long_build" if target_usd > inventory_usd else "long_release"
    if inventory_usd < -1e-9:
        return "short_build" if target_usd < inventory_usd else "short_cover"
    return "long_build" if target_usd > 0 else "short_build"


class RangeInventoryCore:
    def __init__(self, *, params: FrozenRangeInventoryParams = DEFAULT_PARAMS,
                 use_range_gate: bool = True) -> None:
        self.params, self.use_range_gate = params, use_range_gate
        n = max(params.long_window_minutes, params.short_window_minutes,
                params.range_gate_window_minutes) + 2
        self._history: deque[tuple[float, float]] = deque(maxlen=n)

    @staticmethod
    def _f(value: object, name: str) -> float:
        try:
            out = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be numeric") from exc
        if not math.isfinite(out):
            raise ValueError(f"{name} must be finite")
        return out

    @classmethod
    def _row(cls, row: Mapping[str, object]) -> dict:
        names = ("minute_ts", "premium_mean_bps", "entropy_bid", "entropy_ask",
                 "entropy_bid_qty", "entropy_ask_qty", "hedge_bid", "hedge_ask",
                 "hedge_bid_qty", "hedge_ask_qty", "samples")
        if any(name not in row for name in (*names, "time_utc")):
            raise ValueError("minute row missing required fields")
        out = {name: cls._f(row[name], name) for name in names}
        out["time_utc"] = str(row["time_utc"])
        if out["samples"] <= 0:
            raise ValueError("samples must be positive")
        if min(out["entropy_bid"], out["entropy_ask"], out["hedge_bid"],
               out["hedge_ask"]) <= 0:
            raise ValueError("BBO prices must be positive")
        if out["entropy_bid"] > out["entropy_ask"] or out["hedge_bid"] > out["hedge_ask"]:
            raise ValueError("crossed BBO")
        if min(out["entropy_bid_qty"], out["entropy_ask_qty"], out["hedge_bid_qty"],
               out["hedge_ask_qty"]) < 0:
            raise ValueError("BBO quantities must be non-negative")
        return out

    @staticmethod
    def _quantile(values: list[float], q: float) -> float:
        values = sorted(values)
        pos = (len(values) - 1) * q
        lo, hi = math.floor(pos), math.ceil(pos)
        return values[lo] if lo == hi else values[lo] * (hi-pos) + values[hi] * (pos-lo)

    @staticmethod
    def _rank(values: list[float], x: float) -> float:
        less, equal = sum(v < x for v in values), sum(v == x for v in values)
        return (less + (equal + 1.0) / 2.0) / len(values)

    def _values(self, ts: float, minutes: int) -> list[float]:
        start = ts - minutes * 60.0
        return [v for t, v in self._history if start < t <= ts]

    def _ready(self, values: list[float], minutes: int) -> bool:
        return len(values) >= math.ceil(minutes * self.params.min_coverage_pct / 100.0)

    @staticmethod
    def _clip(x: float) -> float:
        return max(0.0, min(1.0, x))

    @staticmethod
    def _ref(r: Mapping[str, float]) -> float:
        return (r["entropy_bid"] + r["entropy_ask"] + r["hedge_bid"] + r["hedge_ask"]) / 4.0

    def _long_target(self, pct: float, frac: float) -> float:
        p = self.params
        if pct <= p.long_entry_pct:
            z = self._clip((p.long_entry_pct-pct)/(p.long_entry_pct-p.long_full_pct))
            return p.long_cap_usd * (z**p.long_gamma if frac <= 1e-12 else max(frac, z**p.long_gamma))
        if frac <= 1e-12:
            return 0.0
        if pct < p.long_release_pct:
            return p.long_cap_usd * frac
        remain = self._clip((p.long_flat_pct-pct)/(p.long_flat_pct-p.long_release_pct))
        return p.long_cap_usd * min(frac, remain)

    def _short_target(self, pct: float, frac: float) -> float:
        p = self.params
        if pct >= p.short_entry_pct:
            z = self._clip((pct-p.short_entry_pct)/(p.short_full_pct-p.short_entry_pct))
            return -p.short_cap_usd * (z**p.short_gamma if frac <= 1e-12 else max(frac, z**p.short_gamma))
        if frac <= 1e-12:
            return 0.0
        if pct > p.short_release_pct:
            return -p.short_cap_usd * frac
        remain = self._clip((pct-p.short_flat_pct)/(p.short_release_pct-p.short_flat_pct))
        return -p.short_cap_usd * min(frac, remain)

    def _target(self, inv: float, lp: float, sp: float) -> float:
        p = self.params
        if inv > 1e-9:
            return self._long_target(lp, min(1.0, abs(inv)/p.long_cap_usd))
        if inv < -1e-9:
            return self._short_target(sp, min(1.0, abs(inv)/p.short_cap_usd))
        can_l, can_s = lp <= p.long_entry_pct, sp >= p.short_entry_pct
        if not can_l and not can_s:
            return 0.0
        if can_l and can_s:
            ls = (p.long_entry_pct-lp)/(p.long_entry_pct-p.long_full_pct)
            ss = (sp-p.short_entry_pct)/(p.short_full_pct-p.short_entry_pct)
            can_l = ls >= ss
        return self._long_target(lp, 0.0) if can_l else self._short_target(sp, 0.0)


    def warmup_row(self, row: Mapping[str, object]) -> None:
        r = self._row(row)
        self._history.append((r["minute_ts"], r["premium_mean_bps"]))

    def on_row(self, row: Mapping[str, object], *, inventory_usd: float) -> RangeSignal:
        r = self._row(row)
        if not math.isfinite(inventory_usd):
            raise ValueError("inventory must be finite")
        self._history.append((r["minute_ts"], r["premium_mean_bps"]))
        lv = self._values(r["minute_ts"], self.params.long_window_minutes)
        sv = self._values(r["minute_ts"], self.params.short_window_minutes)
        gv = self._values(r["minute_ts"], self.params.range_gate_window_minutes)
        lp = self._rank(lv, r["premium_mean_bps"]) if self._ready(lv, self.params.long_window_minutes) else None
        sp = self._rank(sv, r["premium_mean_bps"]) if self._ready(sv, self.params.short_window_minutes) else None
        rg = (self._quantile(gv, self.params.range_gate_high_quantile) -
              self._quantile(gv, self.params.range_gate_low_quantile)
              if self._ready(gv, self.params.range_gate_window_minutes) else None)
        gate = None if rg is None else rg >= self.params.range_gate_min_bps
        target, blocked = None, False
        if lp is not None and sp is not None:
            target = max(-self.params.hard_cap_usd, min(
                self.params.hard_cap_usd, self._target(inventory_usd, lp, sp)))
            if self.use_range_gate and abs(target) > abs(inventory_usd) + 1e-9 and gate is not True:
                target, blocked = inventory_usd, True
        action = "blocked" if blocked else signal_action(inventory_usd, target)
        return RangeSignal(r["minute_ts"], r["minute_ts"] + 60, lp, sp, rg,
                           gate, target, blocked, action)
