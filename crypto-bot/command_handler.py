"""Telegram command + inline-keyboard UI for the crypto bot.

Handles two streams from Telegram getUpdates:

1. ``message`` updates → /commands. Each command opens a "screen" — a
   message containing text plus an inline keyboard that the user navigates
   with button taps.

2. ``callback_query`` updates → button taps. We acknowledge the tap with
   answerCallbackQuery and edit the existing message in-place via
   editMessageText so the chat stays clean.

Multi-step flows (Add Pair) live in a small per-chat state dict.
Dangerous actions always go through a confirmation screen first.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

import runtime_settings
from config import (
    TELEGRAM_BOT_TOKEN,
    EXCHANGE,
    HTF_TIMEFRAME,
    ENTRY_TIMEFRAME,
    SCORE_THRESHOLD_SEND,
    SCORE_MAX,
    LOG_FILE,
)
from logger_setup import get_logger
from telegram_bot import (
    send_message,
    edit_message,
    answer_callback_query,
    should_listen,
    send_photo,
)
from paper_trader import daily_summary, performance_window
from supabase_client import get_open_trades
from risk_manager import get_risk_manager, in_quiet_hours

log = get_logger("commands")


# ---------------------------------------------------------------------------
# Shared state populated by main loop (HTF bias, zones, last signals)
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_state: Dict[str, Any] = {
    "htf_bias": {},
    "active_zones": {},
    "last_signal": {},
    "last_loop_at": None,
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


# ---------------------------------------------------------------------------
# Per-chat conversation state for multi-step flows (e.g. Add Pair)
# ---------------------------------------------------------------------------

_chat_state_lock = threading.Lock()
_chat_state: Dict[int, Dict[str, Any]] = {}


def _set_chat_state(chat_id: int, key: str, value: Any) -> None:
    with _chat_state_lock:
        _chat_state.setdefault(chat_id, {})[key] = value


def _pop_chat_state(chat_id: int, key: str) -> Any:
    with _chat_state_lock:
        return _chat_state.get(chat_id, {}).pop(key, None)


def _peek_chat_state(chat_id: int, key: str) -> Any:
    with _chat_state_lock:
        return _chat_state.get(chat_id, {}).get(key)


# ---------------------------------------------------------------------------
# Inline keyboard helpers
# ---------------------------------------------------------------------------

def _btn(label: str, data: str) -> Dict[str, str]:
    """Inline keyboard button with callback_data."""
    return {"text": label, "callback_data": data}


def _kb(rows: List[List[Dict[str, str]]]) -> Dict[str, Any]:
    return {"inline_keyboard": rows}


BACK_ROW = [_btn("🔙 Back to Menu", "nav:home")]
LEGEND = (
    "\nℹ️ <i>Blue = Safe · ⚠️ Orange = Changes Settings · 🔴 Red = Dangerous</i>"
)


# ---------------------------------------------------------------------------
# Screen builders — each returns (text, reply_markup)
# ---------------------------------------------------------------------------

def _screen_home() -> Tuple[str, Dict[str, Any]]:
    text = (
        "🤖 <b>Crypto Bot Control Centre</b>\n"
        f"Exchange: <code>{EXCHANGE}</code> · "
        f"HTF: {HTF_TIMEFRAME} · Entry: {ENTRY_TIMEFRAME}\n"
        f"Score threshold: {SCORE_THRESHOLD_SEND}/{SCORE_MAX}\n\n"
        "Tap a button below or use the menu in the chat box."
        + LEGEND
    )
    kb = _kb([
        [_btn("📊 Status", "nav:status"),       _btn("📈 Performance", "nav:performance")],
        [_btn("🗺 Zones", "nav:zones"),          _btn("🎯 Last Signal", "nav:last")],
        [_btn("💰 Equity / P&L", "nav:equity"), _btn("📅 Daily Summary", "nav:summary")],
        [_btn("📋 Log", "nav:log"),              _btn("📉 Chart", "nav:chart")],
        [_btn("⚠️ Config", "nav:config"),        _btn("⚠️ Pairs", "nav:pairs")],
        [_btn("🆘 Help", "nav:help")],
    ])
    return text, kb


def _screen_status() -> Tuple[str, Dict[str, Any]]:
    snap = _snapshot()
    open_trades = get_open_trades()
    rs = get_risk_manager().status_dict()
    stopped = runtime_settings.is_stopped()

    lines = ["📡 <b>Bot Status</b>"]
    lines.append(f"Exchange: <code>{EXCHANGE}</code>")
    lines.append(f"Last loop: <code>{snap['last_loop_at'] or 'pending'}</code>")
    lines.append(f"Quiet hours: {'YES' if in_quiet_hours() else 'no'}")
    if stopped:
        lines.append("🛑 <b>Scanning paused</b> (manual /stop)")
    halt_emoji = "🛑" if rs["halt_signals"] else "🟢"
    lines.append(f"{halt_emoji} Risk halt: {rs['halt_reason'] or 'no'}")
    lines.append("")
    lines.append("<b>HTF bias</b>")
    for s in runtime_settings.get_symbols():
        b = snap["htf_bias"].get(s, "?")
        emoji = {"bullish": "🟢", "bearish": "🔴", "flat": "⚪"}.get(b, "❔")
        lines.append(f"{emoji} <code>{s}</code>: {b}")
    lines.append("")
    lines.append(f"<b>Open paper trades:</b> {len(open_trades)}")
    for t in open_trades[:8]:
        entry = float(t.get("entry_price") or t.get("entry") or 0.0)
        sl = float(t.get("stop_loss") or 0.0)
        tp = float(t.get("take_profit") or 0.0)
        lines.append(
            f"• <code>{t['symbol']}</code> {t['direction'].upper()} "
            f"@ {entry:.4f} SL {sl:.4f} TP {tp:.4f}"
        )

    rows = [[_btn("🔄 Refresh", "nav:status")]]
    if stopped:
        rows.append([_btn("▶️ Resume Scanning", "confirm:resume")])
    else:
        rows.append([_btn("⚠️ Stop Bot", "confirm:stop")])
    rows.append(BACK_ROW)
    return "\n".join(lines), _kb(rows)


def _screen_performance(window: str = "menu") -> Tuple[str, Dict[str, Any]]:
    if window == "menu":
        text = (
            "📈 <b>Performance</b>\nChoose a time window:"
        )
        kb = _kb([
            [_btn("7 Days", "perf:7"), _btn("30 Days", "perf:30"), _btn("All Time", "perf:all")],
            BACK_ROW,
        ])
        return text, kb

    hours_map = {"7": 7 * 24, "30": 30 * 24, "all": 10 * 365 * 24}
    label_map = {"7": "Last 7 days", "30": "Last 30 days", "all": "All time"}
    p = performance_window(hours_map.get(window, 7 * 24))
    label = label_map.get(window, window)
    text = (
        f"📈 <b>Performance — {label}</b>\n"
        f"Trades: <b>{p['trades']}</b>\n"
        f"Win rate: <b>{p['win_rate']:.1f}%</b>\n"
        f"P&amp;L: <b>{p['pnl_pct']:+.2f}%</b> ({p['pnl_usd']:+.2f} USDT)"
    )
    kb = _kb([
        [
            _btn("•7d•" if window == "7" else "7 Days", "perf:7"),
            _btn("•30d•" if window == "30" else "30 Days", "perf:30"),
            _btn("•All•" if window == "all" else "All Time", "perf:all"),
        ],
        [_btn("🔄 Refresh", f"perf:{window}")],
        BACK_ROW,
    ])
    return text, kb


def _screen_zones() -> Tuple[str, Dict[str, Any]]:
    snap = _snapshot()
    lines = ["🗺 <b>Active zones</b>"]
    any_zone = False
    for s in runtime_settings.get_symbols():
        zs = snap["active_zones"].get(s, [])
        if not zs:
            lines.append(f"• <code>{s}</code>: none nearby")
            continue
        any_zone = True
        lines.append(f"• <b>{s}</b>")
        for z in zs[-5:]:
            lines.append(f"   {z['kind']:6s} {z['low']:.4f} – {z['high']:.4f}")
    if not any_zone:
        lines.append("(price isn't near any active zone right now)")
    return "\n".join(lines), _kb([
        [_btn("🔄 Refresh", "nav:zones")],
        BACK_ROW,
    ])


def _screen_last() -> Tuple[str, Dict[str, Any]]:
    snap = _snapshot()
    if not snap["last_signal"]:
        text = "🎯 <b>Last signals</b>\nNo signals yet — strict pipeline waiting for setups."
    else:
        lines = ["🎯 <b>Last signal per symbol</b>"]
        for s, sig in snap["last_signal"].items():
            score_max = sig.get("score_max", SCORE_MAX)
            lines.append(
                f"• <code>{s}</code> {sig['direction'].upper()} "
                f"score {sig['score']}/{score_max} @ {sig['entry']:.4f} "
                f"({sig['timestamp']})"
            )
        text = "\n".join(lines)
    return text, _kb([
        [_btn("🔄 Refresh", "nav:last")],
        BACK_ROW,
    ])


def _screen_equity() -> Tuple[str, Dict[str, Any]]:
    rs = get_risk_manager().status_dict()
    open_n = len(get_open_trades())
    halt = "🛑 HALTED" if rs["halt_signals"] else "🟢 active"
    lines = [
        "💰 <b>Equity / P&amp;L</b>",
        f"Status: {halt}" + (f" ({rs['halt_reason']})" if rs["halt_reason"] else ""),
        f"Today P&amp;L: <b>{rs['daily_pnl_usd']:+.2f}</b> USDT",
        f"Lifetime P&amp;L: <b>{rs['lifetime_realised_pnl_usd']:+.2f}</b> USDT",
        f"Equity: <b>{rs['running_equity']:.2f}</b> USDT",
        f"Consecutive losses: {rs['consecutive_losses']}",
        f"Open trades: {open_n}",
    ]
    blocks = rs.get("pair_blocks") or {}
    cooldowns = rs.get("pair_cooldowns") or {}
    if blocks:
        lines.append("Blocked pairs: " + ", ".join(f"{k} until {v}" for k, v in blocks.items()))
    if cooldowns:
        lines.append("Cooldowns (50%): " + ", ".join(f"{k} until {v}" for k, v in cooldowns.items()))

    rows = [[_btn("🔄 Refresh", "nav:equity")]]
    if rs["halt_signals"] or blocks or cooldowns:
        rows.append([_btn("⚠️ Clear All Halts", "confirm:resume_risk")])
    rows.append(BACK_ROW)
    return "\n".join(lines), _kb(rows)


def _screen_summary() -> Tuple[str, Dict[str, Any]]:
    return f"📅 <b>Daily Summary</b>\n{daily_summary()}", _kb([
        [_btn("🔄 Refresh", "nav:summary")],
        BACK_ROW,
    ])


def _screen_config() -> Tuple[str, Dict[str, Any]]:
    max_t = runtime_settings.get_max_open_trades()
    cap = runtime_settings.get_daily_loss_cap_pct()
    risk = runtime_settings.get_risk_per_trade_pct()
    text = (
        "⚙️ <b>Live Settings</b>\n"
        "<i>⚠️ Changes apply immediately to the running bot.</i>\n\n"
        f"<b>Max Open Trades:</b> <code>{max_t}</code>\n"
        f"<b>Daily Loss Cap:</b> <code>-{cap:.1f}%</code>\n"
        f"<b>Risk Per Trade:</b> <code>{risk:.2f}%</code>"
        + LEGEND
    )
    kb = _kb([
        [
            _btn(f"Max Trades: {max_t}", "noop"),
            _btn("➖", "cfg:max_trades:-"),
            _btn("➕", "cfg:max_trades:+"),
        ],
        [
            _btn(f"Loss Cap: -{cap:.1f}%", "noop"),
            _btn("➖", "cfg:loss_cap:-"),
            _btn("➕", "cfg:loss_cap:+"),
        ],
        [
            _btn(f"Risk: {risk:.2f}%", "noop"),
            _btn("➖", "cfg:risk:-"),
            _btn("➕", "cfg:risk:+"),
        ],
        [_btn("⚠️ Stop Bot", "confirm:stop"), _btn("⚠️ Restart Bot", "confirm:restart")],
        BACK_ROW,
    ])
    return text, kb


def _screen_pairs() -> Tuple[str, Dict[str, Any]]:
    pairs = runtime_settings.get_symbols()
    lines = ["🪙 <b>Trading Pairs</b>", "<i>⚠️ Removing a pair stops scanning it immediately.</i>", ""]
    for p in pairs:
        lines.append(f"<code>{p}</code> ✅")
    rows = [
        [_btn(f"❌ Remove {p}", f"confirm:rm_pair:{p}")] for p in pairs
    ]
    rows.append([_btn("➕ Add Pair", "pair:add")])
    rows.append(BACK_ROW)
    return "\n".join(lines), _kb(rows)


def _screen_log() -> Tuple[str, Dict[str, Any]]:
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-25:]
        snippet = "".join(lines).strip() or "(log empty)"
    except FileNotFoundError:
        snippet = "(log file not found yet)"
    except Exception as e:  # noqa: BLE001
        snippet = f"(log read failed: {e})"
    # Telegram message limit is 4096; trim safely
    snippet = snippet[-3500:]
    text = f"📋 <b>Recent Log</b>\n<pre>{_html_escape(snippet)}</pre>"
    return text, _kb([
        [_btn("🔄 Refresh", "nav:log")],
        BACK_ROW,
    ])


def _screen_chart() -> Tuple[str, Dict[str, Any]]:
    pairs = runtime_settings.get_symbols()
    text = "📉 <b>Render Chart</b>\nPick a symbol to render the most-recent setup chart:"
    rows = [[_btn(p, f"chart:{p}")] for p in pairs]
    rows.append(BACK_ROW)
    return text, _kb(rows)


def _screen_backtest() -> Tuple[str, Dict[str, Any]]:
    text = (
        "🧪 <b>Backtest</b>\n\n"
        "Backtests run from the server shell, not from Telegram (they take "
        "minutes and produce charts).\n\n"
        "On the server, run:\n"
        "<pre>python3 backtest.py BTC/USDT 2024-01-01 2024-04-01</pre>"
    )
    return text, _kb([BACK_ROW])


def _screen_help() -> Tuple[str, Dict[str, Any]]:
    text = (
        "🆘 <b>Commands</b>\n"
        "/start — main menu\n"
        "/status — bot status, HTF bias, open trades\n"
        "/performance — win rate over different windows\n"
        "/equity — equity, daily P&amp;L, halt status\n"
        "/zones — active supply / demand zones\n"
        "/last — last signal per symbol\n"
        "/summary — daily paper-trade summary\n"
        "/config — ⚠️ live tuning of risk settings\n"
        "/pairs — ⚠️ manage which pairs are scanned\n"
        "/log — recent log lines\n"
        "/chart — render a chart for a symbol\n"
        "/backtest — instructions for historical backtests\n"
        "/stop — 🔴 pause new trade scanning\n"
        "/restart — 🔴 restart the bot process"
        + LEGEND
    )
    return text, _kb([BACK_ROW])


# ---------------------------------------------------------------------------
# Confirmation screens — every dangerous action goes through here
# ---------------------------------------------------------------------------

def _confirm_stop() -> Tuple[str, Dict[str, Any]]:
    text = (
        "🔴 <b>STOP BOT</b>\n\n"
        "⚠️ Bot will stop scanning for new signals.\n"
        "Open trades remain in the database but become <b>unmanaged</b> "
        "until scanning resumes.\n\n"
        "Are you sure?"
    )
    return text, _kb([
        [_btn("✅ Yes, Stop Bot", "do:stop"), _btn("❌ Cancel", "nav:home")],
    ])


def _confirm_resume() -> Tuple[str, Dict[str, Any]]:
    text = (
        "▶️ <b>Resume Scanning</b>\n\n"
        "The bot will start scanning for new signals again."
    )
    return text, _kb([
        [_btn("✅ Yes, Resume", "do:resume"), _btn("❌ Cancel", "nav:home")],
    ])


def _confirm_restart() -> Tuple[str, Dict[str, Any]]:
    text = (
        "🔴 <b>RESTART BOT</b>\n\n"
        "⚠️ Brief downtime (~15 seconds). The bot will pull the latest code "
        "from GitHub and restart in place.\n"
        "Open trades are preserved in the database.\n\n"
        "Are you sure?"
    )
    return text, _kb([
        [_btn("✅ Yes, Restart", "do:restart"), _btn("❌ Cancel", "nav:home")],
    ])


def _confirm_remove_pair(symbol: str) -> Tuple[str, Dict[str, Any]]:
    text = (
        f"🔴 <b>REMOVE {symbol}</b>\n\n"
        f"⚠️ This pair will stop being scanned immediately.\n"
        f"Existing open <code>{symbol}</code> trades will be left in the database "
        f"but no longer monitored until you re-add the pair.\n\n"
        "Are you sure?"
    )
    return text, _kb([
        [_btn(f"✅ Yes, Remove {symbol}", f"do:rm_pair:{symbol}"), _btn("❌ Cancel", "nav:pairs")],
    ])


def _confirm_resume_risk() -> Tuple[str, Dict[str, Any]]:
    text = (
        "⚠️ <b>Clear All Halts</b>\n\n"
        "Removes the daily-loss / consecutive-loss halt and clears every "
        "pair block & cooldown.\n"
        "<b>This overrides the daily loss protection.</b> Use with care."
    )
    return text, _kb([
        [_btn("✅ Yes, Clear", "do:resume_risk"), _btn("❌ Cancel", "nav:equity")],
    ])


# ---------------------------------------------------------------------------
# Action executors — return (text, keyboard) of the resulting screen
# ---------------------------------------------------------------------------

def _do_stop() -> Tuple[str, Dict[str, Any]]:
    runtime_settings.request_stop(True)
    log.warning("Telegram: stop requested.")
    text = (
        "🛑 <b>Bot stopped</b>\n"
        "Scanning paused. Use <i>Resume Scanning</i> in /status to re-enable."
    )
    return text, _kb([
        [_btn("▶️ Resume Scanning", "confirm:resume")],
        BACK_ROW,
    ])


def _do_resume() -> Tuple[str, Dict[str, Any]]:
    runtime_settings.request_stop(False)
    log.info("Telegram: resume requested.")
    return _screen_status()


def _do_restart() -> Tuple[str, Dict[str, Any]]:
    runtime_settings.request_restart(True)
    log.warning("Telegram: restart requested.")
    text = (
        "🔄 <b>Restarting…</b>\n"
        "Pulling latest code from GitHub and restarting.\n"
        "Send /status in ~20 seconds to confirm."
    )
    return text, _kb([BACK_ROW])


def _do_remove_pair(symbol: str) -> Tuple[str, Dict[str, Any]]:
    if runtime_settings.remove_symbol(symbol):
        log.warning("Telegram: removed pair %s", symbol)
    return _screen_pairs()


def _do_resume_risk() -> Tuple[str, Dict[str, Any]]:
    msg = get_risk_manager().clear_halts()
    log.warning("Telegram: clear_halts — %s", msg)
    return _screen_equity()


def _do_cfg(key: str, op: str) -> Tuple[str, Dict[str, Any]]:
    """key in {max_trades, loss_cap, risk}, op in {+, -}."""
    delta = 1 if op == "+" else -1
    if key == "max_trades":
        runtime_settings.adjust_max_open_trades(delta)
    elif key == "loss_cap":
        runtime_settings.adjust_daily_loss_cap_pct(0.5 * delta)
    elif key == "risk":
        runtime_settings.adjust_risk_per_trade_pct(0.25 * delta)
    return _screen_config()


def _do_chart(symbol: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Render and send a chart photo. Returns the screen to show after send."""
    snap = _snapshot()
    sig = snap["last_signal"].get(symbol)
    if not sig:
        text = (
            f"📉 <b>{symbol}</b>\n"
            "No recent signal to chart yet — waiting for a setup."
        )
        return text, _kb([[_btn("🔙 Back to Charts", "nav:chart")], BACK_ROW])

    try:
        from chart_renderer import render_signal_chart
        from data_fetcher import fetch_ohlcv
        df = fetch_ohlcv(symbol, timeframe=ENTRY_TIMEFRAME, limit=200, exchange_name=EXCHANGE)
        path = render_signal_chart(df, sig)
        if path and os.path.exists(path):
            send_photo(path, caption=f"📉 {symbol} — last setup")
        text = f"📉 <b>{symbol}</b>\nChart sent above."
    except Exception as e:  # noqa: BLE001
        log.warning("chart render failed: %s", e)
        text = f"📉 <b>{symbol}</b>\n⚠️ Chart render failed: <code>{e}</code>"
    return text, _kb([[_btn("🔙 Back to Charts", "nav:chart")], BACK_ROW])


