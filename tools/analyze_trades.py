#!/usr/bin/env python3
"""Analyze execution summaries written by the arbitrage engine.

This analyzer is intentionally separate from ``tools/analyze.py``: that tool
studies minute-level market observations, while this one studies realized
execution quality and hedge outcomes from ``logs/trades``.

Usage::

    python3 tools/analyze_trades.py
    python3 tools/analyze_trades.py --csv logs/trades/trades-SNDK-lighter-rh.csv
    python3 tools/analyze_trades.py --csv path.csv --hours 24
"""
from __future__ import annotations

import argparse
import csv
import glob
import math
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Mapping, Tuple


DEFAULT_CSV = "logs/trades/trades.csv"
TRADE_GLOB = "logs/trades/trades-*.csv"
EPSILON = 1e-9

_TEXT_FIELDS = {
    "run_id", "event_id", "symbol", "hedge", "direction", "buy_venue",
    "sell_venue",
    "buy_status", "sell_status", "hedge_status", "hedge_venue",
    "hedge_side", "first_settled_leg", "buy_reason", "sell_reason",
    "error",
}
_FLAG_FIELDS = {"unresolved", "ok"}
_NUMERIC_FIELDS = {
    "ts", "signal_ts", "execution_ms", "buy_settle_ms", "sell_settle_ms",
    "qty", "buy_bbo_px",
    "buy_bbo_qty", "sell_bbo_px", "sell_bbo_qty", "buy_quote_age_ms",
    "sell_quote_age_ms", "buy_limit", "sell_limit", "buy_protect_limit",
    "sell_protect_limit", "buy_notional", "sell_notional", "exp_edge_usd",
    "gross_edge_usd", "marginal_premium_bps", "midline_bps", "inv_add_bps",
    "buy_fill", "sell_fill", "buy_avg_px", "sell_avg_px", "matched_qty",
    "residual_qty", "hedge_fill", "hedge_duration_ms",
    "hedge_avg_px", "hedge_notional", "remaining_net_qty", "fill_edge_usd",
    "leg_settle_gap_ms", "entropy_book_server_age_ms",
    "entropy_update_gap_ms", "hedge_update_gap_ms",
}
_REQUIRED_FIELDS = {
    "ts", "event_id", "symbol", "hedge", "execution_ms", "direction",
    "buy_venue", "sell_venue", "qty", "buy_bbo_px", "buy_bbo_qty",
    "sell_bbo_px", "sell_bbo_qty", "buy_quote_age_ms", "sell_quote_age_ms",
    "exp_edge_usd", "marginal_premium_bps", "midline_bps", "buy_fill",
    "sell_fill", "buy_avg_px", "sell_avg_px", "matched_qty", "residual_qty",
    "unresolved", "ok", "hedge_status", "hedge_venue", "hedge_side",
    "hedge_fill", "hedge_avg_px", "hedge_notional", "hedge_duration_ms",
    "remaining_net_qty", "fill_edge_usd",
}

_SIGNAL_BUCKETS = (
    (0.0, 2.0, "0-2"),
    (2.0, 4.0, "2-4"),
    (4.0, 6.0, "4-6"),
    (6.0, 8.0, "6-8"),
    (8.0, 10.0, "8-10"),
)


def _pair_csv_matches() -> List[str]:
    """Return namespaced trade files in stable order."""
    return sorted(glob.glob(TRADE_GLOB))


def _resolve_csv_path(path: str) -> str:
    """Use the only namespaced trade file when the base path is absent."""
    if path != DEFAULT_CSV or os.path.exists(path):
        return path
    matches = _pair_csv_matches()
    if len(matches) == 1:
        return matches[0]
    return path


def _parse_float(row: Mapping[str, str], key: str) -> float | None:
    value = (row.get(key) or "").strip()
    if not value:
        return None
    return float(value)


def _parse_flag(row: Mapping[str, str], key: str) -> bool:
    value = (row.get(key) or "").strip()
    if value in {"0", "0.0", "false", "False"}:
        return False
    if value in {"1", "1.0", "true", "True"}:
        return True
    raise ValueError(f"invalid {key} flag: {value!r}")


def parse_row(row: Mapping[str, str]) -> dict:
    """Convert one CSV row to typed values used by the report."""
    parsed = {
        key: (row.get(key) or "").strip()
        for key in _TEXT_FIELDS
    }
    parsed.update({key: _parse_float(row, key) for key in _NUMERIC_FIELDS})
    parsed.update({key: _parse_flag(row, key) for key in _FLAG_FIELDS})
    if parsed["ts"] is None or parsed["qty"] is None:
        raise ValueError("trade row is missing ts or qty")
    return parsed


