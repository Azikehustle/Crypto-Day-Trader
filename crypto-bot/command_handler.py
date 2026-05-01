"""Telegram command + inline-keyboard UI.

New in Phase 2-6:
  /trades   — open + recent closed trades, inline pagination
  /reminder — Alwaysdata login reminder status
  /handbook — embedded trading rules (multi-page)
  /news     — news shield status + upcoming events
  /mode     — switch scalp/day/swing
  Updated /chart, /backtest, /status, /config, /help
"""
from __future__ import annotations

import os
import sys
import threading
import time
import subprocess
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

import runtime_settings
from config import (
    TELEGRAM_BOT_TOKEN, HTF_TIMEFRAME, ENTRY_TIMEFRAME,
    SCORE_THRESHOLD_SEND, SCORE_MAX, LOG_FILE, DATA_DIR,
    ALWAYSDATA_WARN_DAYS,
)
from logger_setup import get_logger
from telegram_bot import (
    send_message, edit_message, answer_callback_query,
    should_listen, send_photo,
)
from paper_trader import daily_summary, performance_window
from supabase_client import get_open_trades
from risk_manager import get_risk_manager, in_quiet_hours
from news_shield import news_summary_text, upcoming_events

log = get_logger("commands")

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_state: Dict[str, Any] = {
    "htf_bias": {}, "active_zones": {}, "last_signal": {}, "last_loop_at": None,
}


def update_state(**kwargs) -> None:
    with _state_lock:
        for k, v in kwargs.items():
            _state[k] = v


def update_symbol_state(symbol: str, **kwargs) -> None:
    with _state_lock:
        for k, v in kwargs.items():
            _state.setdefault(k, {})[symbol] = v


def _snapshot() -> Dict[str, Any]:
    with _state_lock:
        return {
            "htf_bias":    dict(_state["htf_bias"]),
            "active_zones": {k: list(v) for k, v in _state["active_zones"].items()},
            "last_signal": dict(_state["last_signal"]),
            "last_loop_at": _state["last_loop_at"],
        }


# ---------------------------------------------------------------------------
# Per-chat conversation state
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
    return {"text": label, "callback_data": data}


def _kb(rows: List[List[Dict[str, str]]]) -> Dict[str, Any]:
    return {"inline_keyboard": rows}


BACK_ROW = [_btn("🔙 Back to Menu", "nav:home")]
LEGEND = "\nℹ️ <i>Blue = Safe · ⚠️ Orange = Changes Settings · 🔴 Red = Dangerous</i>"


# ---------------------------------------------------------------------------
# Screen builders
# ---------------------------------------------------------------------------

def _screen_home() -> Tuple[str, Dict[str, Any]]:
    mode = runtime_settings.get_mode()
    text = (
        "🤖 <b>Forex Bot Control Centre</b>\n"
        f"Mode: <b>{mode.capitalize()}</b> · Score: {SCORE_THRESHOLD_SEND}/{SCORE_MAX}\n\n"
        "Tap a button or type a command."
        + LEGEND
    )
    kb = _kb([
        [_btn("📊 Status",     "nav:status"),    _btn("📈 Performance", "nav:performance")],
        [_btn("🗺 Zones",       "nav:zones"),     _btn("🎯 Last Signal",  "nav:last")],
        [_btn("💼 Trades",     "nav:trades"),    _btn("💰 Equity / P&L", "nav:equity")],
        [_btn("📅 Summary",    "nav:summary"),   _btn("📉 Chart",        "nav:chart")],
        [_btn("📰 News",       "nav:news"),      _btn("🕹 Mode",          "nav:mode")],
        [_btn("⚙️ Config",    "nav:config"),    _btn("🪙 Pairs",          "nav:pairs")],
        [_btn("📋 Log",        "nav:log"),       _btn("📚 Handbook",      "nav:handbook")],
        [_btn("⏰ Reminder",   "nav:reminder"),  _btn("🆘 Help",          "nav:help")],
    ])
    return text, kb


