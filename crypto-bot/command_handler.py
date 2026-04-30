"""Background Telegram command listener.

Runs in its own thread, long-polling Telegram for /commands and replying with
live state (HTF bias, open paper trades, running P&L, active zones).
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Any

import requests

from config import TELEGRAM_BOT_TOKEN, SYMBOLS, EXCHANGE
from logger_setup import get_logger
from telegram_bot import send_message
from paper_trader import daily_summary, performance_window
from supabase_client import get_open_trades
from risk_manager import get_risk_manager, in_quiet_hours

log = get_logger("commands")

# Shared state populated by main loop
_state_lock = threading.Lock()
_state: Dict[str, Any] = {
    "htf_bias": {},          # symbol -> "bullish"/"bearish"/"flat"
    "active_zones": {},      # symbol -> list of dicts {kind, high, low}
    "last_signal": {},       # symbol -> signal dict
    "last_loop_at": None,    # ISO timestamp of last successful pass
}


def update_state(**kwargs) -> None:
    with _state_lock:
        for k, v in kwargs.items():
            _state[k] = v


def update_symbol_state(symbol: str, **kwargs) -> None:
    with _state_lock:
        for k, v in kwargs.items():
            bucket = _state.setdefault(k, {})
            bucket[symbol] = v


def _snapshot() -> Dict[str, Any]:
    with _state_lock:
        return {
            "htf_bias": dict(_state["htf_bias"]),
            "active_zones": {k: list(v) for k, v in _state["active_zones"].items()},
            "last_signal": dict(_state["last_signal"]),
            "last_loop_at": _state["last_loop_at"],
        }


# ---- Command handlers --------------------------------------------------------

def _cmd_help() -> str:
    return (
        "<b>Commands</b>\n"
        "/status — HTF bias + last loop time + open trades\n"
        "/summary — paper-trade P&amp;L summary (all-time)\n"
        "/zones — active supply/demand zones per symbol\n"
        "/last — most recent signal per symbol\n"
        "/pnl — today's P&amp;L, equity, halt status\n"
        "/performance — win rate (7d / 30d / all-time)\n"
        "/resume — clear all halts and pair blocks\n"
        "/help — this message"
    )


def _cmd_status() -> str:
    snap = _snapshot()
    open_trades = get_open_trades()
    rs = get_risk_manager().status_dict()

    lines = ["<b>📡 Bot status</b>"]
    lines.append(f"Exchange: {EXCHANGE}")
    lines.append(f"Last loop: {snap['last_loop_at'] or 'pending'}")
    lines.append(f"Quiet hours: {'YES' if in_quiet_hours() else 'no'}")
    halt_emoji = "🛑" if rs["halt_signals"] else "🟢"
    lines.append(f"{halt_emoji} Halt: {rs['halt_reason'] or 'no'}")
    lines.append("")
    lines.append("<b>HTF bias</b>")
    for s in SYMBOLS:
        b = snap["htf_bias"].get(s, "?")
        emoji = {"bullish": "🟢", "bearish": "🔴", "flat": "⚪"}.get(b, "❔")
        lines.append(f"{emoji} {s}: {b}")
    lines.append("")
    lines.append(f"<b>Open paper trades:</b> {len(open_trades)}")
    for t in open_trades[:8]:
        entry = float(t.get("entry_price") or t.get("entry") or 0.0)
        sl = float(t.get("stop_loss") or 0.0)
        tp = float(t.get("take_profit") or 0.0)
        lines.append(
            f"• {t['symbol']} {t['direction'].upper()} @ {entry:.4f} "
            f"SL {sl:.4f} TP {tp:.4f}"
        )
    return "\n".join(lines)


def _cmd_summary() -> str:
    return daily_summary()


def _cmd_zones() -> str:
    snap = _snapshot()
    lines = ["<b>📦 Active zones</b>"]
    any_zone = False
    for s in SYMBOLS:
        zs = snap["active_zones"].get(s, [])
        if not zs:
            lines.append(f"• {s}: none nearby")
            continue
        any_zone = True
        lines.append(f"• <b>{s}</b>")
        for z in zs[-5:]:
            lines.append(
                f"   {z['kind']:6s} {z['low']:.4f} – {z['high']:.4f}"
            )
    if not any_zone:
        lines.append("(price isn't near any active zone right now)")
    return "\n".join(lines)


def _cmd_last() -> str:
    snap = _snapshot()
    if not snap["last_signal"]:
        return "No signals yet. Strict pipeline waiting for setups."
    lines = ["<b>🎯 Last signal per symbol</b>"]
    for s, sig in snap["last_signal"].items():
        score_max = sig.get("score_max", 15)
        lines.append(
            f"• {s} {sig['direction'].upper()} score {sig['score']}/{score_max} "
            f"@ {sig['entry']:.4f} ({sig['timestamp']})"
        )
    return "\n".join(lines)


def _cmd_pnl() -> str:
    rs = get_risk_manager().status_dict()
    open_n = len(get_open_trades())
    halt = "🛑 HALTED" if rs["halt_signals"] else "🟢 active"
    lines = [
        "<b>💰 P&amp;L</b>",
        f"Status: {halt}" + (f" ({rs['halt_reason']})" if rs["halt_reason"] else ""),
        f"Today P&amp;L: {rs['daily_pnl_usd']:+.2f} USDT",
        f"Lifetime P&amp;L: {rs['lifetime_realised_pnl_usd']:+.2f} USDT",
        f"Equity: {rs['running_equity']:.2f} USDT",
        f"Consecutive losses: {rs['consecutive_losses']}",
        f"Open trades: {open_n}",
    ]
    blocks = rs.get("pair_blocks") or {}
    cooldowns = rs.get("pair_cooldowns") or {}
    if blocks:
        lines.append("Blocked pairs: " + ", ".join(f"{k} until {v}" for k, v in blocks.items()))
    if cooldowns:
        lines.append("Cooldowns (50%): " + ", ".join(f"{k} until {v}" for k, v in cooldowns.items()))
    return "\n".join(lines)


def _cmd_performance() -> str:
    p7 = performance_window(7 * 24)
    p30 = performance_window(30 * 24)
    pall = performance_window(10 * 365 * 24)
    lines = [
        "<b>🏆 Performance</b>",
        f"7d:  {p7['trades']:>3} trades | win {p7['win_rate']:.1f}% | "
        f"P&amp;L {p7['pnl_pct']:+.2f}% ({p7['pnl_usd']:+.2f} USDT)",
        f"30d: {p30['trades']:>3} trades | win {p30['win_rate']:.1f}% | "
        f"P&amp;L {p30['pnl_pct']:+.2f}% ({p30['pnl_usd']:+.2f} USDT)",
        f"All: {pall['trades']:>3} trades | win {pall['win_rate']:.1f}% | "
        f"P&amp;L {pall['pnl_pct']:+.2f}% ({pall['pnl_usd']:+.2f} USDT)",
    ]
    return "\n".join(lines)


def _cmd_resume() -> str:
    msg = get_risk_manager().clear_halts()
    return "✅ " + msg


HANDLERS: Dict[str, Callable[[], str]] = {
    "/help": _cmd_help,
    "/start": _cmd_help,
    "/status": _cmd_status,
    "/summary": _cmd_summary,
    "/zones": _cmd_zones,
    "/last": _cmd_last,
    "/pnl": _cmd_pnl,
    "/performance": _cmd_performance,
    "/resume": _cmd_resume,
}


# ---- Long-poll loop ----------------------------------------------------------

def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"


def _process_update(update: Dict[str, Any]) -> None:
    msg = update.get("message") or update.get("channel_post") or {}
    text = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not text or chat_id is None:
        return

    # Strip "@botname" suffix from group commands
    cmd = text.split()[0].split("@")[0].lower()
    handler = HANDLERS.get(cmd)
    if not handler:
        return
    try:
        reply = handler()
    except Exception as e:  # noqa: BLE001
        log.error("command %s failed: %s", cmd, e)
        reply = f"⚠️ Command failed: {e}"
    send_message(reply, chat_id=str(chat_id))


def _poll_loop() -> None:
    if not TELEGRAM_BOT_TOKEN:
        log.warning("No TELEGRAM_BOT_TOKEN; command listener disabled.")
        return
    log.info("Telegram command listener started.")
    offset = 0
    # Drain any pending updates before starting (avoid replaying old commands)
    try:
        r = requests.get(_api("getUpdates"), params={"timeout": 0}, timeout=10)
        data = r.json()
        if data.get("ok") and data.get("result"):
            offset = data["result"][-1]["update_id"] + 1
    except Exception as e:  # noqa: BLE001
        log.warning("Initial getUpdates drain failed: %s", e)

    conflict_count = 0
    while True:
        try:
            r = requests.get(
                _api("getUpdates"),
                params={"timeout": 25, "offset": offset, "allowed_updates": "message"},
                timeout=35,
            )
            if r.status_code == 409:
                conflict_count += 1
                if conflict_count <= 3 or conflict_count % 30 == 0:
                    log.warning(
                        "Telegram 409 Conflict — another bot instance may be polling. "
                        "Continuing without command polling."
                    )
                # Back off harder; we keep send_message working but stop fighting.
                time.sleep(60)
                continue
            data = r.json()
            if not data.get("ok"):
                log.warning("getUpdates not ok: %s", data)
                time.sleep(5)
                continue
            conflict_count = 0
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                _process_update(upd)
        except requests.exceptions.ReadTimeout:
            continue
        except Exception as e:  # noqa: BLE001
            log.error("command poll error: %s", e)
            time.sleep(5)


def start_in_background() -> threading.Thread:
    t = threading.Thread(target=_poll_loop, name="telegram-commands", daemon=True)
    t.start()
    return t