def load_rows(path: str, hours: float = 0.0, symbol: str | None = None,
              hedge: str | None = None) -> Tuple[List[dict], int]:
    """Load typed trade rows and return ``(rows, invalid_row_count)``."""
    cutoff = time.time() - hours * 3600.0 if hours > 0 else 0.0
    rows: List[dict] = []
    skipped = 0
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        fields = set(reader.fieldnames or [])
        missing = sorted(_REQUIRED_FIELDS - fields)
        if missing:
            raise ValueError("missing required columns: " + ", ".join(missing))
        for raw in reader:
            try:
                row = parse_row(raw)
            except (TypeError, ValueError):
                skipped += 1
                continue
            if row["ts"] < cutoff:
                continue
            if symbol and row["symbol"] != symbol:
                continue
            if hedge and row["hedge"] != hedge:
                continue
            rows.append(row)
    rows.sort(key=lambda row: row["ts"])
    return rows, skipped


def percentile(values: Iterable[float], q: float) -> float | None:
    """Return a linearly interpolated percentile, or ``None`` when empty."""
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * q / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def _mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return sum(values) / len(values) if values else None


def _sum_field(rows: Iterable[dict], field: str) -> float:
    return sum(float(row[field]) for row in rows if row.get(field) is not None)


def _fill_class(row: Mapping[str, object]) -> str:
    qty = float(row.get("qty") or 0.0)
    matched = float(row.get("matched_qty") or 0.0)
    buy_fill = float(row.get("buy_fill") or 0.0)
    sell_fill = float(row.get("sell_fill") or 0.0)
    if qty > 0 and matched >= qty - EPSILON:
        return "full"
    if matched > EPSILON:
        return "partial"
    if buy_fill > EPSILON or sell_fill > EPSILON:
        return "unmatched"
    return "no_fill"


def _buy_slippage_bps(row: Mapping[str, object]) -> float | None:
    bbo = row.get("buy_bbo_px")
    avg = row.get("buy_avg_px")
    if bbo is None or avg is None or float(bbo) <= 0:
        return None
    return (float(avg) - float(bbo)) / float(bbo) * 10_000.0


def _sell_slippage_bps(row: Mapping[str, object]) -> float | None:
    bbo = row.get("sell_bbo_px")
    avg = row.get("sell_avg_px")
    if bbo is None or avg is None or float(bbo) <= 0:
        return None
    return (float(bbo) - float(avg)) / float(bbo) * 10_000.0


def _realized_edge_bps(row: Mapping[str, object]) -> float | None:
    """Normalize matched primary edge by the actual buy-leg notional."""
    edge = row.get("fill_edge_usd")
    matched = row.get("matched_qty")
    buy_avg = row.get("buy_avg_px")
    if edge is None or matched is None or buy_avg is None:
        return None
    denominator = float(matched) * float(buy_avg)
    if denominator <= 0:
        return None
    return float(edge) / denominator * 10_000.0


def _weighted_slippage_bps(rows: Iterable[Mapping[str, object]], side: str) -> float | None:
    """Calculate adverse BBO slippage weighted by filled BBO notional."""
    adverse_dollars = 0.0
    bbo_notional = 0.0
    fill_field = "buy_fill" if side == "buy" else "sell_fill"
    bbo_field = "buy_bbo_px" if side == "buy" else "sell_bbo_px"
    avg_field = "buy_avg_px" if side == "buy" else "sell_avg_px"
    for row in rows:
        fill = row.get(fill_field)
        bbo = row.get(bbo_field)
        avg = row.get(avg_field)
        if fill is None or bbo is None or avg is None:
            continue
        fill = float(fill)
        bbo = float(bbo)
        avg = float(avg)
        if fill <= 0 or bbo <= 0:
            continue
        adverse = avg - bbo if side == "buy" else bbo - avg
        adverse_dollars += adverse * fill
        bbo_notional += bbo * fill
    if bbo_notional <= 0:
        return None
    return adverse_dollars / bbo_notional * 10_000.0


