"""Direct invocation test for the read-only SNDK × RH verifier."""
import csv
import json
import subprocess
import sys
from pathlib import Path


EFFECTIVE = 1_700_031_600


def test_verifier_runs_from_repo_root_without_pythonpath(tmp_path):
    path = tmp_path / "sndk-rh.csv"
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "minute_ts", "premium_close_bps", "sell_edge_max_bps",
            "buy_edge_max_bps", "samples"])
        writer.writeheader()
        for i in range(480):
            writer.writerow({
                "minute_ts": EFFECTIVE - (480 - i) * 60,
                "premium_close_bps": 0.0,
                "sell_edge_max_bps": 12.0,
                "buy_edge_max_bps": 0.0,
                "samples": 10,
            })

    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "tools/verify_rolling_sndk_rh.py",
         "--csv", str(path), "--effective-at", "2023-11-15T07:00:00Z",
         "--fees-bps", "0"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["valid"] is True
    assert report["rows"] == 480
    assert report["midline_bps"] == 0.0
    assert report["upper_bps"] == 12.0
    assert report["lower_bps"] == 1.0
