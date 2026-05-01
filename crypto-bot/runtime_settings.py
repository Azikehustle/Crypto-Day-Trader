"""Runtime-mutable settings layered on top of compile-time `config`.

Lets `/config` and `/pairs` Telegram commands change behaviour live, with state
persisted to Supabase `bot_state` under the key `runtime_settings`. Modules
that need a tunable value should call the getters here instead of importing
the constant directly from `config`.

Falls back gracefully to `config` defaults when Supabase is unavailable.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

import config
from logger_setup import get_logger

log = get_logger("runtime")

_STATE_KEY = "runtime_settings"
_lock = threading.RLock()
_loaded = False

_state: Dict[str, Any] = {
    "symbols": list(config.SYMBOLS),
    "max_open_trades": int(config.MAX_OPEN_TRADES),
    # Stored as positive percentage of starting equity (e.g. 3.0 == 3%)
    "daily_loss_cap_pct": abs(float(config.DAILY_LOSS_CAP)) * 100.0,
    # Stored as percentage (e.g. 1.0 == 1%)
    "risk_per_trade_pct": float(config.RISK_PER_TRADE) * 100.0,
    "stop_requested": False,
    "restart_requested": False,
}


def _supa():
    """Lazy import to avoid import cycles during config load."""
    try:
        from supabase_client import (  # noqa: WPS433
            get_bot_state,
            set_bot_state,
            is_connected,
        )
        return get_bot_state, set_bot_state, is_connected
    except Exception as e:  # noqa: BLE001
        log.warning("supabase_client unavailable: %s", e)
        return None, None, lambda: False


def load() -> None:
    """Load persisted runtime settings from Supabase. Safe to call repeatedly."""
    global _loaded
    with _lock:
        get_bot_state, _set, is_conn = _supa()
        if not is_conn():
            _loaded = True
            return
        try:
            saved = get_bot_state(_STATE_KEY, None)
            if isinstance(saved, dict):
                # Only accept known keys to avoid stale junk
                for key in list(_state.keys()):
                    if key in saved:
                        _state[key] = saved[key]
                # Sanitise types
                _state["symbols"] = [
                    str(s).strip().upper() for s in (_state.get("symbols") or [])
                    if str(s).strip()
                ] or list(config.SYMBOLS)
                _state["max_open_trades"] = max(1, int(_state.get("max_open_trades", config.MAX_OPEN_TRADES)))
                _state["daily_loss_cap_pct"] = max(0.5, float(_state.get("daily_loss_cap_pct", abs(config.DAILY_LOSS_CAP) * 100)))
                _state["risk_per_trade_pct"] = max(0.1, float(_state.get("risk_per_trade_pct", config.RISK_PER_TRADE * 100)))
                # Never persist a stop / restart flag across processes
                _state["stop_requested"] = False
                _state["restart_requested"] = False
                log.info("Loaded runtime settings: %s", _state)
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to load runtime settings: %s", e)
        _loaded = True


def _save() -> None:
    """Persist runtime settings (minus transient flags)."""
    get_bot_state, set_bot_state, is_conn = _supa()
    if not is_conn() or set_bot_state is None:
        return
    try:
        payload = {
            k: v for k, v in _state.items()
            if k not in ("stop_requested", "restart_requested")
        }
        set_bot_state(_STATE_KEY, payload)
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to save runtime settings: %s", e)


# ---------------- getters --------------------------------------------------

def get_symbols() -> List[str]:
    with _lock:
        return list(_state["symbols"])


def get_max_open_trades() -> int:
    with _lock:
        return int(_state["max_open_trades"])


def get_daily_loss_cap_pct() -> float:
    """Returns positive percent (e.g. 3.0 means -3%)."""
    with _lock:
        return float(_state["daily_loss_cap_pct"])


def get_daily_loss_cap_fraction() -> float:
    """Returns negative fraction (e.g. -0.03)."""
    return -get_daily_loss_cap_pct() / 100.0


def get_risk_per_trade_pct() -> float:
    with _lock:
        return float(_state["risk_per_trade_pct"])


def get_risk_per_trade_fraction() -> float:
    return get_risk_per_trade_pct() / 100.0


def is_stopped() -> bool:
    with _lock:
        return bool(_state["stop_requested"])


def is_restart_requested() -> bool:
    with _lock:
        return bool(_state["restart_requested"])


# ---------------- mutators -------------------------------------------------

def set_max_open_trades(value: int) -> int:
    with _lock:
        _state["max_open_trades"] = max(1, min(20, int(value)))
        _save()
        return _state["max_open_trades"]


def adjust_max_open_trades(delta: int) -> int:
    return set_max_open_trades(get_max_open_trades() + int(delta))


def set_daily_loss_cap_pct(value: float) -> float:
    with _lock:
        _state["daily_loss_cap_pct"] = max(0.5, min(50.0, float(value)))
        _save()
        return _state["daily_loss_cap_pct"]


def adjust_daily_loss_cap_pct(delta: float) -> float:
    return set_daily_loss_cap_pct(get_daily_loss_cap_pct() + float(delta))


def set_risk_per_trade_pct(value: float) -> float:
    with _lock:
        _state["risk_per_trade_pct"] = max(0.1, min(10.0, float(value)))
        _save()
        return _state["risk_per_trade_pct"]


def adjust_risk_per_trade_pct(delta: float) -> float:
    return set_risk_per_trade_pct(get_risk_per_trade_pct() + float(delta))


def add_symbol(symbol: str) -> bool:
    sym = (symbol or "").strip().upper()
    if "/" not in sym or len(sym) > 16:
        return False
    with _lock:
        if sym in _state["symbols"]:
            return False
        _state["symbols"].append(sym)
        _save()
        return True


def remove_symbol(symbol: str) -> bool:
    sym = (symbol or "").strip().upper()
    with _lock:
        if sym not in _state["symbols"]:
            return False
        _state["symbols"].remove(sym)
        _save()
        return True


def request_stop(value: bool = True) -> None:
    with _lock:
        _state["stop_requested"] = bool(value)


def request_restart(value: bool = True) -> None:
    with _lock:
        _state["restart_requested"] = bool(value)