def _screen_status() -> Tuple[str, Dict[str, Any]]:
    snap        = _snapshot()
    open_trades = get_open_trades()
    rs          = get_risk_manager().status_dict()
    stopped     = runtime_settings.is_stopped()
    mode        = runtime_settings.get_mode()
    corr_mode   = runtime_settings.get_correlation_mode()
    news_on     = runtime_settings.get_news_shield_enabled()

    lines = ["📡 <b>Bot Status</b>"]
    lines.append(f"Mode: <b>{mode.capitalize()}</b> | Correlation: <b>{corr_mode}</b>")
    lines.append(f"News shield: {'🛡️ ON' if news_on else '🔓 off'}")
    lines.append(f"Last loop: <code>{snap['last_loop_at'] or 'pending'}</code>")
    lines.append(f"Quiet hours: {'YES 🌙' if in_quiet_hours() else 'no'}")
    if stopped:
        lines.append("🛑 <b>Scanning paused</b> (manual /stop)")
    halt_emoji = "🛑" if rs["halt_signals"] else "🟢"
    lines.append(f"{halt_emoji} Risk halt: {rs['halt_reason'] or 'none'}")
    lines.append("")
    lines.append("<b>HTF bias</b>")
    for s in runtime_settings.get_symbols():
        b = snap["htf_bias"].get(s, "?")
        emoji = {"bullish": "🟢", "bearish": "🔴", "flat": "⚪"}.get(b, "❔")
        lines.append(f"{emoji} <code>{s}</code>: {b}")
    lines.append(f"\n<b>Open trades:</b> {len(open_trades)}")
    for t in open_trades[:6]:
        entry = float(t.get("entry_price") or t.get("entry") or 0.0)
        sl    = float(t.get("stop_loss") or 0.0)
        tp    = float(t.get("take_profit") or 0.0)
        lines.append(
            f"• <code>{t['symbol']}</code> {t['direction'].upper()} "
            f"@ {entry:.5f} SL {sl:.5f} TP {tp:.5f}"
        )

    rows = [[_btn("🔄 Refresh", "nav:status")]]
    if stopped:
        rows.append([_btn("▶️ Resume Scanning", "confirm:resume")])
    else:
        rows.append([_btn("⚠️ Stop Bot", "confirm:stop")])
    rows.append(BACK_ROW)
    return "\n".join(lines), _kb(rows)


def _screen_trades(page: int = 0) -> Tuple[str, Dict[str, Any]]:
    """Open + recent closed trades with pagination."""
    open_t = get_open_trades()
    from paper_trader import performance_window  # noqa: WPS433
    try:
        from supabase_client import get_closed_trades_since  # noqa: WPS433
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        history = get_closed_trades_since(cutoff)
    except Exception:  # noqa: BLE001
        history = []

    lines = [f"💼 <b>Trades</b> — {len(open_t)} open, {len(history)} closed (7d)"]
    lines.append("")
    if open_t:
        lines.append("<b>Open:</b>")
        for t in open_t[:5]:
            entry = float(t.get("entry_price") or t.get("entry") or 0)
            sl    = float(t.get("stop_loss") or 0)
            tp    = float(t.get("take_profit") or 0)
            d     = "📈" if t.get("direction") == "long" else "📉"
            lines.append(
                f"{d} <code>{t['symbol']}</code> {t.get('direction','?').upper()} "
                f"@ {entry:.5f} | SL {sl:.5f} | TP {tp:.5f}"
            )
        if len(open_t) > 5:
            lines.append(f"… +{len(open_t) - 5} more")

    PAGE_SIZE = 4
    start = page * PAGE_SIZE
    page_hist = history[start: start + PAGE_SIZE]
    if page_hist:
        lines.append("")
        lines.append("<b>Recent closed (7d):</b>")
        for t in page_hist:
            r = t.get("result", "?")
            emoji = "✅" if r == "win" else "❌" if r == "loss" else "❔"
            pnl = t.get("pnl_pct") or 0.0
            lines.append(
                f"{emoji} <code>{t['symbol']}</code> {t.get('direction','?').upper()} "
                f"P&L: {pnl:+.2f}% | {(t.get('closed_at') or '')[:10]}"
            )

    rows: List[List[Dict]] = [[_btn("🔄 Refresh", "nav:trades")]]
    nav_row = []
    if page > 0:
        nav_row.append(_btn("◀️ Prev", f"trades:page:{page - 1}"))
    if start + PAGE_SIZE < len(history):
        nav_row.append(_btn("▶️ Next", f"trades:page:{page + 1}"))
    if nav_row:
        rows.append(nav_row)
    rows.append(BACK_ROW)
    return "\n".join(lines), _kb(rows)


def _screen_performance(window: str = "menu") -> Tuple[str, Dict[str, Any]]:
    if window == "menu":
        return (
            "📈 <b>Performance</b>\nChoose a time window:",
            _kb([[_btn("7 Days", "perf:7"), _btn("30 Days", "perf:30"), _btn("All Time", "perf:all")], BACK_ROW]),
        )
    hours_map = {"7": 7 * 24, "30": 30 * 24, "all": 10 * 365 * 24}
    label_map = {"7": "Last 7 days", "30": "Last 30 days", "all": "All time"}
    p = performance_window(hours_map.get(window, 7 * 24))
    label = label_map.get(window, window)
    text = (
        f"📈 <b>Performance — {label}</b>\n"
        f"Trades: <b>{p['trades']}</b>\n"
        f"Win rate: <b>{p['win_rate']:.1f}%</b>\n"
        f"P&amp;L: <b>{p['pnl_pct']:+.2f}%</b> ({p['pnl_usd']:+.2f} USD)"
    )
    kb = _kb([
        [_btn("•7d•" if window == "7" else "7 Days", "perf:7"),
         _btn("•30d•" if window == "30" else "30 Days", "perf:30"),
         _btn("•All•" if window == "all" else "All Time", "perf:all")],
        [_btn("🔄 Refresh", f"perf:{window}")],
        BACK_ROW,
    ])
    return text, kb


