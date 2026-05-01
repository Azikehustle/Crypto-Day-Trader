"""Trailing stop loss manager.

Tracks each open trade's price progression and moves the SL
in stages as the trade moves in its favour.

Stages (configurable via config):
  1. At 1.5R  → move SL to breakeven
  2. At 2.0R  → activate trailing (0.5R distance from price extreme)
  3. At 3.0R  → tighten trail (0.3R distance from price extreme)

SL only ever moves in the favourable direction.
"""
from __future__ import annotations

from typing import Dict, Any, Optional, Tuple

from config import (
    BREAKEVEN_R, TRAILING_ACTIVATE_R, TRAILING_DISTANCE_R,
    TRAILING_TIGHTEN_R, TRAILING_TIGHT_DISTANCE_R,
)
from logger_setup import get_logger

log = get_logger("trail")

# In-memory extreme tracker per trade_id
# { trade_id: {"extreme": float, "stage": int} }
_TRADE_EXTREMES: Dict[str, Dict[str, Any]] = {}


def _risk(trade: Dict[str, Any]) -> float:
    entry = float(trade.get("entry_price") or trade.get("entry") or 0)
    sl = float(trade.get("stop_loss") or 0)
    return abs(entry - sl)


def update_trailing_sl(
    trade: Dict[str, Any],
    current_high: float,
    current_low: float,
) -> Tuple[Optional[float], str]:
    """Check whether the trailing stop should move.

    Returns (new_sl, reason) if SL should be updated, or (None, "") otherwise.
    """
    trade_id = str(trade.get("id") or trade.get("trade_id") or "")
    direction = trade.get("direction", "long")
    entry = float(trade.get("entry_price") or trade.get("entry") or 0)
    current_sl = float(trade.get("stop_loss") or 0)
    risk = _risk(trade)
    if risk <= 0 or entry <= 0:
        return None, ""

    # Track price extreme
    rec = _TRADE_EXTREMES.setdefault(trade_id, {"extreme": entry, "stage": 0})
    if direction == "long":
        rec["extreme"] = max(rec["extreme"], current_high)
    else:
        rec["extreme"] = min(rec["extreme"], current_low)

    extreme = rec["extreme"]
    fav_move = (extreme - entry) if direction == "long" else (entry - extreme)
    r_multiple = fav_move / risk if risk > 0 else 0.0

    new_sl = None
    reason = ""

    if r_multiple >= TRAILING_TIGHTEN_R:
        # Stage 3: tight trail
        distance = TRAILING_TIGHT_DISTANCE_R * risk
        candidate = (extreme - distance) if direction == "long" else (extreme + distance)
        if direction == "long" and candidate > current_sl:
            new_sl, reason = candidate, f"tight trail at {r_multiple:.1f}R"
        elif direction == "short" and candidate < current_sl:
            new_sl, reason = candidate, f"tight trail at {r_multiple:.1f}R"
        rec["stage"] = 3

    elif r_multiple >= TRAILING_ACTIVATE_R:
        # Stage 2: standard trail
        distance = TRAILING_DISTANCE_R * risk
        candidate = (extreme - distance) if direction == "long" else (extreme + distance)
        if direction == "long" and candidate > current_sl:
            new_sl, reason = candidate, f"trail at {r_multiple:.1f}R"
        elif direction == "short" and candidate < current_sl:
            new_sl, reason = candidate, f"trail at {r_multiple:.1f}R"
        rec["stage"] = max(rec["stage"], 2)

    elif r_multiple >= BREAKEVEN_R and rec["stage"] < 1:
        # Stage 1: breakeven
        if direction == "long" and entry > current_sl:
            new_sl, reason = entry, "breakeven at 1.5R"
            rec["stage"] = 1
        elif direction == "short" and entry < current_sl:
            new_sl, reason = entry, "breakeven at 1.5R"
            rec["stage"] = 1

    return new_sl, reason


def clear_trade(trade_id: str) -> None:
    """Remove tracking state when a trade closes."""
    _TRADE_EXTREMES.pop(str(trade_id), None)


def clear_all() -> None:
    _TRADE_EXTREMES.clear()
