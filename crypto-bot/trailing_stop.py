"""Trailing stop manager for open paper trades.

R-multiple milestones:
  BREAKEVEN_R            (default 1.5R) → move SL to entry (breakeven)
  TRAILING_ACTIVATE_R    (default 2.0R) → start trailing at TRAILING_DISTANCE_R
  TRAILING_TIGHTEN_R     (default 3.0R) → tighten trail to TRAILING_TIGHT_DISTANCE_R

SL only ever moves in the favourable direction. Sends Telegram alert on each
SL update. Integrates with paper_trader and supabase_client.
"""
from __future__ import annotations

import threading
from typing import Dict, Any, Optional

from logger_setup import get_logger
from telegram_bot import send_message

log = get_logger("trailing")

# ---------------------------------------------------------------------------
# Config (can be overridden via env vars)
# ---------------------------------------------------------------------------
import os

BREAKEVEN_R: float = float(os.getenv("BREAKEVEN_R", "1.5"))
TRAILING_ACTIVATE_R: float = float(os.getenv("TRAILING_ACTIVATE_R", "2.0"))
TRAILING_DISTANCE_R: float = float(os.getenv("TRAILING_DISTANCE_R", "0.5"))
TRAILING_TIGHTEN_R: float = float(os.getenv("TRAILING_TIGHTEN_R", "3.0"))
TRAILING_TIGHT_DISTANCE_R: float = float(os.getenv("TRAILING_TIGHT_DISTANCE_R", "0.3"))

# ---------------------------------------------------------------------------
# In-memory SL tracker keyed by trade_id
# ---------------------------------------------------------------------------

_lock = threading.Lock()

# trade_id → {"sl": float, "be_triggered": bool, "trail_activated": bool, "tightened": bool}
_trail_state: Dict[Any, Dict[str, Any]] = {}


def _initial_r(trade: Dict[str, Any]) -> float:
    """Return the initial risk (R) in price units for a trade."""
    entry = float(trade.get("entry_price") or trade.get("entry") or 0.0)
    sl = float(trade.get("stop_loss") or 0.0)
    return abs(entry - sl)


def _current_r(trade: Dict[str, Any], current_price: float) -> float:
    """Return how many R's price has moved in favour of the trade."""
    entry = float(trade.get("entry_price") or trade.get("entry") or 0.0)
    direction = (trade.get("direction") or "long").lower()
    r_unit = _initial_r(trade)
    if r_unit <= 0:
        return 0.0
    if direction == "long":
        return (current_price - entry) / r_unit
    else:
        return (entry - current_price) / r_unit


def _sl_state(trade_id: Any) -> Dict[str, Any]:
    with _lock:
        return _trail_state.setdefault(trade_id, {
            "sl": None,
            "be_triggered": False,
            "trail_activated": False,
            "tightened": False,
        })


def _update_sl_state(trade_id: Any, **kwargs) -> None:
    with _lock:
        _trail_state.setdefault(trade_id, {}). update(kwargs)


def _notify_sl_update(trade: Dict[str, Any], new_sl: float, reason: str) -> None:
    symbol = trade.get("symbol", "?")
    direction = (trade.get("direction") or "long").upper()
    entry = float(trade.get("entry_price") or trade.get("entry") or 0.0)
    msg = (
        f"🔄 <b>Oracle_v5 SL Updated</b>\n"
        f"<code>{symbol}</code> {direction} @ {entry:.5f}\n"
        f"New SL: <code>{new_sl:.5f}</code>\n"
        f"Reason: {reason}"
    )
    try:
        send_message(msg)
    except Exception as e:  # noqa: BLE001
        log.warning("notify_sl_update send failed: %s", e)


