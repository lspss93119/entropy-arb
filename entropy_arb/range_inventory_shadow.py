"""Causal shadow simulator for the frozen 4h-long / 2h-short inventory strategy.

It never sends orders. A signal from completed minute T can only be executed on
minute T+1 using that minute's recorder BBO/top-of-book depth. Funding is
excluded; hypothetical execution charges 0.5 bps per leg.
"""
from __future__ import annotations

import csv
import os
from dataclasses import asdict, dataclass
from typing import Iterable, Mapping, Optional


from .range_inventory import DEFAULT_PARAMS, FrozenRangeInventoryParams, RangeInventoryCore


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
        self.core = RangeInventoryCore(params=params, use_range_gate=use_range_gate)
        self._pending: Optional[tuple[float, float]] = None
        self.q_position = self.cash_usd = self.turnover_usd = 0.0
        self._cycle_side = 0
        self._cycle_cash = 0.0
        self.long_realized_usd = self.short_realized_usd = 0.0
        self.closed_long_cycles = self.closed_short_cycles = 0

    _row = staticmethod(RangeInventoryCore._row)
    _ref = staticmethod(RangeInventoryCore._ref)

    def _inventory(self, r: Mapping[str, float]) -> float:
        return self.q_position * self._ref(r)

    def warmup_row(self, row: Mapping[str, object]) -> None:
        self.core.warmup_row(row)
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
                self.long_realized_usd += self._cycle_cash
                self.closed_long_cycles += 1
            else:
                self.short_realized_usd += self._cycle_cash
                self.closed_short_cycles += 1
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
        signal = self.core.on_row(r, inventory_usd=self._inventory(r))
        lp, sp, rg, gate = (signal.long_percentile, signal.short_percentile,
                            signal.range_4h_bps, signal.range_gate_open)
        target, blocked = signal.target_usd, signal.exposure_blocked
        if target is not None:
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
            for shadow in shadows:
                shadow.warmup_row(row)
        else:
            out.extend(shadow.on_row(row) for shadow in shadows)
    return out


def write_results_csv(path: str, results: Iterable[ShadowMinuteResult]) -> None:
    if os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(SHADOW_CSV_HEADER)
        for result in results:
            w.writerow(result_to_csv_row(result))
