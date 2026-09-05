"""Configuration: strategy from a YAML file, credentials from .env, and
the selected venue pair from the command line.

The split is deliberate: config.yaml IS the strategy (thresholds, sizing,
risk) and is safe to share/commit as an example; .env holds only secrets;
which markets to trade is stated explicitly on every start (--symbol,
--venue-a, --venue-b). Every YAML key is validated against the schema below, so a typo
is an error rather than a setting that silently does nothing.

Threshold model (fixed numbers the user derives from recorded minute data):

    premium_bps = (venue_a_price / venue_b_price - 1) * 10_000

    SELL A / BUY B  fires when the executable premium
        (A bid over B ask) >= midline_bps + upper_bps
    BUY A / SELL B  fires when the executable premium
        (B bid over A ask) >= lower_bps - midline_bps

    Both hurdles are net of both venues' taker fees, so a full round trip
    nets >= (upper_bps + lower_bps) after fees by construction.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

import yaml
from dotenv import load_dotenv

HL_API_URL = "https://api.hyperliquid.xyz"
HL_WS_URL = "wss://api.hyperliquid.xyz/ws"   # official ws — the only HL feed used

SUPPORTED_VENUES = ("entropy", "lighter", "lighter-rh", "tradexyz")


@dataclass(frozen=True)
class LighterProfile:
    name: str
    api_url: str
    ws_url: str
    chain_id: int


# Endpoint profiles for the two supported zkLighter deployments (these match
# lighter-python's lighter.endpoint_profiles, duplicated here so --record-only
# data collection works without the SDK installed).
LIGHTER_PROFILES: Dict[str, LighterProfile] = {
    "lighter": LighterProfile(
        "mainnet", "https://mainnet.zklighter.elliot.ai",
        "wss://mainnet.zklighter.elliot.ai/stream", 304),
    "lighter-rh": LighterProfile(
        "robinhood", "https://api.rh.lighter.xyz",
        "wss://api.rh.lighter.xyz/stream", 466324),
}


@dataclass
class LighterCreds:
    account_index: Optional[int]
    api_key_index: Optional[int]
    api_private_key: Optional[str]

    @property
    def complete(self) -> bool:
        return (self.account_index is not None and self.api_key_index is not None
                and bool(self.api_private_key))


@dataclass
class HLCreds:
    private_key: Optional[str]
    account_address: Optional[str]

    @property
    def complete(self) -> bool:
        return bool(self.private_key)


@dataclass
class VenueConf:
    key: str                  # "venue_a" | "venue_b" role
    venue_name: str           # actual supported venue identity
    kind: str                 # "hl" | "lighter"
    label: str                # human name for logs, e.g. "ENTROPY", "RH"
    symbol: str
    fee_bps: float
    cap_usd: float
    orders_per_min: int
    # hl
    hl_dex: str = ""
    hl_creds: Optional[HLCreds] = None
    # lighter
    lighter_profile: Optional[LighterProfile] = None
    lighter_creds: Optional[LighterCreds] = None


@dataclass
class Config:
    symbol: str
    venue_a: VenueConf
    venue_b: VenueConf
    # thresholds (the whole signal)
    midline_bps: float
    upper_bps: float
    lower_bps: float
    # sizing
    take_fraction: float
    max_order_notional: float
    min_order_notional: float
    # inventory ladder
    inventory_scale_bps: float
    inventory_floor_frac: float
    # execution
    premium_persist_sec: float
    cooldown_sec: float
    settle_timeout_sec: float
    leg_slippage_bps: float
    hedge_slippage_bps: float
    net_tolerance_base: float
    max_consecutive_errors: int
    rate_limit_pause_sec: float
    staleness_sec: float
    reconcile_sec: float
    venue_probe_sec: float
    http_keepalive_sec: float
    # recorder
    recorder_enabled: bool
    recorder_csv: str
    # logging
    log_level: str
    status_interval_sec: float
    trades_csv: str
    dashboard: bool
    log_file: str
    # runtime
    hl_api_url: str = HL_API_URL
    hl_ws_url: str = HL_WS_URL

    @property
    def creds_complete(self) -> bool:
        for v in (self.venue_a, self.venue_b):
            if v.kind == "hl" and not (v.hl_creds and v.hl_creds.complete):
                return False
            if v.kind == "lighter" and not (v.lighter_creds
                                            and v.lighter_creds.complete):
                return False
        return True


# ----------------------------------------------------------------- YAML layer

# Schema: nested dict of key -> type (or nested dict). Unknown keys are errors.
_SCHEMA: Dict[str, Any] = {
    "thresholds": {
        "midline_bps": float,
        "upper_bps": float,
        "lower_bps": float,
    },
    "venues": {
        "entropy": {
            "taker_fee_bps": float,
            "max_position_usd": float,
            "max_orders_per_min": int,
        },
        "lighter": {
            "taker_fee_bps": float,
            "max_position_usd": float,
            "max_orders_per_min": int,
        },
        "lighter-rh": {
            "taker_fee_bps": float,
            "max_position_usd": float,
            "max_orders_per_min": int,
        },
        "tradexyz": {
            "taker_fee_bps": float,
            "max_position_usd": float,
            "max_orders_per_min": int,
        },
    },
    "sizing": {
        "take_fraction": float,
        "max_order_notional_usd": float,
        "min_order_notional_usd": float,
    },
    "inventory": {
        "scale_bps": float,
        "floor_frac": float,
    },
    "execution": {
        "premium_persist_sec": float,
        "cooldown_sec": float,
        "settle_timeout_sec": float,
        "leg_slippage_bps": float,
        "hedge_slippage_bps": float,
        "net_tolerance_base": float,
        "max_consecutive_errors": int,
        "rate_limit_pause_sec": float,
        "staleness_sec": float,
        "reconcile_sec": float,
        "venue_probe_sec": float,
        "http_keepalive_sec": float,
    },
    "recorder": {
        "enabled": bool,
        "csv": str,
    },
    "logging": {
        "level": str,
        "status_interval_sec": float,
        "trades_csv": str,
        "dashboard": bool,
        "file": str,
    },
}


class ConfigError(ValueError):
    pass


def _validate(node: Any, schema: Dict[str, Any], path: str = "") -> None:
    if not isinstance(node, dict):
        raise ConfigError(f"'{path or '<root>'}' must be a mapping")
    for key, val in node.items():
        here = f"{path}.{key}" if path else str(key)
        if key not in schema:
            raise ConfigError(f"unknown config key '{here}' "
                              f"(valid: {', '.join(sorted(schema))})")
        want = schema[key]
        if isinstance(want, dict):
            _validate(val, want, here)
        elif want is float:
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                raise ConfigError(f"'{here}' must be a number, got {val!r}")
        elif want is int:
            if not isinstance(val, int) or isinstance(val, bool):
                raise ConfigError(f"'{here}' must be an integer, got {val!r}")
        elif want is bool:
            if not isinstance(val, bool):
                raise ConfigError(f"'{here}' must be true/false, got {val!r}")
        elif want is str:
            if not isinstance(val, str):
                raise ConfigError(f"'{here}' must be a string, got {val!r}")


def _get(d: dict, section: str, key: str, default):
    return (d.get(section) or {}).get(key, default)


# ------------------------------------------------------------------ env layer

def _env_s(name: str) -> Optional[str]:
    v = os.getenv(name)
    return v.strip() if v not in (None, "") else None


def _env_i(name: str) -> Optional[int]:
    v = os.getenv(name)
    return int(v) if v not in (None, "") else None


# Explicit venue construction map. Venue names choose protocol identity;
# VenueConf.key remains only the A/B role used by the engine.
VENUE_SPECS = {
    "entropy": {"kind": "hl", "label": "ENTROPY", "hl_dex": "io",
                "fee_bps": 0.0, "cap_usd": 1000.0, "orders_per_min": 120},
    "tradexyz": {"kind": "hl", "label": "XYZ", "hl_dex": "xyz",
                 "fee_bps": 1.0, "cap_usd": 1000.0, "orders_per_min": 120},
    "lighter": {"kind": "lighter", "label": "LIGHTER",
                 "fee_bps": 0.0, "cap_usd": 1000.0, "orders_per_min": 30},
    "lighter-rh": {"kind": "lighter", "label": "RH",
                    "fee_bps": 0.0, "cap_usd": 1000.0,
                    "orders_per_min": 30},
}


def build_venue_conf(name: str, role: str, symbol: str,
                     raw_config: Dict[str, Any]) -> VenueConf:
    """Build one role-bound venue from the explicit supported venue map."""
    if role not in ("venue_a", "venue_b"):
        raise ConfigError(f"venue role must be venue_a or venue_b, got {role!r}")
    if name not in SUPPORTED_VENUES:
        raise ConfigError(f"unsupported venue {name!r}; supported venues are "
                          f"{list(SUPPORTED_VENUES)}")

    spec = VENUE_SPECS[name]
    settings = (raw_config.get("venues") or {}).get(name) or {}
    common = dict(
        key=role,
        venue_name=name,
        kind=spec["kind"],
        label=spec["label"],
        symbol=symbol,
        fee_bps=float(settings.get("taker_fee_bps", spec["fee_bps"])),
        cap_usd=float(settings.get("max_position_usd", spec["cap_usd"])),
        orders_per_min=int(settings.get("max_orders_per_min",
                                    spec["orders_per_min"])),
    )
    if spec["kind"] == "hl":
        if name == "tradexyz":
            private_key = (_env_s("HL_PRIVATE_KEY_XYZ")
                           or _env_s("HL_PRIVATE_KEY"))
            account_address = (_env_s("HL_ACCOUNT_ADDRESS_XYZ")
                               or _env_s("HL_ACCOUNT_ADDRESS"))
        else:
            private_key = _env_s("HL_PRIVATE_KEY")
            account_address = _env_s("HL_ACCOUNT_ADDRESS")
        return VenueConf(
            **common,
            hl_dex=spec["hl_dex"],
            hl_creds=HLCreds(private_key, account_address),
        )

    return VenueConf(
        **common,
        lighter_profile=LIGHTER_PROFILES[name],
        lighter_creds=LighterCreds(_env_i("LIGHTER_ACCOUNT_INDEX"),
                                   _env_i("LIGHTER_API_KEY_INDEX"),
                                   _env_s("LIGHTER_API_PRIVATE_KEY")),
    )


# -------------------------------------------------------------------- loading

def load_config(config_file: str = "config.yaml", env_file: str = ".env", *,
                symbol: str, venue_a: str, venue_b: str) -> Config:
    load_dotenv(env_file)
    try:
        with open(config_file) as fh:
            raw = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        raise ConfigError(
            f"config file '{config_file}' not found — copy config.example.yaml "
            f"to config.yaml and edit it / 未找到配置文件，请先复制 "
            f"config.example.yaml 为 config.yaml 并修改")
    _validate(raw, _SCHEMA)

    symbol = (symbol or "").strip()
    if not symbol:
        raise ConfigError("--symbol is required, e.g. --symbol SNDK / "
                          "必须用 --symbol 指定交易品种")
    for flag, name in (("--venue-a", venue_a), ("--venue-b", venue_b)):
        if name not in SUPPORTED_VENUES:
            raise ConfigError(f"{flag} must be one of the supported venues "
                              f"{list(SUPPORTED_VENUES)}, got {name!r}")
    if venue_a == venue_b:
        raise ConfigError("--venue-a and --venue-b must be different venues "
                          "/ 两条腿必须是不同交易所")

    thr = raw.get("thresholds") or {}
    for k in ("midline_bps", "upper_bps", "lower_bps"):
        if k not in thr:
            raise ConfigError(f"'thresholds.{k}' is required — derive it from "
                              f"recorded minute data / 必须填写，请用采集的分钟"
                              f"数据计算后填入")
    upper, lower = float(thr["upper_bps"]), float(thr["lower_bps"])
    if upper <= 0 or lower <= 0:
        raise ConfigError("thresholds.upper_bps and lower_bps must be > 0 "
                          "(the round trip nets upper+lower bps after fees)")

    take_fraction = float(_get(raw, "sizing", "take_fraction", 0.5))
    if not 0.0 < take_fraction <= 1.0:
        raise ConfigError("sizing.take_fraction must be in (0, 1] — taking "
                          "more than the profitable depth loses money on the "
                          "tail / 必须在 (0, 1] 之间")

    a_conf = build_venue_conf(venue_a, "venue_a", symbol, raw)
    b_conf = build_venue_conf(venue_b, "venue_b", symbol, raw)
    recorder = raw.get("recorder") or {}
    recorder_csv = (recorder["csv"] if "csv" in recorder else
                    f"logs/minutes-{symbol}-{venue_a}-{venue_b}.csv")

    return Config(
        symbol=symbol,
        venue_a=a_conf,
        venue_b=b_conf,
        midline_bps=float(thr["midline_bps"]),
        upper_bps=upper,
        lower_bps=lower,
        take_fraction=take_fraction,
        max_order_notional=float(_get(raw, "sizing", "max_order_notional_usd", 500.0)),
        min_order_notional=float(_get(raw, "sizing", "min_order_notional_usd", 10.0)),
        inventory_scale_bps=float(_get(raw, "inventory", "scale_bps", 10.0)),
        inventory_floor_frac=float(_get(raw, "inventory", "floor_frac", 0.5)),
        premium_persist_sec=float(_get(raw, "execution", "premium_persist_sec", 0.3)),
        cooldown_sec=float(_get(raw, "execution", "cooldown_sec", 0.0)),
        settle_timeout_sec=float(_get(raw, "execution", "settle_timeout_sec", 5.0)),
        leg_slippage_bps=float(_get(raw, "execution", "leg_slippage_bps", 50.0)),
        hedge_slippage_bps=float(_get(raw, "execution", "hedge_slippage_bps", 20.0)),
        net_tolerance_base=float(_get(raw, "execution", "net_tolerance_base", 0.001)),
        max_consecutive_errors=int(_get(raw, "execution", "max_consecutive_errors", 3)),
        rate_limit_pause_sec=float(_get(raw, "execution", "rate_limit_pause_sec", 10.0)),
        staleness_sec=float(_get(raw, "execution", "staleness_sec", 10.0)),
        reconcile_sec=float(_get(raw, "execution", "reconcile_sec", 15.0)),
        venue_probe_sec=float(_get(raw, "execution", "venue_probe_sec", 30.0)),
        http_keepalive_sec=float(_get(raw, "execution", "http_keepalive_sec", 10.0)),
        recorder_enabled=bool(_get(raw, "recorder", "enabled", True)),
        recorder_csv=recorder_csv,
        log_level=str(_get(raw, "logging", "level", "INFO")).upper(),
        status_interval_sec=float(_get(raw, "logging", "status_interval_sec", 30.0)),
        trades_csv=_get(raw, "logging", "trades_csv", "logs/trades.csv"),
        dashboard=bool(_get(raw, "logging", "dashboard", True)),
        log_file=_get(raw, "logging", "file", "logs/engine.log"),
    )