def _start_add_pair_flow(chat_id: int) -> Tuple[str, Dict[str, Any]]:
    _set_chat_state(chat_id, "awaiting", "add_pair")
    text = (
        "➕ <b>Add Pair</b>\n\n"
        "Send the symbol in the next message, e.g. <code>DOGE/USDT</code>.\n"
        "Format: <code>BASE/QUOTE</code> (uppercase). "
        "Send /cancel to abort."
    )
    return text, _kb([[_btn("❌ Cancel", "pair:cancel_add")]])


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

# Maps /command → screen builder
COMMAND_SCREENS: Dict[str, Callable[[], Tuple[str, Dict[str, Any]]]] = {
    "/start": _screen_home,
    "/menu": _screen_home,
    "/status": _screen_status,
    "/performance": lambda: _screen_performance("menu"),
    "/zones": _screen_zones,
    "/last": _screen_last,
    "/equity": _screen_equity,
    "/pnl": _screen_equity,  # alias
    "/summary": _screen_summary,
    "/config": _screen_config,
    "/pairs": _screen_pairs,
    "/log": _screen_log,
    "/chart": _screen_chart,
    "/backtest": _screen_backtest,
    "/help": _screen_help,
    # Direct-action commands route to their confirmation screen
    "/stop": _confirm_stop,
    "/restart": _confirm_restart,
    "/resume": _confirm_resume_risk,
}


