"""Two-venue arbitrage engine: Entropy vs one hedge venue.

Fixed mode uses a configured midline band. Rolling mode uses a causal median
of completed recorder minutes as its dynamic center and the same configured
upper/lower executable bands. Rolling positions can accumulate same-direction
lots and reduce only through a persistent, pair-specific break-even ledger.

Around both signals: per-direction persistence arming,
per-venue inventory ladder + position caps, per-venue order budgets and
reactive rate-limit exclusion, net-delta hedging, venue-outage pausing with
probing, and periodic on-chain reconciliation. There is no paper mode: the
bot either trades live or runs --record-only (data collection, no strategy).
Both venues' books are recorded to 1-minute CSV bars throughout.
"""
from __future__ import annotations

import asyncio
import csv
import logging
import os
import time
from collections import deque
from dataclasses import replace
from typing import Dict, List, Optional

import aiohttp

from .book import ArbPlan, floor_step, plan_arb, plan_reduce_arb
from .config import Config
from .lot_ledger import LotLedger, LotLedgerError
from .recorder import MinuteRecorder
from .rolling import RollingSignal, RollingWindow
from .venue_hl import HLVenue
from .venue_lighter import LighterVenue

log = logging.getLogger("engine")

CSV_HEADER = [
    "ts", "signal_ts", "run_id", "event_id", "symbol", "hedge",
    "execution_ms", "buy_settle_ms", "sell_settle_ms",
    "leg_settle_gap_ms", "first_settled_leg",
    "direction", "strategy_mode", "reduce_only",
    "rolling_signal_reason", "rolling_action", "rolling_center_bps",
    "rolling_coverage_pct", "rolling_snapshot_ts", "rolling_valid_minutes",
    "rolling_open_lot_count", "rolling_open_qty",
    "rolling_expected_capture_bps", "rolling_realized_capture_bps",
    "rolling_realized_capture_usd",
    "buy_venue", "sell_venue", "qty",
    "buy_bbo_px", "buy_bbo_qty", "sell_bbo_px", "sell_bbo_qty",
    "buy_quote_age_ms", "sell_quote_age_ms",
    "entropy_book_server_age_ms", "entropy_update_gap_ms",
    "hedge_update_gap_ms",
    "buy_limit", "sell_limit", "buy_protect_limit", "sell_protect_limit",
    "buy_notional", "sell_notional", "exp_edge_usd", "gross_edge_usd",
    "marginal_premium_bps", "midline_bps", "inv_add_bps",
    "buy_fill", "sell_fill", "buy_avg_px", "sell_avg_px",
    "matched_qty", "residual_qty", "buy_status", "sell_status",
    "buy_reason", "sell_reason", "unresolved", "ok", "error",
    "hedge_status", "hedge_venue",
    "hedge_side", "hedge_fill", "hedge_avg_px", "hedge_notional",
    "hedge_duration_ms", "remaining_net_qty", "fill_edge_usd",
]
RUN_CONFIG_HEADER = [
    "run_id", "start_ts", "mode", "symbol", "hedge",
    "strategy_mode",
    "midline_bps", "upper_bps", "lower_bps",
    "premium_persist_sec", "cooldown_sec",
    "leg_slippage_bps", "hedge_slippage_bps",
    "take_fraction", "max_order_notional_usd", "inventory_scale_bps",
    "rolling_window_hours", "rolling_update_minutes",
    "rolling_min_coverage_pct", "rolling_seed_from_csv",
    "rolling_min_exit_capture_bps",
    "host_region", "market_data_mode", "entropy_order_transport",
    "hedge_order_transport", "code_version",
]
BALANCE_POLL_SEC = 30.0


def _csv_num(value, digits: int = 8) -> str:
    """Format an optional numeric value without writing a fake zero."""
    if value is None:
        return ""
    return f"{float(value):.{digits}g}"


def _csv_error(*items) -> str:
    """Keep execution errors useful in CSV without multiline log payloads."""
    errors = []
    for item in items:
        if isinstance(item, dict):
            error = item.get("err") or item.get("error")
        else:
            error = item
        if error:
            errors.append(str(error).replace("\r", " ").replace("\n", " ")[:200])
    return " | ".join(errors)


