"""Pre-deployment single-instance and marker-path checks; all offline."""
import asyncio
import fcntl
from pathlib import Path
import pytest
from test_engine import make_engine


def test_second_live_process_cannot_acquire_pair_lock(tmp_path):
    one=make_engine()
    two=make_engine()
    path=str(tmp_path/"trades.SNDK.lighter-rh.unresolved")
    one._recovery_marker=path
    two._recovery_marker=path
    first=one._acquire_live_lock()
    try:
        with pytest.raises(RuntimeError,match="pair lock"):
            two._acquire_live_lock()
    finally:
        fcntl.flock(first.fileno(),fcntl.LOCK_UN)
        first.close()
    second=two._acquire_live_lock()
    second.close()


def test_lock_survives_marker_clear(tmp_path):
    eng=make_engine()
    eng._recovery_marker=str(tmp_path/"incident.unresolved")
    first=eng._acquire_live_lock()
    try:
        eng._persist_unknown("test")
        eng._clear_review_marker()
        another=make_engine()
        another._recovery_marker=eng._recovery_marker
        with pytest.raises(RuntimeError,match="pair lock"):
            another._acquire_live_lock()
    finally:
        first.close()


def test_path_is_derived_from_trades_csv_and_market_pair(tmp_path):
    eng=make_engine()
    original=eng._recovery_marker
    assert original.endswith(".unresolved")
    assert eng.cfg.symbol in original
    assert eng.cfg.hedge_venue in original
    assert Path(original).is_absolute()


def test_record_only_does_not_take_live_lock(tmp_path):
    async def run():
        eng=make_engine()
        eng.record_only=True
        eng._recovery_marker=str(tmp_path/"incident.unresolved")
        called=[]
        async def fake_inner():
            called.append(True)
        eng._run_inner=fake_inner
        await eng.run()
        assert called==[True]
        assert not (tmp_path/"incident.unresolved.lock").exists()
    asyncio.run(run())
