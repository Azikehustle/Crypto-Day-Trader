"""Runtime-mutable settings layered on top of compile-time config.

New settings vs Phase 1:
  mode                — 'scalp' | 'day' | 'swing' (or 'all' for all three)
  correlation_mode    — 'strict' | 'relaxed'
  news_shield_enabled — bool
  weekly_restart      — bool

All state persisted to Supabase bot_state under key 'runtime_settings'.
Falls back gracefully to config defaults when Supabase is unavailable.
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
    "daily_loss_cap_pct": abs(float(config.DAILY_LOSS_CAP)) * 100.0,
    "risk_per_trade_pct": float(config.RISK_PER_TRADE) * 100.0,
    "mode": config.DEFAULT_MODE,
    "correlation_mode": config.CORRELATION_MODE,
    "news_shield_enabled": config.NEWS_SHIELD_ENABLED,
    "weekly_restart_enabled": config.WEEKLY_RESTART_ENABLED,
    "stop_requested": False,
    "restart_requested": False,
}


def _supa():
    try:
        from supabase_client import get_bot_state, set_bot_state, is_connected  # noqa: WPS433
        return get_bot_state, set_bot_state, is_connected
    except Exception as e:  # noqa: BLE001
        log.warning("supabase_client unavailable: %s", e)
        return None, None, lambda: False


def load() -> None:
    global _loaded
    with _lock:
        get_bot_state, _set, is_conn = _supa()
        if not is_conn():
            _loaded = True
            return
        try:
            saved = get_bot_state(_STATE_KEY, None)
            if isinstance(saved, dict):
                for key in list(_state.keys()):
                    if key in saved:
                        _state[key] = saved[key]
                _state["symbols"] = [
                    str(s).strip().upper()
                    for s in (_state.get("symbols") or []) if str(s).strip()
                ] or list(config.SYMBOLS)
                _state["max_open_trades"]  = max(1, int(_state.get("max_open_trades", config.MAX_OPEN_TRADES)))
                _state["daily_loss_cap_pct"] = max(0.5, float(_state.get("daily_loss_cap_pct", abs(config.DAILY_LOSS_CAP) * 100)))
                _state["risk_per_trade_pct"] = max(0.1, float(_state.get("risk_per_trade_pct", config.RISK_PER_TRADE * 100)))
                _state["mode"] = _state.get("mode", config.DEFAULT_MODE)
                if _state["mode"] not in ("scalp", "day", "swing", "all"):
                    _state["mode"] = config.DEFAULT_MODE
                _state["correlation_mode"] = _state.get("correlation_mode", config.CORRELATION_MODE)
                if _state["correlation_mode"] not in ("strict", "relaxed"):
                    _state["correlation_mode"] = "strict"
                _state["news_shield_enabled"]   = bool(_state.get("news_shield_enabled", config.NEWS_SHIELD_ENABLED))
                _state["weekly_restart_enabled"] = bool(_state.get("weekly_restart_enabled", config.WEEKLY_RESTART_ENABLED))
                _state["stop_requested"]    = False
                _state["restart_requested"] = False
                log.info("Loaded runtime settings: %s", _state)
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to load runtime settings: %s", e)
        _loaded = True


def _save() -> None:
    _, set_bot_state, is_conn = _supa()
    if not is_conn() or set_bot_state is None:
        return
    try:
        payload = {k: v for k, v in _state.items() if k not in ("stop_requested", "restart_requested")}
        set_bot_state(_STATE_KEY, payload)
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to save runtime settings: %s", e)


# ── getters ──────────────────────────────────────────────────────────────────

def get_symbols() -> List[str]:
    with _lock: return list(_state["symbols"])

def get_max_open_trades() -> int:
    with _lock: return int(_state["max_open_trades"])

def get_daily_loss_cap_pct() -> float:
    with _lock: return float(_state["daily_loss_cap_pct"])

def get_daily_loss_cap_fraction() -> float:
    return -get_daily_loss_cap_pct() / 100.0

def get_risk_per_trade_pct() -> float:
    with _lock: return float(_state["risk_per_trade_pct"])

def get_risk_per_trade_fraction() -> float:
    return get_risk_per_trade_pct() / 100.0

def get_mode() -> str:
    with _lock: return str(_state.get("mode", config.DEFAULT_MODE))

def get_correlation_mode() -> str:
    with _lock: return str(_state.get("correlation_mode", config.CORRELATION_MODE))

def get_news_shield_enabled() -> bool:
    with _lock: return bool(_state.get("news_shield_enabled", config.NEWS_SHIELD_ENABLED))

def get_weekly_restart_enabled() -> bool:
    with _lock: return bool(_state.get("weekly_restart_enabled", config.WEEKLY_RESTART_ENABLED))

def is_stopped() -> bool:
    with _lock: return bool(_state["stop_requested"])

def is_restart_requested() -> bool:
    with _lock: return bool(_state["restart_requested"])


# ── mutators ─────────────────────────────────────────────────────────────────

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

def set_mode(mode: str) -> str:
    with _lock:
        if mode in ("scalp", "day", "swing", "all"):
            _state["mode"] = mode
            _save()
        return _state["mode"]

def set_correlation_mode(mode: str) -> str:
    with _lock:
        if mode in ("strict", "relaxed"):
            _state["correlation_mode"] = mode
            _save()
        return _state["correlation_mode"]

def set_news_shield(enabled: bool) -> bool:
    with _lock:
        _state["news_shield_enabled"] = bool(enabled)
        _save()
        return _state["news_shield_enabled"]

def set_weekly_restart(enabled: bool) -> bool:
    with _lock:
        _state["weekly_restart_enabled"] = bool(enabled)
        _save()
        return _state["weekly_restart_enabled"]

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
    with _lock: _state["stop_requested"] = bool(value)

def request_restart(value: bool = True) -> None:
    with _lock: _state["restart_requested"] = bool(value)
