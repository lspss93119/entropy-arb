"""Causal shadow simulator for the frozen 4h-long / 2h-short inventory strategy.

It never sends orders. A signal from completed minute T can only be executed on
minute T+1 using that minute's recorder BBO/top-of-book depth. Funding is
excluded; hypothetical execution charges 0.5 bps per leg.
"""
from __future__ import annotations

import csv
import math
import os
from collections import deque
from dataclasses import asdict, dataclass
from typing import Iterable, Mapping, Optional


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
class ShadowMinuteResult:
    minute_ts: float
    time_utc: str
    variant: str
    premium_mean_bps: float
    long_percentile: Optional[float]
    short_percentile: Optional[float]
    range_4h_bps: Optional[float]
    range_gate_open: Optional[bool]
    signal_target_usd: Optional[float]
    signal_exposure_blocked: bool
    executed_signal_ts: Optional[float]
    action: str
    trade_notional_usd: float
    inventory_usd: float
    cash_usd: float
    liquidation_usd: float
    equity_usd: float
    turnover_usd: float
    long_realized_usd: float
    short_realized_usd: float
    closed_long_cycles: int
    closed_short_cycles: int


SHADOW_CSV_HEADER = list(ShadowMinuteResult.__dataclass_fields__)


class RangeInventoryShadow:
    def __init__(self, *, variant: str, use_range_gate: bool,
                 params: FrozenRangeInventoryParams = DEFAULT_PARAMS) -> None:
        self.variant, self.use_range_gate, self.params = variant, use_range_gate, params
        n = max(params.long_window_minutes, params.short_window_minutes,
                params.range_gate_window_minutes) + 2
        self._history: deque[tuple[float, float]] = deque(maxlen=n)
        self._pending: Optional[tuple[float, float]] = None
        self.q_position = self.cash_usd = self.turnover_usd = 0.0
        self._cycle_side = 0
        self._cycle_cash = 0.0
        self.long_realized_usd = self.short_realized_usd = 0.0
        self.closed_long_cycles = self.closed_short_cycles = 0

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

    def _inventory(self, r: Mapping[str, float]) -> float:
        return self.q_position * self._ref(r)

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

    def _target(self, r: Mapping[str, float], lp: float, sp: float) -> float:
        p, inv = self.params, self._inventory(r)
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
        self._pending = None

    def _execute(self, r: Mapping[str, float]) -> tuple[Optional[float], str, float]:
        pending, self._pending = self._pending, None
        if pending is None:
            return None, "none", 0.0
        signal_ts, target_usd = pending
        gap = r["minute_ts"] - signal_ts
        if gap <= 0 or gap > self.params.max_signal_to_execution_gap_seconds:
            return signal_ts, "stale_signal_skipped", 0.0
        ref = self._ref(r)
        target_usd = max(-self.params.hard_cap_usd, min(self.params.hard_cap_usd, target_usd))
        need = target_usd/ref - self.q_position
        if abs(need)*ref < self.params.min_trade_notional_usd:
            return signal_ts, "none", 0.0
        depth = min(r["entropy_ask_qty"], r["hedge_bid_qty"]) if need > 0 else min(r["entropy_bid_qty"], r["hedge_ask_qty"])
        qty = min(abs(need), self.params.max_adjust_usd/ref, depth*self.params.depth_fraction)
        if qty*ref < self.params.min_trade_notional_usd:
            return signal_ts, "depth_blocked", 0.0
        dq = qty if need > 0 else -qty
        before = self.q_position
        if before == 0:
            self._cycle_side, self._cycle_cash = (1 if dq > 0 else -1), 0.0
        trade_cash = ((r["hedge_bid"]-r["entropy_ask"])*dq if dq > 0
                      else (r["entropy_bid"]-r["hedge_ask"])*(-dq))
        notional = abs(dq)*ref
        trade_cash -= notional * 2*self.params.friction_bps_per_leg / 10_000.0
        self.cash_usd += trade_cash
        self._cycle_cash += trade_cash
        self.turnover_usd += notional
        self.q_position += dq
        if abs(self.q_position*ref) < 1e-7:
            self.q_position = 0.0
        action = ("long_build" if before >= 0 and self.q_position > before else
                  "long_release" if before > 0 and self.q_position < before else
                  "short_build" if before <= 0 and self.q_position < before else "short_cover")
        if self.q_position == 0 and self._cycle_side:
            if self._cycle_side > 0:
                self.long_realized_usd += self._cycle_cash; self.closed_long_cycles += 1
            else:
                self.short_realized_usd += self._cycle_cash; self.closed_short_cycles += 1
            self._cycle_side, self._cycle_cash = 0, 0.0
        return signal_ts, action, notional

    def _liq(self, r: Mapping[str, float]) -> float:
        if self.q_position > 0:
            return self.q_position * (r["entropy_bid"]-r["hedge_ask"])
        if self.q_position < 0:
            return (-self.q_position) * (r["hedge_bid"]-r["entropy_ask"])
        return 0.0

    def on_row(self, row: Mapping[str, object]) -> ShadowMinuteResult:
        r = self._row(row)
        executed_ts, action, notional = self._execute(r)
        self._history.append((r["minute_ts"], r["premium_mean_bps"]))
        lv, sv = self._values(r["minute_ts"], self.params.long_window_minutes), self._values(r["minute_ts"], self.params.short_window_minutes)
        gv = self._values(r["minute_ts"], self.params.range_gate_window_minutes)
        lp = self._rank(lv, r["premium_mean_bps"]) if self._ready(lv, self.params.long_window_minutes) else None
        sp = self._rank(sv, r["premium_mean_bps"]) if self._ready(sv, self.params.short_window_minutes) else None
        rg = (self._quantile(gv, self.params.range_gate_high_quantile)-self._quantile(gv, self.params.range_gate_low_quantile)
              if self._ready(gv, self.params.range_gate_window_minutes) else None)
        gate = None if rg is None else rg >= self.params.range_gate_min_bps
        target, blocked = None, False
        if lp is not None and sp is not None:
            target = max(-self.params.hard_cap_usd, min(self.params.hard_cap_usd, self._target(r, lp, sp)))
            inv = self._inventory(r)
            if self.use_range_gate and abs(target) > abs(inv)+1e-9 and gate is not True:
                target, blocked = inv, True
            self._pending = (r["minute_ts"], target)
        liq, inv = self._liq(r), self._inventory(r)
        return ShadowMinuteResult(r["minute_ts"], r["time_utc"], self.variant,
            r["premium_mean_bps"], lp, sp, rg, gate, target, blocked, executed_ts,
            action, notional, inv, self.cash_usd, liq, self.cash_usd+liq,
            self.turnover_usd, self.long_realized_usd, self.short_realized_usd,
            self.closed_long_cycles, self.closed_short_cycles)

    def parameter_snapshot(self) -> dict:
        return {"variant": self.variant, "use_range_gate": self.use_range_gate, **asdict(self.params)}


def result_to_csv_row(result: ShadowMinuteResult) -> list[object]:
    return ["" if value is None else int(value) if isinstance(value, bool) else value
            for value in (getattr(result, name) for name in SHADOW_CSV_HEADER)]


def load_recorder_rows(path: str) -> list[dict[str, str]]:
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def replay_rows(rows: Iterable[Mapping[str, object]], *, start_signal_minute_ts: float,
                params: FrozenRangeInventoryParams = DEFAULT_PARAMS) -> list[ShadowMinuteResult]:
    shadows = (RangeInventoryShadow(variant="baseline", use_range_gate=False, params=params),
               RangeInventoryShadow(variant="range_gate", use_range_gate=True, params=params))
    out = []
    for row in sorted(rows, key=lambda x: float(x["minute_ts"])):
        if float(row["minute_ts"]) < start_signal_minute_ts:
            for shadow in shadows: shadow.warmup_row(row)
        else:
            out.extend(shadow.on_row(row) for shadow in shadows)
    return out


def write_results_csv(path: str, results: Iterable[ShadowMinuteResult]) -> None:
    if os.path.dirname(path): os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(SHADOW_CSV_HEADER)
        for result in results: w.writerow(result_to_csv_row(result))
