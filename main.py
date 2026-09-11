#!/usr/bin/env python3
"""entropy-arb entry point.

    # collect minute data only — no strategy, no credentials needed
    python3 main.py --record-only --symbol SNDK --hedge lighter-rh
    python3 main.py --record-only --no-dashboard \
        --symbol SNDK --symbol BTC --hedge lighter --hedge lighter-rh

    # LIVE trading: real orders, real money (needs .env credentials)
    python3 main.py --symbol SNDK --hedge lighter-rh

--symbol and --hedge are required on every start: the markets you trade are
an explicit decision, not a config default. Multiple values are supported in
--record-only mode and produce one independent recorder per pair. Add --cn
for a Chinese-language dashboard. There is no paper mode. Collect data with
--record-only, set your thresholds with tools/analyze.py, then go live with
small position caps.

On a terminal the bot shows a live Rich dashboard (books, signal, positions,
PnL, last executions) and writes log lines to logging.file; use
--no-dashboard for plain console logs (nohup/systemd). Strategy lives in
config.yaml, credentials in .env — see the README (English) /
README.zh-CN.md (中文).
"""
import argparse
import asyncio
import contextlib
import logging
import os
import re
import signal
import sys

from entropy_arb.config import HEDGE_VENUES, ConfigError, load_config
from entropy_arb.engine import Engine


def _split_cli_values(values):
    """Expand repeatable and comma-separated market CLI values."""
    out = []
    for value in values:
        for item in value.split(","):
            item = item.strip()
            if item and item not in out:
                out.append(item)
    return out


def _record_pairs(symbol_values, hedge_values):
    """Return the unique symbol x hedge matrix in CLI order."""
    symbols = _split_cli_values(symbol_values)
    hedges = _split_cli_values(hedge_values)
    return [(symbol, hedge) for symbol in symbols for hedge in hedges]


def _market_output_path(path: str, symbol: str, hedge: str) -> str:
    """Add a safe market suffix before the configured file extension."""
    directory, filename = os.path.split(path)
    stem, ext = os.path.splitext(filename)
    safe_symbol = re.sub(r"[^A-Za-z0-9_.-]+", "_", symbol.strip())
    safe_hedge = re.sub(r"[^A-Za-z0-9_.-]+", "_", hedge.strip())
    return os.path.join(directory, f"{stem}-{safe_symbol}-{safe_hedge}{ext}")


def _namespace_recording_outputs(cfg):
    """Give one market pair its own runtime output files."""
    cfg.recorder_csv = _market_output_path(cfg.recorder_csv,
                                           cfg.symbol, cfg.hedge_venue)
    cfg.trades_csv = _market_output_path(cfg.trades_csv,
                                         cfg.symbol, cfg.hedge_venue)
    cfg.log_file = _market_output_path(cfg.log_file,
                                       cfg.symbol, cfg.hedge_venue)
    return cfg


def _namespace_config_outputs(configs):
    """Namespace every configured pair without changing its parent folders."""
    for cfg in configs:
        _namespace_recording_outputs(cfg)
    return configs


def setup_logging(level: str, log_file: str = None,
                  extra_handler: logging.Handler = None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S")
    if log_file:
        d = os.path.dirname(log_file)
        if d:
            os.makedirs(d, exist_ok=True)
        h = logging.FileHandler(log_file)
    else:
        h = logging.StreamHandler()
    h.setFormatter(fmt)
    root.addHandler(h)
    if extra_handler is not None:
        root.addHandler(extra_handler)
    logging.getLogger("websockets").setLevel(logging.WARNING)


async def amain(cfg, record_only: bool, use_dashboard: bool, force_tty: bool,
                log_buffer, lang: str) -> None:
    eng = Engine(cfg, record_only=record_only)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, eng.request_stop)
    if not use_dashboard:
        await eng.run()
        return
    from entropy_arb.dashboard import Dashboard
    dash = Dashboard(eng, log_buffer, cfg.log_file, force_terminal=force_tty,
                     lang=lang)
    dash_task = asyncio.create_task(dash.run(), name="dashboard")
    try:
        await eng.run()
    finally:
        eng.request_stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(dash_task, timeout=5)
        if not dash_task.done():
            dash_task.cancel()