def _screen_zones() -> Tuple[str, Dict[str, Any]]:
    snap  = _snapshot()
    lines = ["🗺 <b>Active zones</b>"]
    any_z = False
    for s in runtime_settings.get_symbols():
        zs = snap["active_zones"].get(s, [])
        if not zs:
            lines.append(f"• <code>{s}</code>: none nearby")
            continue
        any_z = True
        lines.append(f"• <b>{s}</b>")
        for z in zs[-4:]:
            lines.append(f"   {z['kind']:6s} {z['low']:.5f} – {z['high']:.5f}")
    if not any_z:
        lines.append("(price isn't near any active zone)")
    return "\n".join(lines), _kb([[_btn("🔄 Refresh", "nav:zones")], BACK_ROW])


def _screen_last() -> Tuple[str, Dict[str, Any]]:
    snap = _snapshot()
    if not snap["last_signal"]:
        text = "🎯 <b>Last signals</b>\nNo signals yet."
    else:
        lines = ["🎯 <b>Last signal per symbol</b>"]
        for s, sig in snap["last_signal"].items():
            lines.append(
                f"• <code>{s}</code> {sig['direction'].upper()} "
                f"score {sig['score']}/{sig.get('score_max', SCORE_MAX)} "
                f"@ {sig['entry']:.5f} | {sig.get('mode','?')} | {sig.get('session','?')} "
                f"({sig['timestamp'][:16]})"
            )
        text = "\n".join(lines)
    return text, _kb([[_btn("🔄 Refresh", "nav:last")], BACK_ROW])


def _screen_equity() -> Tuple[str, Dict[str, Any]]:
    rs    = get_risk_manager().status_dict()
    open_n = len(get_open_trades())
    halt  = "🛑 HALTED" if rs["halt_signals"] else "🟢 active"
    lines = [
        "💰 <b>Equity / P&amp;L</b>",
        f"Status: {halt}" + (f" ({rs['halt_reason']})" if rs["halt_reason"] else ""),
        f"Today P&amp;L: <b>{rs['daily_pnl_usd']:+.2f}</b> USD",
        f"Lifetime P&amp;L: <b>{rs['lifetime_realised_pnl_usd']:+.2f}</b> USD",
        f"Equity: <b>{rs['running_equity']:.2f}</b> USD",
        f"Consecutive losses: {rs['consecutive_losses']}",
        f"Open trades: {open_n}",
    ]
    blocks    = rs.get("pair_blocks") or {}
    cooldowns = rs.get("pair_cooldowns") or {}
    if blocks:
        lines.append("⛔ Blocked: " + ", ".join(f"{k} until {v[:16]}" for k, v in blocks.items()))
    if cooldowns:
        lines.append("⚠️ Cooldowns (50%): " + ", ".join(f"{k} until {v[:16]}" for k, v in cooldowns.items()))

    rows = [[_btn("🔄 Refresh", "nav:equity")]]
    if rs["halt_signals"] or blocks or cooldowns:
        rows.append([_btn("⚠️ Clear All Halts", "confirm:resume_risk")])
    rows.append(BACK_ROW)
    return "\n".join(lines), _kb(rows)


def _screen_summary() -> Tuple[str, Dict[str, Any]]:
    return f"📅 <b>Daily Summary</b>\n{daily_summary()}", _kb([[_btn("🔄 Refresh", "nav:summary")], BACK_ROW])


def _screen_news() -> Tuple[str, Dict[str, Any]]:
    news_on = runtime_settings.get_news_shield_enabled()
    lines = [f"📰 <b>News Shield</b> — {'🛡️ ON' if news_on else '🔓 off'}"]
    for sym in runtime_settings.get_symbols():
        events = upcoming_events(sym, lookahead_hours=4)
        if events:
            lines.append(f"\n<b>{sym}</b>")
            for e in events[:2]:
                lines.append(f"  • {e['time'][11:16]} UTC — {e['event']} ({e['country']})")
        else:
            lines.append(f"<code>{sym}</code>: no high-impact events in 4h")

    toggle_label = "🔓 Disable Shield" if news_on else "🛡️ Enable Shield"
    return "\n".join(lines), _kb([
        [_btn(toggle_label, "do:toggle_news")],
        [_btn("🔄 Refresh", "nav:news")],
        BACK_ROW,
    ])


def _screen_mode() -> Tuple[str, Dict[str, Any]]:
    mode = runtime_settings.get_mode()
    corr = runtime_settings.get_correlation_mode()
    text = (
        "🕹 <b>Trading Mode</b>\n"
        f"Current: <b>{mode.capitalize()}</b> | Correlation: <b>{corr}</b>\n\n"
        "Scalp: 1h HTF / 5m entry  (high frequency)\n"
        "Day:   4h HTF / 15m entry (default)\n"
        "Swing: 1D HTF / 1h entry  (slow & steady)\n"
        "All:   run all three modes simultaneously"
    )
    mode_rows = [
        [_btn("•Scalp•" if mode == "scalp" else "🏎 Scalp", "do:mode:scalp"),
         _btn("•Day•"   if mode == "day"   else "📈 Day",   "do:mode:day"),
         _btn("•Swing•" if mode == "swing" else "🌊 Swing", "do:mode:swing"),
         _btn("•All•"   if mode == "all"   else "⚡ All",   "do:mode:all")],
        [_btn(f"Correlation: {'•strict•' if corr == 'strict' else 'strict'}", "do:corr:strict"),
         _btn(f"{'•relaxed•' if corr == 'relaxed' else 'relaxed'}", "do:corr:relaxed")],
        BACK_ROW,
    ]
    return text, _kb(mode_rows)


