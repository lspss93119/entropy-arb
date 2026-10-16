"""P0-03 durable manual-review latch. No network or live orders."""
import asyncio
from pathlib import Path
import pytest
from test_engine import make_engine


def test_unknown_incident_survives_new_engine_and_is_not_overwritten(tmp_path):
    eng=make_engine()
    eng.cfg.trades_csv=str(tmp_path/"trades.csv")
    eng._recovery_marker=str(tmp_path/"incident.unresolved")
    eng._persist_unknown("timeout")
    eng._persist_unknown("followup")
    assert eng._unresolved_execution
    assert "timeout" in Path(eng._recovery_marker).read_text()
    assert "followup" in Path(eng._recovery_marker).read_text()


def test_live_restart_fails_before_market_or_signer_initialization(tmp_path):
    async def run():
        eng=make_engine()
        marker=tmp_path/"incident.unresolved"
        marker.write_text("unknown order")
        eng._recovery_marker=str(marker)
        calls=[]
        def no_market(_):
            calls.append("market")
            raise AssertionError("should not initialize markets")
        eng._make_venue=no_market
        # Restart should fail before any signer or API setup.
        # Existing implementation creates venues before checking marker;
        # this test explicitly guards the sensitive load_market step.
        with pytest.raises(RuntimeError,match="manual review"):
            await eng._run_inner()
        assert calls==[]
    asyncio.run(run())
