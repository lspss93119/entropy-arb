"""Analyzer path selection and data coverage reporting."""
import csv
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tools.analyze as analyze  # noqa: E402
from entropy_arb.recorder import HEADER as MINUTE_HEADER  # noqa: E402


def test_analyzer_resolves_the_only_namespaced_record_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    record_dir = tmp_path / "logs" / "record"
    record_dir.mkdir(parents=True)
    expected = record_dir / "minutes-SNDK-lighter-rh.csv"
    expected.write_text("header\n")

    default_csv = getattr(analyze, "DEFAULT_CSV", None)
    resolver = getattr(analyze, "_resolve_csv_path", None)
    assert default_csv == "logs/record/minutes.csv"
    assert callable(resolver)
    assert resolver(default_csv) == os.path.join(
        "logs", "record", "minutes-SNDK-lighter-rh.csv")


def test_coverage_summary_reports_missing_minutes():
    t0 = 1_700_000_000.0
    stats = analyze.coverage_summary([
        {"ts": t0},
        {"ts": t0 + 120},
        {"ts": t0 + 180},
    ])

    assert stats["observed_minutes"] == 3
    assert stats["expected_minutes"] == 4
    assert stats["coverage_pct"] == 75.0
    assert stats["gaps"] == [(t0 + 60, t0 + 60)]


def _write_minute_csv(path, missing_index=None):
    t0 = 1_700_000_000
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(MINUTE_HEADER)
        for i in range(31):
            minute = i + (1 if missing_index is not None and i >= missing_index else 0)
            ts = t0 + minute * 60
            writer.writerow([
                "SNDK", "lighter-rh", ts, "2023-11-14T22:13:20Z",
                100, 100.1, 1, 1, 100, 100.1, 1, 1,
                0, 1, -1, 0, 0, 0, 0, 1, 0, 1, 60,
            ])


def test_analyzer_prints_coverage_and_gaps(tmp_path, monkeypatch, capsys):
    path = tmp_path / "minutes.csv"
    _write_minute_csv(path, missing_index=10)
    monkeypatch.setattr(sys, "argv", ["analyze.py", "--csv", str(path)])

    analyze.main()

    out = capsys.readouterr().out
    assert "coverage" in out
    assert "missing interval" in out


def test_default_analyzer_lists_multiple_pair_files(tmp_path, monkeypatch,
                                                    capsys):
    record_dir = tmp_path / "logs" / "record"
    record_dir.mkdir(parents=True)
    (record_dir / "minutes-SNDK-lighter.csv").write_text("header\n")
    (record_dir / "minutes-SNDK-lighter-rh.csv").write_text("header\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["analyze.py"])

    with pytest.raises(SystemExit) as exc:
        analyze.main()

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "multiple pair-specific CSV files" in err
    assert "minutes-SNDK-lighter.csv" in err
    assert "minutes-SNDK-lighter-rh.csv" in err