def _screen_config() -> Tuple[str, Dict[str, Any]]:
    max_t = runtime_settings.get_max_open_trades()
    cap   = runtime_settings.get_daily_loss_cap_pct()
    risk  = runtime_settings.get_risk_per_trade_pct()
    text  = (
        "⚙️ <b>Live Settings</b>\n"
        "<i>⚠️ Changes apply immediately.</i>\n\n"
        f"<b>Max Open Trades:</b> <code>{max_t}</code>\n"
        f"<b>Daily Loss Cap:</b> <code>-{cap:.1f}%</code>\n"
        f"<b>Risk Per Trade:</b> <code>{risk:.2f}%</code>"
        + LEGEND
    )
    kb = _kb([
        [_btn(f"Max Trades: {max_t}", "noop"), _btn("➖", "cfg:max_trades:-"), _btn("➕", "cfg:max_trades:+")],
        [_btn(f"Loss Cap: -{cap:.1f}%", "noop"), _btn("➖", "cfg:loss_cap:-"), _btn("➕", "cfg:loss_cap:+")],
        [_btn(f"Risk: {risk:.2f}%", "noop"), _btn("➖", "cfg:risk:-"), _btn("➕", "cfg:risk:+")],
        [_btn("⚠️ Stop Bot", "confirm:stop"), _btn("⚠️ Restart Bot", "confirm:restart")],
        BACK_ROW,
    ])
    return text, kb


def _screen_pairs() -> Tuple[str, Dict[str, Any]]:
    pairs = runtime_settings.get_symbols()
    lines = ["🪙 <b>Trading Pairs</b>", "<i>⚠️ Removing a pair stops scanning immediately.</i>", ""]
    for p in pairs:
        lines.append(f"<code>{p}</code> ✅")
    rows  = [[_btn(f"❌ Remove {p}", f"confirm:rm_pair:{p}")] for p in pairs]
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
    text = f"📋 <b>Recent Log</b>\n<pre>{_html_escape(snippet[-3500:])}</pre>"
    return text, _kb([[_btn("🔄 Refresh", "nav:log")], BACK_ROW])


def _screen_chart() -> Tuple[str, Dict[str, Any]]:
    pairs = runtime_settings.get_symbols()
    text  = "📉 <b>Render Chart</b>\nPick a symbol:"
    rows  = [[_btn(p, f"chart:{p}")] for p in pairs]
    rows.append(BACK_ROW)
    return text, _kb(rows)


def _screen_backtest() -> Tuple[str, Dict[str, Any]]:
    pairs = runtime_settings.get_symbols()
    text  = (
        "🧪 <b>Backtest</b>\n\n"
        "Pick a pair to run a quick backtest (last 90 days, current mode).\n"
        "For deeper runs, use the shell:\n"
        "<pre>python3 backtest.py EUR/USD 2024-01-01 2024-04-01 day</pre>"
    )
    rows = [[_btn(p, f"bt:{p}")] for p in pairs]
    rows.append(BACK_ROW)
    return text, _kb(rows)


def _screen_reminder() -> Tuple[str, Dict[str, Any]]:
    from datetime import timedelta
    from main import _start_time as bot_start  # noqa: WPS433
    now      = datetime.now(timezone.utc)
    age_days = (now - bot_start).days
    deadline = 120
    remaining = deadline - age_days
    warn_days = sorted(ALWAYSDATA_WARN_DAYS)
    lines = [
        "⏰ <b>Alwaysdata Reminder</b>",
        f"Bot running for <b>{age_days} days</b>",
        f"120-day login deadline: <b>{remaining} days remaining</b>",
        "",
        "Alwaysdata suspends free hosting after 120 days without login.",
        f"Reminder alerts scheduled at days: {', '.join(str(d) for d in warn_days)}",
        "",
        "Log in at: https://www.alwaysdata.com",
    ]
    text = "\n".join(lines)
    return text, _kb([[_btn("🔄 Refresh", "nav:reminder")], BACK_ROW])


# ---------------------------------------------------------------------------
# Handbook — embedded trading rules (3 pages)
# ---------------------------------------------------------------------------

