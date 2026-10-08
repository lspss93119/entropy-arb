#!/usr/bin/env python3
"""Read-only Range inventory PnL report.

The report never imports the live engine/config loader and never opens a
private key.  Venue access is limited to public market metadata/books and the
public Hyperliquid userFillsByTime endpoint for the configured public address.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import html
import io
import json
import math
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - dependency is declared by the repo
    yaml = None


PAIR_SUFFIX = re.compile(r"[^A-Za-z0-9_.-]+")
MAX_SNAPSHOT_ATTEMPTS = 3
HL_RESPONSE_CAP = 2000
HL_HISTORY_LIMIT = 10000
MATCH_CLOCK_SKEW_MS = 5000
NUMERIC_REL_TOL = 5e-8
NUMERIC_ABS_TOL = 5e-10
USD_FEE_TOKENS = {"USD"}  # no unverified stablecoin conversion
CYCLE_COLUMNS = [
    "cycle_id", "direction", "start_utc", "end_utc", "duration_sec",
    "build_count", "release_count", "peak_base_inventory",
    "peak_inventory_usd", "turnover_usd", "gross_spread_capture_usd",
    "gross_pnl_usd", "normal_execution_contribution_usd",
    "fallback_cashflow_usd", "fallback_counterfactual_impact_usd",
    "fallback_count", "fallback_rate", "fallback_attribution_status",
    "gross_pnl_excluding_fallback_drag_usd", "fallback_drag_pct",
    "fees_usd", "fallback_hedge_pnl_usd",
    "realized_pnl_before_funding_usd", "return_on_peak_inventory_pct",
    "unresolved_count", "final_net_delta",
]


class ReportError(RuntimeError):
    """Input is missing, malformed, or ambiguous; do not emit partial PnL."""


def finite(value: Any, label: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ReportError(f"{label} is not numeric") from exc
    if not math.isfinite(out):
        raise ReportError(f"{label} is not finite")
    return out


def safe_pair_path(path: str, symbol: str, hedge: str) -> str:
    """Mirror main._market_output_path without importing the live entrypoint."""
    directory, filename = os.path.split(path)
    stem, ext = os.path.splitext(filename)
    sym = PAIR_SUFFIX.sub("_", symbol.strip())
    hed = PAIR_SUFFIX.sub("_", hedge.strip())
    return os.path.join(directory, f"{stem}-{sym}-{hed}{ext}")


@dataclass(frozen=True)
class ReportPaths:
    root: Path
    state: Path
    halt: Path
    trades: Path
    recorder: Path
    run_log: Path
    runs: Path
    output_html: Path
    output_json: Path
    cycles_csv: Path
    config: Optional[Path] = None


@dataclass(frozen=True)
class SourceBoundary:
    path: Path
    exists: bool
    device: Optional[int] = None
    inode: Optional[int] = None
    size: int = 0
    mtime_ns: Optional[int] = None


@dataclass
class Snapshot:
    state: dict
    halt: dict
    rows: list[dict[str, str]]
    recorder_rows: list[dict[str, str]]
    boundaries: dict[str, SourceBoundary]
    attempts: int = 1
    consistent: bool = True
    reason: Optional[str] = None
    mark: Optional["MarketMark"] = None
    venue_metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CanonicalFill:
    fill_id: str
    event_id: str
    ts: float
    venue: str
    side: str
    qty: float
    price: float
    fallback: bool = False
    fee_usd: Optional[float] = None
    fee_status: str = "unknown"

    @property
    def cashflow(self) -> float:
        gross = -self.qty * self.price if self.side == "buy" else self.qty * self.price
        return gross


@dataclass
class PairEvent:
    event_id: str
    ts: float
    signal_ts: Optional[float]
    direction: str
    reduce_only: bool
    matched_qty: float
    fills: list[CanonicalFill]
    unresolved: bool = False
    anomaly: Optional[str] = None
    source_row: dict[str, Any] = field(default_factory=dict)

    @property
    def entropy_delta(self) -> float:
        return sum((f.qty if f.side == "buy" else -f.qty)
                   for f in self.fills if f.venue == "entropy")

    @property
    def gross_cash(self) -> float:
        return sum(f.cashflow for f in self.fills)

    @property
    def turnover(self) -> float:
        return sum(f.qty * f.price for f in self.fills)

    @property
    def all_fees_known(self) -> bool:
        return all(f.fee_usd is not None for f in self.fills)

    @property
    def fees(self) -> Optional[float]:
        return sum(f.fee_usd or 0.0 for f in self.fills) if self.all_fees_known else None

    @property
    def net_cash(self) -> Optional[float]:
        return self.gross_cash - self.fees if self.fees is not None else None


@dataclass
class AccountingResult:
    events: list[PairEvent]
    fills: list[CanonicalFill]
    completed_cycles: list[dict]
    current_cycle: Optional[dict]
    gross_realized: Optional[float]
    fee_net_realized: Optional[float]
    fees_usd: Optional[float]
    gross_unrealized: Optional[float]
    fee_net_unrealized: Optional[float]
    current_inventory: float
    peak_inventory_usd: Optional[float]
    turnover_usd: float
    fallback_count: int
    unresolved_count: int
    anomalies: list[str]
    inventory_curve: list[dict]


@dataclass(frozen=True)
class FeeComponent:
    complete: bool
    usd: Optional[float]
    reason: Optional[str] = None
    matched: int = 0
    expected: int = 0


@dataclass
class FeeMatchResult:
    complete: bool
    fees_by_fill_id: dict[str, float]
    matched: int
    expected: int
    reason: Optional[str] = None


@dataclass
class MarketMark:
    available: bool
    entropy_bid: Optional[float] = None
    entropy_ask: Optional[float] = None
    hedge_bid: Optional[float] = None
    hedge_ask: Optional[float] = None
    snapshot_utc: Optional[str] = None
    quote_age_sec: Optional[float] = None
    max_quote_age_sec: Optional[float] = None
    source: Optional[str] = None
    exchange_age_ms: Optional[float] = None
    reason: Optional[str] = None


@dataclass
class ReportInputs:
    paths: ReportPaths
    symbol: str
    hedge: str
    offline: bool
    hl_dex: str = "io"
    hl_api_url: str = "https://api.hyperliquid.xyz"
    hl_ws_url: str = "wss://api.hyperliquid.xyz/ws"
    rh_api_url: str = "https://api.rh.lighter.xyz"
    rh_ws_url: str = "wss://api.rh.lighter.xyz/stream"
    staleness_sec: float = 10.0
    hl_account_address: Optional[str] = None
    rh_native_symbol: Optional[str] = None
    max_quote_age_override: Optional[float] = None


def _resolve_relative(root: Path, value: str | os.PathLike[str]) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else root / p


def parse_env_value(path: Path, name: str) -> Optional[str]:
    """Read exactly one allowlisted public env key; never parse private keys."""
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() == name:
            value = value.strip().strip("\"'")
            return value or None
    return None


def resolve_inputs(*, root: Path, symbol: str, hedge: str,
                   recorder_override: Optional[Path] = None,
                   config_path: Optional[Path] = None,
                   env_file: Optional[Path] = None,
                   offline: bool = False,
                   state_path: Optional[Path] = None,
                   halt_path: Optional[Path] = None,
                   trades_path: Optional[Path] = None,
                   max_quote_age_sec: Optional[float] = None) -> ReportInputs:
    root = root.expanduser().resolve()
    sym, hed = symbol.strip().upper(), hedge.strip().lower()
    if not sym or not hed:
        raise ReportError("symbol and hedge are required")
    cfg_path = _resolve_relative(root, config_path or Path("config.yaml")) if not offline else None
    raw: dict = {}
    if not offline and config_path is not None:
        if yaml is None:
            raise ReportError("PyYAML is required for runtime config resolution")
        try:
            loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError) as exc:
            raise ReportError("could not read runtime config") from exc
        if not isinstance(loaded, dict):
            raise ReportError("runtime config root must be a mapping")
        raw = loaded
    elif not offline and config_path is None:
        # The normal runtime config is optional for reports with explicit paths,
        # but an existing config is consumed to resolve the actual recorder.
        default_cfg = root / "config.yaml"
        cfg_path = default_cfg if default_cfg.exists() else None
        if cfg_path:
            if yaml is None:
                raise ReportError("PyYAML is required for runtime config resolution")
            try:
                loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            except (OSError, ValueError) as exc:
                raise ReportError("could not read runtime config") from exc
            if not isinstance(loaded, dict):
                raise ReportError("runtime config root must be a mapping")
            raw = loaded

    if offline and recorder_override is None:
        raise ReportError("--offline requires --recorder; config/env are not read")
    if recorder_override is not None:
        recorder = _resolve_relative(root, recorder_override)
    else:
        recorder_cfg = (raw.get("recorder") or {}).get("csv")
        if not recorder_cfg:
            raise ReportError("recorder path cannot be uniquely resolved; pass --recorder")
        # Runtime path resolution is exactly main._namespace_recording_outputs:
        # configured relative path + pair suffix before the extension.
        recorder = _resolve_relative(root, safe_pair_path(str(recorder_cfg), sym, hed))

    trade_cfg = (raw.get("logging") or {}).get("trades_csv", "logs/trades/trades.csv")
    log_cfg = (raw.get("logging") or {}).get("file", "logs/engine/engine.log")
    trade_default = _resolve_relative(root, safe_pair_path(str(trade_cfg), sym, hed))
    trade_file = _resolve_relative(root, trades_path) if trades_path else trade_default
    trade_dir = trade_file.parent
    state_dir = trade_dir.parent / "state" if trade_dir.name == "trades" else trade_dir / "state"
    state = _resolve_relative(root, state_path) if state_path else state_dir / f"range-{sym}-{hed}.json"
    halt = _resolve_relative(root, halt_path) if halt_path else state_dir / f"halt-{sym}-{hed}.json"
    log_file = _resolve_relative(root, safe_pair_path(str(log_cfg), sym, hed))
    runs = log_file.parent / f"runs-{sym}-{hed}.csv"
    output_dir = root / "logs" / "performance"
    paths = ReportPaths(root, state, halt, trade_file, recorder, log_file, runs,
                        output_dir / f"range-performance-{sym}-{hed}.html",
                        output_dir / f"range-performance-{sym}-{hed}.json",
                        output_dir / f"range-cycles-{sym}-{hed}.csv", cfg_path)

    env_path = _resolve_relative(root, env_file or Path(".env")) if not offline else None
    env_address = os.environ.get("HL_ACCOUNT_ADDRESS") if not offline else None
    if not env_address and env_path is not None:
        env_address = parse_env_value(env_path, "HL_ACCOUNT_ADDRESS")
    entropy_cfg, hedge_cfg = raw.get("entropy") or {}, raw.get("hedge") or {}
    exec_cfg = raw.get("execution") or {}
    market_aliases = {"ANTH": "ANTHROPIC", "OAI": "OPENAI"}
    native = market_aliases.get(sym, sym) if hed.startswith("lighter") else sym
    staleness = float(exec_cfg.get("staleness_sec", 10.0))
    if not math.isfinite(staleness) or staleness <= 0:
        raise ReportError("runtime staleness_sec must be positive and finite")
    if max_quote_age_sec is not None and (not math.isfinite(max_quote_age_sec) or max_quote_age_sec <= 0):
        raise ReportError("max quote age must be positive and finite")
    return ReportInputs(
        paths, sym, hed, offline,
        hl_dex=str(entropy_cfg.get("dex", "io")),
        staleness_sec=staleness, hl_account_address=env_address,
        rh_native_symbol=native, max_quote_age_override=max_quote_age_sec)


def capture_boundary(path: Path) -> SourceBoundary:
    try:
        info = path.stat()
    except FileNotFoundError:
        return SourceBoundary(path, False)
    return SourceBoundary(path, True, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _same_boundary(left: SourceBoundary, right: SourceBoundary) -> bool:
    return (left.path == right.path and left.exists == right.exists
            and (not left.exists or (left.device, left.inode, left.size, left.mtime_ns)
                 == (right.device, right.inode, right.size, right.mtime_ns)))


def read_bounded(boundary: SourceBoundary) -> bytes:
    if not boundary.exists:
        return b""
    try:
        before = capture_boundary(boundary.path)
        if not _same_boundary(boundary, before):
            raise ReportError(f"source changed before bounded read: {boundary.path.name}")
        with boundary.path.open("rb") as stream:
            data = stream.read(boundary.size)
        after = capture_boundary(boundary.path)
    except OSError as exc:
        raise ReportError(f"could not read source: {boundary.path.name}") from exc
    if len(data) != boundary.size or not _same_boundary(boundary, after):
        raise ReportError(f"source changed during bounded read: {boundary.path.name}")
    return data


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"invalid or unavailable JSON: {path.name}") from exc
    if not isinstance(payload, dict):
        raise ReportError(f"JSON root must be an object: {path.name}")
    return payload


def _read_halt(path: Path) -> dict:
    payload = _read_json(path)
    if not isinstance(payload.get("halted"), bool):
        raise ReportError("HALT JSON lacks boolean halted status")
    return payload


def _json_state_signature(state: dict) -> tuple:
    required = ("entropy_qty", "hedge_qty", "pending_intent", "consumed_minute_ts")
    if any(k not in state for k in required):
        raise ReportError("Range state missing required inventory fields")
    for key in ("current_target_usd", "mean_cost_per_base"):
        if state.get(key) is not None:
            finite(state[key], key)
    consumed = state.get("consumed_minute_ts")
    if consumed is not None:
        consumed = finite(consumed, "consumed_minute_ts")
    try:
        serialized = json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ReportError("Range state contains non-JSON or non-finite value") from exc
    return (finite(state["entropy_qty"], "entropy_qty"),
            finite(state["hedge_qty"], "hedge_qty"), consumed, serialized)


def _parse_csv_bounded(boundary: SourceBoundary, expected_fields: Optional[set[str]] = None) -> list[dict[str, str]]:
    data = read_bounded(boundary)
    if not data:
        return []
    try:
        text = data.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ReportError(f"CSV header missing or duplicated: {boundary.path.name}")
        if expected_fields and not expected_fields.issubset(set(reader.fieldnames)):
            raise ReportError(f"CSV schema mismatch: {boundary.path.name}")
        rows = []
        for row in reader:
            if None in row or any(v is None for v in row.values()):
                raise ReportError(f"malformed CSV row: {boundary.path.name}")
            rows.append({str(k): str(v) for k, v in row.items()})
        return rows
    except UnicodeDecodeError as exc:
        raise ReportError(f"CSV is not UTF-8: {boundary.path.name}") from exc


def _halt_consistent(a: dict, b: dict) -> bool:
    return all(a.get(key) == b.get(key) for key in
               ("halted", "timestamp", "resume_timestamp", "source", "reason"))


def capture_consistent_snapshot(inputs: ReportInputs,
                                quote_provider: Callable[[dict], MarketMark],
                                retries: int = MAX_SNAPSHOT_ATTEMPTS) -> Snapshot:
    paths = inputs.paths
    last_reason = "source boundary changed"
    for attempt in range(1, retries + 1):
        state_a = _read_json(paths.state)
        halt_a = _read_halt(paths.halt)
        state_sig_a = _json_state_signature(state_a)
        # The unstructured engine log is intentionally not a snapshot source;
        # settled Range execution rows are the financial record.
        source_paths = (paths.trades, paths.recorder, paths.runs)
        boundaries = {str(path): capture_boundary(path) for path in source_paths}
        try:
            trade_boundary = boundaries[str(paths.trades)]
            recorder_boundary = boundaries[str(paths.recorder)]
            rows = _parse_csv_bounded(trade_boundary)
            rec_rows = _parse_csv_bounded(recorder_boundary)
            run_rows = _parse_csv_bounded(boundaries[str(paths.runs)])
        except ReportError as exc:
            last_reason = str(exc)
            continue
        if run_rows:  # run metadata contributes identity only; no PnL authority
            for row in run_rows:
                if row.get("symbol") and row["symbol"].upper() != inputs.symbol:
                    continue
        try:
            quote_result = quote_provider(state_a)
            if isinstance(quote_result, tuple):
                mark, venue_metadata = quote_result
            else:
                mark, venue_metadata = quote_result, {}
        except Exception as exc:  # providers must not leak request payloads
            mark = MarketMark(False, reason=f"public market data unavailable ({type(exc).__name__})")
            venue_metadata = {}
        state_b = _read_json(paths.state)
        halt_b = _read_halt(paths.halt)
        state_sig_b = _json_state_signature(state_b)
        boundaries_after = {str(path): capture_boundary(path) for path in source_paths}
        stable_sources = all(_same_boundary(boundaries[k], boundaries_after[k])
                             for k in boundaries)
        stable_state = state_sig_a == state_sig_b
        stable_halt = _halt_consistent(halt_a, halt_b)
        if stable_sources and stable_state and stable_halt:
            _filter_pair_rows(rows, inputs.symbol, inputs.hedge)
            _filter_pair_rows(rec_rows, inputs.symbol, inputs.hedge)
            return Snapshot(state_a, halt_a, rows, rec_rows, boundaries, attempt,
                            True, None, mark, venue_metadata)
        last_reason = "state changed during report" if not stable_state else (
            "HALT changed during report" if not stable_halt else "append-only source boundary changed")
    return Snapshot({}, {}, [], [], {}, retries, False,
                    f"inconsistent_snapshot: {last_reason}", None, {})


def snapshot_still_current(inputs: ReportInputs, snapshot: Snapshot) -> bool:
    """Revalidate the captured state and EOF boundaries after enrichment calls.

    Fee enrichment can take substantially longer than the local snapshot.  Do
    not publish it as a current valuation if any production input advanced
    while the read-only venue query was in flight.
    """
    if not snapshot.consistent or not snapshot.boundaries:
        return False
    try:
        state_now = _read_json(inputs.paths.state)
        halt_now = _read_halt(inputs.paths.halt)
        boundaries_now = {
            str(path): capture_boundary(path)
            for path in (inputs.paths.trades, inputs.paths.recorder, inputs.paths.runs)
        }
        return (
            _json_state_signature(state_now) == _json_state_signature(snapshot.state)
            and _halt_consistent(snapshot.halt, halt_now)
            and all(_same_boundary(snapshot.boundaries[key], boundaries_now[key])
                    for key in snapshot.boundaries)
        )
    except (ReportError, OSError, KeyError):
        return False


def _filter_pair_rows(rows: list[dict[str, str]], symbol: str, hedge: str) -> None:
    rows[:] = [r for r in rows if (not r.get("symbol") or r["symbol"].upper() == symbol)
               and (not r.get("hedge") or r["hedge"].lower() == hedge)
               and (not r.get("strategy_mode") or r["strategy_mode"].lower() == "range_inventory")]


def _optional_num(row: Mapping[str, Any], key: str) -> Optional[float]:
    value = row.get(key)
    if value in (None, ""):
        return None
    return finite(value, key)


def _venue(value: str) -> str:
    normalized = (value or "").strip().lower()
    if normalized in {"entropy", "hl", "hyperliquid"}:
        return "entropy"
    if normalized in {"rh", "lighter", "lighter-rh", "hedge"}:
        return "hedge"
    raise ReportError("unrecognized venue in actual-fill record")


def _stable_row_id(row: Mapping[str, Any], index: int) -> str:
    value = (row.get("event_id") or "").strip()
    if not value:
        raise ReportError(f"execution row {index + 1} has no stable event_id")
    return value


def build_fill_ledger(rows: Iterable[Mapping[str, Any]]) -> tuple[list[PairEvent], list[CanonicalFill], list[str]]:
    events: list[PairEvent] = []
    fills: list[CanonicalFill] = []
    anomalies: list[str] = []
    seen_events: dict[str, tuple] = {}
    for index, source in enumerate(rows):
        row = dict(source)
        event_id = _stable_row_id(row, index)
        signature = tuple(row.get(k, "") for k in (
            "ts", "signal_ts", "direction", "reduce_only", "buy_fill", "sell_fill",
            "qty", "matched_qty", "buy_venue", "sell_venue", "buy_avg_px",
            "sell_avg_px", "buy_limit", "sell_limit", "buy_bbo_px",
            "sell_bbo_px", "buy_bbo_qty", "sell_bbo_qty", "hedge_fill",
            "hedge_avg_px", "hedge_venue", "hedge_side", "hedge_status",
            "fallback_reason", "buy_reason", "sell_reason", "error", "unresolved"))
        if event_id in seen_events:
            if seen_events[event_id] != signature:
                anomalies.append(f"conflicting duplicate event identity {event_id}")
            # Event id is stable source identity: exact duplicate is represented once.
            continue
        seen_events[event_id] = signature
        try:
            ts = finite(row.get("ts"), "execution timestamp")
            signal_ts = _optional_num(row, "signal_ts")
            direction = str(row.get("direction") or "")
            if direction not in {"buy_entropy", "sell_entropy"}:
                raise ReportError(f"event {event_id} has invalid Range direction")
            reduce_only = str(row.get("reduce_only", "0")).strip().lower() in {"1", "true"}
            matched = _optional_num(row, "matched_qty") or 0.0
            if matched < 0:
                raise ReportError(f"event {event_id} has negative matched quantity")
            event_fills: list[CanonicalFill] = []
            for leg in ("buy", "sell"):
                qty = _optional_num(row, f"{leg}_fill") or 0.0
                if qty < 0:
                    raise ReportError(f"event {event_id} has negative {leg} fill")
                if qty == 0:
                    continue
                px = _optional_num(row, f"{leg}_avg_px")
                if px is None or px <= 0:
                    raise ReportError(f"event {event_id} positive {leg} fill has no actual average price")
                venue = _venue(str(row.get(f"{leg}_venue") or ""))
                fill = CanonicalFill(f"{event_id}:{leg}", event_id, ts, venue,
                                     leg, qty, px, False)
                event_fills.append(fill)
            hedge_qty = _optional_num(row, "hedge_fill") or 0.0
            if hedge_qty < 0:
                raise ReportError(f"event {event_id} has negative fallback fill")
            if hedge_qty > 0:
                hedge_px = _optional_num(row, "hedge_avg_px")
                if hedge_px is None or hedge_px <= 0:
                    raise ReportError(f"event {event_id} positive fallback fill has no actual average price")
                hedge_side = str(row.get("hedge_side") or "").lower()
                if hedge_side not in {"buy", "sell"}:
                    raise ReportError(f"event {event_id} fallback side is unknown")
                venue = _venue(str(row.get("hedge_venue") or ""))
                event_fills.append(CanonicalFill(f"{event_id}:fallback", event_id,
                    ts, venue, hedge_side, hedge_qty, hedge_px, True))
            is_unresolved = str(row.get("unresolved", "0")).strip().lower() in {"1", "true"}
            event = PairEvent(event_id, ts, signal_ts, direction, reduce_only,
                              matched, event_fills, is_unresolved,
                              source_row=dict(row))
            events.append(event)
            fills.extend(event_fills)
            if is_unresolved:
                anomalies.append(f"event {event_id} is unresolved")
        except ReportError as exc:
            anomalies.append(str(exc))
    return sorted(events, key=lambda e: (e.ts, e.event_id)), fills, anomalies


def _event_fallback_used(event: PairEvent) -> bool:
    if any(fill.fallback for fill in event.fills):
        return True
    status = str(event.source_row.get("hedge_status") or "").strip().lower()
    return status not in {"", "not_needed", "not_attempted", "unhedgeable"}


def _safe_source_num(row: Mapping[str, Any], key: str) -> Optional[float]:
    try:
        return _optional_num(row, key)
    except ReportError:
        return None


def _weighted_fill_details(fills: list[CanonicalFill]) -> tuple[Optional[str], float, Optional[float]]:
    if not fills:
        return None, 0.0, None
    qty = sum(fill.qty for fill in fills)
    if qty <= 0:
        return None, 0.0, None
    venues = {fill.venue for fill in fills}
    venue = next(iter(venues)) if len(venues) == 1 else "multiple"
    avg_px = sum(fill.qty * fill.price for fill in fills) / qty
    return venue, qty, avg_px


def _counterfactual_fallback_impact(
        event: PairEvent, primary_buy_qty: float, primary_sell_qty: float,
        normal_primary_cashflow: float, fallback_cashflow: float,
        net_before: float, net_after: float,
        fallback_qty: float) -> tuple[Optional[float], Optional[str], Optional[float], Optional[str]]:
    """Compare event cash with a fully completed primary pair at logged prices.

    Only prices captured for this execution are considered: its intended limit,
    then its pre-submit top-of-book BBO. No recorder/future price is consulted.
    """
    if not _event_fallback_used(event):
        return 0.0, None, normal_primary_cashflow, "no fallback"
    if fallback_qty <= 0:
        return None, "incomplete_fallback_repair", None, None
    tolerance = max(1e-8, abs(net_before) * 1e-6)
    if (abs(net_before) <= 1e-12 or abs(net_after) > tolerance
            or abs(fallback_qty - abs(net_before)) > tolerance):
        return None, "incomplete_fallback_repair", None, None
    if abs(primary_buy_qty - primary_sell_qty) <= tolerance:
        return None, "insufficient_counterfactual_price", None, None

    if primary_buy_qty > primary_sell_qty:
        leg = "sell"
        limit_key, bbo_key = "sell_limit", "sell_bbo_px"
        missing_qty = primary_buy_qty - primary_sell_qty
        sign = 1.0
    else:
        leg = "buy"
        limit_key, bbo_key = "buy_limit", "buy_bbo_px"
        missing_qty = primary_sell_qty - primary_buy_qty
        sign = -1.0
    intended = _safe_source_num(event.source_row, limit_key)
    source = f"logged intended {leg} limit"
    if intended is None or intended <= 0:
        intended = _safe_source_num(event.source_row, bbo_key)
        source = f"logged pre-submit {leg} BBO"
    if intended is None or intended <= 0:
        return None, "insufficient_counterfactual_price", None, None
    if abs(missing_qty - abs(net_before)) > tolerance:
        return None, "incomplete_fallback_repair", None, None
    hypothetical = normal_primary_cashflow + sign * missing_qty * intended
    actual = normal_primary_cashflow + fallback_cashflow
    return actual - hypothetical, None, hypothetical, source


def _guard_trace_diagnostics(row: Mapping[str, Any]) -> dict[str, Any]:
    traces: dict[str, Optional[list[dict[str, Any]]]] = {}
    for leg in ("buy", "sell"):
        raw = row.get(f"{leg}_guard_trace")
        if not raw:
            traces[leg] = None
            continue
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            traces[leg] = None
            continue
        traces[leg] = (parsed if isinstance(parsed, list)
                       and all(isinstance(item, dict) for item in parsed)
                       else None)

    blocked: list[tuple[str, dict[str, Any]]] = []
    for leg in ("buy", "sell"):
        for item in traces[leg] or []:
            if item.get("ok") is False:
                blocked.append((leg, item))
    if blocked:
        blocked.sort(key=lambda pair: str(pair[1].get("timestamp") or ""))
        block_leg, block_item = blocked[0]
        block_stage = str(block_item.get("stage") or "UNKNOWN")
        exact_reason = str(block_item.get("reason") or "UNKNOWN")
        wait = _safe_source_num(row, f"{block_leg}_pre_submit_wait_ms")
        pre_submit_wait: Any = wait if wait is not None else "UNKNOWN"
    elif any(trace is not None for trace in traces.values()):
        block_leg = None
        block_stage, exact_reason = "NONE", "NONE"
        wait_values = []
        for leg in ("buy", "sell"):
            if traces[leg] is None:
                continue
            value = _safe_source_num(row, f"{leg}_pre_submit_wait_ms")
            wait_values.append(f"{leg}={value:.6g}ms" if value is not None
                               else f"{leg}=UNKNOWN")
        pre_submit_wait = "; ".join(wait_values) if wait_values else "UNKNOWN"
    else:
        block_leg = None
        block_stage, exact_reason, pre_submit_wait = "UNKNOWN", "UNKNOWN", "UNKNOWN"

    hedge_nonce_wait: Any = "UNKNOWN"
    for leg in ("buy", "sell"):
        venue_name = str(row.get(f"{leg}_venue") or "")
        try:
            is_rh = _venue(venue_name) == "hedge"
        except ReportError:
            is_rh = False
        if is_rh:
            value = _safe_source_num(row, f"{leg}_nonce_wait_ms")
            hedge_nonce_wait = value if value is not None else "UNKNOWN"
            break

    attempted = [_parse_optional_bool(row.get(f"{leg}_transport_attempted"))
                 for leg in ("buy", "sell")]
    if any(value is True for value in attempted):
        transport_attempted: Any = True
    elif all(value is False for value in attempted):
        transport_attempted = False
    else:
        transport_attempted = "UNKNOWN"

    return {
        "block_stage": block_stage,
        "exact_guard_reason": exact_reason,
        "rh_nonce_wait_ms": hedge_nonce_wait,
        "pre_submit_wait_ms": pre_submit_wait,
        "transport_attempted": transport_attempted,
    }


def _parse_optional_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true"}:
        return True
    if text in {"0", "false"}:
        return False
    return None


def build_execution_attributions(events: list[PairEvent]) -> list[dict[str, Any]]:
    """Build a reporting-only canonical per-event ledger from actual fills."""
    rows = []
    for event in events:
        primary = [fill for fill in event.fills if not fill.fallback]
        fallback = [fill for fill in event.fills if fill.fallback]
        buy_fills = [fill for fill in primary if fill.side == "buy"]
        sell_fills = [fill for fill in primary if fill.side == "sell"]
        buy_venue, buy_qty, buy_avg = _weighted_fill_details(buy_fills)
        sell_venue, sell_qty, sell_avg = _weighted_fill_details(sell_fills)
        fallback_venue, fallback_qty, fallback_avg = _weighted_fill_details(fallback)
        primary_cash = sum(fill.cashflow for fill in primary)
        fallback_cash = sum(fill.cashflow for fill in fallback)
        net_before = sum(fill.qty if fill.side == "buy" else -fill.qty
                         for fill in primary)
        net_after = net_before + sum(fill.qty if fill.side == "buy" else -fill.qty
                                     for fill in fallback)
        impact, impact_reason, hypothetical, price_source = _counterfactual_fallback_impact(
            event, buy_qty, sell_qty, primary_cash, fallback_cash,
            net_before, net_after, fallback_qty)
        row = event.source_row
        action_raw = str(row.get("rolling_action") or "").strip().lower()
        if action_raw in {"reduce", "release", "cover"} or event.reduce_only:
            action = "release"
        elif action_raw in {"entry", "build"}:
            action = "build"
        else:
            action = "build"
        requested_qty = _safe_source_num(row, "qty")
        if requested_qty is None:
            requested_qty = _safe_source_num(row, "requested_qty")
        signal_age_ms = _safe_source_num(row, "signal_age_ms")
        depth_fraction = _safe_source_num(row, "depth_used_fraction")
        if depth_fraction is None and requested_qty is not None and requested_qty > 0:
            depths = []
            for key in ("buy_bbo_qty", "sell_bbo_qty"):
                visible = _safe_source_num(row, key)
                if visible is None or visible <= 0:
                    depths = []
                    break
                depths.append(requested_qty / visible)
            if depths:
                depth_fraction = max(depths)
        leg_reasons = [f"{leg} leg: {str(row.get(f'{leg}_reason') or '').strip()}"
                       for leg in ("buy", "sell") if str(row.get(f"{leg}_reason") or "").strip()]
        reason = str(row.get("fallback_reason") or row.get("error") or "").strip()
        if not reason and leg_reasons:
            reason = "; ".join(leg_reasons)
        if not reason and event.unresolved:
            reason = str(row.get("hedge_status") or "fallback settlement unresolved").strip()
        if not reason and _event_fallback_used(event):
            reason = "primary fills left residual net quantity"
        if not reason:
            reason = None
        if buy_qty > sell_qty:
            exposed = f"buy {buy_venue or 'unknown'} {buy_qty - sell_qty:.8g} base"
        elif sell_qty > buy_qty:
            exposed = f"sell {sell_venue or 'unknown'} {sell_qty - buy_qty:.8g} base"
        elif abs(net_before) > 1e-12:
            exposed = f"net {net_before:+.8g} base"
        else:
            exposed = None
        status = ("unresolved" if event.unresolved else
                  "fallback" if _event_fallback_used(event) else "normal")
        diagnostics = _guard_trace_diagnostics(row)
        rows.append({
            "event_id": event.event_id,
            "timestamp": _utc(event.ts),
            "action": action,
            "reduce_only": event.reduce_only,
            "requested_qty": _number(requested_qty),
            "primary_buy_venue": buy_venue,
            "primary_buy_side": "buy" if buy_fills else None,
            "primary_buy_qty": _number(buy_qty),
            "primary_buy_avg_px": _number(buy_avg),
            "primary_buy_status": str(row.get("buy_status") or "") or None,
            "primary_buy_reason": str(row.get("buy_reason") or "") or None,
            "primary_sell_venue": sell_venue,
            "primary_sell_side": "sell" if sell_fills else None,
            "primary_sell_qty": _number(sell_qty),
            "primary_sell_avg_px": _number(sell_avg),
            "primary_sell_status": str(row.get("sell_status") or "") or None,
            "primary_sell_reason": str(row.get("sell_reason") or "") or None,
            "fallback_used": _event_fallback_used(event),
            "fallback_reason": reason,
            "fallback_status": str(row.get("hedge_status") or "") or None,
            "fallback_venue": fallback_venue or str(row.get("hedge_venue") or "") or None,
            "fallback_side": fallback[0].side if fallback else (str(row.get("hedge_side") or "") or None),
            "fallback_qty": _number(fallback_qty),
            "fallback_avg_px": _number(fallback_avg),
            "net_qty_before_fallback": _number(net_before),
            "net_qty_after_fallback": _number(net_after),
            "normal_primary_cashflow_usd": _number(primary_cash),
            "fallback_repair_cashflow_usd": _number(fallback_cash),
            "fallback_cashflow_usd": _number(fallback_cash),
            "actual_event_cashflow_usd": _number(event.gross_cash),
            "event_gross_pnl_impact_usd": _number(event.gross_cash),
            "fallback_counterfactual_impact_usd": _number(impact),
            "fallback_impact_reason": impact_reason,
            "hypothetical_normal_event_cashflow_usd": _number(hypothetical),
            "fallback_counterfactual_price_source": price_source,
            "signal_age_ms": _number(signal_age_ms),
            "depth_used_fraction": _number(depth_fraction),
            "exposed_leg": exposed,
            "unresolved": event.unresolved,
            "status": status,
            **diagnostics,
        })
    return rows


def resolve_rh_fee(metadata: Mapping[str, Any], fills_present: bool = True) -> FeeComponent:
    if not fills_present:
        return FeeComponent(True, 0.0, "no RH actual fills in report range", 0, 0)
    try:
        fee = finite(metadata.get("taker_fee"), "RH taker_fee")
    except ReportError:
        return FeeComponent(False, None, "official RH market metadata lacks numeric taker_fee")
    inactive = any(metadata.get(key) is False for key in
                   ("fee_mechanism_active", "fee_active", "fees_enabled"))
    explicit_zero = fee == 0 and inactive
    if not explicit_zero:
        return FeeComponent(False, None,
                            "official RH metadata does not prove taker_fee=0 and inactive fee mechanism")
    return FeeComponent(True, 0.0, "official RH metadata taker_fee=0 and mechanism inactive")


def _fill_identity(fill: Mapping[str, Any]) -> Optional[str]:
    if fill.get("tid") not in (None, ""):
        return f"tid:{fill['tid']}"
    if fill.get("hash") not in (None, "") and fill.get("oid") not in (None, ""):
        return f"hash:{fill['hash']}:oid:{fill['oid']}"
    return None


def _normal_hl_fill(raw: Mapping[str, Any]) -> dict:
    required = ("time", "coin", "side", "sz", "px", "fee", "feeToken")
    if any(raw.get(k) in (None, "") for k in required):
        raise ReportError("Hyperliquid fill missing required matching/fee field")
    ts = finite(raw["time"], "venue fill time") / 1000.0
    qty, price, fee = finite(raw["sz"], "venue fill size"), finite(raw["px"], "venue fill price"), finite(raw["fee"], "venue fill fee")
    if qty <= 0 or price <= 0 or fee < 0:
        raise ReportError("Hyperliquid fill has invalid size, price, or fee")
    side = str(raw["side"]).lower()
    if side in {"b", "buy"}:
        side = "buy"
    elif side in {"a", "s", "sell"}:
        side = "sell"
    else:
        raise ReportError("Hyperliquid fill has unsupported side")
    identity = _fill_identity(raw)
    if identity is None:
        raise ReportError("Hyperliquid fill lacks stable fill identity")
    token = str(raw["feeToken"]).upper()
    if token not in USD_FEE_TOKENS:
        raise ReportError("Hyperliquid fee token is not directly USD-denominated")
    return {"identity": identity, "oid": str(raw.get("oid") or ""),
            "time": ts, "coin": str(raw["coin"]), "side": side,
            "qty": qty, "price": price, "fee_usd": fee}


def _venue_fill_groups(raw_fills: Iterable[Mapping[str, Any]]) -> tuple[list[dict], Optional[str]]:
    identities: dict[str, dict] = {}
    groups: dict[str, list[dict]] = {}
    try:
        for raw in raw_fills:
            fill = _normal_hl_fill(raw)
            old = identities.get(fill["identity"])
            if old is not None:
                if old != fill:
                    return [], "conflicting duplicate Hyperliquid fill identity"
                continue
            identities[fill["identity"]] = fill
            # order id groups legitimate partial fills; no-oid fills are unique
            group = f"oid:{fill['oid']}" if fill["oid"] else fill["identity"]
            groups.setdefault(group, []).append(fill)
    except ReportError as exc:
        return [], str(exc)
    result = []
    for group_id, members in groups.items():
        if len({(x["coin"], x["side"]) for x in members}) != 1:
            return [], "Hyperliquid order identity spans conflicting coin or side"
        qty = sum(x["qty"] for x in members)
        result.append({"group_id": group_id, "members": members, "qty": qty,
                       "price": sum(x["qty"] * x["price"] for x in members) / qty,
                       "fee_usd": sum(x["fee_usd"] for x in members),
                       "time_min": min(x["time"] for x in members),
                       "time_max": max(x["time"] for x in members),
                       "coin": members[0]["coin"], "side": members[0]["side"]})
    return result, None


def match_entropy_fees(expected: Iterable[CanonicalFill], raw_fills: Iterable[Mapping[str, Any]], *,
                       coin: str, coverage_complete: bool = True,
                       clock_skew_ms: int = MATCH_CLOCK_SKEW_MS) -> FeeMatchResult:
    ent = [f for f in expected if f.venue == "entropy"]
    if not ent:
        return FeeMatchResult(coverage_complete, {}, 0, 0,
                              None if coverage_complete else "venue coverage incomplete")
    groups, error = _venue_fill_groups(raw_fills)
    if error:
        return FeeMatchResult(False, {}, 0, len(ent), error)
    candidates: dict[str, list[dict]] = {}
    for local in ent:
        side_groups = []
        for group in groups:
            if group["coin"] != coin or group["side"] != local.side:
                continue
            if not math.isclose(group["qty"], local.qty, rel_tol=NUMERIC_REL_TOL, abs_tol=NUMERIC_ABS_TOL):
                continue
            if not math.isclose(group["price"], local.price, rel_tol=NUMERIC_REL_TOL, abs_tol=NUMERIC_ABS_TOL):
                continue
            ts_ms = local.ts * 1000
            if group["time_min"] * 1000 - clock_skew_ms <= ts_ms <= group["time_max"] * 1000 + clock_skew_ms:
                side_groups.append(group)
        candidates[local.fill_id] = side_groups
    solutions: list[dict[str, dict]] = []
    expected_sorted = sorted(ent, key=lambda f: (len(candidates[f.fill_id]), f.fill_id))
    def assign(index: int, used: set[str], current: dict[str, dict]) -> None:
        if len(solutions) > 1:
            return
        if index == len(expected_sorted):
            solutions.append(dict(current)); return
        local = expected_sorted[index]
        for group in candidates[local.fill_id]:
            if group["group_id"] in used:
                continue
            used.add(group["group_id"]); current[local.fill_id] = group
            assign(index + 1, used, current)
            current.pop(local.fill_id, None); used.remove(group["group_id"])
    assign(0, set(), {})
    if not coverage_complete:
        return FeeMatchResult(False, {}, 0, len(ent), "Hyperliquid venue-fill coverage incomplete")
    if not solutions:
        return FeeMatchResult(False, {}, 0, len(ent), "missing or non-unique Hyperliquid fill assignment")
    if len(solutions) != 1:
        return FeeMatchResult(False, {}, 0, len(ent), "ambiguous global Hyperliquid fill assignment")
    matched = solutions[0]
    fees = {fill_id: group["fee_usd"] for fill_id, group in matched.items()}
    return FeeMatchResult(True, fees, len(fees), len(ent))


def attach_fees(events: list[PairEvent], hl_match: FeeMatchResult,
                rh_fee: FeeComponent) -> tuple[list[PairEvent], FeeComponent]:
    output = []
    hl_ids = {f.fill_id: f for e in events for f in e.fills if f.venue == "entropy"}
    any_rh = any(f.venue == "hedge" for e in events for f in e.fills)
    complete = hl_match.complete and rh_fee.complete
    reasons = [x for x in (hl_match.reason, rh_fee.reason if not rh_fee.complete else None) if x]
    matched_events = []
    total = 0.0
    for event in events:
        next_fills = []
        for fill in event.fills:
            if fill.venue == "entropy":
                fee = hl_match.fees_by_fill_id.get(fill.fill_id)
                if fee is None:
                    complete = False
                else:
                    total += fee
                    next_fills.append(CanonicalFill(**{**asdict(fill), "fee_usd": fee, "fee_status": "complete"}))
                    continue
                next_fills.append(fill)
            elif not any_rh:
                next_fills.append(CanonicalFill(**{**asdict(fill), "fee_usd": 0.0, "fee_status": "no_fills"}))
            elif rh_fee.complete:
                fee = 0.0 if rh_fee.usd == 0 else None
                if fee is None:
                    complete = False
                else:
                    total += fee
                next_fills.append(CanonicalFill(**{**asdict(fill), "fee_usd": fee, "fee_status": "complete" if fee is not None else "unknown"}))
            else:
                next_fills.append(fill)
        updated = PairEvent(event.event_id, event.ts, event.signal_ts, event.direction,
                            event.reduce_only, event.matched_qty, next_fills,
                            event.unresolved, event.anomaly,
                            dict(event.source_row))
        output.append(updated)
        matched_events.append(updated)
    expected_hl = len(hl_ids)
    if hl_match.matched != expected_hl:
        complete = False
    return output, FeeComponent(complete, total if complete else None,
                                "; ".join(reasons) if reasons else ("complete" if complete else "actual fee evidence incomplete"),
                                hl_match.matched, expected_hl + (sum(1 for e in events for f in e.fills if f.venue == "hedge") if any_rh else 0))


def _empty_cycle(direction: str, cycle_no: int, event: PairEvent) -> dict:
    return {"cycle_id": f"cycle-{cycle_no:04d}", "direction": direction,
            "start_ts": event.ts, "end_ts": None, "build_count": 0,
            "release_count": 0, "peak_base_inventory": 0.0,
            "peak_inventory_usd": 0.0, "peak_usd_known": True,
            "turnover_usd": 0.0, "gross_realized": 0.0,
            "fee_net_realized": 0.0, "fees": 0.0, "fees_known": True,
            "fallback_hedge_pnl": 0.0,
            "normal_execution_contribution": 0.0,
            "fallback_cashflow": 0.0,
            "fallback_counterfactual_impact": 0.0,
            "fallback_counterfactual_complete": True,
            "fallback_count": 0, "execution_event_count": 0,
            "unresolved_count": 0, "final_net_delta": 0.0,
            "event_ids": []}


def _utc(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def account_events(events: list[PairEvent], state: dict,
                   mark: Optional[MarketMark] = None) -> AccountingResult:
    inventory = 0.0
    gross_cost: Optional[float] = 0.0
    fee_cost: Optional[float] = 0.0
    gross_realized = 0.0
    fee_realized: Optional[float] = 0.0
    fees_total: Optional[float] = 0.0
    turnover = 0.0
    fallback_count = 0
    unresolved_count = 0
    anomalies: list[str] = []
    completed: list[dict] = []
    current: Optional[dict] = None
    curve = []
    peak_inventory_usd: Optional[float] = 0.0
    all_fills = [f for event in events for f in event.fills]
    attribution_by_id = {row["event_id"]: row
                         for row in build_execution_attributions(events)}
    for event in events:
        if event.unresolved:
            unresolved_count += 1
        if event.anomaly:
            anomalies.append(event.anomaly)
        is_fallback = _event_fallback_used(event)
        fallback_count += int(is_fallback)
        before = inventory
        delta = event.entropy_delta
        if event.matched_qty and abs(delta) > 0 and abs(abs(delta) - event.matched_qty) > max(1e-8, event.matched_qty * 1e-6):
            anomalies.append(f"event {event.event_id} Entropy fill delta differs from matched_qty")
        if before == 0 and delta != 0:
            current = _empty_cycle("LONG" if delta > 0 else "SHORT", len(completed) + 1, event)
        if current:
            if event.unresolved:
                current["unresolved_count"] += 1
            current["event_ids"].append(event.event_id)
            current["turnover_usd"] += event.turnover
            attribution = attribution_by_id[event.event_id]
            current["normal_execution_contribution"] += (
                attribution["normal_primary_cashflow_usd"] or 0.0)
            current["fallback_cashflow"] += (
                attribution["fallback_repair_cashflow_usd"] or 0.0)
            current["execution_event_count"] += 1
            if attribution["fallback_used"]:
                current["fallback_count"] += 1
                impact = attribution["fallback_counterfactual_impact_usd"]
                if impact is None:
                    current["fallback_counterfactual_complete"] = False
                else:
                    current["fallback_counterfactual_impact"] += impact
            if event.fees is None:
                current["fees_known"] = False
            elif current["fees_known"]:
                current["fees"] += event.fees
            same_direction_add = (before == 0 or before * delta > 0)
            if delta and same_direction_add:
                current["build_count"] += 1
            elif delta:
                current["release_count"] += 1
        turnover += event.turnover
        if any(f.fallback for f in event.fills):
            current_cash = event.gross_cash
            if current:
                current["fallback_hedge_pnl"] += current_cash
        net_cash = event.net_cash
        fees_known = event.fees is not None
        if event.fees is None:
            fees_total = None
        elif fees_total is not None:
            fees_total += event.fees
        if delta:
            after = before + delta
            if before * after < -1e-9 or abs(delta) > abs(before) + 1e-8 and before * delta < 0:
                anomalies.append(f"event {event.event_id} reverses inventory without flattening")
                gross_cost = fee_cost = None
            elif before == 0 or before * delta > 0:
                if gross_cost is not None:
                    gross_cost = (abs(before) * gross_cost - event.gross_cash) / abs(after)
                if fee_cost is not None:
                    fee_cost = ((abs(before) * fee_cost - net_cash) / abs(after)
                                if net_cash is not None else None)
            else:
                closed = abs(delta)
                if gross_realized is not None:
                    gross_realized = (gross_realized + event.gross_cash - gross_cost * closed
                                      if gross_cost is not None else None)
                fee_closed = (net_cash - fee_cost * closed
                              if net_cash is not None and fee_cost is not None else None)
                if fee_realized is not None:
                    fee_realized = fee_realized + fee_closed if fee_closed is not None else None
                if current:
                    current["gross_realized"] = (
                        current["gross_realized"] + event.gross_cash - gross_cost * closed
                        if current["gross_realized"] is not None and gross_cost is not None
                        else None)
                    if current.get("fee_net_realized") is not None:
                        current["fee_net_realized"] = (current["fee_net_realized"] + fee_closed
                            if fee_closed is not None else None)
                if abs(after) < 1e-10:
                    after = 0.0
                    gross_cost = 0.0
                    fee_cost = 0.0
            inventory = after
        elif abs(event.gross_cash) > 1e-12:
            if gross_realized is not None:
                gross_realized += event.gross_cash
            if fee_realized is not None:
                fee_realized = fee_realized + net_cash if net_cash is not None else None
            if current:
                if current["gross_realized"] is not None:
                    current["gross_realized"] += event.gross_cash
                if current.get("fee_net_realized") is not None:
                    current["fee_net_realized"] = (current["fee_net_realized"] + net_cash
                        if net_cash is not None else None)
        if current and inventory != 0:
            current["peak_base_inventory"] = max(current["peak_base_inventory"], abs(inventory))
            if delta and all(f.price for f in event.fills if f.venue in {"entropy", "hedge"}) and len([f for f in event.fills if f.venue == "entropy"]) and len([f for f in event.fills if f.venue == "hedge"]):
                e = [f for f in event.fills if f.venue == "entropy"]
                h = [f for f in event.fills if f.venue == "hedge"]
                avg_e = sum(f.qty * f.price for f in e) / sum(f.qty for f in e)
                avg_h = sum(f.qty * f.price for f in h) / sum(f.qty for f in h)
                current["peak_inventory_usd"] = max(current["peak_inventory_usd"], abs(inventory) * (avg_e + avg_h) / 2)
            else:
                current["peak_usd_known"] = False
        if current and inventory == 0:
            current["end_ts"] = event.ts
            current["final_net_delta"] = 0.0
            if current["unresolved_count"] == 0 and not event.unresolved:
                completed.append(_cycle_output(current))
            current = None
        curve.append({"ts": event.ts, "inventory": inventory,
                      "gross_realized": gross_realized,
                      "fee_net_realized": fee_realized})

    state_inventory = finite(state.get("entropy_qty"), "state entropy_qty")
    state_hedge = finite(state.get("hedge_qty"), "state hedge_qty")
    if abs(inventory - state_inventory) > max(1e-8, abs(state_inventory) * 1e-7) or abs(state_inventory + state_hedge) > 1e-8:
        anomalies.append("replayed actual fills do not reconcile to persisted Range inventory")
    pending = state.get("pending_intent")
    if pending is not None:
        anomalies.append("persisted Range state has pending intent")
    gross_unrealized: Optional[float] = None
    fee_unrealized: Optional[float] = None
    if mark and mark.available and not anomalies:
        if inventory > 0:
            liquidation = inventory * mark.entropy_bid - inventory * mark.hedge_ask
        elif inventory < 0:
            qty = abs(inventory)
            liquidation = -qty * mark.entropy_ask + qty * mark.hedge_bid
        else:
            liquidation = 0.0
        if gross_cost is not None:
            gross_unrealized = liquidation - gross_cost * abs(inventory)
        if fee_cost is not None:
            fee_unrealized = liquidation - fee_cost * abs(inventory)
    if current:
        current["final_net_delta"] = state_inventory + state_hedge
        current["current_inventory"] = inventory
        current["direction"] = "LONG" if inventory > 0 else "SHORT" if inventory < 0 else current["direction"]
        current["duration_sec"] = max(0.0, time.time() - current["start_ts"])
        current["open"] = True
        if current["peak_inventory_usd"] == 0 or not current["peak_usd_known"]:
            peak_inventory_usd = None
        else:
            peak_inventory_usd = current["peak_inventory_usd"]
    elif completed:
        peak_inventory_usd = max((c["peak_inventory_usd"] for c in completed if c["peak_inventory_usd"] is not None), default=0.0)
    return AccountingResult(events, all_fills, completed,
        _cycle_output(current, open_cycle=True) if current else None,
        gross_realized, fee_realized, fees_total, gross_unrealized,
        fee_unrealized, inventory, peak_inventory_usd, turnover,
        fallback_count, unresolved_count, anomalies, curve)


def build_recorder_curve(recorder_rows: list[Mapping[str, Any]],
                         events: list[PairEvent]) -> list[dict]:
    """Replay WAC at each completed-minute boundary; invalid rows remain gaps."""
    ordered_events = sorted(events, key=lambda e: (e.ts, e.event_id))
    event_index = 0
    inventory = 0.0
    gross_cost: Optional[float] = 0.0
    fee_cost: Optional[float] = 0.0
    gross_realized = 0.0
    fee_realized: Optional[float] = 0.0
    result = []
    ordered_rows = sorted(recorder_rows,
                          key=lambda r: finite(r.get("minute_ts"), "recorder minute_ts"))
    last_minute: Optional[float] = None
    for row in ordered_rows:
        minute_ts = finite(row.get("minute_ts"), "recorder minute_ts")
        if last_minute is not None and minute_ts > last_minute + 60:
            missing_ts = last_minute + 60
            while missing_ts < minute_ts:
                result.append({"ts": missing_ts, "gross_pnl": None,
                               "fee_net_pnl": None, "inventory_usd": None})
                missing_ts += 60
        last_minute = minute_ts
        boundary = minute_ts + 60.0
        while event_index < len(ordered_events) and ordered_events[event_index].ts <= boundary:
            event = ordered_events[event_index]
            before, delta = inventory, event.entropy_delta
            net_cash = event.net_cash
            if delta:
                after = before + delta
                if before * after < -1e-9 or (before * delta < 0 and abs(delta) > abs(before) + 1e-8):
                    gross_cost = fee_cost = None
                elif before == 0 or before * delta > 0:
                    if gross_cost is not None:
                        gross_cost = (abs(before) * gross_cost - event.gross_cash) / abs(after)
                    if fee_cost is not None:
                        fee_cost = (abs(before) * fee_cost - net_cash) / abs(after) if net_cash is not None else None
                else:
                    closed = abs(delta)
                    if gross_realized is not None:
                        gross_realized = (gross_realized + event.gross_cash - gross_cost * closed
                                          if gross_cost is not None else None)
                    if fee_realized is not None:
                        fee_realized = fee_realized + net_cash - fee_cost * closed if net_cash is not None and fee_cost is not None else None
                    if abs(after) < 1e-10:
                        after = 0.0
                        gross_cost = fee_cost = 0.0
                inventory = after
            elif abs(event.gross_cash) > 1e-12:
                if gross_realized is not None:
                    gross_realized += event.gross_cash
                if fee_realized is not None:
                    fee_realized = fee_realized + net_cash if net_cash is not None else None
            event_index += 1
        try:
            eb, ea, hb, ha = (finite(row.get(k), k) for k in
                              ("entropy_bid", "entropy_ask", "hedge_bid", "hedge_ask"))
            samples = finite(row.get("samples", 1), "recorder samples")
            if min(eb, ea, hb, ha) <= 0 or eb >= ea or hb >= ha or samples <= 0:
                raise ReportError("invalid historical BBO")
            if inventory > 0:
                liquidation = inventory * eb - inventory * ha
            elif inventory < 0:
                qty = abs(inventory)
                liquidation = -qty * ea + qty * hb
            else:
                liquidation = 0.0
            gross_unrealized = (liquidation - gross_cost * abs(inventory)
                                if gross_cost is not None else None)
            fee_unrealized = (liquidation - fee_cost * abs(inventory)
                              if fee_cost is not None else None)
            gross_pnl = (gross_realized + gross_unrealized
                         if gross_realized is not None and gross_unrealized is not None else None)
            fee_net = (fee_realized + fee_unrealized if fee_realized is not None
                       and fee_unrealized is not None else None)
            inventory_usd = abs(inventory) * ((eb + ea + hb + ha) / 4)
        except (ReportError, TypeError, ValueError):
            gross_pnl = fee_net = inventory_usd = None
        result.append({"ts": minute_ts, "gross_pnl": gross_pnl,
                       "fee_net_pnl": fee_net, "inventory_usd": inventory_usd})
    return result


def _cycle_output(cycle: Optional[dict], open_cycle: bool = False) -> Optional[dict]:
    if cycle is None:
        return None
    result = dict(cycle)
    result["start_utc"] = _utc(result.get("start_ts"))
    result["end_utc"] = _utc(result.get("end_ts"))
    result["duration_sec"] = ((result["end_ts"] - result["start_ts"])
                              if result.get("end_ts") is not None else result.get("duration_sec"))
    result["gross_spread_capture_usd"] = result.get("gross_realized")
    result["gross_pnl_usd"] = result.get("gross_realized")
    result["normal_execution_contribution_usd"] = result.get("normal_execution_contribution", 0.0)
    result["fallback_cashflow_usd"] = result.get("fallback_cashflow", 0.0)
    fallback_count = int(result.get("fallback_count", 0))
    execution_count = int(result.get("execution_event_count", 0))
    result["fallback_count"] = fallback_count
    result["fallback_rate"] = fallback_count / execution_count if execution_count else 0.0
    attribution_complete = bool(result.get("fallback_counterfactual_complete", True))
    result["fallback_attribution_status"] = (
        "not_applicable" if fallback_count == 0 else
        "complete" if attribution_complete else "incomplete")
    impact = (result.get("fallback_counterfactual_impact", 0.0)
              if attribution_complete else None)
    result["fallback_counterfactual_impact_usd"] = impact
    gross = result.get("gross_pnl_usd")
    excluding_drag = gross - impact if gross is not None and impact is not None else None
    result["gross_pnl_excluding_fallback_drag_usd"] = excluding_drag
    result["fallback_drag_pct"] = (
        -impact / excluding_drag * 100
        if impact is not None and excluding_drag is not None and excluding_drag > 0
        else None)
    result["fee_net_realized_pnl_usd"] = (
        result.get("fee_net_realized") if result.get("fees_known") else None)
    result["realized_pnl_before_funding_usd"] = (
        result.get("fee_net_realized") if result.get("fees_known") else None)
    result["fallback_hedge_pnl_usd"] = result.get("fallback_hedge_pnl", 0.0)
    result["fees_usd"] = result.get("fees") if result.get("fees_known") else None
    result["return_on_peak_inventory_pct"] = (
        100 * result["realized_pnl_before_funding_usd"] / result["peak_inventory_usd"]
        if result["realized_pnl_before_funding_usd"] is not None and result.get("peak_inventory_usd") else None)
    result["unresolved_count"] = result.get("unresolved_count", 0)
    result["open"] = open_cycle
    return result


def _state_signal(state: dict) -> dict:
    metadata = state.get("signal_metadata") or {}
    return {"long_percentile": metadata.get("long_percentile"),
            "short_percentile": metadata.get("short_percentile"),
            "range_4h_bps": metadata.get("range_4h_bps"),
            "range_gate_open": metadata.get("range_gate_open"),
            "target_usd": state.get("current_target_usd")}


def _inventory_usd(state: dict, mark: MarketMark) -> Optional[float]:
    if not mark.available:
        return None
    qty = abs(finite(state.get("entropy_qty"), "state entropy_qty"))
    mids = ((mark.entropy_bid + mark.entropy_ask) / 2,
            (mark.hedge_bid + mark.hedge_ask) / 2)
    return qty * sum(mids) / 2


def _number(value: Optional[float], digits: int = 8) -> Optional[float]:
    return round(float(value), digits) if value is not None and math.isfinite(value) else None


def _latest_recorder_minute(rows: list[dict[str, str]]) -> Optional[dict]:
    if not rows:
        return None
    row = rows[-1]
    return {k: row.get(k) for k in ("minute_ts", "time_utc", "entropy_bid", "entropy_ask",
        "hedge_bid", "hedge_ask", "samples")}


def build_fill_fee_expectations(events: list[PairEvent], coin: str) -> list[dict]:
    return [{"fill_id": f.fill_id, "ts": f.ts, "venue": f.venue, "side": f.side,
             "qty": f.qty, "price": f.price, "coin": coin}
            for e in events for f in e.fills if f.venue == "entropy"]


def fetch_hl_fills(account: str, start_ms: int, end_ms: int,
                   *, api_url: str = "https://api.hyperliquid.xyz",
                   post_json: Optional[Callable[[dict], Any]] = None) -> tuple[list[dict], bool, Optional[str]]:
    """Query bounded time slices; intervals at response cap are subdivided."""
    if not account:
        return [], False, "configured HL public account address is unavailable"
    if end_ms < start_ms:
        return [], False, "invalid Hyperliquid fill time range"
    request = post_json or (lambda body: _http_json(api_url + "/info", body, method="POST"))
    all_rows: list[dict] = []
    stack = [(int(start_ms), int(end_ms))]
    while stack:
        lo, hi = stack.pop()
        try:
            response = request({"type": "userFillsByTime", "user": account,
                                "startTime": lo, "endTime": hi})
            if not isinstance(response, list):
                return all_rows, False, "Hyperliquid userFillsByTime returned unexpected response"
        except Exception as exc:
            return all_rows, False, f"Hyperliquid userFillsByTime unavailable ({type(exc).__name__})"
        if len(response) >= HL_RESPONSE_CAP:
            if len(all_rows) + len(response) >= HL_HISTORY_LIMIT:
                return all_rows, False, "Hyperliquid history availability limit may truncate coverage"
            if hi - lo <= 1:
                return all_rows, False, "minimum Hyperliquid interval remains at response cap"
            mid = (lo + hi) // 2
            # stack LIFO; push right then left to retain deterministic time ordering
            stack.append((mid + 1, hi)); stack.append((lo, mid))
        else:
            all_rows.extend(response)
    dedup: dict[str, dict] = {}
    try:
        for row in all_rows:
            identity = _fill_identity(row)
            if identity is None:
                return all_rows, False, "Hyperliquid fill lacks stable identity"
            if identity in dedup and dedup[identity] != row:
                return all_rows, False, "conflicting duplicate Hyperliquid fill identity"
            dedup[identity] = row
    except Exception:
        return all_rows, False, "malformed Hyperliquid fill response"
    return sorted(dedup.values(), key=lambda r: (int(r.get("time", 0)), str(r.get("tid", "")))), True, None


def _http_json(url: str, payload: Optional[dict] = None,
               *, method: str = "GET", params: Optional[dict] = None,
               timeout: float = 12.0) -> Any:
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
        headers={"Content-Type": "application/json", "User-Agent": "range-performance-report/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        # Never include URL query/body or API response in output diagnostics.
        raise ReportError(f"read-only venue request failed ({type(exc).__name__})") from exc


def _hl_market(inputs: ReportInputs) -> tuple[str, str]:
    dexs = _http_json(inputs.hl_api_url + "/info", {"type": "perpDexs"}, method="POST")
    dex_names = [(x or {}).get("name") for x in dexs if isinstance(x, dict)] if isinstance(dexs, list) else []
    if inputs.hl_dex not in dex_names:
        raise ReportError("configured Hyperliquid DEX is not present in current metadata")
    meta = _http_json(inputs.hl_api_url + "/info", {"type": "meta", "dex": inputs.hl_dex}, method="POST")
    universe = meta.get("universe") if isinstance(meta, dict) else None
    candidates = [x.get("name") for x in universe or [] if isinstance(x, dict)
                  and x.get("name") in {f"{inputs.hl_dex}:{inputs.symbol}", inputs.symbol}
                  and not x.get("isDelisted")]
    if len(candidates) != 1:
        raise ReportError("Hyperliquid market identity is missing or ambiguous")
    return str(candidates[0]), inputs.hl_ws_url


def _rh_market(inputs: ReportInputs) -> tuple[int, dict, str]:
    response = _http_json(inputs.rh_api_url + "/api/v1/orderBooks")
    books = response.get("order_books") if isinstance(response, dict) else None
    found = [x for x in books or [] if isinstance(x, dict)
             and x.get("symbol") == inputs.rh_native_symbol and x.get("status") == "active"]
    if len(found) != 1:
        raise ReportError("RH active market identity is missing or ambiguous")
    market = found[0]
    return int(market["market_id"]), market, inputs.rh_ws_url


async def _read_public_books(inputs: ReportInputs) -> tuple[MarketMark, dict]:
    from entropy_arb.book import OrderBook
    from entropy_arb.feeds import HLBookFeed, LighterBookFeed
    coin, _ = _hl_market(inputs)
    market_id, rh_meta, _ = _rh_market(inputs)
    e_book, h_book = OrderBook(), OrderBook()
    changed = asyncio.Event()
    stop = asyncio.Event()
    e_feed = HLBookFeed("report-entropy", inputs.hl_ws_url, coin, e_book, changed.set)
    h_feed = LighterBookFeed("report-rh", inputs.rh_ws_url, market_id, h_book, changed.set)
    tasks = [asyncio.create_task(e_feed.run(stop)), asyncio.create_task(h_feed.run(stop))]
    max_age = inputs.max_quote_age_override or inputs.staleness_sec
    deadline = time.monotonic() + max(12.0, max_age + 8.0)
    try:
        while time.monotonic() < deadline:
            if (e_book.ready and h_book.ready and e_book.bids and e_book.asks
                    and h_book.bids and h_book.asks):
                now = time.time()
                age_e = now - e_book.last_update_ts
                age_h = now - h_book.last_update_ts
                eb, ea, hb, ha = e_book.best_bid(), e_book.best_ask(), h_book.best_bid(), h_book.best_ask()
                valid = all(x is not None and math.isfinite(x) and x > 0 for x in (eb, ea, hb, ha))
                fresh = valid and max(age_e, age_h) <= max_age
                uncrossed = valid and eb < ea and hb < ha
                server_age = e_book.server_age_ms(now)
                snapshot = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                if fresh and uncrossed:
                    mark = MarketMark(True, eb, ea, hb, ha, snapshot,
                        max(age_e, age_h), max_age,
                        f"Hyperliquid public l2Book + RH public order_book/{market_id}",
                        server_age, None)
                    metadata = dict(rh_meta)
                    metadata["hl_coin"] = coin
                    metadata["rh_market_id"] = market_id
                    return mark, metadata
                reason = "crossed or invalid BBO" if not uncrossed else "stale BBO"
            try:
                await asyncio.wait_for(changed.wait(), timeout=0.5)
                changed.clear()
            except asyncio.TimeoutError:
                pass
        return MarketMark(False, max_quote_age_sec=max_age, source="public WebSocket books",
                          reason="fresh BBO timeout"), rh_meta
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def public_market_snapshot(inputs: ReportInputs) -> tuple[MarketMark, dict]:
    try:
        return asyncio.run(_read_public_books(inputs))
    except Exception as exc:
        return MarketMark(False, source="public WebSocket books",
                          max_quote_age_sec=inputs.max_quote_age_override or inputs.staleness_sec,
                          reason=f"fresh public BBO unavailable ({type(exc).__name__})"), {}


def evaluate_ec2_regression(archive_rows: list[Mapping[str, Any]],
                            fee_result: FeeMatchResult,
                            provenance: bool,
                            realized_pnl: Optional[float]) -> dict:
    if not provenance:
        reason = "archived account provenance is not established"
    elif len(archive_rows) != 28 or not fee_result.complete or fee_result.matched != 28:
        reason = f"expected 28 uniquely matched archived fills; got {fee_result.matched}/{len(archive_rows)}"
    elif not archive_rows or not (archive_rows[-1].get("flat") or archive_rows[-1].get("final_net_delta") in (0, 0.0, "0")):
        reason = "archived cycle completion/flat state is not proven"
    elif realized_pnl is None or not math.isclose(realized_pnl, 0.261158, rel_tol=0, abs_tol=5e-6):
        reason = "recomputed realized fee-net PnL is outside documented rounding tolerance"
    else:
        return {"status": "PASS", "matched": 28, "expected": 28, "reason": None,
                "realized_pnl_before_funding_usd": realized_pnl, "tolerance_usd": 5e-6}
    return {"status": "NOT VERIFIABLE", "matched": fee_result.matched,
            "expected": 28, "reason": reason,
            "realized_pnl_before_funding_usd": _number(realized_pnl)}


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _svg_line_chart(title: str, points: list[tuple[float, Optional[float]]], *, width: int = 740, height: int = 180) -> str:
    vals = [v for _, v in points if v is not None and math.isfinite(v)]
    if not vals:
        return f'<section><h2>{html.escape(title)}</h2><p>No complete data; gaps retained.</p></section>'
    lo, hi = min(vals), max(vals)
    if hi == lo:
        hi = lo + 1
    coords: list[str] = []
    segments: list[list[str]] = []
    current: list[str] = []
    for i, (_, value) in enumerate(points):
        if value is None or not math.isfinite(value):
            if current:
                segments.append(current); current = []
            continue
        x = 20 + (width - 40) * (i / max(1, len(points) - 1))
        y = height - 20 - (height - 40) * ((value - lo) / (hi - lo))
        current.append(f"{x:.2f},{y:.2f}")
    if current:
        segments.append(current)
    polylines = "".join(f'<polyline fill="none" stroke="#2878b5" stroke-width="2" points="{" ".join(seg)}"/>' for seg in segments)
    return (f'<section><h2>{html.escape(title)}</h2><svg role="img" viewBox="0 0 {width} {height}" '
            f'width="100%" height="{height}"><path d="M20 {height-20}H{width-20}" stroke="#aaa"/>'
            f'{polylines}</svg><small>{lo:.6g} … {hi:.6g}</small></section>')


def render_html(metrics: dict, cycles: list[dict], curve: list[dict], *, fee_status: dict,
                mark: MarketMark, symbol: str, hedge: str, consistent: bool = True,
                execution_events: Optional[list[dict]] = None) -> str:
    def show(key: str, suffix: str = "") -> str:
        value = metrics.get(key)
        return "N/A" if value is None else f"{value}{suffix}"
    current = metrics.get("current_open_cycle")
    direction = metrics.get("current_direction") or "FLAT"
    state_html = html.escape(json.dumps(current, sort_keys=True, ensure_ascii=False)) if current else "None"
    curve_label = ("Trading PnL / equity curve (fee-net)" if fee_status.get("complete")
                   else "Trading PnL / equity curve (gross; fees incomplete)")
    equity = [(p["ts"], p.get("fee_net_pnl") if fee_status.get("complete") else p.get("gross_pnl")) for p in curve]
    inv = [(p["ts"], p.get("inventory_usd")) for p in curve]
    execution_events = execution_events or []
    def event_cell(value: Any) -> str:
        return "N/A" if value is None else html.escape(str(value))
    event_rows = "".join(
        "<tr>" + "".join(f"<td>{event_cell(value)}</td>" for value in (
            event.get("timestamp"), event.get("event_id"), event.get("action"),
            event.get("fallback_reason"), event.get("exposed_leg"),
            (f"{event.get('fallback_side')} {event.get('fallback_venue')}"
             if event.get("fallback_used") else "No"),
            event.get("fallback_qty"), event.get("signal_age_ms"),
            event.get("depth_used_fraction"),
            event.get("event_gross_pnl_impact_usd"), event.get("status"),
            event.get("block_stage"), event.get("exact_guard_reason"),
            event.get("rh_nonce_wait_ms"), event.get("pre_submit_wait_ms"),
            event.get("transport_attempted"))) + "</tr>"
        for event in execution_events)
    if not event_rows:
        event_rows = '<tr><td colspan="16">No execution events</td></tr>'
    cycle_rows = "".join(
        "<tr>" + "".join(f"<td>{event_cell(value)}</td>" for value in (
            cycle.get("cycle_id"), cycle.get("direction"), cycle.get("start_utc"),
            cycle.get("end_utc"), cycle.get("gross_pnl_usd"),
            cycle.get("fallback_counterfactual_impact_usd"),
            cycle.get("gross_pnl_excluding_fallback_drag_usd"),
            cycle.get("fallback_drag_pct"), cycle.get("fallback_count"),
            cycle.get("fallback_rate"), cycle.get("fallback_attribution_status"))) + "</tr>"
        for cycle in cycles)
    if not cycle_rows:
        cycle_rows = '<tr><td colspan="11">No completed cycles</td></tr>'
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Range performance {html.escape(symbol)} / {html.escape(hedge)}</title>
<style>body{{font:15px system-ui,sans-serif;max-width:1280px;margin:2rem auto;padding:0 1rem;color:#18212b}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:.65rem}}.card,section{{border:1px solid #d5dde5;border-radius:8px;padding:1rem;margin:.65rem 0}}.label{{color:#536273;font-size:.85rem}}.value{{font-size:1.3rem;font-weight:650}}table{{border-collapse:collapse;width:100%;display:block;overflow-x:auto}}td,th{{border-bottom:1px solid #ddd;text-align:left;padding:.4rem;white-space:nowrap}}.warn{{color:#9b2c20}}</style></head><body>
<h1>Range Inventory Performance — {html.escape(symbol)} / {html.escape(hedge)}</h1><p>Funding: excluded. Snapshot consistent: {str(consistent).lower()}.</p>
{f'<p class="warn">Snapshot reason: {html.escape(str(metrics.get("snapshot_reason")))}</p>' if metrics.get('snapshot_reason') else ''}
<div class="grid">
<div class="card"><div class="label">Gross Trading PnL</div><div class="value">{show('gross_total_pnl_usd', ' USD')}</div></div>
<div class="card"><div class="label">Actual Fees</div><div class="value">{show('fees_usd', ' USD')}</div></div>
<div class="card"><div class="label">Normal execution contribution (cashflow subtotal)</div><div class="value">{show('normal_execution_contribution_usd', ' USD')}</div></div>
<div class="card"><div class="label">Fallback contribution / counterfactual impact</div><div class="value">{show('fallback_cashflow_usd', ' / ')}{show('fallback_counterfactual_impact_usd', ' USD')}</div><small>Attribution subtotal only; already included in gross PnL.</small></div>
<div class="card"><div class="label">Fallback count / rate</div><div class="value">{metrics.get('fallback_count',0)} / {show('fallback_rate')}</div></div>
<div class="card"><div class="label">Gross PnL excluding fallback drag / drag %</div><div class="value">{show('gross_pnl_excluding_fallback_drag_usd', ' / ')}{show('fallback_drag_pct','%')}</div></div>
<div class="card"><div class="label">Fallback attribution status</div><div class="value">{html.escape(str(metrics.get('fallback_attribution_status','unknown')))}</div></div>
<div class="card"><div class="label">Legacy fallback event cashflow diagnostic (not opportunity cost)</div><div class="value">{show('fallback_hedge_pnl_usd', ' USD')}</div></div>
<div class="card"><div class="label">Trading PnL before funding</div><div class="value">{show('total_pnl_before_funding_usd', ' USD')}</div></div>
<div class="card"><div class="label">Realized (gross / fee-net)</div><div class="value">{show('gross_realized_pnl_usd', ' / ')}{show('fee_net_realized_pnl_usd', ' USD')}</div></div>
<div class="card"><div class="label">Unrealized (gross / fee-net)</div><div class="value">{show('gross_unrealized_pnl_usd', ' / ')}{show('unrealized_pnl_usd', ' USD')}</div></div>
<div class="card"><div class="label">Fee data status</div><div class="value">{html.escape(fee_status.get('status','unknown'))}</div><small>{html.escape(fee_status.get('reason') or '')}</small></div>
<div class="card"><div class="label">Market mark freshness</div><div class="value">{html.escape(mark.snapshot_utc or 'N/A')}</div><small>age={show('quote_age_sec','s')}; max={show('max_quote_age_sec','s')}; source={html.escape(mark.source or 'N/A')}</small></div>
<div class="card"><div class="label">Current direction/state</div><div class="value">{html.escape(str(direction))}</div><small>{state_html}</small></div>
<div class="card"><div class="label">Inventory USD / cap</div><div class="value">{show('current_inventory_usd',' / ')}{show('cap_usd',' USD')}</div></div>
<div class="card"><div class="label">Peak inventory USD / current cycle return</div><div class="value">{show('peak_inventory_usd',' / ')}{show('current_cycle_return_pct','%')}</div></div>
<div class="card"><div class="label">Build / release-cover</div><div class="value">{metrics.get('build_count',0)} / {metrics.get('release_count',0)}</div></div>
<div class="card"><div class="label">Executions / fallbacks / unresolved</div><div class="value">{metrics.get('execution_count',0)} / {metrics.get('fallback_count',0)} / {metrics.get('unresolved_count',0)}</div></div>
<div class="card"><div class="label">Current signal</div><div class="value">long={show('long_percentile')} short={show('short_percentile')} range={show('range_4h_bps')}bps gate={html.escape(str(metrics.get('range_gate_open')))} target={show('current_target_usd')} USD</div></div>
<div class="card"><div class="label">Current net delta (state-derived) / HALT</div><div class="value">{show('current_net_delta')} / {html.escape(str(metrics.get('halted','unknown')))}</div></div>
</div>
<p class="warn">Fee status reason: {html.escape(fee_status.get('reason') or 'complete')}</p>
<h2>Current open cycle</h2><pre>{state_html if consistent else 'N/A (' + html.escape(str(metrics.get('snapshot_reason') or 'inconsistent_snapshot')) + ')'}</pre>
<h2>Execution Cost / Fallback</h2><table><thead><tr><th>Time</th><th>Event</th><th>Action</th><th>Failure reason</th><th>Exposed leg</th><th>Fallback</th><th>Qty</th><th>Signal age ms</th><th>Depth</th><th>Gross impact</th><th>Status</th><th>Block stage</th><th>Exact guard reason</th><th>RH nonce wait ms</th><th>Time between guard #1 and #2</th><th>Transport attempted</th></tr></thead><tbody>{event_rows}</tbody></table>
{_svg_line_chart(curve_label, equity)}
{_svg_line_chart('Range inventory USD over time', inv)}
<h2>Completed cycles — gross PnL / fallback attribution</h2><table><thead><tr><th>Cycle</th><th>Direction</th><th>Start</th><th>End</th><th>Gross PnL</th><th>Fallback impact</th><th>Gross excluding drag</th><th>Fallback drag %</th><th>Fallback count</th><th>Rate</th><th>Attribution status</th></tr></thead><tbody>{cycle_rows}</tbody></table>
</body></html>"""