def _route_callback(chat_id: int, data: str) -> Tuple[Optional[str], Optional[Dict[str, Any]], str]:
    """Resolve a callback_data string to (text, keyboard, toast_message)."""
    if not data or data == "noop":
        return None, None, ""

    parts = data.split(":", 2)
    head = parts[0]

    try:
        if head == "nav":
            target = parts[1]
            mapper = {
                "home": _screen_home,
                "status": _screen_status,
                "performance": lambda: _screen_performance("menu"),
                "zones": _screen_zones,
                "last": _screen_last,
                "equity": _screen_equity,
                "summary": _screen_summary,
                "config": _screen_config,
                "pairs": _screen_pairs,
                "log": _screen_log,
                "chart": _screen_chart,
                "help": _screen_help,
            }
            if target in mapper:
                t, kb = mapper[target]()
                return t, kb, ""
        elif head == "perf":
            t, kb = _screen_performance(parts[1])
            return t, kb, ""
        elif head == "cfg":
            key, op = parts[1], parts[2]
            t, kb = _do_cfg(key, op)
            return t, kb, "Updated"
        elif head == "confirm":
            sub = parts[1]
            if sub == "stop":
                t, kb = _confirm_stop(); return t, kb, ""
            if sub == "resume":
                t, kb = _confirm_resume(); return t, kb, ""
            if sub == "restart":
                t, kb = _confirm_restart(); return t, kb, ""
            if sub == "rm_pair":
                t, kb = _confirm_remove_pair(parts[2]); return t, kb, ""
            if sub == "resume_risk":
                t, kb = _confirm_resume_risk(); return t, kb, ""
        elif head == "do":
            sub = parts[1]
            if sub == "stop":
                t, kb = _do_stop(); return t, kb, "Bot stopped"
            if sub == "resume":
                t, kb = _do_resume(); return t, kb, "Resumed"
            if sub == "restart":
                t, kb = _do_restart(); return t, kb, "Restarting"
            if sub == "rm_pair":
                t, kb = _do_remove_pair(parts[2]); return t, kb, f"Removed {parts[2]}"
            if sub == "resume_risk":
                t, kb = _do_resume_risk(); return t, kb, "Halts cleared"
        elif head == "pair":
            sub = parts[1]
            if sub == "add":
                t, kb = _start_add_pair_flow(chat_id); return t, kb, ""
            if sub == "cancel_add":
                _pop_chat_state(chat_id, "awaiting")
                t, kb = _screen_pairs(); return t, kb, "Cancelled"
        elif head == "chart":
            t, kb = _do_chart(parts[1])
            return t, kb, ""
    except Exception as e:  # noqa: BLE001
        log.error("callback %s failed: %s", data, e)
        text = f"⚠️ <b>Action failed</b>\n<code>{e}</code>"
        kb = _kb([[_btn("🔄 Retry", data)], BACK_ROW])
        return text, kb, "Failed"

    log.warning("Unhandled callback data: %s", data)
    return None, None, ""


