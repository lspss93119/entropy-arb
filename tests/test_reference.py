import csv
import logging
import os

import pytest

from entropy_arb.reference import (
    ENTROPY_REFERENCE_HEADER,
    LIGHTER_REFERENCE_HEADER,
    ReferenceCsvWriter,
    reference_paths,
)


def test_reference_headers_are_exact():
    assert ENTROPY_REFERENCE_HEADER == (
        "recv_ms", "oracle_px", "mark_px",
    )
    assert LIGHTER_REFERENCE_HEADER == (
        "recv_ms", "server_ms", "index_px", "mark_px",
    )


@pytest.mark.parametrize(
    ("symbol", "hedge_key", "expected"),
    [
        (
            "SNDK",
            "lighter",
            (
                "logs/reference-SNDK-lighter-entropy.csv",
                "logs/reference-SNDK-lighter.csv",
            ),
        ),
        (
            "SNDK",
            "lighter-rh",
            (
                "logs/reference-SNDK-lighter-rh-entropy.csv",
                "logs/reference-SNDK-lighter-rh.csv",
            ),
        ),
        (
            "ETH",
            "future-lighter-profile",
            (
                "logs/reference-ETH-future-lighter-profile-entropy.csv",
                "logs/reference-ETH-future-lighter-profile.csv",
            ),
        ),
    ],
)
def test_reference_paths_are_namespaced(symbol, hedge_key, expected):
    assert reference_paths(symbol, hedge_key) == expected


def read_rows(path):
    with open(path, newline="") as fh:
        return list(csv.reader(fh))


def test_writer_persists_identical_rows_independently(tmp_path):
    path = tmp_path / "reference.csv"
    writer = ReferenceCsvWriter(str(path), ENTROPY_REFERENCE_HEADER)
    row = (1_787_993_704_054, 1485.0, 1485.0)
    writer.write(row)
    writer.write(row)
    writer.close()
    assert read_rows(path) == [
        list(ENTROPY_REFERENCE_HEADER),
        [str(value) for value in row],
        [str(value) for value in row],
    ]


def test_writer_flushes_row_ten_and_close_flushes_remainder(tmp_path):
    path = tmp_path / "reference.csv"
    writer = ReferenceCsvWriter(str(path), ENTROPY_REFERENCE_HEADER)
    for recv_ms in range(1, 10):
        writer.write((recv_ms, 100.0, 101.0))
    assert read_rows(path) == [list(ENTROPY_REFERENCE_HEADER)]

    writer.write((10, 100.0, 101.0))
    assert len(read_rows(path)) == 11

    writer.write((11, 100.0, 101.0))
    writer.write((12, 100.0, 101.0))
    assert len(read_rows(path)) == 11
    writer.close()
    assert len(read_rows(path)) == 13


def test_writer_restart_appends_one_header(tmp_path):
    path = tmp_path / "reference.csv"
    for recv_ms in (1, 2):
        writer = ReferenceCsvWriter(str(path), ENTROPY_REFERENCE_HEADER)
        writer.write((recv_ms, 100.0, 101.0))
        writer.close()
    rows = read_rows(path)
    assert rows[0] == list(ENTROPY_REFERENCE_HEADER)
    assert rows.count(list(ENTROPY_REFERENCE_HEADER)) == 1
    assert len(rows) == 3


def test_stop_accepting_rejects_late_rows_but_close_flushes_prior_rows(tmp_path):
    path = tmp_path / "reference.csv"
    writer = ReferenceCsvWriter(str(path), ENTROPY_REFERENCE_HEADER)
    writer.write((1, 100.0, 101.0))
    writer.stop_accepting()
    writer.write((2, 102.0, 103.0))
    writer.close()
    assert read_rows(path) == [
        list(ENTROPY_REFERENCE_HEADER),
        ["1", "100.0", "101.0"],
    ]


def test_bad_header_disables_without_touching_file(tmp_path, caplog):
    path = tmp_path / "reference.csv"
    original = b"wrong,header\n1,2\n"
    path.write_bytes(original)
    with caplog.at_level(logging.ERROR, logger="reference"):
        writer = ReferenceCsvWriter(str(path), ENTROPY_REFERENCE_HEADER)
    assert writer.enabled is False
    writer.write((3, 100.0, 101.0))
    writer.close()
    assert path.read_bytes() == original
    assert not (tmp_path / "reference.csv.old").exists()
    assert sum(
        "header validation" in record.message for record in caplog.records
    ) == 1


def test_write_error_logs_once_and_disables(tmp_path, caplog):
    class BrokenCsvWriter:
        def writerow(self, row):
            raise OSError("disk full")

    path = tmp_path / "reference.csv"
    writer = ReferenceCsvWriter(str(path), ENTROPY_REFERENCE_HEADER)
    writer._writer = BrokenCsvWriter()
    with caplog.at_level(logging.ERROR, logger="reference"):
        writer.write((1, 100.0, 101.0))
        writer.write((2, 100.0, 101.0))
        writer.close()
    assert writer.enabled is False
    assert sum("disk full" in record.message for record in caplog.records) == 1


def test_open_error_is_contained_and_logged_once(tmp_path, monkeypatch, caplog):
    target = tmp_path / "blocked" / "reference.csv"

    def fail_makedirs(path, exist_ok):
        raise OSError("read-only directory")

    monkeypatch.setattr(os, "makedirs", fail_makedirs)
    with caplog.at_level(logging.ERROR, logger="reference"):
        writer = ReferenceCsvWriter(str(target), ENTROPY_REFERENCE_HEADER)
        writer.write((1, 100.0, 101.0))
        writer.close()
    assert writer.enabled is False
    assert sum(
        "read-only directory" in record.message for record in caplog.records
    ) == 1


def test_close_is_idempotent(tmp_path):
    path = tmp_path / "reference.csv"
    writer = ReferenceCsvWriter(str(path), ENTROPY_REFERENCE_HEADER)
    writer.write((1, 100.0, 101.0))
    writer.close()
    before = path.read_bytes()
    writer.close()
    writer.write((2, 102.0, 103.0))
    assert path.read_bytes() == before
