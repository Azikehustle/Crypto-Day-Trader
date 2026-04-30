"""Paper trading journal — backed by Supabase (`trades` table).

Includes position sizing (ACCOUNT_EQUITY × RISK_PER_TRADE / stop distance) and
notional P&L tracking on close. Equity is tracked by the risk manager.

All state lives in Supabase — no local JSON files.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from config import ACCOUNT_EQUITY, RISK_PER_TRADE
from logger_setup import get_logger
from supabase_client import (
    insert_trade,
    close_trade as _supa_close_trade,
    get_open_trades,
    get_trades_today,
    get_closed_trades_since,
    get_all_closed_trades,
    is_connected,
)

log = get_logger("paper")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _position_size(entry: float, stop: float, weight: float = 1.0) -> float:
    """Risk-based position size in base units."""
    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        return 0.0
    risk_amount = ACCOUNT_EQUITY * RISK_PER_TRADE * max(0.0, min(1.0, weight))
    return risk_amount / stop_distance


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def open_trade(
    signal: Dict[str, Any],
    pair_weight: float = 1.0,
    pair_zone_id: Optional[int] = None,
) -> bool:
    """Open a paper trade if no open trade exists for this symbol/direction."""
    if pair_weight <= 0:
        return False
    if not is_connected():
        log.warning("Supabase unavailable — skipping open_trade.")
        return False

    symbol = signal["symbol"]
    direction = signal["direction"]

    # Avoid duplicate open trade on the same symbol+direction
    for t in get_open_trades(symbol):
        if t.get("direction") == direction:
            return False

    entry = float(signal["entry"])
    stop = float(signal["stop_loss"])
    tp = float(signal["take_profit"])
    size = _position_size(entry, stop, weight=pair_weight)
    notional_at_entry = size * entry
    risked_usd = ACCOUNT_EQUITY * RISK_PER_TRADE * pair_weight

    notes_payload: Dict[str, Any] = {
        k: signal.get(k)
        for k in (
            "sweep_confirmed",
            "displacement",
            "shift_label",
            "premium_discount",
            "rr",
            "atr_pct",
            "vol_ratio",
            "session",
            "htf_bias",
        )
        if signal.get(k) is not None
    }

    trade_id = insert_trade(
        symbol=symbol,
        direction=direction,
        entry_price=entry,
        stop_loss=stop,
        take_profit=tp,
        score=int(signal.get("score") or 0),
        confidence=str(signal.get("confidence") or ""),
        pair_zone_id=pair_zone_id,
        notes=notes_payload or None,
        position_size=round(size, 8),
        pair_weight=round(pair_weight, 4),
        notional_at_entry=round(notional_at_entry, 4),
        risked_usd=round(risked_usd, 4),
    )
    if trade_id is None:
        log.error("insert_trade returned no id; aborting open_trade.")
        return False
    log.info(
        "Opened trade #%s: %s %s @ %s size=%.6f weight=%.2f risk=%.2f USDT",
        trade_id, symbol, direction, entry, size, pair_weight, risked_usd,
    )
    return True


def update_trades_with_price(
    symbol: str, last_high: float, last_low: float
) -> List[Dict[str, Any]]:
    """Mark trades as won/lost based on whether SL or TP was touched."""
    if not is_connected():
        return []
    closed: List[Dict[str, Any]] = []
    for t in get_open_trades(symbol):
        entry = float(t["entry_price"])
        sl = float(t["stop_loss"])
        tp = float(t["take_profit"])
        size = float(t.get("position_size") or 0.0)
        direction = t["direction"]
        hit_tp = last_high >= tp if direction == "long" else last_low <= tp
        hit_sl = last_low <= sl if direction == "long" else last_high >= sl
        if hit_tp and hit_sl:
            # ambiguous within candle — assume SL hit first (conservative)
            hit_tp = False
        if not (hit_tp or hit_sl):
            continue
        exit_price = tp if hit_tp else sl
        if direction == "long":
            pnl_pct = (exit_price - entry) / entry * 100
            pnl_usd = (exit_price - entry) * size
        else:
            pnl_pct = (entry - exit_price) / entry * 100
            pnl_usd = (entry - exit_price) * size
        result = "win" if hit_tp else "loss"
        ok = _supa_close_trade(
            trade_id=int(t["id"]),
            status="closed",
            exit_price=float(exit_price),
            pnl=round(pnl_usd, 4),
            pnl_pct=round(pnl_pct, 3),
            result=result,
        )
        if not ok:
            log.error("Failed to close trade #%s in Supabase.", t.get("id"))
            continue
        closed_record = {
            **t,
            "status": "closed",
            "exit_price": exit_price,
            "pnl": round(pnl_usd, 4),
            "pnl_usd": round(pnl_usd, 4),
            "pnl_pct": round(pnl_pct, 3),
            "result": result,
            "closed_at": datetime.now(timezone.utc).isoformat(),
            # Compatibility aliases for downstream consumers
            "entry": entry,
            "direction": direction,
            "symbol": t["symbol"],
        }
        closed.append(closed_record)
        log.info(
            "Closed #%s %s %s: exit=%.4f pnl=%+.2f%% (%+.2f USDT) result=%s",
            t.get("id"), t["symbol"], direction, exit_price,
            pnl_pct, pnl_usd, result,
        )
    return closed


def daily_summary() -> str:
    """Plain text summary of today's trades (today = since 00:00 UTC)."""
    if not is_connected():
        return "📓 Paper journal: Supabase unavailable."
    today = get_trades_today()
    closed = [t for t in today if t.get("status") == "closed"]
    open_n = sum(1 for t in today if t.get("status") == "open")
    if not closed:
        return f"📓 Paper journal (today): 0 closed trades, {open_n} open."
    wins = [t for t in closed if t.get("result") == "win"]
    losses = [t for t in closed if t.get("result") == "loss"]
    total_pnl_pct = sum((t.get("pnl_pct") or 0.0) for t in closed)
    total_pnl_usd = sum((t.get("pnl") or 0.0) for t in closed)
    win_rate = len(wins) / len(closed) * 100
    avg_win = (sum(t.get("pnl_pct") or 0.0 for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t.get("pnl_pct") or 0.0 for t in losses) / len(losses)) if losses else 0.0
    return (
        "📓 <b>Paper journal — today</b>\n"
        f"Closed: {len(closed)} | Open: {open_n}\n"
        f"Win rate: {win_rate:.1f}%\n"
        f"Total P&L: {total_pnl_pct:+.2f}% ({total_pnl_usd:+.2f} USDT)\n"
        f"Avg win: {avg_win:+.2f}% | Avg loss: {avg_loss:+.2f}%"
    )


def performance_window(hours: int) -> Dict[str, Any]:
    """Win rate / P&L over the last `hours` (closed trades only)."""
    if not is_connected():
        return {"trades": 0, "win_rate": 0.0, "pnl_pct": 0.0, "pnl_usd": 0.0}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    if hours >= 24 * 365:  # treat huge windows as "all-time"
        rows = get_all_closed_trades()
    else:
        rows = get_closed_trades_since(cutoff.isoformat())
    closed: List[Dict[str, Any]] = []
    for t in rows:
        ts = _parse_iso(t.get("closed_at"))
        if ts is not None and ts >= cutoff:
            closed.append(t)
    if not closed:
        return {"trades": 0, "win_rate": 0.0, "pnl_pct": 0.0, "pnl_usd": 0.0}
    wins = [t for t in closed if t.get("result") == "win"]
    return {
        "trades": len(closed),
        "win_rate": round(len(wins) / len(closed) * 100, 1),
        "pnl_pct": round(sum((t.get("pnl_pct") or 0.0) for t in closed), 2),
        "pnl_usd": round(sum((t.get("pnl") or 0.0) for t in closed), 2),
    }


def open_trades_count() -> int:
    if not is_connected():
        return 0
    return len(get_open_trades())