def summarize_trades(rows: List[dict]) -> dict:
    """Calculate aggregate execution, edge, slippage, and hedge metrics."""
    fill_counts = Counter(_fill_class(row) for row in rows)
    requested_qty = _sum_field(rows, "qty")
    matched_qty = _sum_field(rows, "matched_qty")
    exp_edge_total = _sum_field(rows, "exp_edge_usd")
    fill_edge_total = _sum_field(rows, "fill_edge_usd")
    hedge_status_counts = Counter(row.get("hedge_status", "") for row in rows)
    hedge_attempts = sum(
        1 for row in rows
        if row.get("hedge_status") not in {"", "not_needed"}
    )
    hedge_completed = sum(
        1 for row in rows
        if row.get("hedge_status") not in {"", "not_needed", "not_attempted",
                                            "unhedgeable"}
        and abs(float(row.get("remaining_net_qty") or 0.0)) <= EPSILON
    )
    buy_slippage = [value for row in rows
                    if (value := _buy_slippage_bps(row)) is not None]
    sell_slippage = [value for row in rows
                     if (value := _sell_slippage_bps(row)) is not None]
    realized_edge = [value for row in rows
                     if (value := _realized_edge_bps(row)) is not None]
    execution_ms = [float(row["execution_ms"]) for row in rows
                    if row.get("execution_ms") is not None]
    leg_settle_gap_ms = [float(row["leg_settle_gap_ms"]) for row in rows
                         if row.get("leg_settle_gap_ms") is not None]
    buy_settle_ms = [float(row["buy_settle_ms"]) for row in rows
                     if row.get("buy_settle_ms") is not None]
    sell_settle_ms = [float(row["sell_settle_ms"]) for row in rows
                      if row.get("sell_settle_ms") is not None]
    entropy_server_age_ms = [
        float(row["entropy_book_server_age_ms"]) for row in rows
        if row.get("entropy_book_server_age_ms") is not None]
    entropy_update_gap_ms = [float(row["entropy_update_gap_ms"]) for row in rows
                             if row.get("entropy_update_gap_ms") is not None]
    hedge_update_gap_ms = [float(row["hedge_update_gap_ms"]) for row in rows
                           if row.get("hedge_update_gap_ms") is not None]
    hedge_duration_ms = [float(row["hedge_duration_ms"]) for row in rows
                         if row.get("hedge_duration_ms") is not None
                         and row.get("hedge_status") not in {"", "not_needed"}]

    count = len(rows)
    return {
        "count": count,
        "ok_count": sum(1 for row in rows if row.get("ok")),
        "unresolved_count": sum(1 for row in rows if row.get("unresolved")),
        "error_count": sum(1 for row in rows if row.get("error")),
        "fill_counts": {
            "full": fill_counts["full"],
            "partial": fill_counts["partial"],
            "unmatched": fill_counts["unmatched"],
            "no_fill": fill_counts["no_fill"],
        },
        "requested_qty": requested_qty,
        "matched_qty": matched_qty,
        "matched_ratio": matched_qty / requested_qty if requested_qty else 0.0,
        "exp_edge_total": exp_edge_total,
        "fill_edge_total": fill_edge_total,
        "edge_capture_ratio": (fill_edge_total / exp_edge_total
                                if exp_edge_total else None),
        "realized_edge_bps_mean": _mean(realized_edge),
        "realized_edge_bps_median": percentile(realized_edge, 50),
        "realized_edge_bps_p90": percentile(realized_edge, 90),
        "buy_slippage_bps_mean": _mean(buy_slippage),
        "sell_slippage_bps_mean": _mean(sell_slippage),
        "buy_slippage_bps_weighted": _weighted_slippage_bps(rows, "buy"),
        "sell_slippage_bps_weighted": _weighted_slippage_bps(rows, "sell"),
        "execution_ms_p50": percentile(execution_ms, 50),
        "execution_ms_p90": percentile(execution_ms, 90),
        "leg_settle_gap_ms_mean": _mean(leg_settle_gap_ms),
        "leg_settle_gap_ms_p50": percentile(leg_settle_gap_ms, 50),
        "leg_settle_gap_ms_p90": percentile(leg_settle_gap_ms, 90),
        "buy_settle_ms_p50": percentile(buy_settle_ms, 50),
        "buy_settle_ms_p90": percentile(buy_settle_ms, 90),
        "sell_settle_ms_p50": percentile(sell_settle_ms, 50),
        "sell_settle_ms_p90": percentile(sell_settle_ms, 90),
        "entropy_book_server_age_ms_p50": percentile(entropy_server_age_ms, 50),
        "entropy_book_server_age_ms_p90": percentile(entropy_server_age_ms, 90),
        "entropy_update_gap_ms_p50": percentile(entropy_update_gap_ms, 50),
        "entropy_update_gap_ms_p90": percentile(entropy_update_gap_ms, 90),
        "hedge_update_gap_ms_p50": percentile(hedge_update_gap_ms, 50),
        "hedge_update_gap_ms_p90": percentile(hedge_update_gap_ms, 90),
        "first_settled_leg_counts": dict(Counter(
            row["first_settled_leg"] for row in rows
            if row.get("first_settled_leg"))),
        "hedge_status_counts": dict(sorted(hedge_status_counts.items())),
        "hedge_attempts": hedge_attempts,
        "hedge_completed": hedge_completed,
        "hedge_residual_count": sum(
            1 for row in rows
            if abs(float(row.get("remaining_net_qty") or 0.0)) > EPSILON
        ),
        "hedge_duration_ms_p50": percentile(hedge_duration_ms, 50),
        "hedge_duration_ms_p90": percentile(hedge_duration_ms, 90),
    }