# ---------------------------------------------------------------------------
# Update processing
# ---------------------------------------------------------------------------

def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _process_message(msg: Dict[str, Any]) -> None:
    text = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not text or chat_id is None:
        return

    # /cancel always wins
    if text.lower().startswith("/cancel"):
        _pop_chat_state(chat_id, "awaiting")
        screen_text, kb = _screen_home()
        send_message("Cancelled.\n\n" + screen_text, chat_id=str(chat_id), reply_markup=kb)
        return

    # If we're awaiting a multi-step input, route there first
    awaiting = _peek_chat_state(chat_id, "awaiting")
    if awaiting == "add_pair" and not text.startswith("/"):
        _pop_chat_state(chat_id, "awaiting")
        sym = text.strip().upper()
        if runtime_settings.add_symbol(sym):
            t = f"✅ Added <code>{sym}</code> to scan list."
        else:
            t = (
                f"⚠️ Couldn't add <code>{_html_escape(sym)}</code>. "
                "Use format <code>BASE/QUOTE</code> (e.g. DOGE/USDT) and ensure it's not already added."
            )
        screen_text, kb = _screen_pairs()
        send_message(t + "\n\n" + screen_text, chat_id=str(chat_id), reply_markup=kb)
        return

    # Strip "@botname" from group commands
    cmd = text.split()[0].split("@")[0].lower()
    builder = COMMAND_SCREENS.get(cmd)
    if not builder:
        return
    try:
        screen_text, kb = builder()
    except Exception as e:  # noqa: BLE001
        log.error("command %s failed: %s", cmd, e)
        screen_text, kb = (
            f"⚠️ <b>Command failed</b>\n<code>{e}</code>",
            _kb([[_btn("🔄 Retry", f"nav:home")], BACK_ROW]),
        )
    send_message(screen_text, chat_id=str(chat_id), reply_markup=kb)