_HANDBOOK_PAGES = [
    (
        "📚 <b>Handbook — Strategy Overview (1/3)</b>\n\n"
        "<b>Pairs:</b> EUR/USD · GBP/USD · USD/JPY · AUD/USD · USD/CAD · EUR/GBP\n\n"
        "<b>Multi-Timeframe Modes:</b>\n"
        "• Scalp — 1h HTF / 5m entry. Fast, tight stops. Best London/NY open.\n"
        "• Day   — 4h HTF / 15m entry. Core mode, balanced RR.\n"
        "• Swing — 1D HTF / 1h entry. Slow, wide stops, 3–5R targets.\n\n"
        "<b>Signal Pipeline:</b>\n"
        "1. HTF EMA-200 bias filter (bullish/bearish/flat)\n"
        "2. Supply/demand zone detection (pivot + impulse)\n"
        "3. Liquidity sweep (wick beyond zone, close inside)\n"
        "4. MSS / displacement candle\n"
        "5. Volume + ATR filter\n"
        "6. Score ≥ 8/15 to alert"
    ),
    (
        "📚 <b>Handbook — Risk & Money Management (2/3)</b>\n\n"
        "<b>Risk per trade:</b> 1% of equity (default)\n"
        "<b>Max open trades:</b> 3 simultaneous\n"
        "<b>Daily loss cap:</b> -3% → auto-halt\n"
        "<b>Consecutive losses:</b> 5 → 24h halt\n\n"
        "<b>Trailing Stop Stages:</b>\n"
        "• 1.5R → Move SL to breakeven\n"
        "• 2.0R → Activate trail (0.5R from extreme)\n"
        "• 3.0R → Tighten trail (0.3R from extreme)\n\n"
        "<b>Partial Take-Profits:</b>\n"
        "• 2R → Close 50% of position\n"
        "• 3R → Close 25% of position\n"
        "• Remainder trails with tightened SL\n\n"
        "<b>Correlation filter (strict):</b>\n"
        "  Only 1 trade per direction across all pairs."
    ),
    (
        "📚 <b>Handbook — Sessions & News (3/3)</b>\n\n"
        "<b>Session Rules (UTC):</b>\n"
        "• Asian  00-07 → Score ≥ 10, 70% size, tighter SL\n"
        "• London 07-12 → Score ≥ 8, full size\n"
        "• Overlap 12-14 → Score ≥ 8, best liquidity\n"
        "• NY Open 12-16 → Score ≥ 8, wider SL (+20%)\n"
        "• Quiet  21-01 → No new trades\n\n"
        "<b>News Shield (Finnhub):</b>\n"
        "  Blocks trading ±30 min around high-impact\n"
        "  events (NFP, FOMC, CPI, etc.).\n\n"
        "<b>Weekly restart:</b> Sunday 00:00 UTC (RAM flush)\n"
        "<b>Alwaysdata:</b> Log in every 120 days\n\n"
        "<b>Commands quick-ref:</b>\n"
        "/status /trades /performance /equity\n"
        "/zones /last /chart /backtest /news /mode\n"
        "/config /pairs /reminder /summary /log\n"
        "/stop /restart /help"
    ),
]


def _screen_handbook(page: int = 0) -> Tuple[str, Dict[str, Any]]:
    n    = len(_HANDBOOK_PAGES)
    page = max(0, min(page, n - 1))
    text = _HANDBOOK_PAGES[page]
    nav  = []
    if page > 0:
        nav.append(_btn("◀️ Prev", f"handbook:{page - 1}"))
    if page < n - 1:
        nav.append(_btn("▶️ Next", f"handbook:{page + 1}"))
    rows: List[List[Dict]] = []
    if nav:
        rows.append(nav)
    rows.append(BACK_ROW)
    return text, _kb(rows)


def _screen_help() -> Tuple[str, Dict[str, Any]]:
    text = (
        "🆘 <b>Commands</b>\n"
        "/start — main menu\n"
        "/status — bot status, HTF bias, open trades\n"
        "/trades — open + recent closed trades\n"
        "/performance — win rate over different windows\n"
        "/equity — equity, daily P&L, halt status\n"
        "/zones — active supply / demand zones\n"
        "/last — last signal per symbol\n"
        "/summary — daily paper-trade summary\n"
        "/news — news shield status + upcoming events\n"
        "/mode — switch scalp / day / swing / all\n"
        "/config — ⚠️ live tuning of risk settings\n"
        "/pairs — ⚠️ manage which pairs are scanned\n"
        "/log — recent log lines\n"
        "/chart — render a chart for a symbol\n"
        "/backtest — ⚠️ run backtest for a symbol\n"
        "/reminder — Alwaysdata 120-day reminder\n"
        "/handbook — embedded trading rules\n"
        "/stop — 🔴 pause new trade scanning\n"
        "/restart — 🔴 restart the bot process\n"
        "/help — show this menu"
        + LEGEND
    )
    return text, _kb([BACK_ROW])


# ---------------------------------------------------------------------------
# Confirmation screens
# ---------------------------------------------------------------------------

def _confirm_stop() -> Tuple[str, Dict[str, Any]]:
    return (
        "🔴 <b>STOP BOT</b>\n\nBot will stop scanning for new signals.\nAre you sure?",
        _kb([[_btn("✅ Yes, Stop Bot", "do:stop"), _btn("❌ Cancel", "nav:home")]]),
    )


def _confirm_resume() -> Tuple[str, Dict[str, Any]]:
    return (
        "▶️ <b>Resume Scanning</b>\nThe bot will start scanning again.",
        _kb([[_btn("✅ Yes, Resume", "do:resume"), _btn("❌ Cancel", "nav:home")]]),
    )


