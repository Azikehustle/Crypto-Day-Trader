"""Thin Supabase wrapper for trades, zones, and bot_state.

All persistence the bot used to do via local JSON files now goes through here.
The module exposes the helpers listed in the integration spec plus a few small
internal helpers used by paper_trader/risk_manager.

Importing this module does NOT raise if the credentials are missing — it
creates a stub client whose methods raise on call. Call `is_connected()` to
verify before relying on it.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from logger_setup import get_logger

log = get_logger("supabase")

_SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
_SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

supabase = None  # populated below if creds available

try:
    if _SUPABASE_URL and _SUPABASE_KEY:
        from supabase import create_client  # type: ignore
        supabase = create_client(_SUPABASE_URL, _SUPABASE_KEY)
        log.info("Supabase client initialised (%s)", _SUPABASE_URL)
    else:
        log.warning(
            "SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY missing — Supabase disabled."
        )
except Exception as e:  # noqa: BLE001
    log.error("Supabase client init failed: %s", e)
    supabase = None


def is_connected() -> bool:
    return supabase is not None


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_start_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Connectivity check
# ---------------------------------------------------------------------------

def ping() -> bool:
    """Cheap round-trip: select a single row from bot_state.
    Returns True if the connection works (table may be empty)."""
    if not is_connected():
        return False
    try:
        supabase.table("bot_state").select("key").limit(1).execute()
        return True
    except Exception as e:  # noqa: BLE001
        log.error("Supabase ping failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# TRADES
# ---------------------------------------------------------------------------

def insert_trade(
    symbol: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    score: int,
    confidence: str,
    pair_zone_id: Optional[int] = None,
    notes: Optional[Dict[str, Any]] = None,
    position_size: Optional[float] = None,
    pair_weight: Optional[float] = None,
    notional_at_entry: Optional[float] = None,
    risked_usd: Optional[float] = None,
) -> Optional[int]:
    """Insert a new open trade. Returns the row id or None on failure."""
    if not is_connected():
        return None
    payload: Dict[str, Any] = {
        "symbol": symbol,
        "direction": direction,
        "entry_price": float(entry_price),
        "stop_loss": float(stop_loss),
        "take_profit": float(take_profit),
        "score": int(score) if score is not None else None,
        "confidence": confidence,
        "pair_zone_id": pair_zone_id,
        "notes": notes,
        "status": "open",
        "position_size": position_size,
        "pair_weight": pair_weight,
        "notional_at_entry": notional_at_entry,
        "risked_usd": risked_usd,
        "opened_at": _utcnow_iso(),
    }
    try:
        res = supabase.table("trades").insert(payload).execute()
        rows = res.data or []
        return int(rows[0]["id"]) if rows else None
    except Exception as e:  # noqa: BLE001
        log.error("insert_trade failed: %s", e)
        return None


def close_trade(
    trade_id: int,
    status: str,
    exit_price: float,
    pnl: float,
    pnl_pct: float,
    result: Optional[str] = None,
) -> bool:
    if not is_connected():
        return False
    payload: Dict[str, Any] = {
        "status": status,
        "exit_price": float(exit_price),
        "pnl": float(pnl),
        "pnl_pct": float(pnl_pct),
        "closed_at": _utcnow_iso(),
    }
    if result is not None:
        payload["result"] = result
    try:
        supabase.table("trades").update(payload).eq("id", int(trade_id)).execute()
        return True
    except Exception as e:  # noqa: BLE001
        log.error("close_trade(%s) failed: %s", trade_id, e)
        return False


def get_open_trades(symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    if not is_connected():
        return []
    try:
        q = supabase.table("trades").select("*").eq("status", "open")
        if symbol:
            q = q.eq("symbol", symbol)
        res = q.execute()
        return list(res.data or [])
    except Exception as e:  # noqa: BLE001
        log.error("get_open_trades failed: %s", e)
        return []


def get_trades_today() -> List[Dict[str, Any]]:
    if not is_connected():
        return []
    try:
        res = (
            supabase.table("trades")
            .select("*")
            .gte("opened_at", _today_start_iso())
            .execute()
        )
        return list(res.data or [])
    except Exception as e:  # noqa: BLE001
        log.error("get_trades_today failed: %s", e)
        return []


def get_trade_count_today() -> int:
    return len(get_trades_today())


def get_closed_trades_since(iso_ts: str) -> List[Dict[str, Any]]:
    if not is_connected():
        return []
    try:
        res = (
            supabase.table("trades")
            .select("*")
            .eq("status", "closed")
            .gte("closed_at", iso_ts)
            .execute()
        )
        return list(res.data or [])
    except Exception as e:  # noqa: BLE001
        log.error("get_closed_trades_since failed: %s", e)
        return []


def get_all_closed_trades() -> List[Dict[str, Any]]:
    if not is_connected():
        return []
    try:
        res = supabase.table("trades").select("*").eq("status", "closed").execute()
        return list(res.data or [])
    except Exception as e:  # noqa: BLE001
        log.error("get_all_closed_trades failed: %s", e)
        return []


def get_daily_pnl() -> float:
    """Sum of pnl from trades closed since 00:00 UTC today."""
    if not is_connected():
        return 0.0
    try:
        res = (
            supabase.table("trades")
            .select("pnl")
            .eq("status", "closed")
            .gte("closed_at", _today_start_iso())
            .execute()
        )
        return float(sum((r.get("pnl") or 0.0) for r in (res.data or [])))
    except Exception as e:  # noqa: BLE001
        log.error("get_daily_pnl failed: %s", e)
        return 0.0


# ---------------------------------------------------------------------------
# ZONES
# ---------------------------------------------------------------------------

def insert_zone(
    symbol: str,
    zone_type: str,
    price_top: float,
    price_bottom: float,
    origin_ts: Optional[str] = None,
) -> Optional[int]:
    """Idempotent insert keyed on (symbol, zone_type, origin_ts).

    Returns the new id or the existing id on conflict.
    """
    if not is_connected():
        return None
    payload: Dict[str, Any] = {
        "symbol": symbol,
        "zone_type": zone_type,
        "price_top": float(price_top),
        "price_bottom": float(price_bottom),
        "origin_ts": origin_ts,
        "status": "active",
    }
    try:
        res = (
            supabase.table("zones")
            .upsert(payload, on_conflict="symbol,zone_type,origin_ts")
            .execute()
        )
        rows = res.data or []
        return int(rows[0]["id"]) if rows else None
    except Exception as e:  # noqa: BLE001
        log.error("insert_zone failed: %s", e)
        return None


def sweep_zone(zone_id: int) -> bool:
    if not is_connected():
        return False
    try:
        supabase.table("zones").update(
            {"status": "swept", "swept_at": _utcnow_iso()}
        ).eq("id", int(zone_id)).execute()
        return True
    except Exception as e:  # noqa: BLE001
        log.error("sweep_zone(%s) failed: %s", zone_id, e)
        return False


def invalidate_zone(zone_id: int) -> bool:
    if not is_connected():
        return False
    try:
        supabase.table("zones").update({"status": "invalidated"}).eq(
            "id", int(zone_id)
        ).execute()
        return True
    except Exception as e:  # noqa: BLE001
        log.error("invalidate_zone(%s) failed: %s", zone_id, e)
        return False


def get_active_zones(symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    if not is_connected():
        return []
    try:
        q = supabase.table("zones").select("*").eq("status", "active")
        if symbol:
            q = q.eq("symbol", symbol)
        res = q.execute()
        return list(res.data or [])
    except Exception as e:  # noqa: BLE001
        log.error("get_active_zones failed: %s", e)
        return []


def prune_old_zones(days: int = 7) -> int:
    """Delete inactive (swept/invalidated) zones older than `days` days."""
    if not is_connected():
        return 0
    try:
        from datetime import timedelta
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=days)
        ).isoformat()
        res = (
            supabase.table("zones")
            .delete()
            .neq("status", "active")
            .lt("created_at", cutoff)
            .execute()
        )
        return len(res.data or [])
    except Exception as e:  # noqa: BLE001
        log.error("prune_old_zones failed: %s", e)
        return 0


# ---------------------------------------------------------------------------
# BOT_STATE  (key/value JSONB)
# ---------------------------------------------------------------------------

def get_bot_state(key: str, default: Any = None) -> Any:
    if not is_connected():
        return default
    try:
        res = (
            supabase.table("bot_state")
            .select("value")
            .eq("key", key)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        if not rows:
            return default
        return rows[0].get("value", default)
    except Exception as e:  # noqa: BLE001
        log.error("get_bot_state(%s) failed: %s", key, e)
        return default


def set_bot_state(key: str, value: Any) -> bool:
    if not is_connected():
        return False
    try:
        supabase.table("bot_state").upsert(
            {"key": key, "value": value}, on_conflict="key"
        ).execute()
        return True
    except Exception as e:  # noqa: BLE001
        log.error("set_bot_state(%s) failed: %s", key, e)
        return False


def set_bot_state_bulk(items: Dict[str, Any]) -> bool:
    """Upsert many keys at once (single round-trip)."""
    if not is_connected() or not items:
        return False
    payload = [{"key": k, "value": v} for k, v in items.items()]
    try:
        supabase.table("bot_state").upsert(payload, on_conflict="key").execute()
        return True
    except Exception as e:  # noqa: BLE001
        log.error("set_bot_state_bulk failed: %s", e)
        return False


def get_all_bot_state() -> Dict[str, Any]:
    if not is_connected():
        return {}
    try:
        res = supabase.table("bot_state").select("key,value").execute()
        return {r["key"]: r.get("value") for r in (res.data or [])}
    except Exception as e:  # noqa: BLE001
        log.error("get_all_bot_state failed: %s", e)
        return {}