class Engine:
    def __init__(self, cfg: Config, record_only: bool = False) -> None:
        self.cfg = cfg
        self.record_only = record_only
        self.session: Optional[aiohttp.ClientSession] = None
        self.entropy = None
        self.hedge = None
        self.venues: Dict[str, object] = {}
        self.recorder: Optional[MinuteRecorder] = None
        self.markets_ready = False
        self.stop = asyncio.Event()
        self._update_evt = asyncio.Event()
        self._reconcile_evt = asyncio.Event()
        # per-venue locks: an execution holds both; a reconcile holds one, so
        # a chain read can never race an in-flight order on that venue
        self._venue_locks: Dict[str, asyncio.Lock] = {}
        self._exec_tasks: set = set()
        self.halted = False
        self.consec_errors = 0
        self.last_trade_ts = 0.0
        self.trades = 0
        self.hedges = 0
        self.total_exp_edge = 0.0
        self.total_fill_edge = 0.0
        self.start_ts = time.time()
        self._last_skiplog = 0.0
        self._poke_due: Optional[float] = None
        # per-direction persistence arming: direction key -> first-seen ts
        self._armed: Dict[str, Optional[float]] = {"sell_entropy": None,
                                                   "buy_entropy": None}
        self._rolling: Optional[RollingWindow] = (
            RollingWindow(cfg.rolling) if cfg.strategy_mode == "rolling" else None)
        self._rolling_open_direction: Optional[str] = None
        self._rolling_open_qty = 0.0
        self._rolling_entry_ts: Optional[float] = None
        self._rolling_open_lot_count = 0
        self._rolling_ledger: Optional[LotLedger] = (
            LotLedger(self._rolling_state_path(), symbol=cfg.symbol,
                      hedge=cfg.hedge_venue,
                      tolerance=cfg.net_tolerance_base)
            if cfg.strategy_mode == "rolling" else None)
        self._rolling_ledger_loaded = False
        self._rolling_signal_meta: dict = {}
        self._rolling_result_meta: dict = {}
        self._step = 1e-4
        self._min_base = 0.0
        self._min_notional = 10.0
        self._mtm_baseline: Optional[float] = None
        # proactive per-venue send budget: timestamps of recent order sends
        self._sends: Dict[str, deque] = {}
        # reactive per-venue throttle: venue key -> excluded until
        self._venue_limited_until: Dict[str, float] = {}
        # venue outage tracking: key -> down-since ts; a down venue pauses
        # trading and is probed every venue_probe_sec until it answers
        self._venue_down: Dict[str, float] = {}
        self._venue_probe_at: Dict[str, float] = {}
        self._venue_fetch_fails: Dict[str, int] = {}
        # per-execution records for the dashboard (newest last)
        self.recent_trades: deque = deque(maxlen=50)
        self._event_seq = 0
        self.run_id = f"{self.cfg.symbol}-{self.cfg.hedge_venue}-" \
                      f"{int(self.start_ts * 1000)}"

    # ------------------------------------------------------------- utilities

    def _vlock(self, key: str) -> asyncio.Lock:
        lock = self._venue_locks.get(key)
        if lock is None:
            lock = self._venue_locks[key] = asyncio.Lock()
        return lock

    def _venue_rate_ok(self, v) -> bool:
        """True while the venue is under its max_orders_per_min (sliding 60s)."""
        dq = self._sends.setdefault(v.key, deque())
        now = time.time()
        while dq and now - dq[0] > 60.0:
            dq.popleft()
        return len(dq) < v.orders_per_min

    def _venue_limited(self, v) -> bool:
        return time.time() < self._venue_limited_until.get(v.key, 0.0)

    def _mark_limited(self, v) -> None:
        self._venue_limited_until[v.key] = time.time() + self.cfg.rate_limit_pause_sec
        log.warning("[%s] rate limited — trading paused for %.0fs",
                    v.name, self.cfg.rate_limit_pause_sec)

    def _record_send(self, v) -> None:
        self._sends.setdefault(v.key, deque()).append(time.time())

    def _run_config_path(self) -> str:
        directory = os.path.dirname(self.cfg.log_file) or "logs/engine"
        return os.path.join(
            directory, f"runs-{self.cfg.symbol}-{self.cfg.hedge_venue}.csv")

    def _rolling_state_path(self) -> str:
        """Return the ignored pair-specific runtime ledger path."""
        trade_dir = os.path.dirname(os.path.abspath(self.cfg.trades_csv))
        if os.path.basename(trade_dir) == "trades":
            root = os.path.dirname(trade_dir)
        else:
            root = trade_dir
        return os.path.join(root, "state",
                            f"lots-{self.cfg.symbol}-{self.cfg.hedge_venue}.json")

    def _runtime_metadata(self) -> dict:
        """Return small deployment identifiers for local/AWS comparisons."""
        hedge_kind = getattr(self.cfg.hedge, "kind", "")
        default_market_mode = (
            "entropy:hl_l2book_fast|hedge:lighter_order_book"
            if hedge_kind == "lighter" else
            "entropy:hl_l2book_fast|hedge:hl_l2book_fast")
        return {
            "host_region": (
                os.getenv("ENTROPY_ARB_HOST_REGION")
                or os.getenv("AWS_REGION")
                or os.getenv("AWS_DEFAULT_REGION")
                or "local"),
            "market_data_mode": (
                os.getenv("ENTROPY_ARB_MARKET_DATA_MODE")
                or default_market_mode),
            "entropy_order_transport": os.getenv(
                "ENTROPY_ARB_ENTROPY_ORDER_TRANSPORT", "http_exchange"),
            "hedge_order_transport": os.getenv(
                "ENTROPY_ARB_HEDGE_ORDER_TRANSPORT",
                "lighter_sdk" if hedge_kind == "lighter" else "http_exchange"),
            "code_version": (os.getenv("ENTROPY_ARB_CODE_VERSION")
                             or os.getenv("GIT_COMMIT") or "unknown"),
        }

    def _write_run_config(self) -> None:
        """Append one effective strategy snapshot for this process run."""
        try:
            path = self._run_config_path()
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            if os.path.exists(path):
                with open(path) as fh0:
                    if fh0.readline().strip() != ",".join(RUN_CONFIG_HEADER):
                        os.replace(path, path + ".old")
            new = not os.path.exists(path)
            metadata = self._runtime_metadata()
            with open(path, "a", newline="") as fh:
                writer = csv.writer(fh)
                if new:
                    writer.writerow(RUN_CONFIG_HEADER)
                writer.writerow([
                    self.run_id,
                    f"{self.start_ts:.3f}",
                    "record-only" if self.record_only else "live",
                    self.cfg.symbol,
                    self.cfg.hedge_venue,
                    self.cfg.strategy_mode,
                    f"{self.cfg.midline_bps:.6g}",
                    f"{self.cfg.upper_bps:.6g}",
                    f"{self.cfg.lower_bps:.6g}",
                    f"{self.cfg.premium_persist_sec:.6g}",
                    f"{self.cfg.cooldown_sec:.6g}",
                    f"{self.cfg.leg_slippage_bps:.6g}",
                    f"{self.cfg.hedge_slippage_bps:.6g}",
                    f"{self.cfg.take_fraction:.6g}",
                    f"{self.cfg.max_order_notional:.6g}",
                    f"{self.cfg.inventory_scale_bps:.6g}",
                    f"{self.cfg.rolling.window_hours:.6g}",
                    self.cfg.rolling.update_minutes,
                    f"{self.cfg.rolling.min_coverage_pct:.6g}",
                    int(self.cfg.rolling.seed_from_csv),
                    f"{self.cfg.rolling.min_exit_capture_bps:.6g}",
                    metadata["host_region"],
                    metadata["market_data_mode"],
                    metadata["entropy_order_transport"],
                    metadata["hedge_order_transport"],
                    metadata["code_version"],
                ])
        except Exception:
            log.exception("run config write failed")

    def request_stop(self) -> None:
        self.stop.set()
        self._update_evt.set()
        self._reconcile_evt.set()

    def _reset_rolling_arming(self) -> None:
        self._armed["sell_entropy"] = None
        self._armed["buy_entropy"] = None

    def _halt_rolling(self, reason: str) -> None:
        """Stop rolling entries until a human reconciles and restarts."""
        if self._rolling is None:
            return
        self.halted = True
        self._rolling_signal_meta = {}
        self._reconcile_evt.set()
        log.critical("ROLLING HALTED after %s — reconcile positions and "
                     "restart before trading", reason)

    def _validate_rolling_runtime(self, live: bool) -> None:
        if live and self._rolling is not None and not self.cfg.recorder_enabled:
            raise RuntimeError(
                "rolling strategy requires recorder.enabled=true so live "
                "signals receive completed minute updates")

    # ------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        if not self.record_only:
            self._write_run_config()
        # Long keepalive so order-path connections survive quiet spells; the
        # keepalive loop pings inside this window to hold them open.
        self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(
            keepalive_timeout=75.0, ttl_dns_cache=300))
        try:
            await self._run_inner()
        finally:
            await self.session.close()

    def _make_venue(self, vc):
        if vc.kind == "lighter":
            return LighterVenue(vc, self.session, self.cfg.settle_timeout_sec)
        return HLVenue(vc, self.cfg.hl_api_url, self.cfg.hl_ws_url,
                       self.session, self.cfg.settle_timeout_sec)

    async def _run_inner(self) -> None:
        cfg = self.cfg
        self.entropy = self._make_venue(cfg.entropy)
        self.hedge = self._make_venue(cfg.hedge)
        self.venues = {"entropy": self.entropy, "hedge": self.hedge}
        await asyncio.gather(self.entropy.load_market(), self.hedge.load_market())
        self.markets_ready = True
        if self._rolling is not None and cfg.rolling.seed_from_csv:
            seeded = self._rolling.load_csv(cfg.recorder_csv)
            log.info("rolling window seeded with %d recorder row(s) from %s",
                     seeded, cfg.recorder_csv)

        live = not self.record_only
        self._validate_rolling_runtime(live)
        if live:
            if not cfg.creds_complete:
                raise RuntimeError(
                    "live trading needs credentials for both venues in .env "
                    "(see .env.example); use --record-only to run without "
                    "them / 实盘需要在 .env 中配置两个交易所的密钥，仅采集数据"
                    "请用 --record-only")
            self.entropy.init_signer()
            self.hedge.init_signer()
            if self.hedge.kind == "hl":
                self.entropy.share_nonces_with(self.hedge)
        if (self.hedge.kind == "hl"
                and self.entropy._query_address()
                and self.entropy._query_address() == self.hedge._query_address()):
            self.hedge.include_core_equity = False  # shared account: count once

        self._step = 10 ** -min(self.entropy.size_decimals,
                                self.hedge.size_decimals)
        self._min_base = max(self.entropy.min_base, self.hedge.min_base,
                             self._step)
        self._min_notional = max(cfg.min_order_notional,
                                 self.entropy.min_quote, self.hedge.min_quote)
        if cfg.strategy_mode == "rolling":
            log.info("pair ENTROPY(%s)-%s(%s): center=rolling-median "
                     "band=[-%.2f, +%.2f] fees=%.2f+%.2f step=%g "
                     "min_ntl=$%g",
                     self.entropy.conf.symbol, self.hedge.name,
                     self.hedge.conf.symbol, cfg.lower_bps, cfg.upper_bps,
                     self.entropy.fee_bps, self.hedge.fee_bps,
                     self._step, self._min_notional)
        else:
            log.info("pair ENTROPY(%s)-%s(%s): midline=%+.2fbps "
                     "band=[-%.2f, +%.2f] fees=%.2f+%.2f step=%g "
                     "min_ntl=$%g",
                     self.entropy.conf.symbol, self.hedge.name,
                     self.hedge.conf.symbol, cfg.midline_bps, cfg.lower_bps,
                     cfg.upper_bps, self.entropy.fee_bps, self.hedge.fee_bps,
                     self._step, self._min_notional)

        if self.record_only:
            log.warning("RECORD-ONLY — collecting minute data, no strategy, "
                        "no orders")
        else:
            log.warning("LIVE — real orders will be sent (use --record-only "
                        "for credential-less data collection)")
            await self._reconcile_positions(hedge=False, strict=True)
            self._check_rolling_start_state()
            log.info("starting positions: %s (net %+.6g)",
                     " ".join(f"{v.name}={v.position:+.6g}"
                              for v in self.venues.values()),
                     sum(v.position for v in self.venues.values()))

        tasks: List[asyncio.Task] = []
        for v in self.venues.values():
            tasks += v.start_tasks(self.stop, self._update_evt.set, live)
        if cfg.recorder_enabled or self.record_only:
            self.recorder = MinuteRecorder(cfg.recorder_csv, self.entropy.book,
                                           self.hedge.book, cfg.staleness_sec,
                                           symbol=cfg.symbol,
                                           hedge=cfg.hedge_venue,
                                           on_minute=self._on_minute
                                           if self._rolling is not None else None)
            tasks.append(asyncio.create_task(self.recorder.run(self.stop),
                                             name="recorder"))
        if not self.record_only:
            tasks.append(asyncio.create_task(self._strategy_loop(),
                                             name="strategy"))
            tasks.append(asyncio.create_task(self._balance_loop(),
                                             name="balances"))
            tasks.append(asyncio.create_task(self._http_keepalive_loop(),
                                             name="keepalive"))
        tasks.append(asyncio.create_task(self._status_loop(), name="status"))
        if live:
            tasks.append(asyncio.create_task(self._reconcile_loop(),
                                             name="reconcile"))

        await self.stop.wait()
        if self._exec_tasks:  # let in-flight executions settle, never cancel
            log.info("waiting for %d in-flight execution(s) to settle",
                     len(self._exec_tasks))
            await asyncio.wait(self._exec_tasks,
                               timeout=cfg.settle_timeout_sec + 2.0)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for v in self.venues.values():
            await v.close()
        log.info("shutdown — %d trades, %d hedges, exp edge $%.4f, "
                 "fill edge $%.4f", self.trades, self.hedges,
                 self.total_exp_edge, self.total_fill_edge)

    # --------------------------------------------------------------- signals

    def _on_minute(self, row: dict) -> None:
        """Feed completed recorder bars into the live rolling calculator."""
        if self._rolling is not None and self._rolling.ingest_row(row):
            self._update_evt.set()

    def _sync_rolling_cache(self) -> None:
        """Mirror the persistent lot ledger into the hot-path cache."""
        if self._rolling_ledger is None:
            self._rolling_open_direction = None
            self._rolling_open_qty = 0.0
            self._rolling_entry_ts = None
            self._rolling_open_lot_count = 0
            return
        self._rolling_open_direction = self._rolling_ledger.direction
        self._rolling_open_qty = self._rolling_ledger.total_qty
        self._rolling_entry_ts = self._rolling_ledger.first_entry_ts
        self._rolling_open_lot_count = len(self._rolling_ledger.lots)

    def _check_rolling_start_state(self) -> None:
        """Load and validate the persisted rolling inventory before live mode."""
        if self._rolling is None or self._rolling_ledger is None:
            return
        try:
            self._rolling_ledger.load()
            self._sync_rolling_cache()
            positions = {key: venue.position
                         for key, venue in self.venues.items()}
            self._rolling_ledger.validate_positions(positions)
        except LotLedgerError as exc:
            raise RuntimeError(f"rolling lot ledger validation failed: {exc}") \
                from exc
        nonflat = [f"{v.name}={v.position:+.6g}"
                   for v in self.venues.values()
                   if abs(v.position) > self.cfg.net_tolerance_base]
        if nonflat and not self._rolling_ledger.lots:
            raise RuntimeError(
                "rolling positions are non-flat but no persisted lot ledger "
                "is available: " + ", ".join(nonflat))
        self._rolling_ledger_loaded = True
        self._reset_rolling_arming()

    # Kept as a narrow compatibility alias for callers that used the old
    # pre-ledger startup helper. Live mode uses _check_rolling_start_state.
    def _check_rolling_start_flat(self) -> None:
        self._check_rolling_start_state()

    def _rolling_signal_meta_for(self, signal: RollingSignal,
                                 action: str = "entry") -> dict:
        return {
            "reason": signal.reason,
            "action": action,
            "direction": signal.direction,
            "center_bps": signal.center_bps,
            "coverage_pct": signal.coverage_pct,
            "snapshot_ts": signal.snapshot_ts,
            "valid_minutes": signal.valid_minutes,
        }

    @staticmethod
    def _direction_venues(direction: str, entropy, hedge):
        if direction == "sell_entropy":
            return hedge, entropy
        if direction == "buy_entropy":
            return entropy, hedge
        raise ValueError(f"unknown strategy direction: {direction}")

    def _inv_add_bps(self, buy, sell) -> float:
        """Inventory ladder: a surcharge that grows once a venue's position
        passes floor_frac of its cap in the direction the trade would add to
        (buying adds when that venue is >= flat long; selling adds when the
        venue is <= flat short). Max of the two venues' ramps."""
        scale = self.cfg.inventory_scale_bps
        if scale <= 0:
            return 0.0
        floor = min(max(self.cfg.inventory_floor_frac, 0.0), 0.99)

        def ramp(v, adding: bool) -> float:
            if not adding:
                return 0.0
            ref = v.book.mid()
            if ref is None:
                return 0.0
            u = min(abs(v.position) * ref / v.cap_usd, 1.0)
            if u <= floor:
                return 0.0
            return scale * (u - floor) / (1.0 - floor)

        return max(ramp(buy, buy.position >= 0), ramp(sell, sell.position <= 0))

    def _eff_threshold(self, buy, sell) -> float:
        """Net hurdle (bps, on top of fees) for the direction buy->sell.

        selling entropy: executable premium must clear midline + upper;
        buying entropy: the reverse premium must clear lower - midline."""
        if sell.key == "entropy":
            base = self.cfg.midline_bps + self.cfg.upper_bps
        else:
            base = self.cfg.lower_bps - self.cfg.midline_bps
        return base + self._inv_add_bps(buy, sell)

    def _rolling_threshold(self, signal: RollingSignal, buy, sell) -> float:
        """Return the dynamic executable hurdle for a rolling entry/add."""
        if signal.center_bps is None:
            raise ValueError("rolling signal has no dynamic center")
        if sell.key == "entropy":
            base = signal.center_bps + self.cfg.upper_bps
        else:
            base = self.cfg.lower_bps - signal.center_bps
        return base + self._inv_add_bps(buy, sell)

    def _headroom(self, buy, sell, ref_px: float) -> float:
        hb = buy.cap_usd - buy.position * ref_px
        hs = sell.cap_usd + sell.position * ref_px
        return min(hb, hs)

    def _plan(self, buy, sell, cap_notional: float, *,
              threshold_bps: Optional[float] = None,
              require_edge: bool = True, reduce_only: bool = False):
        plan, reason = plan_arb(
            buy.book, sell.book,
            threshold_bps=(self._eff_threshold(buy, sell)
                           if threshold_bps is None else threshold_bps),
            buy_fee_bps=buy.fee_bps, sell_fee_bps=sell.fee_bps,
            take_fraction=self.cfg.take_fraction,
            cap_notional=cap_notional,
            min_base=self._min_base,
            min_notional=self._min_notional,
            size_step=self._step,
            require_edge=require_edge,
        )
        if plan is not None and reduce_only:
            plan = replace(plan, reduce_only=True)
        return plan, reason

    # -------------------------------------------------------------- strategy

    async def _strategy_loop(self) -> None:
        while not self.stop.is_set():
            await self._update_evt.wait()
            self._update_evt.clear()
            if self.stop.is_set():
                break
            try:
                await self._evaluate()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("evaluate failed")

    def _schedule_poke(self, delay: float) -> None:
        loop = asyncio.get_running_loop()
        due = loop.time() + max(delay, 0.01)
        if self._poke_due is not None and self._poke_due <= due + 0.02:
            return

        def _fire() -> None:
            self._poke_due = None
            self._update_evt.set()

        self._poke_due = due
        loop.call_at(due, _fire)

    def _skiplog(self, fmt: str, *args) -> None:
        now = time.time()
        if now - self._last_skiplog >= 2.0:
            self._last_skiplog = now
            log.info(fmt, *args)

    async def _evaluate(self) -> None:
        cfg = self.cfg
        if self.halted:
            return
        now = time.time()
        if now - self.last_trade_ts < cfg.cooldown_sec:
            self._schedule_poke(cfg.cooldown_sec - (now - self.last_trade_ts))
            return
        best = self._scan(now)
        if best is None:
            return
        buy, sell, plan = best
        # _scan verified both locks free and nothing ran since (no awaits),
        # so these acquires take the no-suspension fast path
        await self._vlock(buy.key).acquire()
        await self._vlock(sell.key).acquire()
        # run as a task so a shutdown cancels the strategy loop's await, never
        # the in-flight execution itself (both legs must settle)
        t = asyncio.create_task(self._execute_locked(buy, sell, plan))
        self._exec_tasks.add(t)
        t.add_done_callback(self._exec_tasks.discard)
        await asyncio.shield(t)

    async def _execute_locked(self, buy, sell, plan: ArbPlan) -> None:
        """Run one execution while holding both venue locks (acquired by the
        caller), then release them and settle the aftermath: unresolved
        outcomes escalate to reconcile, everything else gets a net-delta
        check."""
        execution = None
        try:
            execution = await self._execute(buy, sell, plan)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("execute failed")
        finally:
            self._vlock(buy.key).release()
            self._vlock(sell.key).release()
        if execution is None:
            self._halt_rolling("execution failed before settlement")
        elif execution["unresolved"]:
            hedge = {
                "status": "not_attempted",
                "venue": "",
                "side": "",
                "filled_qty": 0.0,
                "avg_px": None,
                "notional": None,
                "duration_ms": 0.0,
                "remaining_net_qty": sum(v.position
                                          for v in self.venues.values()),
                "error": "primary_execution_unresolved",
            }
            self._reconcile_evt.set()
            self._halt_rolling("unresolved primary execution")
            self._log_csv(execution, hedge)
        else:
            hedge = await self._maybe_hedge()
            if hedge.get("status") == "unresolved":
                self._halt_rolling("unresolved hedge execution")
            if not execution["ok"]:
                self._halt_rolling("failed primary execution")
            self._update_rolling_position(execution, hedge)
            self._log_csv(execution, hedge)
        self._update_evt.set()  # freed venues may have a queued opportunity

    def _scan(self, now: float):
        if self.cfg.strategy_mode == "rolling":
            return self._scan_rolling(now)
        return self._scan_fixed(now)

    def _scan_fixed(self, now: float):
        """Evaluate both directions; returns the best executable
        (buy, sell, plan), or None."""
        cfg = self.cfg
        best = None
        for buy, sell, dkey in ((self.hedge, self.entropy, "sell_entropy"),
                                (self.entropy, self.hedge, "buy_entropy")):
            if not (buy.book.is_fresh(cfg.staleness_sec)
                    and sell.book.is_fresh(cfg.staleness_sec)):
                continue
            if not (buy.ready_to_trade() and sell.ready_to_trade()):
                continue
            if self._venue_down:
                continue  # a venue in outage pauses the (only) pair
            if self._vlock(buy.key).locked() or self._vlock(sell.key).locked():
                continue  # mid-execution or mid-reconcile
            if self._venue_limited(buy) or self._venue_limited(sell):
                continue  # reactive 429 exclusion
            if not (self._venue_rate_ok(buy) and self._venue_rate_ok(sell)):
                self._skiplog("%s deferred: venue order budget exhausted", dkey)
                continue
            # never refire into books that predate the venue's own last trade
            if (buy.book.last_update_ts <= buy.last_traded_ts
                    or sell.book.last_update_ts <= sell.last_traded_ts):
                continue
            plan, reason = self._plan(buy, sell, cfg.max_order_notional)
            edge_present = reason not in ("no_edge", "empty_book")
            if not edge_present:
                self._armed[dkey] = None
                continue
            armed = self._armed.get(dkey)
            if armed is None:
                # premium persistence: only fire if the edge survives
                # premium_persist_sec (filters one-tick phantoms)
                self._armed[dkey] = now
                self._schedule_poke(cfg.premium_persist_sec)
                continue
            if now - armed < cfg.premium_persist_sec:
                self._schedule_poke(cfg.premium_persist_sec - (now - armed))
                continue
            if plan is None:
                continue
            headroom = self._headroom(buy, sell, plan.buy_limit)
            if headroom < plan.buy_notional:
                plan, _ = self._plan(buy, sell,
                                     min(cfg.max_order_notional, headroom))
                if plan is None:
                    self._skiplog("%s blocked by position caps (headroom $%.0f)",
                                  dkey, max(headroom, 0.0))
                    continue
            if best is None or plan.exp_edge_usd > best[2].exp_edge_usd:
                best = (buy, sell, plan)
        return best

    def _scan_rolling(self, now: float):
        """Evaluate rolling entry, add, and directional exit signals."""
        rolling = self._rolling
        ledger = self._rolling_ledger
        if rolling is None or ledger is None:
            return None
        cfg = self.cfg
        entropy, hedge = self.entropy, self.hedge
        if not (entropy.book.is_fresh(cfg.staleness_sec)
                and hedge.book.is_fresh(cfg.staleness_sec)):
            return None
        if not (entropy.ready_to_trade() and hedge.ready_to_trade()):
            return None
        if self._venue_down:
            return None
        premium = self.premium_bps()
        if premium is None:
            return None

        open_direction = self._rolling_open_direction
        signal = rolling.signal(
            premium, now, upper_bps=cfg.upper_bps, lower_bps=cfg.lower_bps)
        if signal is None:
            self._reset_rolling_arming()
            self._rolling_signal_meta = {}
            return None

        if open_direction is not None and signal.direction != open_direction:
            # A signal on the opposite side of the dynamic center is the only
            # normal rolling exit. The ledger chooses lots/depth that remain
            # above their configured break-even capture floor.
            buy, sell = self._direction_venues(signal.direction, entropy, hedge)
            if (self._vlock(buy.key).locked()
                    or self._vlock(sell.key).locked()
                    or self._venue_limited(buy)
                    or self._venue_limited(sell)
                    or not (self._venue_rate_ok(buy)
                            and self._venue_rate_ok(sell))):
                return None
            if (buy.book.last_update_ts <= buy.last_traded_ts
                    or sell.book.last_update_ts <= sell.last_traded_ts):
                return None
            ref_px = buy.book.best_ask()
            if ref_px is None:
                return None
            cap_notional = min(cfg.max_order_notional,
                               self._rolling_open_qty * ref_px)
            plan, reason = plan_reduce_arb(
                buy.book, sell.book,
                candidates=ledger.exit_candidates(
                    cfg.rolling.min_exit_capture_bps),
                buy_fee_bps=buy.fee_bps, sell_fee_bps=sell.fee_bps,
                take_fraction=cfg.take_fraction,
                cap_notional=cap_notional,
                min_base=self._min_base, min_notional=self._min_notional,
                size_step=self._step)
            if plan is None:
                self._skiplog("rolling exit %s unavailable: %s",
                              signal.direction, reason)
                return None
            self._reset_rolling_arming()
            self._rolling_signal_meta = self._rolling_signal_meta_for(
                signal, action="reduce")
            return buy, sell, plan

        if open_direction is None:
            # Do not build a second spread on top of a known residual leg. The
            # reconcile loop may still be able to flatten a below-minimum or
            # temporarily unavailable residual position.
            if any(abs(v.position) > cfg.net_tolerance_base
                   for v in self.venues.values()):
                self._skiplog("rolling blocked: non-flat residual position")
                return None
        elif signal.direction != open_direction:
            # The branch above returns a reduce-only plan. This is a guard for
            # future changes that might accidentally route an opposite entry.
            return None

        buy, sell = self._direction_venues(signal.direction, entropy, hedge)
        dkey = signal.direction
        for direction in self._armed:
            if direction != dkey:
                self._armed[direction] = None
        if (self._vlock(buy.key).locked() or self._vlock(sell.key).locked()
                or self._venue_limited(buy) or self._venue_limited(sell)
                or not (self._venue_rate_ok(buy)
                        and self._venue_rate_ok(sell))):
            return None
        if (buy.book.last_update_ts <= buy.last_traded_ts
                or sell.book.last_update_ts <= sell.last_traded_ts):
            return None
        threshold_bps = self._rolling_threshold(signal, buy, sell)
        plan, reason = self._plan(
            buy, sell, cfg.max_order_notional,
            threshold_bps=threshold_bps)
        if reason in ("no_edge", "empty_book"):
            self._armed[dkey] = None
            return None
        armed = self._armed.get(dkey)
        if armed is None:
            self._armed[dkey] = now
            self._schedule_poke(cfg.premium_persist_sec)
            return None
        if now - armed < cfg.premium_persist_sec:
            self._schedule_poke(cfg.premium_persist_sec - (now - armed))
            return None
        if plan is None:
            return None
        headroom = self._headroom(buy, sell, plan.buy_limit)
        if headroom < plan.buy_notional:
            plan, _ = self._plan(
                buy, sell, min(cfg.max_order_notional, headroom),
                threshold_bps=threshold_bps)
            if plan is None:
                self._skiplog("rolling %s blocked by position caps", dkey)
                return None
        self._rolling_signal_meta = self._rolling_signal_meta_for(signal)
        return buy, sell, plan

    # ------------------------------------------------------------- execution

    def _update_rolling_position(self, execution: dict, hedge: dict) -> None:
        if (self._rolling is None or self._rolling_ledger is None
                or not execution.get("ok")):
            return
        matched = float(execution.get("matched_qty") or 0.0)
        plan = execution["plan"]
        if matched <= self.cfg.net_tolerance_base and not plan.reduce_only:
            return
        hedge_status = hedge.get("status", "not_needed")
        if hedge_status in ("not_attempted", "unhedgeable", "unresolved"):
            self._halt_rolling(
                f"rolling {'reduce' if plan.reduce_only else 'entry'} "
                f"has unsettled hedge ({hedge_status})")
            return
        try:
            remaining_net = hedge.get("remaining_net_qty")
            if (remaining_net is not None
                    and abs(float(remaining_net)) > self.cfg.net_tolerance_base):
                self._halt_rolling(
                    f"rolling {'reduce' if plan.reduce_only else 'entry'} "
                    f"leaves residual net position ({float(remaining_net):+.6g})")
                return
        except (TypeError, ValueError):
            self._halt_rolling("rolling settlement has invalid net position")
            return
        try:
            hedge_fill = max(float(hedge.get("filled_qty") or 0.0), 0.0)
        except (TypeError, ValueError):
            self._halt_rolling("rolling settlement has invalid hedge fill")
            return

        if plan.reduce_only:
            closed_qty = matched
            if hedge_status not in ("not_needed", "not_attempted",
                                     "unhedgeable", "unresolved"):
                # A successful reduce-only hedge completes the opposite
                # primary leg, so it contributes to the truly closed spread
                # quantity just as the pre-ledger rolling state did.
                closed_qty += hedge_fill
            if closed_qty <= self.cfg.net_tolerance_base:
                return
            allocations = self._scaled_lot_allocations(
                plan.lot_allocations, closed_qty)
            if not allocations:
                self._halt_rolling("rolling reduce has no lot allocations")
                return
            buy_px, sell_px = self._rolling_exit_prices(execution, hedge)
            try:
                realized_usd = 0.0
                reference_notional = 0.0
                for allocation in allocations:
                    usd, _ = self._rolling_ledger.realized_capture(
                        allocation["lot_id"], allocation["qty"],
                        buy_px=buy_px, sell_px=sell_px,
                        buy_fee_bps=execution["buy"].fee_bps,
                        sell_fee_bps=execution["sell"].fee_bps)
                    realized_usd += usd
                    lot = next(lot for lot in self._rolling_ledger.lots
                               if lot.lot_id == allocation["lot_id"])
                    reference_notional += (allocation["qty"]
                                           * lot.reference_entry_notional_per_base)
                self._rolling_ledger.close_allocations(allocations)
            except (LotLedgerError, StopIteration, TypeError, ValueError) as exc:
                self._halt_rolling(f"rolling lot close failed: {exc}")
                return
            realized_bps = (realized_usd / reference_notional * 1e4
                            if reference_notional > 0 else None)
            self._rolling_result_meta = {
                "action": "reduce",
                "closed_qty": closed_qty,
                "realized_capture_usd": realized_usd,
                "realized_capture_bps": realized_bps,
            }
        else:
            buy_info = execution.get("buy_info") or {}
            sell_info = execution.get("sell_info") or {}
            buy_px = buy_info.get("avg_px")
            sell_px = sell_info.get("avg_px")
            if buy_px is None or sell_px is None:
                self._halt_rolling("rolling entry is missing actual average fills")
                return
            try:
                self._rolling_ledger.add_lot(
                    lot_id=execution["event_id"],
                    source_event_id=execution["event_id"],
                    direction=execution["direction"],
                    open_qty=matched,
                    entry_ts=execution["settled_ts"],
                    buy_venue=execution["buy"].name,
                    sell_venue=execution["sell"].name,
                    buy_avg_px=buy_px,
                    sell_avg_px=sell_px,
                    buy_fee_bps=execution["buy"].fee_bps,
                    sell_fee_bps=execution["sell"].fee_bps,
                )
            except (LotLedgerError, KeyError, TypeError, ValueError) as exc:
                self._halt_rolling(f"rolling lot entry failed: {exc}")
                return
            self._rolling_result_meta = {"action": "entry"}

        try:
            self._sync_rolling_cache()
        except LotLedgerError as exc:
            self._halt_rolling(f"rolling ledger state invalid: {exc}")
            return
        self._reset_rolling_arming()
        self._rolling_signal_meta = {}

    @staticmethod
    def _rolling_exit_prices(execution: dict, hedge: dict) -> tuple[float, float]:
        """Return effective exit prices, blending a settled residual hedge."""
        plan = execution["plan"]
        buy_info = execution.get("buy_info") or {}
        sell_info = execution.get("sell_info") or {}
        buy_px = buy_info.get("avg_px") or plan.buy_limit
        sell_px = sell_info.get("avg_px") or plan.sell_limit
        try:
            buy_qty = float(buy_info.get("filled_base")
                            or execution.get("matched_qty") or 0.0)
            sell_qty = float(sell_info.get("filled_base")
                             or execution.get("matched_qty") or 0.0)
            hedge_qty = max(float(hedge.get("filled_qty") or 0.0), 0.0)
            hedge_px = hedge.get("avg_px")
        except (TypeError, ValueError):
            return float(buy_px), float(sell_px)
        if hedge_qty <= 0 or hedge_px is None:
            return float(buy_px), float(sell_px)
        hedge_px = float(hedge_px)
        hedge_venue = str(hedge.get("venue") or "")
        if hedge_venue == getattr(execution["buy"], "name", ""):
            total = buy_qty + hedge_qty
            if total > 0:
                buy_px = (buy_qty * float(buy_px)
                          + hedge_qty * hedge_px) / total
        elif hedge_venue == getattr(execution["sell"], "name", ""):
            total = sell_qty + hedge_qty
            if total > 0:
                sell_px = (sell_qty * float(sell_px)
                           + hedge_qty * hedge_px) / total
        return float(buy_px), float(sell_px)

    @staticmethod
    def _scaled_lot_allocations(allocations, qty: float) -> tuple:
        """Trim planner allocations to the quantity actually paired/fillable."""
        remaining = max(float(qty), 0.0)
        selected = []
        for allocation in allocations or ():
            if remaining <= 1e-12:
                break
            take = min(float(allocation["qty"]), remaining)
            if take > 1e-12:
                selected.append({"lot_id": allocation["lot_id"],
                                 "qty": take})
                remaining -= take
        return tuple(selected) if remaining <= 1e-9 else ()

    async def _execute(self, buy, sell, plan: ArbPlan) -> Optional[dict]:
        """Send both legs and settle the fills. Both venue locks are held by
        the caller. Returns the execution record, or None when halted."""
        if self.halted:
            return None
        cfg = self.cfg
        self._rolling_result_meta = {}
        signal_ts = time.time()
        self._event_seq += 1
        event_id = f"{int(self.start_ts * 1000)}-{self._event_seq:06d}"
        buy_bbo_px = buy.book.best_ask()
        buy_bbo_qty = (buy.book.asks.get(buy_bbo_px)
                       if buy_bbo_px is not None else None)
        sell_bbo_px = sell.book.best_bid()
        sell_bbo_qty = (sell.book.bids.get(sell_bbo_px)
                        if sell_bbo_px is not None else None)
        buy_quote_age_ms = (max(0.0, signal_ts - buy.book.last_update_ts) * 1000.0
                            if buy.book.last_update_ts else None)
        sell_quote_age_ms = (max(0.0, signal_ts - sell.book.last_update_ts) * 1000.0
                             if sell.book.last_update_ts else None)
        entropy_book_server_age_ms = self.entropy.book.server_age_ms(signal_ts)
        entropy_update_gap_ms = self.entropy.book.last_update_gap_ms
        hedge_update_gap_ms = self.hedge.book.last_update_gap_ms
        inv_bps = 0.0 if plan.reduce_only else self._inv_add_bps(buy, sell)
        direction = "sell_entropy" if sell.key == "entropy" else "buy_entropy"
        rolling_signal_meta = (
            dict(self._rolling_signal_meta)
            if self.cfg.strategy_mode == "rolling" else {})
        self.last_trade_ts = signal_ts
        log.info("[ARB] %s: BUY %s %.6g @<=%.6g | SELL %s @>=%.6g | "
                 "take $%.0f of $%.0f | prem %.2fbps | exp $%.4f",
                 direction, buy.name, plan.qty, plan.buy_limit, sell.name,
                 plan.sell_limit, plan.buy_notional, plan.q_max_notional,
                 plan.marginal_premium_bps, plan.exp_edge_usd)
        if plan.reduce_only and cfg.strategy_mode == "rolling":
            # Rolling reduce plans already contain break-even-safe protective
            # limits. Widening them with generic entry slippage could turn a
            # profitable lot close into a loss.
            buy_bound = buy.px_round(plan.buy_limit, round_up=False)
            sell_bound = sell.px_round(plan.sell_limit, round_up=True)
        else:
            slip = cfg.leg_slippage_bps / 1e4
            buy_bound = buy.px_round(plan.buy_limit * (1 + slip),
                                     round_up=False)
            sell_bound = sell.px_round(plan.sell_limit * (1 - slip),
                                       round_up=True)
        self._record_send(buy)
        self._record_send(sell)

        async def send_with_completion_ts(venue, *, is_buy, qty, limit_px,
                                          reduce_only):
            started_ts = time.time()
            try:
                info = await venue.send_taker(is_buy=is_buy, qty=qty,
                                               limit_px=limit_px,
                                               reduce_only=reduce_only)
            except Exception as exc:
                info = {"status": "send-failed", "filled_base": 0.0,
                        "avg_px": None, "err": repr(exc),
                        "reason": repr(exc), "unresolved": False}
            return info, started_ts, time.time()

        res = await asyncio.gather(
            send_with_completion_ts(buy, is_buy=True, qty=plan.qty,
                                    limit_px=buy_bound,
                                    reduce_only=plan.reduce_only),
            send_with_completion_ts(sell, is_buy=False, qty=plan.qty,
                                    limit_px=sell_bound,
                                    reduce_only=plan.reduce_only),
            return_exceptions=True)

        def unpack_send_result(result):
            if (isinstance(result, tuple) and len(result) == 3
                    and isinstance(result[0], dict)):
                return result
            return ({"status": "send-failed", "filled_base": 0.0,
                     "avg_px": None, "err": repr(result),
                     "reason": repr(result), "unresolved": False},
                    None, None)

        (binfo, buy_started_ts, buy_done_ts), (sinfo, sell_started_ts,
                                               sell_done_ts) = (
            unpack_send_result(result) for result in res)
        buy_settle_ms = (
            max(0.0, buy_done_ts - buy_started_ts) * 1000.0
            if buy_started_ts is not None and buy_done_ts is not None else None)
        sell_settle_ms = (
            max(0.0, sell_done_ts - sell_started_ts) * 1000.0
            if sell_started_ts is not None and sell_done_ts is not None else None)
        leg_settle_gap_ms = (
            abs(buy_done_ts - sell_done_ts) * 1000.0
            if buy_done_ts is not None and sell_done_ts is not None else None)
        if buy_done_ts is None or sell_done_ts is None:
            first_settled_leg = ""
        elif abs(buy_done_ts - sell_done_ts) <= 1e-6:
            first_settled_leg = "same"
        else:
            first_settled_leg = "buy" if buy_done_ts < sell_done_ts else "sell"
        for v, info, side in ((buy, binfo, "buy"), (sell, sinfo, "sell")):
            if info.get("err"):
                log.error("[%s] %s leg: %s", v.name, side, info["err"])
        bfill = binfo["filled_base"]
        sfill = sinfo["filled_base"]
        buy.position += bfill
        sell.position -= sfill
        if bfill:
            bpx = binfo.get("avg_px") or plan.buy_limit
            buy.cash -= bfill * bpx * (1 + plan.buy_fee)
            buy.volume_usd += bfill * bpx
        if sfill:
            spx = sinfo.get("avg_px") or plan.sell_limit
            sell.cash += sfill * spx * (1 - plan.sell_fee)
            sell.volume_usd += sfill * spx

        matched = min(bfill, sfill)
        residual = abs(bfill - sfill)
        fill_edge = 0.0
        if matched > 0 and binfo.get("avg_px") and sinfo.get("avg_px"):
            fill_edge = matched * (sinfo["avg_px"] * (1 - plan.sell_fee)
                                   - binfo["avg_px"] * (1 + plan.buy_fee))
            self.total_fill_edge += fill_edge
        log.info("[SETTLED] %s: buy %s %s %.6g/%.6g | sell %s %s %.6g/%.6g | "
                 "matched %.6g | fill edge $%.4f", direction,
                 buy.name, binfo["status"], bfill, plan.qty,
                 sell.name, sinfo["status"], sfill, plan.qty, matched, fill_edge)
        buy.last_traded_ts = sell.last_traded_ts = time.time()

        unresolved = binfo.get("unresolved") or sinfo.get("unresolved")
        hard_err = (binfo.get("err") is not None
                    or sinfo.get("err") is not None)
        rate_limited = False
        for v, info in ((buy, binfo), (sell, sinfo)):
            if str(info.get("err", "")).startswith("RATE_LIMITED"):
                rate_limited = True
                self._mark_limited(v)
            elif "margin" in str(info.get("status", "")).lower():
                log.warning("[%s] margin rejection — collateral exhausted, "
                            "pausing venue", v.name)
                self._mark_limited(v)
        sent_ok = not hard_err and not unresolved
        if sent_ok:
            self.consec_errors = 0
        elif not rate_limited:
            self.consec_errors += 1
            if self.consec_errors >= cfg.max_consecutive_errors:
                self.halted = True
                log.critical("HALTED after %d consecutive execution problems "
                             "— flatten manually and restart / 连续执行异常，"
                             "引擎已停止，请手动平仓后重启", self.consec_errors)
        if sent_ok:
            self.trades += 1
            self.total_exp_edge += plan.exp_edge_usd
        self._record_trade(direction, plan,
                           None if unresolved else fill_edge,
                           f"{binfo['status']}/{sinfo['status']}", sent_ok,
                           event_id=event_id)
        settled_ts = time.time()
        self.last_trade_ts = settled_ts
        return {
            "event_id": event_id,
            "signal_ts": signal_ts,
            "settled_ts": settled_ts,
            "execution_ms": max(0.0, (settled_ts - signal_ts) * 1000.0),
            "buy_settle_ms": buy_settle_ms,
            "sell_settle_ms": sell_settle_ms,
            "leg_settle_gap_ms": leg_settle_gap_ms,
            "first_settled_leg": first_settled_leg,
            "direction": direction,
            "strategy_mode": cfg.strategy_mode,
            "reduce_only": bool(plan.reduce_only),
            "rolling_signal_meta": rolling_signal_meta,
            "buy": buy,
            "sell": sell,
            "plan": plan,
            "buy_bbo_px": buy_bbo_px,
            "buy_bbo_qty": buy_bbo_qty,
            "sell_bbo_px": sell_bbo_px,
            "sell_bbo_qty": sell_bbo_qty,
            "buy_quote_age_ms": buy_quote_age_ms,
            "sell_quote_age_ms": sell_quote_age_ms,
            "entropy_book_server_age_ms": entropy_book_server_age_ms,
            "entropy_update_gap_ms": entropy_update_gap_ms,
            "hedge_update_gap_ms": hedge_update_gap_ms,
            "buy_bound": buy_bound,
            "sell_bound": sell_bound,
            "buy_info": binfo,
            "sell_info": sinfo,
            "matched_qty": matched,
            "residual_qty": residual,
            "fill_edge": fill_edge,
            "inv_bps": inv_bps,
            "unresolved": bool(unresolved),
            "ok": sent_ok,
        }

    def _record_trade(self, direction: str, plan: ArbPlan, fill_edge,
                      status: str, ok: bool, event_id: str = "") -> None:
        self.recent_trades.append({
            "ts": time.time(), "event_id": event_id,
            "direction": direction, "qty": plan.qty,
            "notional": plan.buy_notional,
            "prem_bps": plan.marginal_premium_bps,
            "exp": plan.exp_edge_usd, "fill": fill_edge, "status": status,
            "ok": ok, "strategy_mode": self.cfg.strategy_mode,
            "reduce_only": plan.reduce_only})

    async def _maybe_hedge(self) -> dict:
        net = sum(v.position for v in self.venues.values())
        if abs(net) <= self.cfg.net_tolerance_base:
            return {
                "status": "not_needed",
                "venue": "",
                "side": "",
                "filled_qty": 0.0,
                "avg_px": None,
                "notional": None,
                "duration_ms": 0.0,
                "remaining_net_qty": net,
                "error": "",
            }
        return await self._hedge(net)

    async def _hedge(self, net: float) -> dict:
        """Reduce the venue that carries the imbalance back toward net zero
        (reduce-only taker with hedge_slippage_bps price protection)."""
        cfg = self.cfg
        is_sell = net > 0
        sgn = 1.0 if net > 0 else -1.0
        slip = cfg.hedge_slippage_bps / 1e4
        for v in sorted(self.venues.values(),
                        key=lambda x: (self._venue_limited(x), -x.position * sgn)):
            if v.position * sgn <= 0:
                continue
            if v.key in self._venue_down \
                    or not v.book.is_fresh(cfg.staleness_sec):
                continue  # unreachable or blind: cannot hedge here
            lk = self._vlock(v.key)
            if lk.locked():
                continue
            qty = floor_step(min(abs(net), abs(v.position)), self._step)
            if qty < v.min_base:
                continue
            ref = v.book.best_bid() if is_sell else v.book.best_ask()
            if ref is None:
                continue
            limit = v.px_round(ref * (1 - slip), False) if is_sell \
                else v.px_round(ref * (1 + slip), True)
            if qty * limit < max(cfg.min_order_notional, v.min_quote):
                continue
            await lk.acquire()  # verified free, no awaits since: fast path
            try:
                log.warning("[HEDGE] net %+.6g — %s %.6g on %s @%.6g",
                            net, "SELL" if is_sell else "BUY", qty, v.name, limit)
                self.hedges += 1
                self._record_send(v)  # counts toward the budget, never blocked
                hedge_started = time.time()
                info = await v.send_taker(is_buy=not is_sell, qty=qty,
                                          limit_px=limit, reduce_only=True)
                hedge_duration_ms = max(0.0, (time.time() - hedge_started) * 1000.0)
                fill = float(info.get("filled_base") or 0.0)
                avg_px = info.get("avg_px")
                hedge_notional = (fill * float(avg_px)
                                  if avg_px is not None else None)
                if info.get("err") or info.get("unresolved"):
                    log.error("[HEDGE] %s: %s", v.name,
                              info.get("err") or "unresolved")
                    if str(info.get("err", "")).startswith("RATE_LIMITED"):
                        self._mark_limited(v)
                    self._reconcile_evt.set()
                    return {
                        "status": info.get("status", "unresolved"),
                        "venue": v.name,
                        "side": "sell" if is_sell else "buy",
                        "filled_qty": fill,
                        "avg_px": avg_px,
                        "notional": hedge_notional,
                        "duration_ms": hedge_duration_ms,
                        "remaining_net_qty": sum(x.position
                                                  for x in self.venues.values()),
                        "error": info.get("err") or "unresolved",
                    }
                else:
                    v.position += -fill if is_sell else fill
                    if fill:
                        px = avg_px or limit
                        fee = v.fee_bps / 1e4
                        v.cash += fill * px * (1 - fee) if is_sell \
                            else -fill * px * (1 + fee)
                        v.volume_usd += fill * px
                    log.info("[HEDGE SETTLED] %s %s %.6g/%.6g",
                             v.name, info["status"], fill, qty)
                v.last_traded_ts = time.time()
                return {
                    "status": info.get("status", "settled"),
                    "venue": v.name,
                    "side": "sell" if is_sell else "buy",
                    "filled_qty": fill,
                    "avg_px": avg_px,
                    "notional": hedge_notional,
                    "duration_ms": hedge_duration_ms,
                    "remaining_net_qty": sum(x.position
                                              for x in self.venues.values()),
                    "error": "",
                }
            finally:
                lk.release()
        log.warning("[HEDGE] net %+.6g below hedgeable minimum — carrying "
                    "(next reconcile retries)", net)
        return {
            "status": "unhedgeable",
            "venue": "",
            "side": "",
            "filled_qty": 0.0,
            "avg_px": None,
            "notional": None,
            "duration_ms": 0.0,
            "remaining_net_qty": net,
            "error": "below_hedgeable_minimum",
        }

    # --------------------------------------------------- reconcile / status

    # Lighter's REST account state lags its ws settlements; overwriting a
    # venue that traded seconds ago "restores" stale positions and triggers
    # phantom hedge oscillations. Grace-guard + venue lock prevent that.
    RECONCILE_GRACE_SEC = 5.0

    async def _reconcile_positions(self, hedge: bool,
                                   strict: bool = False) -> None:
        now = time.time()
        vs = []
        for v in self.venues.values():
            if now - v.last_traded_ts <= self.RECONCILE_GRACE_SEC:
                continue  # just traded: chain read would be stale
            if v.key in self._venue_down \
                    and now < self._venue_probe_at.get(v.key, 0.0):
                continue  # down venue: probe only every venue_probe_sec
            vs.append(v)
        if not vs:
            return
        got = await asyncio.gather(
            *(self._reconcile_venue(v, strict) for v in vs),
            return_exceptions=True)
        for r in got:
            if isinstance(r, BaseException):
                raise r  # strict startup: fail loudly
        if hedge:
            await self._maybe_hedge()
        if (self._rolling_ledger_loaded and not self._venue_down
                and self._rolling_ledger is not None):
            try:
                self._rolling_ledger.validate_positions({
                    key: venue.position for key, venue in self.venues.items()})
                self._sync_rolling_cache()
            except LotLedgerError as exc:
                self._halt_rolling(f"position reconciliation mismatch: {exc}")
                if strict:
                    raise RuntimeError(str(exc)) from exc

    async def _reconcile_venue(self, v, strict: bool) -> None:
        async with self._vlock(v.key):
            now = time.time()
            if now - v.last_traded_ts <= self.RECONCILE_GRACE_SEC:
                return  # traded while waiting for the lock
            try:
                r = await v.fetch_position()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if strict:
                    raise RuntimeError(
                        f"[{v.name}] cannot fetch starting position: {e!r}")
                # exchange unreachable (e.g. scheduled maintenance): pause
                # trading and keep probing until it answers again
                n = self._venue_fetch_fails.get(v.key, 0) + 1
                self._venue_fetch_fails[v.key] = n
                self._venue_probe_at[v.key] = now + self.cfg.venue_probe_sec
                if n >= 3 and v.key not in self._venue_down:
                    self._venue_down[v.key] = now
                    log.critical("[%s] API unreachable (%d attempts) — "
                                 "trading PAUSED; probing every %.0fs until "
                                 "it recovers", v.name, n,
                                 self.cfg.venue_probe_sec)
                elif v.key not in self._venue_down:
                    log.warning("[%s] position fetch failed (%d): %r",
                                v.name, n, e)
                return
            if v.key in self._venue_down:
                log.warning("[%s] API recovered after %.0fs outage — "
                            "trading RESUMED", v.name,
                            now - self._venue_down.pop(v.key))
                self._update_evt.set()
            self._venue_fetch_fails[v.key] = 0
            delta = r - v.position
            if abs(delta) > 1e-12:
                if abs(delta) > self.cfg.net_tolerance_base:
                    log.warning("[%s] reconcile: chain %+.6g vs local %+.6g "
                                "— adopting chain", v.name, r, v.position)
                mid = v.book.mid()
                if mid is not None:
                    v.cash -= delta * mid
                v.position = r

    async def _reconcile_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self._reconcile_evt.wait(),
                                       timeout=self.cfg.reconcile_sec)
                self._reconcile_evt.clear()
                await asyncio.sleep(1.0)
            except asyncio.TimeoutError:
                pass
            if self.stop.is_set():
                break
            try:
                await self._reconcile_positions(hedge=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reconcile failed")

    async def _balance_loop(self) -> None:
        while not self.stop.is_set():
            for v in self.venues.values():
                try:
                    got = await v.fetch_equity()
                    if got is not None:
                        v.equity, v.free = got
                        if v.start_equity is None:
                            v.start_equity = v.equity
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.debug("[%s] equity poll failed: %r", v.name, e)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=BALANCE_POLL_SEC)
            except asyncio.TimeoutError:
                pass

    async def _http_keepalive_loop(self) -> None:
        if self.cfg.http_keepalive_sec <= 0:
            return
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(),
                                       timeout=self.cfg.http_keepalive_sec)
                return
            except asyncio.TimeoutError:
                pass
            await asyncio.gather(*(v.warm_http() for v in self.venues.values()),
                                 return_exceptions=True)

    def account_delta(self) -> Optional[float]:
        """Change in real account equity since start (both venues)."""
        total = 0.0
        for v in self.venues.values():
            if v.equity is None or v.start_equity is None:
                return None
            total += v.equity - v.start_equity
        return total

    def session_pnl(self) -> Optional[float]:
        total = 0.0
        for v in self.venues.values():
            m = v.book.mid()
            if m is None:
                return None
            total += v.cash + v.position * m
        if self._mtm_baseline is None:
            self._mtm_baseline = total
        return total - self._mtm_baseline

    def premium_bps(self) -> Optional[float]:
        em, hm = self.entropy.book.mid(), self.hedge.book.mid()
        if not (em and hm):
            return None
        return (em / hm - 1.0) * 1e4

    async def _status_loop(self) -> None:
        cfg = self.cfg
        while not self.stop.is_set():
            try:
                await asyncio.sleep(cfg.status_interval_sec)
            except asyncio.CancelledError:
                raise
            books = " | ".join(
                f"{v.name} {v.book.best_bid() or '—'}/{v.book.best_ask() or '—'}"
                + ("" if v.book.is_fresh(cfg.staleness_sec) else " STALE")
                + (" RATE-LTD" if self._venue_limited(v) else "")
                + (" DOWN" if v.key in self._venue_down else "")
                for v in self.venues.values())
            prem = self.premium_bps()
            prem_s = f"{prem:+.2f}" if prem is not None else "—"
            pos = " ".join(f"{v.name} {v.position:+.6g}"
                           for v in self.venues.values())
            net = sum(v.position for v in self.venues.values())
            pnl = self.session_pnl()
            rec = (f" | rec {self.recorder.rows_written} rows"
                   if self.recorder else "")
            if cfg.strategy_mode == "rolling":
                open_state = (f"{self._rolling_open_direction} "
                              f"qty={self._rolling_open_qty:.6g} "
                              f"lots={self._rolling_open_lot_count}"
                              if self._rolling_open_direction else "flat")
                signal = self._rolling_signal_meta
                snapshot = self._rolling.snapshot_for(
                    self._rolling._block_start(time.time()))
                if signal:
                    signal_s = (f"{signal.get('reason')} "
                                f"center={signal.get('center_bps')}bps "
                                f"coverage={signal.get('coverage_pct')}%")
                else:
                    signal_s = (f"no signal center={snapshot.median_bps}bps "
                                f"coverage={snapshot.coverage_pct}%")
                log.info(
                    "[status] %s | prem %s bps (rolling %s; %s) | pos %s "
                    "net %+.6g | trades %d hedges %d | MTM %s expEdge $%.4f "
                    "fillEdge $%.4f%s%s",
                    books, prem_s, open_state, signal_s, pos, net, self.trades,
                    self.hedges,
                    f"${pnl:+.4f}" if pnl is not None else "—",
                    self.total_exp_edge, self.total_fill_edge, rec,
                    " *** HALTED ***" if self.halted else "")
            else:
                log.info(
                    "[status] %s | prem %s bps (band %+.2f..%+.2f) | pos %s "
                    "net %+.6g | trades %d hedges %d | MTM %s expEdge $%.4f "
                    "fillEdge $%.4f%s%s",
                    books, prem_s, cfg.midline_bps - cfg.lower_bps,
                    cfg.midline_bps + cfg.upper_bps, pos, net, self.trades,
                    self.hedges,
                    f"${pnl:+.4f}" if pnl is not None else "—",
                    self.total_exp_edge, self.total_fill_edge, rec,
                    " *** HALTED ***" if self.halted else "")

    def _log_csv(self, execution: dict, hedge: dict) -> None:
        try:
            path = self.cfg.trades_csv
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            if os.path.exists(path):
                with open(path) as fh0:
                    if fh0.readline().strip() != ",".join(CSV_HEADER):
                        os.replace(path, path + ".old")
            new = not os.path.exists(path)
            with open(path, "a", newline="") as fh:
                w = csv.writer(fh)
                if new:
                    w.writerow(CSV_HEADER)
                buy = execution["buy"]
                sell = execution["sell"]
                plan = execution["plan"]
                binfo = execution["buy_info"]
                sinfo = execution["sell_info"]
                rolling = execution.get("rolling_signal_meta") or {}
                result = self._rolling_result_meta if (
                    self.cfg.strategy_mode == "rolling") else {}
                w.writerow([
                    f"{execution['settled_ts']:.3f}",
                    f"{execution['signal_ts']:.3f}", self.run_id,
                    execution["event_id"],
                    self.cfg.symbol, self.cfg.hedge_venue,
                    f"{execution['execution_ms']:.3f}",
                    _csv_num(execution["buy_settle_ms"], 6),
                    _csv_num(execution["sell_settle_ms"], 6),
                    _csv_num(execution["leg_settle_gap_ms"], 6),
                    execution["first_settled_leg"],
                    execution["direction"], execution["strategy_mode"],
                    int(execution["reduce_only"]),
                    rolling.get("reason", ""),
                    rolling.get("action") or result.get("action", ""),
                    _csv_num(rolling.get("center_bps"), 6),
                    _csv_num(rolling.get("coverage_pct"), 6),
                    _csv_num(rolling.get("snapshot_ts"), 6),
                    _csv_num(rolling.get("valid_minutes"), 6),
                    self._rolling_open_lot_count
                    if self.cfg.strategy_mode == "rolling" else "",
                    _csv_num(self._rolling_open_qty)
                    if self.cfg.strategy_mode == "rolling" else "",
                    _csv_num(plan.expected_exit_capture_bps),
                    _csv_num(result.get("realized_capture_bps"), 6),
                    _csv_num(result.get("realized_capture_usd"), 6),
                    buy.name, sell.name,
                    _csv_num(plan.qty),
                    _csv_num(execution["buy_bbo_px"]),
                    _csv_num(execution["buy_bbo_qty"]),
                    _csv_num(execution["sell_bbo_px"]),
                    _csv_num(execution["sell_bbo_qty"]),
                    _csv_num(execution["buy_quote_age_ms"], 6),
                    _csv_num(execution["sell_quote_age_ms"], 6),
                    _csv_num(execution["entropy_book_server_age_ms"], 6),
                    _csv_num(execution["entropy_update_gap_ms"], 6),
                    _csv_num(execution["hedge_update_gap_ms"], 6),
                    _csv_num(plan.buy_limit), _csv_num(plan.sell_limit),
                    _csv_num(execution["buy_bound"]),
                    _csv_num(execution["sell_bound"]),
                    f"{plan.buy_notional:.2f}", f"{plan.sell_notional:.2f}",
                    f"{plan.exp_edge_usd:.4f}", f"{plan.gross_edge_usd:.4f}",
                    f"{plan.marginal_premium_bps:.3f}",
                    ("" if self.cfg.strategy_mode == "rolling"
                     else f"{self.cfg.midline_bps:.3f}"),
                    f"{execution['inv_bps']:.3f}",
                    _csv_num(binfo.get("filled_base")),
                    _csv_num(sinfo.get("filled_base")),
                    _csv_num(binfo.get("avg_px")),
                    _csv_num(sinfo.get("avg_px")),
                    _csv_num(execution["matched_qty"]),
                    _csv_num(execution["residual_qty"]),
                    binfo.get("status", ""), sinfo.get("status", ""),
                    binfo.get("reason") or binfo.get("err") or "",
                    sinfo.get("reason") or sinfo.get("err") or "",
                    int(execution["unresolved"]), int(execution["ok"]),
                    _csv_error(binfo, sinfo, hedge),
                    hedge.get("status", ""), hedge.get("venue", ""),
                    hedge.get("side", ""), _csv_num(hedge.get("filled_qty")),
                    _csv_num(hedge.get("avg_px")),
                    _csv_num(hedge.get("notional")),
                    _csv_num(hedge.get("duration_ms"), 6),
                    _csv_num(hedge.get("remaining_net_qty")),
                    f"{execution['fill_edge']:.4f}",
                ])
        except Exception:
            log.exception("csv write failed")