def _confirm_restart() -> Tuple[str, Dict[str, Any]]:
    return (
        "🔴 <b>RESTART BOT</b>\n\n⚠️ Brief downtime. Open trades preserved.\nAre you sure?",
        _kb([[_btn("✅ Yes, Restart", "do:restart"), _btn("❌ Cancel", "nav:home")]]),
    )


def _confirm_remove_pair(symbol: str) -> Tuple[str, Dict[str, Any]]:
    return (
        f"🔴 <b>REMOVE {symbol}</b>\n\n⚠️ Will stop scanning immediately.\nAre you sure?",
        _kb([[_btn(f"✅ Yes, Remove {symbol}", f"do:rm_pair:{symbol}"), _btn("❌ Cancel", "nav:pairs")]]),
    )


def _confirm_resume_risk() -> Tuple[str, Dict[str, Any]]:
    return (
        "⚠️ <b>Clear All Halts</b>\n\nRemoves all halts, cooldowns, and pair blocks.\n"
        "<b>This overrides daily-loss protection.</b>",
        _kb([[_btn("✅ Yes, Clear", "do:resume_risk"), _btn("❌ Cancel", "nav:equity")]]),
    )


# ---------------------------------------------------------------------------
# Action executors
# ---------------------------------------------------------------------------

def _do_stop() -> Tuple[str, Dict[str, Any]]:
    runtime_settings.request_stop(True)
    log.warning("Telegram: stop requested.")
    return ("🛑 <b>Bot stopped</b>\nScanning paused.",
            _kb([[_btn("▶️ Resume", "confirm:resume")], BACK_ROW]))


def _do_resume() -> Tuple[str, Dict[str, Any]]:
    runtime_settings.request_stop(False)
    return _screen_status()


def _do_restart() -> Tuple[str, Dict[str, Any]]:
    runtime_settings.request_restart(True)
    log.warning("Telegram: restart requested.")
    return ("🔄 <b>Restarting…</b>\nSend /status in ~20 s.", _kb([BACK_ROW]))


def _do_remove_pair(symbol: str) -> Tuple[str, Dict[str, Any]]:
    runtime_settings.remove_symbol(symbol)
    return _screen_pairs()


def _do_resume_risk() -> Tuple[str, Dict[str, Any]]:
    msg = get_risk_manager().clear_halts()
    log.warning("Telegram: clear_halts — %s", msg)
    return _screen_equity()


def _do_cfg(key: str, op: str) -> Tuple[str, Dict[str, Any]]:
    delta = 1 if op == "+" else -1
    if key == "max_trades":
        runtime_settings.adjust_max_open_trades(delta)
    elif key == "loss_cap":
        runtime_settings.adjust_daily_loss_cap_pct(0.5 * delta)
    elif key == "risk":
        runtime_settings.adjust_risk_per_trade_pct(0.25 * delta)
    return _screen_config()


def _do_mode(mode: str) -> Tuple[str, Dict[str, Any]]:
    runtime_settings.set_mode(mode)
    log.info("Mode set to %s via Telegram", mode)
    return _screen_mode()


def _do_corr(corr: str) -> Tuple[str, Dict[str, Any]]:
    runtime_settings.set_correlation_mode(corr)
    return _screen_mode()


def _do_toggle_news() -> Tuple[str, Dict[str, Any]]:
    current = runtime_settings.get_news_shield_enabled()
    runtime_settings.set_news_shield(not current)
    return _screen_news()


def _do_chart(symbol: str) -> Tuple[str, Dict[str, Any]]:
    snap = _snapshot()
    sig  = snap["last_signal"].get(symbol)
    if not sig:
        return (
            f"📉 <b>{symbol}</b>\nNo recent signal to chart yet.",
            _kb([[_btn("🔙 Back to Charts", "nav:chart")], BACK_ROW]),
        )
    try:
        from chart_renderer import render_signal_chart  # noqa: WPS433
        from data_fetcher import fetch_ohlcv  # noqa: WPS433
        mode = sig.get("mode", "day")
        from timeframe_manager import timeframes_for_mode  # noqa: WPS433
        _, entry_tf = timeframes_for_mode(mode)
        df   = fetch_ohlcv(symbol, timeframe=entry_tf, limit=200)
        path = render_signal_chart(df, sig)
        if path and os.path.exists(path):
            send_photo(path, caption=f"📉 {symbol} — last setup ({mode})")
        text = f"📉 <b>{symbol}</b>\nChart sent above."
    except Exception as e:  # noqa: BLE001
        log.warning("chart render failed: %s", e)
        text = f"📉 <b>{symbol}</b>\n⚠️ Render failed: <code>{_html_escape(str(e))}</code>"
    return text, _kb([[_btn("🔙 Back", "nav:chart")], BACK_ROW])


