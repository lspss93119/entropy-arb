from __future__ import annotations

import csv
import logging
import os

log = logging.getLogger("reference")

ENTROPY_REFERENCE_HEADER: tuple[str, ...] = ("recv_ms", "oracle_px", "mark_px")
LIGHTER_REFERENCE_HEADER: tuple[str, ...] = (
    "recv_ms", "server_ms", "index_px", "mark_px"
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
