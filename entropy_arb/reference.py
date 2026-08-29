from __future__ import annotations

import csv
import logging
import math
import os
from typing import Any

log = logging.getLogger("reference")

ENTROPY_REFERENCE_HEADER: tuple[str, ...] = ("recv_ms", "oracle_px", "mark_px")
LIGHTER_REFERENCE_HEADER: tuple[str, ...] = (
    "recv_ms", "server_ms", "index_px", "mark_px"
)


class ReferenceParseError(ValueError):
    """A relevant reference frame cannot produce a complete valid row."""


def _positive_float(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ReferenceParseError(f"{field} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReferenceParseError(f"{field} must be numeric") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ReferenceParseError(f"{field} must be positive and finite")
    return parsed


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ReferenceParseError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReferenceParseError(f"{field} must be an integer") from exc
    if isinstance(value, float) and not value.is_integer():
        raise ReferenceParseError(f"{field} must be an integer")
    if parsed <= 0:
        raise ReferenceParseError(f"{field} must be positive")
    return parsed


def _market_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return parsed


def _channel_market_id(channel: object) -> int | None:
    if not isinstance(channel, str):
        return None
    for separator in (":", "/"):
        prefix = f"market_stats{separator}"
        if channel.startswith(prefix):
            return _market_id(channel.removeprefix(prefix))
    return None


def parse_hl_reference(
    msg: dict,
    *,
    coin: str,
) -> tuple[float, float] | None:
    """Return a Hyperliquid oracle/mark pair for the configured coin."""
    if msg.get("channel") != "activeAssetCtx":
        return None
    data = msg.get("data")
    if not isinstance(data, dict):
        return None
    if data.get("coin") != coin:
        return None
    ctx = data.get("ctx")
    if not isinstance(ctx, dict):
        raise ReferenceParseError("activeAssetCtx ctx must be an object")
    try:
        oracle_px = _positive_float(ctx["oraclePx"], "oraclePx")
        mark_px = _positive_float(ctx["markPx"], "markPx")
    except KeyError as exc:
        raise ReferenceParseError(f"missing required field {exc.args[0]}") from exc
    return oracle_px, mark_px


def parse_lighter_reference(
    msg: dict,
    *,
    market_id: int,
) -> tuple[int, float, float] | None:
    """Return a Lighter server timestamp, index, and mark for one market."""
    if msg.get("type") not in {
        "subscribed/market_stats",
        "update/market_stats",
    }:
        return None

    channel_market_id = _channel_market_id(msg.get("channel"))
    if channel_market_id is not None and channel_market_id != market_id:
        return None

    stats = msg.get("market_stats")
    if not isinstance(stats, dict):
        if channel_market_id == market_id:
            raise ReferenceParseError("market_stats must be an object")
        return None

    payload_market_id = _market_id(stats.get("market_id"))
    if payload_market_id is None:
        if channel_market_id == market_id:
            raise ReferenceParseError("market_id must be an integer")
        return None
    if payload_market_id != market_id:
        return None
    if (
        channel_market_id is not None
        and channel_market_id != payload_market_id
    ):
        return None

    return (
        _positive_int(msg.get("timestamp"), "timestamp"),
        _positive_float(stats.get("index_price"), "index_price"),
        _positive_float(stats.get("mark_price"), "mark_price"),
    )


def reference_paths(
    symbol: str,
    hedge_key: str,
    directory: str = "logs",
) -> tuple[str, str]:
    entropy_name = f"reference-{symbol}-{hedge_key}-entropy.csv"
    hedge_name = f"reference-{symbol}-{hedge_key}.csv"
    return (
        os.path.join(directory, entropy_name),
        os.path.join(directory, hedge_name),
    )


class ReferenceCsvWriter:
    def __init__(
        self,
        path: str,
        header: tuple[str, ...],
        flush_rows: int = 10,
    ) -> None:
        self.path = path
        self.header = header
        self.flush_rows = flush_rows
        self._enabled = False
        self._accepting = False
        self._closed = False
        self._pending = 0
        self._error_logged = False
        self._fh = None
        self._writer = None
        self._open()

    @property
    def enabled(self) -> bool:
        return self._enabled and self._accepting and not self._closed

    def _open(self) -> None:
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            write_header = (
                not os.path.exists(self.path)
                or os.path.getsize(self.path) == 0
            )
            if not write_header:
                with open(self.path, newline="") as existing:
                    actual = next(csv.reader(existing), None)
                if tuple(actual or ()) != self.header:
                    self._disable(
                        "header validation",
                        ValueError(
                            f"expected {self.header!r}, got {actual!r}"
                        ),
                    )
                    return
            self._fh = open(self.path, "a", newline="")
            self._writer = csv.writer(self._fh)
            if write_header:
                self._writer.writerow(self.header)
                self._fh.flush()
            self._enabled = True
            self._accepting = True
        except (OSError, csv.Error, UnicodeError) as exc:
            self._disable("open", exc)

    def _disable(self, operation: str, error: BaseException | str) -> None:
        if not self._error_logged:
            log.error(
                "reference writer disabled path=%s operation=%s error=%s",
                self.path,
                operation,
                error,
            )
            self._error_logged = True
        self._enabled = False
        self._accepting = False
        handle = self._fh
        self._fh = None
        self._writer = None
        self._pending = 0
        if handle is not None:
            try:
                handle.close()
            except OSError:
                return

    def write(self, row: tuple[object, ...]) -> None:
        if not self.enabled:
            return
        try:
            self._writer.writerow(row)
            self._pending += 1
            if self._pending >= self.flush_rows:
                self._fh.flush()
                self._pending = 0
        except (OSError, csv.Error) as exc:
            self._disable("write", exc)

    def stop_accepting(self) -> None:
        self._accepting = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop_accepting()
        handle = self._fh
        self._enabled = False
        self._fh = None
        self._writer = None
        try:
            if handle is not None and self._pending:
                handle.flush()
        except (OSError, csv.Error) as exc:
            self._disable("close flush", exc)
        finally:
            self._pending = 0
            if handle is not None:
                try:
                    handle.close()
                except OSError as exc:
                    self._disable("close", exc)
