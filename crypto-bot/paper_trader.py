"""Paper trading journal with partial take-profits and trailing stop.

State is persisted to Supabase (trades table) + local JSON fallback.
Partial TP levels: close 50% at 2R, 25% at 3R, trail remaining 25%.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from config import (
    ACCOUNT_EQUITY, RISK_PER_TRADE,
    MAX_OPEN_TRADES,
    TRADES_FILE,
    PARTIAL_TP_ENABLED, PARTIAL_TP_LEVELS,
)
import runtime_settings
from trailing_stop import update_trailing_sl, clear_trade as _clear_trail
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
    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        return 0.0
    risk_pct = runtime_settings.get_risk_per_trade_fraction()
    risk_amount = ACCOUNT_EQUITY * risk_pct * max(0.0, min(1.0, weight))
    return risk_amount / stop_distance


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def _load_local() -> List[Dict[str, Any]]:
    if not os.path.exists(TRADES_FILE):
        return []
    try:
        with open(TRADES_FILE) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return []


def _save_local(trades: List[Dict[str, Any]]) -> None:
    try:
        with open(TRADES_FILE, "w") as f:
            json.dump(trades, f, default=str, indent=2)
    except Exception as e:  # noqa: BLE001
        log.error("Local trade save failed: %s", e)


def _sync_local(trade: Dict[str, Any]) -> None:
    trades = _load_local()
    for i, t in enumerate(trades):
        if str(t.get("id")) == str(trade.get("id")):
            trades[i] = trade
            _save_local(trades)
            return
    trades.append(trade)
    _save_local(trades)


# ---------------------------------------------------------------------------
# Open trade
# ---------------------------------------------------------------------------

def open_trade(
    signal: Dict[str, Any],
    pair_weight: float = 1.0,
    pair_zone_id: Optional[int] = None,
) -> bool:
    """Open a paper trade if no open trade exists for this symbol/direction."""
    if pair_weight <= 0:
        return False

    symbol    = signal["symbol"]
    direction = signal["direction"]

    # Avoid duplicate open trade on the same symbol+direction
    for t in get_open_trades(symbol):
        if t.get("direction") == direction:
            return False

    entry = float(signal["entry"])
    stop  = float(signal["stop_loss"])
    tp    = float(signal["take_profit"])
    size  = _position_size(entry, stop, weight=pair_weight)
    notional_at_entry = size * entry
    risked_usd = ACCOUNT_EQUITY * runtime_settings.get_risk_per_trade_fraction() * pair_weight

    notes_payload: Dict[str, Any] = {
        k: signal.get(k)
        for k in (
            "sweep_confirmed", "displacement", "shift_label",
            "premium_discount", "rr", "atr_pct", "vol_ratio",
            "session", "htf_bias", "mode", "size_mult",
        )
        if signal.get(k) is not None
    }

    # Track partial TP state in notes
    notes_payload["partial_closes"] = []
    notes_payload["remaining_lots"] = round(size, 8)
    notes_payload["original_sl"]    = stop
    notes_payload["original_tp"]    = tp

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

    # Mirror to local JSON
    _sync_local({**signal, "id": trade_id, "status": "open",
                 "entry_price": entry, "stop_loss": stop, "take_profit": tp,
                 "position_size": round(size, 8), "pair_weight": round(pair_weight, 4),
                 "notes": notes_payload})
    log.info(
        "Opened trade #%s: %s %s @ %s size=%.6f weight=%.2f risk=%.2f USDT",
        trade_id, symbol, direction, entry, size, pair_weight, risked_usd,
    )
    return True


# ---------------------------------------------------------------------------
# Price tick — partial TPs + trailing SL
# ---------------------------------------------------------------------------

def update_trades_with_price(
    symbol: str,
    last_high: float,
    last_low: float,
) -> List[Dict[str, Any]]:
    """Process one price tick for all open trades on `symbol`.

    Checks partial TPs and trailing SL first, then full SL/TP.
    Returns list of fully closed trade records.
    """
    if not is_connected():
        return []
    closed: List[Dict[str, Any]] = []
    for t in get_open_trades(symbol):
        result = _tick_trade(t, last_high, last_low)
        if result:
            closed.append(result)
    return closed


def _tick_trade(
    t: Dict[str, Any],
    last_high: float,
    last_low: float,
) -> Optional[Dict[str, Any]]:
    """Process one open trade tick. Returns closed record or None."""
    entry     = float(t.get("entry_price") or t.get("entry") or 0)
    sl        = float(t.get("stop_loss") or 0)
    tp        = float(t.get("take_profit") or 0)
    direction = t.get("direction", "long")
    size      = float(t.get("position_size") or 0.0)
    notes     = t.get("notes") or {}
    partial_closes = list(notes.get("partial_closes") or [])
    remaining_lots = float(notes.get("remaining_lots") or size)
    risk      = abs(entry - sl)
    trade_id  = int(t["id"])
    last_price = last_high if direction == "long" else last_low

    # ── Partial TPs ─────────────────────────────────────────────────────────
    if PARTIAL_TP_ENABLED and risk > 0 and remaining_lots > 0:
        closed_r = {pc["r_mult"] for pc in partial_closes}
        for r_mult, fraction in PARTIAL_TP_LEVELS:
            if r_mult in closed_r:
                continue
            target = (entry + r_mult * risk) if direction == "long" else (entry - r_mult * risk)
            hit = (last_high >= target) if direction == "long" else (last_low <= target)
            if not hit:
                continue
            close_lots = round(size * fraction, 8)
            close_lots = min(close_lots, remaining_lots)
            pnl_per_lot = (target - entry) if direction == "long" else (entry - target)
            pnl = pnl_per_lot * close_lots
            pc_record = {
                "r_mult":    r_mult, "fraction": fraction,
                "price":     target, "lots":     close_lots,
                "pnl":       round(pnl, 4),
                "closed_at": datetime.now(tz=timezone.utc).isoformat(),
            }
            partial_closes.append(pc_record)
            remaining_lots = round(remaining_lots - close_lots, 8)
            notes["partial_closes"] = partial_closes
            notes["remaining_lots"] = remaining_lots
            log.info(
                "PARTIAL TP %.1fR %s %s: closed %.6f lots @ %.5f pnl=%.4f",
                r_mult, t["symbol"], direction, close_lots, target, pnl
            )
            _notify_partial_tp(t, pc_record)
            # Persist updated notes
            _supa_update_notes(trade_id, notes)
            if remaining_lots <= 0:
                total_pnl = sum(p["pnl"] for p in partial_closes)
                _supa_close_trade(trade_id, "closed", target,
                                  round(total_pnl, 4), 0.0, "win")
                _clear_trail(str(trade_id))
                return {**t, "status": "closed", "pnl": total_pnl,
                        "pnl_usd": total_pnl, "result": "win",
                        "exit_price": target, "pnl_pct": pnl / (entry * size) * 100 if entry * size > 0 else 0}

    # ── Trailing SL ─────────────────────────────────────────────────────────
    new_sl, trail_reason = update_trailing_sl(t, last_high, last_low)
    if new_sl is not None:
        log.info("TRAIL SL %s %s: %.5f → %.5f (%s)",
                 t["symbol"], direction, sl, new_sl, trail_reason)
        _supa_update_sl(trade_id, new_sl)
        t["stop_loss"] = new_sl
        sl = new_sl
        _notify_trail(t, new_sl, trail_reason)

    # ── SL hit ──────────────────────────────────────────────────────────────
    hit_sl = (last_low  <= sl) if direction == "long" else (last_high >= sl)
    # ── TP hit ──────────────────────────────────────────────────────────────
    hit_tp = (last_high >= tp) if direction == "long" else (last_low  <= tp)

    if hit_tp and hit_sl:
        hit_tp = False  # conservative: SL first within candle

    if not (hit_tp or hit_sl):
        return None

    exit_price = tp if hit_tp else sl
    pnl_per_lot = (exit_price - entry) if direction == "long" else (entry - exit_price)
    realized_partial = sum(p.get("pnl", 0) for p in partial_closes)
    remaining_pnl = pnl_per_lot * remaining_lots
    pnl_usd = round(realized_partial + remaining_pnl, 4)
    pnl_pct = round(pnl_usd / (entry * size) * 100, 3) if entry * size > 0 else 0.0
    result  = "win" if hit_tp else "loss"

    ok = _supa_close_trade(trade_id, "closed", float(exit_price), pnl_usd, pnl_pct, result)
    if not ok:
        log.error("Failed to close trade #%s in Supabase.", trade_id)
        return None

    _clear_trail(str(trade_id))
    closed_record = {
        **t, "status": "closed",
        "exit_price": exit_price,
        "pnl": pnl_usd, "pnl_usd": pnl_usd, "pnl_pct": pnl_pct,
        "result": result,
        "closed_at": datetime.now(timezone.utc).isoformat(),
        "entry": entry, "direction": direction, "symbol": t["symbol"],
    }
    log.info(
        "Closed #%s %s %s: exit=%.5f pnl=%+.2f%% (%+.4f USD) result=%s",
        trade_id, t["symbol"], direction, exit_price, pnl_pct, pnl_usd, result,
    )
    return closed_record


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def _supa_update_notes(trade_id: int, notes: Dict[str, Any]) -> None:
    try:
        from supabase_client import _client  # noqa: WPS433
        _client().table("trades").update({"notes": notes}).eq("id", trade_id).execute()
    except Exception as e:  # noqa: BLE001
        log.warning("update notes failed %s: %s", trade_id, e)


def _supa_update_sl(trade_id: int, new_sl: float) -> None:
    try:
        from supabase_client import _client  # noqa: WPS433
        _client().table("trades").update({"stop_loss": new_sl}).eq("id", trade_id).execute()
    except Exception as e:  # noqa: BLE001
        log.warning("update sl failed %s: %s", trade_id, e)


# ---------------------------------------------------------------------------
# Notification helpers
# ---------------------------------------------------------------------------

def _notify_partial_tp(trade: Dict[str, Any], pc: Dict[str, Any]) -> None:
    try:
        from telegram_bot import send_message  # noqa: WPS433
        d = "📈" if trade.get("direction") == "long" else "📉"
        send_message(
            f"{d} <b>Partial TP {pc['r_mult']}R — {trade['symbol']}</b>\n"
            f"Closed {int(pc['fraction'] * 100)}% @ {pc['price']:.5f}\n"
            f"P&L: {'+' if pc['pnl'] >= 0 else ''}{pc['pnl']:.4f}"
        )
    except Exception:  # noqa: BLE001
        pass


def _notify_trail(trade: Dict[str, Any], new_sl: float, reason: str) -> None:
    try:
        from telegram_bot import send_message  # noqa: WPS433
        send_message(
            f"🔒 <b>Trail SL — {trade['symbol']}</b>\n"
            f"SL → {new_sl:.5f}  ({reason})"
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def daily_summary() -> str:
    if not is_connected():
        return "📓 Paper journal: Supabase unavailable."
    today = get_trades_today()
    closed = [t for t in today if t.get("status") == "closed"]
    open_n = sum(1 for t in today if t.get("status") == "open")
    if not closed:
        return f"📓 Paper journal (today): 0 closed trades, {open_n} open."
    wins   = [t for t in closed if t.get("result") == "win"]
    losses = [t for t in closed if t.get("result") == "loss"]
    total_pnl_usd = sum((t.get("pnl") or 0.0) for t in closed)
    total_pnl_pct = sum((t.get("pnl_pct") or 0.0) for t in closed)
    win_rate = len(wins) / len(closed) * 100
    avg_win  = (sum(t.get("pnl_pct") or 0.0 for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t.get("pnl_pct") or 0.0 for t in losses) / len(losses)) if losses else 0.0
    return (
        "📓 <b>Paper journal — today</b>\n"
        f"Closed: {len(closed)} | Open: {open_n}\n"
        f"Win rate: {win_rate:.1f}%\n"
        f"Total P&L: {total_pnl_pct:+.2f}% ({total_pnl_usd:+.2f} USD)\n"
        f"Avg win: {avg_win:+.2f}% | Avg loss: {avg_loss:+.2f}%"
    )


def performance_window(hours: int) -> Dict[str, Any]:
    if not is_connected():
        return {"trades": 0, "win_rate": 0.0, "pnl_pct": 0.0, "pnl_usd": 0.0}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    if hours >= 24 * 365:
        rows = get_all_closed_trades()
    else:
        rows = get_closed_trades_since(cutoff.isoformat())
    closed = [t for t in rows if _parse_iso(t.get("closed_at")) is not None and
              (_parse_iso(t.get("closed_at")) or cutoff) >= cutoff]
    if not closed:
        return {"trades": 0, "win_rate": 0.0, "pnl_pct": 0.0, "pnl_usd": 0.0}
    wins = [t for t in closed if t.get("result") == "win"]
    return {
        "trades":   len(closed),
        "win_rate": round(len(wins) / len(closed) * 100, 1),
        "pnl_pct":  round(sum((t.get("pnl_pct") or 0.0) for t in closed), 2),
        "pnl_usd":  round(sum((t.get("pnl") or 0.0) for t in closed), 2),
    }


def open_trades_count() -> int:
    if not is_connected():
        return 0
    return len(get_open_trades())