def process_trade(
    trade: Dict[str, Any],
    current_price: float,
) -> Optional[float]:
    """Evaluate trailing-stop logic for a single trade at `current_price`.

    Returns the new SL value if it changed, else None.
    Applies SL updates to paper_trader + supabase in-place.
    """
    trade_id = trade.get("id") or id(trade)
    direction = (trade.get("direction") or "long").lower()
    entry = float(trade.get("entry_price") or trade.get("entry") or 0.0)
    orig_sl = float(trade.get("stop_loss") or 0.0)
    r_unit = _initial_r(trade)

    if r_unit <= 0 or entry <= 0:
        return None

    state = _sl_state(trade_id)
    current_sl = state["sl"] if state["sl"] is not None else orig_sl
    current_r_multiple = _current_r(trade, current_price)
    new_sl = current_sl
    reason: Optional[str] = None

    # ----------------------------------------------------------------
    # Stage 1: Breakeven at BREAKEVEN_R
    # ----------------------------------------------------------------
    if not state["be_triggered"] and current_r_multiple >= BREAKEVEN_R:
        be_sl = entry  # move SL to entry
        if direction == "long" and be_sl > current_sl:
            new_sl = be_sl
            reason = f"Breakeven @ {BREAKEVEN_R}R"
        elif direction == "short" and be_sl < current_sl:
            new_sl = be_sl
            reason = f"Breakeven @ {BREAKEVEN_R}R"
        if reason:
            _update_sl_state(trade_id, sl=new_sl, be_triggered=True)

    # ----------------------------------------------------------------
    # Stage 2: Trailing stop activation at TRAILING_ACTIVATE_R
    # ----------------------------------------------------------------
    if current_r_multiple >= TRAILING_ACTIVATE_R:
        if not state["trail_activated"]:
            _update_sl_state(trade_id, trail_activated=True)

        # Choose trail distance (tighten at TRAILING_TIGHTEN_R)
        if current_r_multiple >= TRAILING_TIGHTEN_R:
            if not state["tightened"]:
                _update_sl_state(trade_id, tightened=True)
            trail_dist = TRAILING_TIGHT_DISTANCE_R * r_unit
        else:
            trail_dist = TRAILING_DISTANCE_R * r_unit

        if direction == "long":
            trail_sl = current_price - trail_dist
            if trail_sl > new_sl:
                new_sl = trail_sl
                tighten_flag = current_r_multiple >= TRAILING_TIGHTEN_R
                reason = (
                    f"Trail tightened @ {TRAILING_TIGHTEN_R}R ({TRAILING_TIGHT_DISTANCE_R}R dist)"
                    if tighten_flag
                    else f"Trail activated @ {TRAILING_ACTIVATE_R}R ({TRAILING_DISTANCE_R}R dist)"
                )
        else:
            trail_sl = current_price + trail_dist
            if trail_sl < new_sl:
                new_sl = trail_sl
                tighten_flag = current_r_multiple >= TRAILING_TIGHTEN_R
                reason = (
                    f"Trail tightened @ {TRAILING_TIGHTEN_R}R ({TRAILING_TIGHT_DISTANCE_R}R dist)"
                    if tighten_flag
                    else f"Trail activated @ {TRAILING_ACTIVATE_R}R ({TRAILING_DISTANCE_R}R dist)"
                )

    if reason and new_sl != current_sl:
        _update_sl_state(trade_id, sl=new_sl)
        log.info(
            "TrailingStop %s %s: SL %s → %s (%s) [%.2fR]",
            trade.get("symbol"), direction, current_sl, new_sl, reason, current_r_multiple,
        )
        _notify_sl_update(trade, new_sl, reason)
        # Persist to supabase
        try:
            from supabase_client import is_connected
            if is_connected():
                from supabase_client import supabase as _supa
                _supa.table("trades").update(
                    {"stop_loss": new_sl}
                ).eq("id", int(trade_id)).execute()
        except Exception as e:  # noqa: BLE001
            log.warning("TrailingStop: supabase SL update failed: %s", e)
        return new_sl

    return None


def process_all_trades(
    open_trades: list,
    price_map: Dict[str, float],
) -> None:
    """Run trailing-stop logic for every open trade.

    Args:
        open_trades: list of trade dicts from supabase_client.get_open_trades()
        price_map:   {symbol: current_price}
    """
    for trade in open_trades:
        symbol = trade.get("symbol", "")
        price = price_map.get(symbol)
        if price is None:
            continue
        try:
            process_trade(trade, price)
        except Exception as e:  # noqa: BLE001
            log.error("TrailingStop error for %s: %s", symbol, e)


def clear_trade(trade_id: Any) -> None:
    """Remove trailing-stop state for a closed trade."""
    with _lock:
        _trail_state.pop(trade_id, None)