def _do_backtest(symbol: str) -> Tuple[str, Dict[str, Any]]:
    """Trigger a quick 90-day backtest in a background thread."""
    mode = runtime_settings.get_mode()
    if mode == "all":
        mode = "day"
    text = (
        f"🧪 <b>Backtest started</b>\n"
        f"{symbol} | {mode} | last 90 days\n\n"
        "Results will arrive as a Telegram message. This takes ~1–2 min."
    )
    def _run():
        try:
            from backtest import run_backtest  # noqa: WPS433
            from datetime import timedelta
            end   = datetime.now(timezone.utc)
            start = end - timedelta(days=90)
            summary = run_backtest(symbol, start, end, mode=mode)
            if summary:
                send_message(
                    f"🧪 <b>Backtest Result — {symbol} ({mode})</b>\n"
                    f"Trades: {summary['trades']} | WR: {summary['win_rate']:.1f}%\n"
                    f"Avg R: {summary['avg_R']:+.3f} | Sharpe: {summary['sharpe']:.2f}\n"
                    f"Expectancy: {summary['expectancy']:+.3f}R | MaxDD: {summary['max_drawdown_pct']:.2f}%\n"
                    f"PF: {summary.get('profit_factor') or 'inf'}"
                )
                if summary.get("equity_png") and os.path.exists(summary["equity_png"]):
                    send_photo(summary["equity_png"], caption=f"📊 {symbol} equity curve ({mode})")
        except Exception as e:  # noqa: BLE001
            log.error("Telegram backtest %s: %s", symbol, e)
            send_message(f"🧪 <b>Backtest failed for {symbol}</b>\n<code>{e}</code>")
    threading.Thread(target=_run, daemon=True, name=f"bt-{symbol}").start()
    return text, _kb([[_btn("🔙 Back", "nav:backtest")], BACK_ROW])


def _start_add_pair_flow(chat_id: int) -> Tuple[str, Dict[str, Any]]:
    _set_chat_state(chat_id, "awaiting", "add_pair")
    return (
        "➕ <b>Add Pair</b>\n\nSend the symbol, e.g. <code>EUR/USD</code>.\nSend /cancel to abort.",
        _kb([[_btn("❌ Cancel", "pair:cancel_add")]]),
    )


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

COMMAND_SCREENS: Dict[str, Callable[[], Tuple[str, Dict[str, Any]]]] = {
    "/start":       _screen_home,
    "/menu":        _screen_home,
    "/status":      _screen_status,
    "/trades":      lambda: _screen_trades(0),
    "/performance": lambda: _screen_performance("menu"),
    "/zones":       _screen_zones,
    "/last":        _screen_last,
    "/equity":      _screen_equity,
    "/pnl":         _screen_equity,
    "/summary":     _screen_summary,
    "/news":        _screen_news,
    "/mode":        _screen_mode,
    "/config":      _screen_config,
    "/pairs":       _screen_pairs,
    "/log":         _screen_log,
    "/chart":       _screen_chart,
    "/backtest":    _screen_backtest,
    "/reminder":    _screen_reminder,
    "/handbook":    lambda: _screen_handbook(0),
    "/help":        _screen_help,
    "/stop":        _confirm_stop,
    "/restart":     _confirm_restart,
    "/resume":      _confirm_resume_risk,
}


def _route_callback(chat_id: int, data: str) -> Tuple[Optional[str], Optional[Dict], str]:
    if not data or data == "noop":
        return None, None, ""
    parts = data.split(":", 2)
    head  = parts[0]
    try:
        if head == "nav":
            target = parts[1]
            mapper = {
                "home": _screen_home, "status": _screen_status,
                "trades": lambda: _screen_trades(0),
                "performance": lambda: _screen_performance("menu"),
                "zones": _screen_zones, "last": _screen_last,
                "equity": _screen_equity, "summary": _screen_summary,
                "news": _screen_news, "mode": _screen_mode,
                "config": _screen_config, "pairs": _screen_pairs,
                "log": _screen_log, "chart": _screen_chart,
                "backtest": _screen_backtest, "reminder": _screen_reminder,
                "handbook": lambda: _screen_handbook(0), "help": _screen_help,
            }
            if target in mapper:
                t, kb = mapper[target]()
                return t, kb, ""
        elif head == "perf":
            t, kb = _screen_performance(parts[1]); return t, kb, ""
        elif head == "trades":
            page = int(parts[2]) if len(parts) > 2 else 0
            t, kb = _screen_trades(page); return t, kb, ""
        elif head == "handbook":
            t, kb = _screen_handbook(int(parts[1])); return t, kb, ""
        elif head == "cfg":
            t, kb = _do_cfg(parts[1], parts[2]); return t, kb, "Updated"
        elif head == "confirm":
            sub = parts[1]
            if sub == "stop":         t, kb = _confirm_stop();                return t, kb, ""
            if sub == "resume":       t, kb = _confirm_resume();              return t, kb, ""
            if sub == "restart":      t, kb = _confirm_restart();             return t, kb, ""
            if sub == "rm_pair":      t, kb = _confirm_remove_pair(parts[2]); return t, kb, ""
            if sub == "resume_risk":  t, kb = _confirm_resume_risk();         return t, kb, ""
        elif head == "do":
            sub = parts[1]
            if sub == "stop":         t, kb = _do_stop();             return t, kb, "Bot stopped"
            if sub == "resume":       t, kb = _do_resume();           return t, kb, "Resumed"
            if sub == "restart":      t, kb = _do_restart();          return t, kb, "Restarting"
            if sub == "rm_pair":      t, kb = _do_remove_pair(parts[2]); return t, kb, f"Removed {parts[2]}"
            if sub == "resume_risk":  t, kb = _do_resume_risk();      return t, kb, "Halts cleared"
            if sub == "toggle_news":  t, kb = _do_toggle_news();      return t, kb, "News shield toggled"
            if sub == "mode":         t, kb = _do_mode(parts[2]);     return t, kb, f"Mode: {parts[2]}"
            if sub == "corr":         t, kb = _do_corr(parts[2]);     return t, kb, f"Correlation: {parts[2]}"
        elif head == "pair":
            sub = parts[1]
            if sub == "add":
                t, kb = _start_add_pair_flow(chat_id); return t, kb, ""
            if sub == "cancel_add":
                _pop_chat_state(chat_id, "awaiting")
                t, kb = _screen_pairs(); return t, kb, "Cancelled"
        elif head == "chart":
            t, kb = _do_chart(parts[1]); return t, kb, ""
        elif head == "bt":
            t, kb = _do_backtest(parts[1]); return t, kb, "Backtest queued"
    except Exception as e:  # noqa: BLE001
        log.error("callback %s failed: %s", data, e)
        return f"⚠️ <b>Action failed</b>\n<code>{e}</code>", _kb([[_btn("🔄 Retry", data)], BACK_ROW]), "Failed"

    log.warning("Unhandled callback: %s", data)
    return None, None, ""