async def amain_record_only(configs) -> None:
    """Run one independent, credential-free recorder per market pair."""
    engines = [Engine(cfg, record_only=True) for cfg in configs]
    loop = asyncio.get_running_loop()

    def stop_all() -> None:
        for eng in engines:
            eng.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_all)
    await asyncio.gather(*(eng.run() for eng in engines))


def main() -> None:
    p = argparse.ArgumentParser(
        description="Two-venue LIVE arbitrage: Entropy vs Lighter mainnet / "
                    "Lighter Robinhood / trade.xyz. Without --record-only, "
                    "real orders are sent.")
    p.add_argument("--symbol", required=True, action="append",
                   help="symbol; repeat or comma-separate in --record-only / "
                        "品种；仅采集模式可重复或用逗号分隔")
    p.add_argument("--hedge", required=True, action="append",
                   metavar="VENUE",
                   help=f"hedge venue ({', '.join(HEDGE_VENUES)}); repeat or "
                        "comma-separate in --record-only / 对冲腿")
    p.add_argument("--config", default="config.yaml",
                   help="strategy config (default: config.yaml)")
    p.add_argument("--env-file", default=".env",
                   help="credentials file (default: .env)")
    p.add_argument("--record-only", action="store_true",
                   help="only collect minute data, run no strategy, send no "
                        "orders (needs no credentials)")
    p.add_argument("--cn", action="store_true",
                   help="display the dashboard in Chinese / 仪表盘使用中文")
    disp = p.add_mutually_exclusive_group()
    disp.add_argument("--dashboard", action="store_true",
                      help="force the Rich dashboard even without a tty")
    disp.add_argument("--no-dashboard", action="store_true",
                      help="plain console logs instead of the dashboard")
    args = p.parse_args()

    symbols = _split_cli_values(args.symbol)
    hedges = _split_cli_values(args.hedge)
    if not symbols:
        print("config error: --symbol is required", file=sys.stderr)
        sys.exit(2)
    if not hedges:
        print("config error: --hedge is required", file=sys.stderr)
        sys.exit(2)
    invalid = [hedge for hedge in hedges if hedge not in HEDGE_VENUES]
    if invalid:
        print(f"config error: --hedge must be one of {list(HEDGE_VENUES)}, "
              f"got {invalid[0]!r}", file=sys.stderr)
        sys.exit(2)
    pairs = _record_pairs(symbols, hedges)
    if len(pairs) > 1 and not args.record_only:
        print("config error: multiple --symbol/--hedge values are supported "
              "only with --record-only", file=sys.stderr)
        sys.exit(2)

    try:
        configs = [load_config(args.config, args.env_file,
                               symbol=symbol, hedge_venue=hedge)
                   for symbol, hedge in pairs]
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)

    _namespace_config_outputs(configs)

    if len(configs) > 1:
        setup_logging(configs[0].log_level)
        try:
            asyncio.run(amain_record_only(configs))
        except RuntimeError as e:
            print(f"startup error: {e}", file=sys.stderr)
            sys.exit(1)
        return

    cfg = configs[0]

    use_dashboard = (cfg.dashboard or args.dashboard) and not args.no_dashboard
    force_tty = args.dashboard
    if use_dashboard and not (sys.stdout.isatty() or force_tty):
        use_dashboard = False

    log_buffer = None
    if use_dashboard:
        try:
            from entropy_arb.dashboard import BufferLogHandler
        except ImportError:
            print("`rich` is not installed — falling back to plain logs "
                  "(pip install -r requirements.txt)", file=sys.stderr)
            use_dashboard = False
    if use_dashboard:
        log_buffer = BufferLogHandler()
        setup_logging(cfg.log_level, log_file=cfg.log_file,
                      extra_handler=log_buffer)
    else:
        setup_logging(cfg.log_level)

    try:
        asyncio.run(amain(cfg, record_only=args.record_only,
                          use_dashboard=use_dashboard, force_tty=force_tty,
                          log_buffer=log_buffer,
                          lang="zh" if args.cn else "en"))
    except RuntimeError as e:
        # startup failures (missing credentials, market not found, venue
        # unreachable) — a clean message, not a traceback
        print(f"startup error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
