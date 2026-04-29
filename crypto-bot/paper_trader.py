"""Paper trading journal: persists open trades + closes them on TP/SL hit."""
import json
import os
from datetime import datetime, timezone
from typing import List, Dict, Any

from config import TRADES_FILE
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


def open_trade(signal: Dict[str, Any]) -> bool:
    """Open a paper trade if no open trade exists for this symbol/direction."""
    trades = _load()
    for t in trades:
        if (
            t["symbol"] == signal["symbol"]
            and t["direction"] == signal["direction"]
            and t["status"] == "open"
        ):
            return False
    record = {
        **signal,
        "status": "open",
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "closed_at": None,
        "exit_price": None,
        "pnl_pct": None,
        "result": None,
    }
    trades.append(record)
    _save(trades)
    log.info("Opened paper trade: %s %s @ %s", signal["symbol"], signal["direction"], signal["entry"])
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
        hit_tp = last_high >= tp if t["direction"] == "long" else last_low <= tp
        hit_sl = last_low <= sl if t["direction"] == "long" else last_high >= sl
        if hit_tp and hit_sl:
            # ambiguous within candle — assume SL hit first (conservative)
            hit_tp = False
        if hit_tp or hit_sl:
            exit_price = tp if hit_tp else sl
            if t["direction"] == "long":
                pnl = (exit_price - entry) / entry * 100
            else:
                pnl = (entry - exit_price) / entry * 100
            t["status"] = "closed"
            t["exit_price"] = exit_price
            t["closed_at"] = datetime.now(timezone.utc).isoformat()
            t["pnl_pct"] = round(pnl, 3)
            t["result"] = "win" if hit_tp else "loss"
            closed.append(t)
            changed = True
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
    win_rate = len(wins) / len(closed) * 100
    avg_win = (sum(t["pnl_pct"] for t in wins) / len(wins)) if wins else 0
    avg_loss = (sum(t["pnl_pct"] for t in losses) / len(losses)) if losses else 0
    return (
        "📓 <b>Paper journal</b>\n"
        f"Closed: {len(closed)} | Open: {open_n}\n"
        f"Win rate: {win_rate:.1f}%\n"
        f"Total P&L: {total_pnl:+.2f}%\n"
        f"Avg win: {avg_win:+.2f}% | Avg loss: {avg_loss:+.2f}%"
    )
