"""Paper trading journal: persists open trades + closes them on TP/SL hit.

Includes position sizing (ACCOUNT_EQUITY × RISK_PER_TRADE / stop distance) and
notional P&L tracking on close. Equity is also tracked in `risk_state.json`."""
import json
import os
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any

from config import (
    TRADES_FILE,
    ACCOUNT_EQUITY,
    RISK_PER_TRADE,
)
from logger_setup import get_logger

log = get_logger("paper")


def _load() -> List[Dict[str, Any]]:
    if not os.path.exists(TRADES_FILE):
        return []
    try:
        with open(TRADES_FILE) as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        log.error("Failed to load trades: %s", e)
        return []


def _save(trades: List[Dict[str, Any]]) -> None:
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2, default=str)


def _position_size(entry: float, stop: float, weight: float = 1.0) -> float:
    """Risk-based position size in base units (e.g. BTC).

    risk_amount = ACCOUNT_EQUITY × RISK_PER_TRADE × weight
    size       = risk_amount / |entry - stop|
    """
    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        return 0.0
    risk_amount = ACCOUNT_EQUITY * RISK_PER_TRADE * max(0.0, min(1.0, weight))
    return risk_amount / stop_distance


def open_trade(signal: Dict[str, Any], pair_weight: float = 1.0) -> bool:
    """Open a paper trade if no open trade exists for this symbol/direction.

    `pair_weight` (0.0..1.0) comes from the risk manager and scales position
    size (0.5 in cooldown, 0 in block). Caller is responsible for skipping
    when weight is 0.
    """
    if pair_weight <= 0:
        return False
    trades = _load()
    for t in trades:
        if (
            t["symbol"] == signal["symbol"]
            and t["direction"] == signal["direction"]
            and t["status"] == "open"
        ):
            return False
    entry = float(signal["entry"])
    stop = float(signal["stop_loss"])
    size = _position_size(entry, stop, weight=pair_weight)
    notional_at_entry = size * entry
    record = {
        **signal,
        "status": "open",
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "closed_at": None,
        "exit_price": None,
        "pnl_pct": None,
        "pnl_usd": None,
        "result": None,
        "position_size": round(size, 8),
        "pair_weight": round(pair_weight, 4),
        "notional_at_entry": round(notional_at_entry, 4),
        "risked_usd": round(ACCOUNT_EQUITY * RISK_PER_TRADE * pair_weight, 4),
    }
    trades.append(record)
    _save(trades)
    log.info(
        "Opened paper trade: %s %s @ %s size=%.6f weight=%.2f risk=%.2f USDT",
        signal["symbol"], signal["direction"], entry, size, pair_weight,
        record["risked_usd"],
    )
    return True


def update_trades_with_price(symbol: str, last_high: float, last_low: float) -> List[Dict[str, Any]]:
    """Mark trades as won/lost based on whether SL or TP was touched."""
    trades = _load()
    closed = []
    changed = False
    for t in trades:
        if t["symbol"] != symbol or t["status"] != "open":
            continue
        entry = float(t["entry"])
        sl = float(t["stop_loss"])
        tp = float(t["take_profit"])
        size = float(t.get("position_size") or 0.0)
        hit_tp = last_high >= tp if t["direction"] == "long" else last_low <= tp
        hit_sl = last_low <= sl if t["direction"] == "long" else last_high >= sl
        if hit_tp and hit_sl:
            # ambiguous within candle — assume SL hit first (conservative)
            hit_tp = False
        if hit_tp or hit_sl:
            exit_price = tp if hit_tp else sl
            if t["direction"] == "long":
                pnl_pct = (exit_price - entry) / entry * 100
                pnl_usd = (exit_price - entry) * size
            else:
                pnl_pct = (entry - exit_price) / entry * 100
                pnl_usd = (entry - exit_price) * size
            t["status"] = "closed"
            t["exit_price"] = exit_price
            t["closed_at"] = datetime.now(timezone.utc).isoformat()
            t["pnl_pct"] = round(pnl_pct, 3)
            t["pnl_usd"] = round(pnl_usd, 4)
            t["result"] = "win" if hit_tp else "loss"
            closed.append(t)
            changed = True
            log.info(
                "Closed %s %s: exit=%.4f pnl=%+.2f%% (%+.2f USDT) result=%s",
                t["symbol"], t["direction"], exit_price,
                pnl_pct, pnl_usd, t["result"],
            )
    if changed:
        _save(trades)
    return closed


def daily_summary() -> str:
    trades = _load()
    closed = [t for t in trades if t["status"] == "closed"]
    open_n = len([t for t in trades if t["status"] == "open"])
    if not closed:
        return f"📓 Paper journal: 0 closed trades, {open_n} open."
    wins = [t for t in closed if t["result"] == "win"]
    losses = [t for t in closed if t["result"] == "loss"]
    total_pnl = sum(t["pnl_pct"] for t in closed)
    total_pnl_usd = sum(float(t.get("pnl_usd") or 0.0) for t in closed)
    win_rate = len(wins) / len(closed) * 100
    avg_win = (sum(t["pnl_pct"] for t in wins) / len(wins)) if wins else 0
    avg_loss = (sum(t["pnl_pct"] for t in losses) / len(losses)) if losses else 0
    return (
        "📓 <b>Paper journal</b>\n"
        f"Closed: {len(closed)} | Open: {open_n}\n"
        f"Win rate: {win_rate:.1f}%\n"
        f"Total P&L: {total_pnl:+.2f}% ({total_pnl_usd:+.2f} USDT)\n"
        f"Avg win: {avg_win:+.2f}% | Avg loss: {avg_loss:+.2f}%"
    )


def performance_window(hours: int) -> Dict[str, Any]:
    """Win rate / P&L over the last `hours` (closed trades only)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    trades = _load()
    closed: List[Dict[str, Any]] = []
    for t in trades:
        if t.get("status") != "closed":
            continue
        ts = t.get("closed_at")
        try:
            dt = datetime.fromisoformat(ts) if ts else None
        except Exception:  # noqa: BLE001
            dt = None
        if dt is not None and dt >= cutoff:
            closed.append(t)
    if not closed:
        return {"trades": 0, "win_rate": 0.0, "pnl_pct": 0.0, "pnl_usd": 0.0}
    wins = [t for t in closed if t["result"] == "win"]
    return {
        "trades": len(closed),
        "win_rate": round(len(wins) / len(closed) * 100, 1),
        "pnl_pct": round(sum(t["pnl_pct"] for t in closed), 2),
        "pnl_usd": round(sum(float(t.get("pnl_usd") or 0.0) for t in closed), 2),
    }


def open_trades_count() -> int:
    return sum(1 for t in _load() if t.get("status") == "open")