def build_report(snapshot: Snapshot, inputs: ReportInputs, mark: MarketMark,
                 fee_component: FeeComponent, hl_match: Optional[FeeMatchResult] = None,
                 regression: Optional[dict] = None,
                 prepared_events: Optional[list[PairEvent]] = None,
                 ledger_anomalies: Optional[list[str]] = None) -> tuple[dict, str, str]:
    has_artifact_snapshot = snapshot.consistent or bool(snapshot.boundaries)
    if has_artifact_snapshot:
        if prepared_events is None:
            events, fills, anomalies = build_fill_ledger(snapshot.rows)
            events, fee_component = attach_fees(events,
                hl_match or FeeMatchResult(not any(f.venue == "entropy" for e in events for f in e.fills), {}, 0,
                    sum(1 for e in events for f in e.fills if f.venue == "entropy"), "fee enrichment not run"),
                fee_component)
        else:
            events = prepared_events
            fills = [f for e in events for f in e.fills]
            anomalies = list(ledger_anomalies or [])
        state = snapshot.state
        accounting = account_events(events, state,
            mark if snapshot.consistent else MarketMark(False, reason=snapshot.reason))
        accounting.anomalies.extend(anomalies)
        if not snapshot.consistent and snapshot.reason:
            accounting.anomalies.append(snapshot.reason)
    else:
        state = {}
        accounting = AccountingResult([], [], [], None, None, None, None,
            None, None, float("nan"), None, 0.0, 0, 0,
            [snapshot.reason or "inconsistent_snapshot"], [])
    execution_attributions = build_execution_attributions(accounting.events)
    fallback_events = [event for event in execution_attributions if event["fallback_used"]]
    fallback_event_count = len(fallback_events)
    fallback_rate = fallback_event_count / len(execution_attributions) if execution_attributions else 0.0
    fallback_cashflow = sum(event["fallback_repair_cashflow_usd"] or 0.0
                            for event in execution_attributions)
    normal_execution_contribution = sum(event["normal_primary_cashflow_usd"] or 0.0
                                        for event in execution_attributions)
    fallback_attribution_complete = all(
        event["fallback_counterfactual_impact_usd"] is not None
        for event in fallback_events)
    fallback_impact = (
        sum(event["fallback_counterfactual_impact_usd"] or 0.0 for event in fallback_events)
        if fallback_event_count == 0 or fallback_attribution_complete else None)
    fallback_attribution_status = (
        "not_applicable" if fallback_event_count == 0 else
        "complete" if fallback_attribution_complete else "incomplete")
    if snapshot.consistent:
        accounting.inventory_curve = build_recorder_curve(snapshot.recorder_rows, accounting.events)
    current_total_gross = (accounting.gross_realized + accounting.gross_unrealized
                           if accounting.gross_realized is not None
                           and accounting.gross_unrealized is not None and snapshot.consistent else None)
    gross_excluding_fallback_drag = (
        current_total_gross - fallback_impact
        if current_total_gross is not None and fallback_impact is not None else None)
    fallback_drag_pct = (
        -fallback_impact / gross_excluding_fallback_drag * 100
        if fallback_impact is not None and gross_excluding_fallback_drag is not None
        and gross_excluding_fallback_drag > 0 else None)
    fee_complete = fee_component.complete and accounting.fee_net_realized is not None and snapshot.consistent
    fee_net_total = (accounting.fee_net_realized + accounting.fee_net_unrealized
                     if fee_complete and accounting.fee_net_unrealized is not None else None)
    signal = _state_signal(state) if state and snapshot.consistent else {}
    inventory_usd = _inventory_usd(state, mark) if state and snapshot.consistent else None
    current = accounting.current_cycle or {} if snapshot.consistent else {}
    cap = None
    if state and snapshot.consistent:
        cap = ((state.get("parameters") or {}).get("hard_cap_usd")
               or state.get("parameters", {}).get("max_inventory_usd"))
    build_count = sum(c.get("build_count", 0) for c in accounting.completed_cycles) + current.get("build_count", 0)
    release_count = sum(c.get("release_count", 0) for c in accounting.completed_cycles) + current.get("release_count", 0)
    latest = _latest_recorder_minute(snapshot.recorder_rows) if snapshot.consistent else None
    metrics = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "symbol": inputs.symbol, "hedge": inputs.hedge, "funding_included": False,
        "gross_total_pnl_usd": _number(current_total_gross),
        "total_pnl_before_funding_usd": _number(fee_net_total),
        "gross_realized_pnl_usd": _number(accounting.gross_realized) if has_artifact_snapshot else None,
        "realized_pnl_usd": _number(accounting.fee_net_realized) if fee_complete else None,
        "fee_net_realized_pnl_usd": _number(accounting.fee_net_realized) if fee_complete else None,
        "gross_unrealized_pnl_usd": _number(accounting.gross_unrealized) if snapshot.consistent else None,
        "unrealized_pnl_usd": _number(accounting.fee_net_unrealized) if fee_complete else None,
        "fees_usd": _number(fee_component.usd) if fee_complete else None,
        "fee_status": "complete" if fee_complete else "incomplete",
        "fee_reason": fee_component.reason,
        "fee_match_count": hl_match.matched if hl_match else 0,
        "fee_expected_count": hl_match.expected if hl_match else sum(1 for f in accounting.fills if f.venue == "entropy"),
        "fallback_hedge_pnl_usd": _number(sum(e.gross_cash for e in accounting.events if any(f.fallback for f in e.fills))),
        "normal_execution_contribution_usd": _number(normal_execution_contribution),
        "fallback_cashflow_usd": _number(fallback_cashflow),
        "fallback_counterfactual_impact_usd": _number(fallback_impact),
        "fallback_attribution_status": fallback_attribution_status,
        "fallback_events": fallback_events,
        "execution_events": execution_attributions,
        "fallback_rate": _number(fallback_rate),
        "gross_pnl_excluding_fallback_drag_usd": _number(gross_excluding_fallback_drag),
        "fallback_drag_pct": _number(fallback_drag_pct),
        "current_inventory_usd": _number(inventory_usd),
        "current_signed_inventory_base": _number(accounting.current_inventory) if snapshot.consistent else None,
        "peak_inventory_usd": _number(accounting.peak_inventory_usd),
        "current_target_usd": _number(signal.get("target_usd")),
        "current_direction": state.get("current_direction") if state and snapshot.consistent else None,
        "current_net_delta": _number((finite(state.get("entropy_qty"), "entropy_qty") + finite(state.get("hedge_qty"), "hedge_qty")) if state and snapshot.consistent else None),
        "cap_usd": _number(cap), "completed_cycles": len(accounting.completed_cycles),
        "build_count": build_count, "release_count": release_count,
        "execution_count": len(accounting.events), "fallback_count": fallback_event_count,
        "unresolved_count": accounting.unresolved_count,
        "turnover_usd": _number(accounting.turnover_usd),
        "current_cycle_return_pct": (None if not current or fee_net_total is None or not current.get("peak_inventory_usd") else _number(100 * ((current.get("fee_net_realized_pnl_usd") or 0) + (accounting.fee_net_unrealized or 0)) / current["peak_inventory_usd"])),
        "long_percentile": _number(signal.get("long_percentile")),
        "short_percentile": _number(signal.get("short_percentile")),
        "range_4h_bps": _number(signal.get("range_4h_bps")),
        "range_gate_open": signal.get("range_gate_open"),
        "latest_recorder_minute": latest,
        "pending_intent": state.get("pending_intent") if state and snapshot.consistent else None,
        "halted": (snapshot.halt.get("halted") if snapshot.consistent else None),
        "current_open_cycle": accounting.current_cycle if snapshot.consistent else None,
        "lots": ([{"direction": "LONG" if accounting.current_inventory > 0 else "SHORT",
                   "quantity": _number(abs(accounting.current_inventory)),
                   "mean_cost_per_base": _number(state.get("mean_cost_per_base") if state else None)}]
                 if accounting.current_inventory and snapshot.consistent else []),
        "anomalies": accounting.anomalies,
        "snapshot_consistent": snapshot.consistent,
        "snapshot_attempts": snapshot.attempts,
        "snapshot_reason": snapshot.reason,
        "market_mark": asdict(mark),
        "quote_age_sec": _number(mark.quote_age_sec),
        "max_quote_age_sec": _number(mark.max_quote_age_sec),
        "regression": regression or {"status": "NOT VERIFIABLE", "reason": "archived fixture/provenance unavailable"},
    }
    metrics = _json_safe(metrics)
    fee_status = {"status": metrics["fee_status"], "reason": metrics["fee_reason"]}
    html_text = render_html(metrics, accounting.completed_cycles, accounting.inventory_curve,
                            fee_status=fee_status, mark=mark, symbol=inputs.symbol,
                            hedge=inputs.hedge, consistent=snapshot.consistent,
                            execution_events=execution_attributions)
    cycle_buf = io.StringIO(newline="")
    writer = csv.DictWriter(cycle_buf, fieldnames=CYCLE_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for cycle in accounting.completed_cycles:
        writer.writerow({
            "cycle_id": cycle.get("cycle_id"), "direction": cycle.get("direction"),
            "start_utc": cycle.get("start_utc"), "end_utc": cycle.get("end_utc"),
            "duration_sec": cycle.get("duration_sec"), "build_count": cycle.get("build_count"),
            "release_count": cycle.get("release_count"), "peak_base_inventory": cycle.get("peak_base_inventory"),
            "peak_inventory_usd": cycle.get("peak_inventory_usd"), "turnover_usd": cycle.get("turnover_usd"),
            "gross_spread_capture_usd": cycle.get("gross_spread_capture_usd"), "fees_usd": cycle.get("fees_usd"),
            "fallback_hedge_pnl_usd": cycle.get("fallback_hedge_pnl_usd"),
            "gross_pnl_usd": cycle.get("gross_pnl_usd"),
            "normal_execution_contribution_usd": cycle.get("normal_execution_contribution_usd"),
            "fallback_cashflow_usd": cycle.get("fallback_cashflow_usd"),
            "fallback_counterfactual_impact_usd": cycle.get("fallback_counterfactual_impact_usd"),
            "fallback_count": cycle.get("fallback_count"),
            "fallback_rate": cycle.get("fallback_rate"),
            "fallback_attribution_status": cycle.get("fallback_attribution_status"),
            "gross_pnl_excluding_fallback_drag_usd": cycle.get("gross_pnl_excluding_fallback_drag_usd"),
            "fallback_drag_pct": cycle.get("fallback_drag_pct"),
            "realized_pnl_before_funding_usd": cycle.get("realized_pnl_before_funding_usd"),
            "return_on_peak_inventory_pct": cycle.get("return_on_peak_inventory_pct"),
            "unresolved_count": cycle.get("unresolved_count"), "final_net_delta": cycle.get("final_net_delta")})
    return metrics, html_text, cycle_buf.getvalue()


def atomic_write_reports(paths: ReportPaths, metrics: dict, html_text: str, cycles_csv: str) -> None:
    outputs = {paths.output_json: json.dumps(_json_safe(metrics), indent=2, sort_keys=True, allow_nan=False) + "\n",
              paths.output_html: html_text,
              paths.cycles_csv: cycles_csv}
    staged: list[tuple[Path, Path]] = []
    try:
        paths.output_html.parent.mkdir(parents=True, exist_ok=True)
        for target, content in outputs.items():
            fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
            temp = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
            staged.append((temp, target))
        for temp, target in staged:
            os.replace(temp, target)
    except OSError as exc:
        for temp, _ in staged:
            try: temp.unlink(missing_ok=True)
            except OSError: pass
        raise ReportError("could not atomically write report artifacts") from exc


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--hedge", required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--recorder", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--halt-state", type=Path)
    parser.add_argument("--trades", type=Path)
    parser.add_argument("--max-quote-age-sec", type=float)
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        inputs = resolve_inputs(root=args.root, symbol=args.symbol, hedge=args.hedge,
            recorder_override=args.recorder, config_path=args.config, env_file=args.env_file,
            offline=args.offline, state_path=args.state, halt_path=args.halt_state,
            trades_path=args.trades, max_quote_age_sec=args.max_quote_age_sec)
        def quote_provider(_state):
            if args.offline:
                return MarketMark(False, reason="offline mode; no current network mark"), {}
            return public_market_snapshot(inputs)

        snapshot: Optional[Snapshot] = None
        events: list[PairEvent] = []
        fills: list[CanonicalFill] = []
        ledger_anomalies: list[str] = []
        hl_match = FeeMatchResult(False, {}, 0, 0, "actual-fee enrichment not completed")
        fee_component = FeeComponent(False, None, "actual-fee enrichment not completed")
        accepted_snapshot = False
        last_reason = "snapshot could not be validated"
        # Re-run the complete read sequence if venue enrichment overlapped a
        # state/artifact change.  Each pass remains read-only and bounded.
        for pass_number in range(1, MAX_SNAPSHOT_ATTEMPTS + 1):
            candidate = capture_consistent_snapshot(inputs, quote_provider, retries=1)
            candidate.attempts = pass_number
            snapshot = candidate
            if not candidate.consistent:
                last_reason = candidate.reason or "source snapshot changed"
                continue
            events, fills, ledger_anomalies = build_fill_ledger(candidate.rows)
            rh_metadata = candidate.venue_metadata
            if args.offline:
                hl_match = FeeMatchResult(not any(f.venue == "entropy" for f in fills), {}, 0,
                    sum(1 for f in fills if f.venue == "entropy"),
                    "offline mode has no actual-fee enrichment")
                rh_fee = resolve_rh_fee({}, any(f.venue == "hedge" for f in fills))
            else:
                expected = build_fill_fee_expectations(events, inputs.hl_dex + ":" + inputs.symbol)
                if expected:
                    times = [int(x["ts"] * 1000) for x in expected]
                    raw_fills, coverage, cover_reason = fetch_hl_fills(inputs.hl_account_address or "",
                        min(times) - MATCH_CLOCK_SKEW_MS, max(times) + MATCH_CLOCK_SKEW_MS)
                else:
                    raw_fills, coverage, cover_reason = [], True, None
                coin = rh_metadata.get("hl_coin") or (inputs.hl_dex + ":" + inputs.symbol)
                hl_match = match_entropy_fees(fills, raw_fills, coin=coin,
                                               coverage_complete=coverage)
                if cover_reason and not hl_match.complete:
                    hl_match.reason = cover_reason
                rh_fee = resolve_rh_fee(rh_metadata, any(f.venue == "hedge" for f in fills))
            events, fee_component = attach_fees(events, hl_match, rh_fee)
            if snapshot_still_current(inputs, candidate):
                accepted_snapshot = True
                break
            last_reason = "state, HALT, or append-only source boundary changed during enrichment"

        if snapshot is None:  # defensive; the bounded loop always assigns one
            raise ReportError("could not obtain a report snapshot")
        if not accepted_snapshot:
            snapshot.consistent = False
            snapshot.reason = f"inconsistent_snapshot: {last_reason}"
            fee_component = FeeComponent(False, None, snapshot.reason,
                                         fee_component.matched, fee_component.expected)
            hl_match = FeeMatchResult(False, {}, 0, hl_match.expected, snapshot.reason)
        mark = snapshot.mark or MarketMark(False, reason=snapshot.reason or "no BBO snapshot")
        if not snapshot.consistent:
            mark = MarketMark(False, snapshot_utc=mark.snapshot_utc,
                quote_age_sec=mark.quote_age_sec, max_quote_age_sec=mark.max_quote_age_sec,
                source=mark.source, exchange_age_ms=mark.exchange_age_ms,
                reason=snapshot.reason or "inconsistent_snapshot")
        if mark.available and mark.snapshot_utc:
            try:
                snapshot_time = datetime.fromisoformat(mark.snapshot_utc.replace("Z", "+00:00")).timestamp()
                age = max(0.0, time.time() - snapshot_time)
                if age > (mark.max_quote_age_sec or 0):
                    mark = MarketMark(False, snapshot_utc=mark.snapshot_utc,
                        quote_age_sec=age, max_quote_age_sec=mark.max_quote_age_sec,
                        source=mark.source, exchange_age_ms=mark.exchange_age_ms,
                        reason="BBO exceeded freshness limit before report completion")
                else:
                    mark.quote_age_sec = age
            except (TypeError, ValueError):
                mark = MarketMark(False, reason="BBO snapshot timestamp is invalid")
        metrics, html_text, cycles_csv = build_report(snapshot, inputs, mark,
            FeeComponent(fee_component.complete, fee_component.usd, fee_component.reason,
                         fee_component.matched, fee_component.expected),
            hl_match=hl_match, prepared_events=events if snapshot.boundaries else None,
            ledger_anomalies=ledger_anomalies)
        atomic_write_reports(inputs.paths, metrics, html_text, cycles_csv)
        print(json.dumps({"html": str(inputs.paths.output_html), "json": str(inputs.paths.output_json),
                          "cycles_csv": str(inputs.paths.cycles_csv),
                          "snapshot_consistent": metrics["snapshot_consistent"],
                          "fee_status": metrics["fee_status"],
                          "total_pnl_before_funding_usd": metrics["total_pnl_before_funding_usd"]},
                         sort_keys=True))
        return 0
    except ReportError as exc:
        print(f"report error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