def _signal_bucket(row: Mapping[str, object]) -> str:
    distance = abs(float(row["marginal_premium_bps"])
                   - float(row["midline_bps"]))
    for lower, upper, label in _SIGNAL_BUCKETS:
        if lower <= distance < upper:
            return label
    return "10+"


def group_signal_rows(rows: List[dict]) -> Dict[Tuple[str, str], List[dict]]:
    """Group executions by direction and distance from the configured midline."""
    groups: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    for row in rows:
        groups[(str(row["direction"]), _signal_bucket(row))].append(row)
    return dict(groups)


def _fmt(value: float | None, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}{suffix}"


def _print_report(path: str, rows: List[dict], skipped: int) -> None:
    summary = summarize_trades(rows)
    span_hours = ((rows[-1]["ts"] - rows[0]["ts"]) / 3600.0
                  if len(rows) > 1 else 0.0)
    start = datetime.fromtimestamp(rows[0]["ts"], tz=timezone.utc)
    end = datetime.fromtimestamp(rows[-1]["ts"], tz=timezone.utc)
    fills = summary["fill_counts"]

    print(f"\n=== {path}: {len(rows)} trades over {span_hours:.1f}h ===")
    print(f"period / 時間: {start:%Y-%m-%d %H:%MZ} -> {end:%Y-%m-%d %H:%MZ}")

    print("\ntrade summary / 交易摘要")
    print(f"  rows {summary['count']}   ok {summary['ok_count']}   "
          f"unresolved {summary['unresolved_count']}   "
          f"errors {summary['error_count']}")
    print(f"  fills full {fills['full']}   partial {fills['partial']}   "
          f"unmatched {fills['unmatched']}   no-fill {fills['no_fill']}")
    print(f"  requested qty {_fmt(summary['requested_qty'], 6)}   "
          f"matched qty {_fmt(summary['matched_qty'], 6)}   "
          f"matched ratio {_fmt(summary['matched_ratio'] * 100, 1, '%')}")

    print("\nexecution quality / 成交品質")
    print(f"  expected edge ${_fmt(summary['exp_edge_total'], 4)}   "
          f"fill edge ${_fmt(summary['fill_edge_total'], 4)}   "
          f"capture {_fmt(summary['edge_capture_ratio'] * 100 if summary['edge_capture_ratio'] is not None else None, 1, '%')}")
    print(f"  realized edge bps mean/median/p90 "
          f"{_fmt(summary['realized_edge_bps_mean'], 3)}/"
          f"{_fmt(summary['realized_edge_bps_median'], 3)}/"
          f"{_fmt(summary['realized_edge_bps_p90'], 3)}")
    print(f"  slippage mean buy/sell "
          f"{_fmt(summary['buy_slippage_bps_mean'], 3)}/"
          f"{_fmt(summary['sell_slippage_bps_mean'], 3)} bps")
    print(f"  slippage weighted buy/sell "
          f"{_fmt(summary['buy_slippage_bps_weighted'], 3)}/"
          f"{_fmt(summary['sell_slippage_bps_weighted'], 3)} bps")
    print(f"  execution p50/p90 {_fmt(summary['execution_ms_p50'], 1)}/"
          f"{_fmt(summary['execution_ms_p90'], 1)} ms")
    print(f"  leg settle gap mean/p50/p90 "
          f"{_fmt(summary['leg_settle_gap_ms_mean'], 1)}/"
          f"{_fmt(summary['leg_settle_gap_ms_p50'], 1)}/"
          f"{_fmt(summary['leg_settle_gap_ms_p90'], 1)} ms")
    print(f"  leg settle p50/p90 buy/sell "
          f"{_fmt(summary['buy_settle_ms_p50'], 1)}/"
          f"{_fmt(summary['buy_settle_ms_p90'], 1)} / "
          f"{_fmt(summary['sell_settle_ms_p50'], 1)}/"
          f"{_fmt(summary['sell_settle_ms_p90'], 1)} ms")
    print(f"  entropy server age p50/p90 "
          f"{_fmt(summary['entropy_book_server_age_ms_p50'], 1)}/"
          f"{_fmt(summary['entropy_book_server_age_ms_p90'], 1)} ms")
    print(f"  feed update gap p50/p90 entropy/hedge "
          f"{_fmt(summary['entropy_update_gap_ms_p50'], 1)}/"
          f"{_fmt(summary['entropy_update_gap_ms_p90'], 1)} / "
          f"{_fmt(summary['hedge_update_gap_ms_p50'], 1)}/"
          f"{_fmt(summary['hedge_update_gap_ms_p90'], 1)} ms")

    print("\nhedge / 對沖")
    statuses = ", ".join(
        f"{key or 'empty'}={value}"
        for key, value in summary["hedge_status_counts"].items()
    ) or "none"
    print(f"  attempts {summary['hedge_attempts']}   "
          f"completed {summary['hedge_completed']}   "
          f"residual {summary['hedge_residual_count']}")
    print(f"  status {statuses}")
    print(f"  duration p50/p90 {_fmt(summary['hedge_duration_ms_p50'], 1)}/"
          f"{_fmt(summary['hedge_duration_ms_p90'], 1)} ms")

    print("\nsignal buckets / 信號區間")
    print("  direction       distance  trades  full%  fill-edge  residual%")
    bucket_order = {label: index for index, (_, _, label)
                    in enumerate(_SIGNAL_BUCKETS)}
    bucket_order["10+"] = len(_SIGNAL_BUCKETS)
    groups = group_signal_rows(rows)
    for (direction, bucket), grouped in sorted(
            groups.items(), key=lambda item: (item[0][0], bucket_order[item[0][1]])):
        grouped_summary = summarize_trades(grouped)
        grouped_fills = grouped_summary["fill_counts"]
        full_rate = grouped_fills["full"] / len(grouped) * 100.0
        residual_rate = grouped_summary["hedge_residual_count"] / len(grouped) * 100.0
        print(f"  {direction:<15} {bucket:>8} {len(grouped):>7} "
              f"{full_rate:>6.1f} {grouped_summary['fill_edge_total']:>10.4f} "
              f"{residual_rate:>9.1f}")

    if skipped:
        print(f"\nskipped invalid rows: {skipped}")
    print("\nNote: fill_edge_usd is the matched primary two-leg edge; it is not a "
          "complete PnL calculation including hedge execution.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="analyze execution and hedge quality from trades CSV")
    parser.add_argument("--csv", default=DEFAULT_CSV)
    parser.add_argument("--hours", type=float, default=0.0,
                        help="only use the last N hours (0 = all data)")
    parser.add_argument("--symbol", help="only include this canonical symbol")
    parser.add_argument("--hedge", help="only include this hedge venue")
    args = parser.parse_args()

    csv_path = _resolve_csv_path(args.csv)
    if (args.csv == DEFAULT_CSV and not os.path.exists(DEFAULT_CSV)
            and len(_pair_csv_matches()) > 1):
        print("multiple pair-specific trade files found; choose one with "
              "--csv / 找到多个市场成交 CSV，请用 --csv 指定一个文件:",
              file=sys.stderr)
        for match in _pair_csv_matches():
            print(f"  {match}", file=sys.stderr)
        raise SystemExit(2)

    try:
        rows, skipped = load_rows(csv_path, args.hours, args.symbol, args.hedge)
    except FileNotFoundError:
        print(f"{csv_path} not found — run the bot in live mode to collect "
              "trade data first / 未找到成交資料，請先執行實盤收集資料",
              file=sys.stderr)
        raise SystemExit(1)
    except ValueError as exc:
        print(f"invalid trade CSV: {exc} / 成交 CSV 格式無效",
              file=sys.stderr)
        raise SystemExit(1)

    if not rows:
        print(f"no usable trades in {csv_path} / 找不到可分析的成交資料",
              file=sys.stderr)
        if skipped:
            print(f"skipped invalid rows: {skipped}", file=sys.stderr)
        raise SystemExit(1)
    _print_report(csv_path, rows, skipped)


if __name__ == "__main__":
    main()