# ---------------------------------------------------------------------------
# Update processing
# ---------------------------------------------------------------------------

def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _process_message(msg: Dict[str, Any]) -> None:
    text    = (msg.get("text") or "").strip()
    chat    = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not text or chat_id is None:
        return

    if text.lower().startswith("/cancel"):
        _pop_chat_state(chat_id, "awaiting")
        st, kb = _screen_home()
        send_message("Cancelled.\n\n" + st, chat_id=str(chat_id), reply_markup=kb)
        return

    awaiting = _peek_chat_state(chat_id, "awaiting")
    if awaiting == "add_pair" and not text.startswith("/"):
        _pop_chat_state(chat_id, "awaiting")
        sym = text.strip().upper()
        if runtime_settings.add_symbol(sym):
            notice = f"✅ Added <code>{sym}</code>."
        else:
            notice = f"⚠️ Couldn't add <code>{_html_escape(sym)}</code>. Use BASE/QUOTE format."
        st, kb = _screen_pairs()
        send_message(notice + "\n\n" + st, chat_id=str(chat_id), reply_markup=kb)
        return

    cmd = text.split()[0].split("@")[0].lower()
    builder = COMMAND_SCREENS.get(cmd)
    if not builder:
        return
    try:
        screen_text, kb = builder()
    except Exception as e:  # noqa: BLE001
        log.error("command %s failed: %s", cmd, e)
        screen_text = f"⚠️ <b>Command failed</b>\n<code>{e}</code>"
        kb = _kb([[_btn("🔙 Home", "nav:home")]])
    send_message(screen_text, chat_id=str(chat_id), reply_markup=kb)


def _process_callback(cbq: Dict[str, Any]) -> None:
    cb_id      = cbq.get("id")
    data       = cbq.get("data") or ""
    msg        = cbq.get("message") or {}
    chat       = msg.get("chat") or {}
    chat_id    = chat.get("id")
    message_id = msg.get("message_id")
    if not cb_id or chat_id is None or message_id is None:
        return
    text, kb, toast = _route_callback(chat_id, data)
    answer_callback_query(cb_id, text=toast)
    if text is None:
        return
    edited = edit_message(str(chat_id), int(message_id), text, reply_markup=kb)
    if not edited:
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
    log.info("Telegram command listener started.")
    offset = 0
    try:
        r = requests.get(_api("getUpdates"), params={"timeout": 0}, timeout=10)
        data = r.json()
        if data.get("ok") and data.get("result"):
            offset = data["result"][-1]["update_id"] + 1
    except Exception as e:  # noqa: BLE001
        log.warning("Initial drain failed: %s", e)

    conflict_count = 0
    while True:
        try:
            r = requests.get(
                _api("getUpdates"),
                params={"timeout": 25, "offset": offset,
                        "allowed_updates": '["message","callback_query"]'},
                timeout=35,
            )
            if r.status_code == 409:
                conflict_count += 1
                if conflict_count <= 3 or conflict_count % 30 == 0:
                    log.warning("Telegram 409 Conflict — another instance may be polling.")
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
        log.info("Telegram listener disabled (TELEGRAM_LISTEN=0).")
        return None
    t = threading.Thread(target=_poll_loop, name="telegram-commands", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Restart helper
# ---------------------------------------------------------------------------

def perform_restart() -> None:
    log.warning("perform_restart: pulling latest from GitHub then re-execing")
    try:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        subprocess.run(["git", "-C", repo_root, "pull", "--ff-only"], timeout=30, check=False)
    except Exception as e:  # noqa: BLE001
        log.warning("git pull failed: %s", e)
    try:
        os.execv(sys.executable, [sys.executable, *sys.argv])
    except Exception as e:  # noqa: BLE001
        log.error("os.execv failed: %s — exiting hard", e)
        os._exit(1)
