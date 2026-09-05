"""Analyzer accepts both legacy Entropy/hedge CSVs and generic pair CSVs."""
import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools import analyze  # noqa: E402


def write_rows(path, generic):
    fields = ["minute_ts", "time_utc", "samples", "premium_close_bps",
              "premium_mean_bps"]
    if generic:
        fields += ["venue_a", "venue_b", "symbol", "a_bid", "a_ask",
                   "b_bid", "b_ask", "sell_a_edge_max_bps",
                   "buy_a_edge_max_bps"]
    else:
        fields += ["entropy_bid", "entropy_ask", "hedge_bid", "hedge_ask",
                   "sell_edge_max_bps", "buy_edge_max_bps"]
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for i in range(30):
            row = {
                "minute_ts": 1_700_000_000 + i * 60,
                "time_utc": "2023-11-14T00:00:00Z",
                "samples": 60,
                "premium_close_bps": 1.0 + i / 10,
                "premium_mean_bps": 1.0 + i / 10,
            }
            if generic:
                row.update({
                    "venue_a": "entropy", "venue_b": "lighter-rh",
                    "symbol": "SNDK", "a_bid": 100.0, "a_ask": 100.1,
                    "b_bid": 99.9, "b_ask": 100.0,
                    "sell_a_edge_max_bps": 8.0 + i / 10,
                    "buy_a_edge_max_bps": 7.0 + i / 10,
                })
            else:
                row.update({
                    "entropy_bid": 100.0, "entropy_ask": 100.1,
                    "hedge_bid": 99.9, "hedge_ask": 100.0,
                    "sell_edge_max_bps": 8.0 + i / 10,
                    "buy_edge_max_bps": 7.0 + i / 10,
                })
            writer.writerow(row)


def test_legacy_and_generic_rows_normalize_identically():
    directory = tempfile.mkdtemp()
    legacy = os.path.join(directory, "legacy.csv")
    generic = os.path.join(directory, "generic.csv")
    write_rows(legacy, generic=False)
    write_rows(generic, generic=True)
    assert analyze.load_rows(legacy, hours=0, min_samples=10) == \
        analyze.load_rows(generic, hours=0, min_samples=10)


def test_analysis_model_receives_same_legacy_and_generic_edges(monkeypatch,
                                                                capsys):
    directory = tempfile.mkdtemp()
    legacy = os.path.join(directory, "legacy.csv")
    generic = os.path.join(directory, "generic.csv")
    write_rows(legacy, generic=False)
    write_rows(generic, generic=True)

    monkeypatch.setattr(sys, "argv", ["analyze.py", "--csv", legacy,
                                       "--min-samples", "10"])
    analyze.main()
    legacy_out = capsys.readouterr().out.replace(legacy, "CSV")

    monkeypatch.setattr(sys, "argv", ["analyze.py", "--csv", generic,
                                       "--min-samples", "10"])
    analyze.main()
    generic_out = capsys.readouterr().out.replace(generic, "CSV")
    assert legacy_out == generic_out
