"""Pure signal extraction and frozen shadow behavior regression."""
import dataclasses
import importlib.util
import math
import subprocess
import sys
import types

import pytest

from entropy_arb import range_inventory_shadow as shadow_module
from test_range_inventory_shadow import row, small_params


def core_type():
    assert importlib.util.find_spec("entropy_arb.range_inventory"), "shared pure core missing"
    from entropy_arb.range_inventory import RangeInventoryCore
    return RangeInventoryCore


def oracle():
    source = subprocess.check_output([
        "git", "show", "a81eb822baa1d27ce609c4a5b95fae0a3b383a59:entropy_arb/range_inventory_shadow.py",
    ], text=True)
    module = types.ModuleType("range_shadow_pre_refactor")
    sys.modules[module.__name__] = module
    exec(compile(source, "<pinned-shadow-oracle>", "exec"), module.__dict__)
    return module


def test_shadow_owns_shared_core():
    cls = core_type()
    shadow = shadow_module.RangeInventoryShadow(variant="baseline", use_range_gate=False)
    assert isinstance(shadow.core, cls)


def test_completed_minute_signal_is_pure_target_not_execution():
    core = core_type()(params=small_params(), use_range_gate=False)
    for i, premium in enumerate([0, 1, 2]):
        core.warmup_row(row(i, premium))
    signal = core.on_row(row(3, -10), inventory_usd=0)
    assert signal.minute_ts == 180
    assert signal.completed_ts == 240
    assert signal.target_usd > 0
    assert signal.action == "long_build"
    assert not hasattr(core, "q_position")
    assert not hasattr(core, "send_taker")


@pytest.mark.parametrize("inventory,premium,action", [
    (3000, .3, "long_release"), (-3000, -.1, "short_cover"),
])
def test_closed_gate_never_blocks_reduction(inventory, premium, action):
    minutes = 50 if inventory < 0 else 4
    core = core_type()(params=small_params(range_gate_min_bps=50,
                                           short_window_minutes=minutes), use_range_gate=True)
    for i in range(minutes - 1):
        core.warmup_row(row(i, .1 * i))
    signal = core.on_row(row(minutes - 1, premium), inventory_usd=inventory)
    assert signal.range_gate_open is False
    assert abs(signal.target_usd) < abs(inventory)
    assert not signal.exposure_blocked
    assert signal.action == action
    assert signal.target_usd * inventory >= 0


def test_gate_blocks_add_and_sparse_window_is_not_ready():
    core = core_type()(params=small_params(range_gate_min_bps=50), use_range_gate=True)
    assert core.on_row(row(0, 0), inventory_usd=0).target_usd is None
    for i, premium in enumerate([1, 2], 1):
        core.warmup_row(row(i, premium))
    signal = core.on_row(row(3, -1), inventory_usd=0)
    assert signal.target_usd == 0
    assert signal.exposure_blocked
    assert signal.action == "blocked"
    assert core.on_row(row(20, -10), inventory_usd=0).target_usd is None


def test_shadow_all_fields_match_pinned_pre_refactor_oracle():
    core_type()
    old = oracle()
    params = small_params()
    for gate in (False, True):
        before = old.RangeInventoryShadow(variant="test", use_range_gate=gate,
                                          params=old.FrozenRangeInventoryParams(**dataclasses.asdict(params)))
        after = shadow_module.RangeInventoryShadow(variant="test", use_range_gate=gate, params=params)
        for i in range(600):
            data = row(i + (i // 100) * 3, 30 * math.sin(i / 11),
                       bid=100 + i / 100, ask=100.1 + i / 100, qty=5)
            expected = dataclasses.asdict(before.on_row(data))
            actual = dataclasses.asdict(after.on_row(data))
            assert actual == expected, f"minute={i}, gate={gate}"
