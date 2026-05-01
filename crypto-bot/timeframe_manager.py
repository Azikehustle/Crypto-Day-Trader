"""Timeframe manager — three simultaneous trading modes.

Modes
-----
  SCALP  : bias on 1h, entry on 5m   → signal tag [SCALP-5m]
  DAY    : bias on 4h, entry on 15m  → signal tag [DAY-15m]   ← default
  SWING  : bias on 1D, entry on 1h   → signal tag [SWING-1h]

Each mode is independent: it maintains its own HTF-bias cache, entry-timeframe
OHLCV fetch, and feeds the shared signal_engine / zone_detector pipeline.

Modes are toggled live via runtime_settings (persisted to Supabase bot_state
under the key "active_modes").  Default: ["DAY"].
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Tuple

import runtime_settings
from logger_setup import get_logger

log = get_logger("tf_mgr")


# ---------------------------------------------------------------------------
# Mode definitions
# ---------------------------------------------------------------------------

class TradingMode:
    __slots__ = ("name", "bias_tf", "entry_tf", "tag", "htf_ema_period", "htf_refresh_minutes")

    def __init__(
        self,
        name: str,
        bias_tf: str,
        entry_tf: str,
        tag: str,
        htf_ema_period: int = 200,
        htf_refresh_minutes: int = 60,
    ) -> None:
        self.name = name
        self.bias_tf = bias_tf
        self.entry_tf = entry_tf
        self.tag = tag
        self.htf_ema_period = htf_ema_period
        self.htf_refresh_minutes = htf_refresh_minutes

    def __repr__(self) -> str:
        return f"TradingMode({self.name}, bias={self.bias_tf}, entry={self.entry_tf})"


MODES: Dict[str, TradingMode] = {
    "SCALP": TradingMode(
        name="SCALP",
        bias_tf="1h",
        entry_tf="5m",
        tag="[SCALP-5m]",
        htf_ema_period=50,
        htf_refresh_minutes=15,
    ),
    "DAY": TradingMode(
        name="DAY",
        bias_tf="4h",
        entry_tf="15m",
        tag="[DAY-15m]",
        htf_ema_period=200,
        htf_refresh_minutes=60,
    ),
    "SWING": TradingMode(
        name="SWING",
        bias_tf="1D",
        entry_tf="1h",
        tag="[SWING-1h]",
        htf_ema_period=200,
        htf_refresh_minutes=240,
    ),
}

_DEFAULT_MODES = ["DAY"]
_STATE_KEY = "active_modes"

# ---------------------------------------------------------------------------
# Active modes storage (thread-safe)
# ---------------------------------------------------------------------------

_lock = threading.RLock()
_active_mode_names: List[str] = list(_DEFAULT_MODES)

# Per-(mode, symbol) HTF bias cache: {"bias": str, "fetched_at": datetime}
_htf_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _save_modes() -> None:
    try:
        from supabase_client import is_connected, set_bot_state
        if is_connected():
            set_bot_state(_STATE_KEY, list(_active_mode_names))
    except Exception as e:  # noqa: BLE001
        log.warning("timeframe_manager: failed to save modes: %s", e)


def load_modes() -> None:
    """Load active-modes list from Supabase (called at bot startup)."""
    global _active_mode_names
    try:
        from supabase_client import is_connected, get_bot_state
        if not is_connected():
            return
        saved = get_bot_state(_STATE_KEY, None)
        if isinstance(saved, list):
            valid = [m for m in saved if m in MODES]
            if valid:
                with _lock:
                    _active_mode_names = valid
                log.info("Loaded active modes from Supabase: %s", valid)
    except Exception as e:  # noqa: BLE001
        log.warning("timeframe_manager: failed to load modes: %s", e)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_active_modes() -> List[TradingMode]:
    """Return the currently active TradingMode objects."""
    with _lock:
        return [MODES[n] for n in _active_mode_names if n in MODES]


def get_active_mode_names() -> List[str]:
    with _lock:
        return list(_active_mode_names)


def set_active_modes(names: List[str]) -> List[str]:
    """Set the active modes. Returns the validated list that was saved."""
    global _active_mode_names
    valid = [m for m in names if m in MODES]
    if not valid:
        log.warning("set_active_modes: no valid modes in %s — keeping current", names)
        return list(_active_mode_names)
    with _lock:
        _active_mode_names = valid
    _save_modes()
    log.info("Active modes updated → %s", valid)
    return valid


def enable_mode(name: str) -> bool:
    """Add a mode to the active set. Returns True if added, False if already active or invalid."""
    global _active_mode_names
    if name not in MODES:
        log.warning("enable_mode: unknown mode %s", name)
        return False
    with _lock:
        if name in _active_mode_names:
            return False
        _active_mode_names.append(name)
    _save_modes()
    log.info("Enabled mode: %s", name)
    return True


def disable_mode(name: str) -> bool:
    """Remove a mode from the active set. Returns True if removed."""
    global _active_mode_names
    with _lock:
        if name not in _active_mode_names:
            return False
        if len(_active_mode_names) == 1:
            log.warning("Cannot disable last active mode %s", name)
            return False
        _active_mode_names.remove(name)
    _save_modes()
    log.info("Disabled mode: %s", name)
    return True


def get_mode(name: str) -> Optional[TradingMode]:
    return MODES.get(name)


# ---------------------------------------------------------------------------
# HTF bias cache (per mode × symbol)
# ---------------------------------------------------------------------------

def get_htf_bias_cached(
    mode: TradingMode,
    symbol: str,
    exchange_name: Optional[str] = None,
) -> str:
    """Return HTF bias for (mode, symbol) using a per-mode refresh window."""
    from data_fetcher import fetch_ohlcv, htf_bias

    key = (mode.name, symbol)
    rec = _htf_cache.get(key)
    now = datetime.now(timezone.utc)

    if rec and now - rec["fetched_at"] < timedelta(minutes=mode.htf_refresh_minutes):
        return rec["bias"]

    from config import EXCHANGE
    ex = exchange_name or EXCHANGE
    limit = mode.htf_ema_period + 50

    try:
        df = fetch_ohlcv(symbol, timeframe=mode.bias_tf, limit=limit, exchange_name=ex)
        bias = htf_bias(df, mode.htf_ema_period)
    except Exception as e:  # noqa: BLE001
        log.warning("get_htf_bias_cached %s %s %s: %s", mode.name, symbol, mode.bias_tf, e)
        if rec:
            return rec["bias"]
        return "flat"

    _htf_cache[key] = {"bias": bias, "fetched_at": now}
    log.info("[%s] HTF bias %s (%s): %s", mode.name, symbol, mode.bias_tf, bias)
    return bias


# ---------------------------------------------------------------------------
# Entry OHLCV fetch (per mode × symbol)
# ---------------------------------------------------------------------------

def fetch_entry_ohlcv(
    mode: TradingMode,
    symbol: str,
    limit: int = 300,
    exchange_name: Optional[str] = None,
):
    """Fetch the entry-timeframe OHLCV for a given mode and symbol."""
    from data_fetcher import fetch_ohlcv
    from config import EXCHANGE
    ex = exchange_name or EXCHANGE
    return fetch_ohlcv(symbol, timeframe=mode.entry_tf, limit=limit, exchange_name=ex)


# ---------------------------------------------------------------------------
# Status helpers (for Telegram /status)
# ---------------------------------------------------------------------------

def status_lines() -> List[str]:
    """Return human-readable lines about active modes for /status."""
    lines = ["<b>Trading Modes</b>"]
    for mode in get_active_modes():
        lines.append(
            f"• <code>{mode.name}</code> {mode.tag} "
            f"bias:{mode.bias_tf} entry:{mode.entry_tf}"
        )
    inactive = [n for n in MODES if n not in get_active_mode_names()]
    if inactive:
        lines.append(f"Inactive: {', '.join(inactive)}")
    return lines


def mode_summary() -> str:
    """Single-line summary of active modes."""
    names = get_active_mode_names()
    return " + ".join(names) if names else "NONE"