def _process_callback(cbq: Dict[str, Any]) -> None:
    cb_id = cbq.get("id")
    data = cbq.get("data") or ""
    msg = cbq.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    message_id = msg.get("message_id")

    if not cb_id or chat_id is None or message_id is None:
        return

    text, kb, toast = _route_callback(chat_id, data)
    answer_callback_query(cb_id, text=toast)

    if text is None:
        return

    # Edit in place to keep the chat clean
    edited = edit_message(str(chat_id), int(message_id), text, reply_markup=kb)
    if not edited:
        # Fallback if Telegram refuses (e.g. message too old): send a new one
        send_message(text, chat_id=str(chat_id), reply_markup=kb)


def _process_update(update: Dict[str, Any]) -> None:
    if "callback_query" in update:
        _process_callback(update["callback_query"])
        return
    msg = update.get("message") or update.get("channel_post")
    if msg:
        _process_message(msg)


# ---------------------------------------------------------------------------
# Long-poll loop
# ---------------------------------------------------------------------------

def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"


def _poll_loop() -> None:
    if not TELEGRAM_BOT_TOKEN:
        log.warning("No TELEGRAM_BOT_TOKEN; command listener disabled.")
        return
    log.info("Telegram command listener started (with inline keyboards).")
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
                params={
                    "timeout": 25,
                    "offset": offset,
                    "allowed_updates": '["message","callback_query"]',
                },
                timeout=35,
            )
            if r.status_code == 409:
                conflict_count += 1
                if conflict_count <= 3 or conflict_count % 30 == 0:
                    log.warning(
                        "Telegram 409 Conflict — another bot instance may be polling. "
                        "Continuing without command polling."
                    )
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


def start_in_background() -> Optional[threading.Thread]:
    if not should_listen():
        log.info(
            "Telegram command listener disabled by TELEGRAM_LISTEN=0; "
            "signal sending remains active."
        )
        return None
    t = threading.Thread(target=_poll_loop, name="telegram-commands", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Restart helper used by main loop
# ---------------------------------------------------------------------------

def perform_restart() -> None:
    """Pull latest code (best-effort) and exec a fresh process in place."""
    log.warning("perform_restart: pulling latest from GitHub then re-execing")
    try:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        subprocess.run(
            ["git", "-C", repo_root, "pull", "--ff-only"],
            timeout=30,
            check=False,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("git pull failed: %s", e)
    try:
        os.execv(sys.executable, [sys.executable, *sys.argv])
    except Exception as e:  # noqa: BLE001
        log.error("os.execv failed: %s — exiting hard so screen restarts us", e)
        os._exit(1)
