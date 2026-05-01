"""Main loop: multi-timeframe pipeline with weekly restart & news shield."""
from __future__ import annotations

import os
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from typing import Dict, List

from config import (
    SYMBOLS, HTF_EMA_PERIOD, HTF_REFRESH_MINUTES,
    LOOP_SLEEP_SECONDS, SCORE_THRESHOLD_SEND, SCORE_MAX,
    ZONE_PROXIMITY_PCT, HEARTBEAT_INTERVAL_HOURS,
    LOCK_FILE, LOCK_STALE_SECONDS,
    WEEKLY_RESTART_DAY, ALWAYSDATA_WARN_DAYS,
)
from logger_setup import get_logger
from data_fetcher import fetch_ohlcv, htf_bias, fetch_batch_for_symbols
from zone_detector import detect_zones, filter_active_zones
from signal_engine import evaluate_zone, should_send
from timeframe_manager import (
    timeframes_for_mode, needs_htf_refresh, mark_htf_fetched,
    entry_tf_limit, htf_limit, ALL_MODES,
)
from news_shield import is_news_blocked
from telegram_bot import (
    send_message, send_signal, send_heartbeat, send_crash_alert,
    send_risk_alert, set_my_commands,
)
from paper_trader import open_trade, update_trades_with_price, daily_summary, open_trades_count
from command_handler import (
    start_in_background as start_command_listener,
    update_state, update_symbol_state, perform_restart,
)
from github_sync import start_in_background as start_github_sync
from risk_manager import get_risk_manager
from chart_renderer import render_signal_chart
import runtime_settings
from supabase_client import (
    is_connected as supabase_connected,
    ping as supabase_ping,
    insert_zone, sweep_zone, prune_old_zones, set_bot_state_bulk,
)
from broker import get_broker

log = get_logger("main")

_htf_bias_cache: Dict[str, Dict] = {}
_last_signal_ts: Dict[str, str] = {}
_last_summary_day: Dict[str, str] = {"day": ""}
_loop_count   = 0
_signals_today = 0
_signals_today_date = ""
_last_heartbeat: datetime = datetime.now(timezone.utc) - timedelta(hours=HEARTBEAT_INTERVAL_HOURS)
_start_time: datetime = datetime.now(timezone.utc)
_weekly_restarted_this_week = False


# ---------------------------------------------------------------------------
# File lock
# ---------------------------------------------------------------------------

def _acquire_lock() -> bool:
    try:
        if os.path.exists(LOCK_FILE):
            try:
                age = time.time() - os.path.getmtime(LOCK_FILE)
            except OSError:
                age = LOCK_STALE_SECONDS + 1
            if age < LOCK_STALE_SECONDS:
                with open(LOCK_FILE) as f:
                    other = f.read().strip()
                if other and other != str(os.getpid()):
                    log.warning("Another instance holds lock (pid=%s, age=%.0fs).", other, age)
                    return False
        _refresh_lock()
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("Lock check failed (%s) — proceeding without lock", e)
        return True


def _refresh_lock() -> None:
    try:
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
        os.utime(LOCK_FILE, None)
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to refresh lock: %s", e)


def _release_lock() -> None:
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE) as f:
                if f.read().strip() == str(os.getpid()):
                    os.remove(LOCK_FILE)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# HTF bias cache
# ---------------------------------------------------------------------------

def get_htf_bias_cached(symbol: str, htf_tf: str) -> str:
    rec = _htf_bias_cache.get(f"{symbol}:{htf_tf}")
    now = datetime.now(timezone.utc)
    if rec and now - rec["fetched_at"] < timedelta(minutes=HTF_REFRESH_MINUTES):
        return rec["bias"]
    try:
        df_htf = fetch_ohlcv(symbol, timeframe=htf_tf, limit=HTF_EMA_PERIOD + 50)
        bias   = htf_bias(df_htf, HTF_EMA_PERIOD)
    except Exception as e:  # noqa: BLE001
        log.warning("HTF fetch failed %s %s: %s", symbol, htf_tf, e)
        bias = "flat"
    _htf_bias_cache[f"{symbol}:{htf_tf}"] = {"bias": bias, "fetched_at": now}
    update_symbol_state(symbol, htf_bias=bias)
    log.info("HTF %s %s bias: %s", htf_tf, symbol, bias)
    return bias


# ---------------------------------------------------------------------------
# Signal counter
# ---------------------------------------------------------------------------

def _bump_signal_counter() -> None:
    global _signals_today, _signals_today_date
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today != _signals_today_date:
        _signals_today = 0
        _signals_today_date = today
    _signals_today += 1


# ---------------------------------------------------------------------------
# Per-symbol processing (one mode at a time)
# ---------------------------------------------------------------------------

def process_symbol(symbol: str, mode: str) -> None:
    htf_tf, entry_tf = timeframes_for_mode(mode)
    bias = get_htf_bias_cached(symbol, htf_tf)

    try:
        df = fetch_ohlcv(symbol, timeframe=entry_tf, limit=entry_tf_limit(mode))
    except Exception as e:  # noqa: BLE001
        log.error("fetch_ohlcv %s %s: %s", symbol, entry_tf, e)
        return

    if df is None or df.is_empty():
        return

    last_high  = float(df["high"][-1])
    last_low   = float(df["low"][-1])
    last_close = float(df["close"][-1])

    # ── Manage open trades first ─────────────────────────────────────────────
    rm = get_risk_manager()
    try:
        closed = update_trades_with_price(symbol, last_high, last_low)
    except Exception as e:  # noqa: BLE001
        log.error("update_trades_with_price %s: %s", symbol, e)
        closed = []

    for c in closed:
        emoji = "✅" if c.get("result") == "win" else "❌"
        send_message(
            f"{emoji} <b>Closed {c['symbol']} {c['direction'].upper()}</b>\n"
            f"Exit: {c.get('exit_price', '?'):.5f} | "
            f"P&L: {c.get('pnl_pct', 0.0):+.2f}% ({c.get('pnl_usd', 0.0):+.4f} USD)"
        )
        events = rm.register_trade_close(
            c["symbol"], float(c.get("pnl_usd") or 0.0), c.get("result", "loss")
        )
        for ev in events:
            send_risk_alert(ev)

    if bias == "flat":
        update_symbol_state(symbol, active_zones=[])
        return

    # ── News shield ──────────────────────────────────────────────────────────
    if runtime_settings.get_news_shield_enabled():
        if is_news_blocked(symbol):
            log.info("NEWS BLOCKED %s — skipping signal scan", symbol)
            return

    # ── Zone detection ───────────────────────────────────────────────────────
    zones = detect_zones(df, lookback=5)
    zones = filter_active_zones(zones, df)

    nearby = [z for z in zones
              if abs(last_close - (z.high if z.kind == "supply" else z.low)) /
                 max(abs(last_close), 1e-9) <= max(ZONE_PROXIMITY_PCT * 5, 0.02)]

    update_symbol_state(symbol, active_zones=[
        {"kind": z.kind, "high": z.high, "low": z.low} for z in nearby[-5:]
    ])

    if supabase_connected():
        for z in nearby[-10:]:
            try:
                z.db_id = insert_zone(
                    symbol=symbol,
                    zone_type=z.kind,
                    price_top=float(z.high),
                    price_bottom=float(z.low),
                    origin_ts=z.origin_ts.isoformat() if hasattr(z.origin_ts, "isoformat") else str(z.origin_ts),
                )
            except Exception as e:  # noqa: BLE001
                log.warning("insert_zone failed %s: %s", symbol, e)

    open_n = open_trades_count()
    open_trades = get_broker().get_open_positions()

    for zone in nearby[-10:]:
        try:
            sig = evaluate_zone(
                symbol, df, zone, bias,
                timeframe=entry_tf, mode=mode,
                open_trades=open_trades,
            )
        except Exception as e:  # noqa: BLE001
            log.error("evaluate_zone %s: %s", symbol, e)
            continue
        if sig is None:
            continue

        sig_id = f"{symbol}-{mode}-{zone.origin_ts}-{sig.direction}"
        if _last_signal_ts.get(f"{symbol}:{mode}") == sig_id:
            continue

        signal_dict = sig.to_dict()
        update_symbol_state(symbol, last_signal=signal_dict)

        if not should_send(sig):
            if sig.score >= SCORE_THRESHOLD_SEND - 2:
                log.info("Partial setup %s %s score=%d/%d (no alert)", symbol, mode, sig.score, SCORE_MAX)
            _last_signal_ts[f"{symbol}:{mode}"] = sig_id
            continue

        blocked, reason = rm.should_block_signal(symbol, open_n)
        if blocked:
            log.info("Skip %s %s (score %d/%d): %s", symbol, sig.direction, sig.score, SCORE_MAX, reason)
            _last_signal_ts[f"{symbol}:{mode}"] = sig_id
            continue

        weight = rm.get_pair_weight(symbol)
        if weight <= 0:
            _last_signal_ts[f"{symbol}:{mode}"] = sig_id
            continue

        log.info("SIGNAL %s %s %s score=%d/%d weight=%.2f", mode, symbol, sig.direction, sig.score, SCORE_MAX, weight)

        chart_path = None
        try:
            chart_path = render_signal_chart(df, signal_dict)
        except Exception as e:  # noqa: BLE001
            log.warning("Chart render failed: %s", e)

        send_signal(signal_dict, chart_path=chart_path)
        if open_trade(signal_dict, pair_weight=weight * sig.size_mult, pair_zone_id=getattr(zone, "db_id", None)):
            open_n += 1
            _bump_signal_counter()
            if getattr(zone, "db_id", None) is not None and signal_dict.get("sweep_idx"):
                try:
                    sweep_zone(zone.db_id)
                except Exception as e:  # noqa: BLE001
                    log.warning("sweep_zone failed: %s", e)

        _last_signal_ts[f"{symbol}:{mode}"] = sig_id


# ---------------------------------------------------------------------------
# Daily summary + heartbeat
# ---------------------------------------------------------------------------

def maybe_send_daily_summary() -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    hour  = datetime.now(timezone.utc).hour
    if hour == 0 and _last_summary_day["day"] != today:
        send_message(daily_summary())
        _last_summary_day["day"] = today


def maybe_send_heartbeat() -> None:
    global _last_heartbeat
    now = datetime.now(timezone.utc)
    if now - _last_heartbeat < timedelta(hours=HEARTBEAT_INTERVAL_HOURS):
        return
    rs = get_risk_manager().status_dict()
    send_heartbeat({
        "loop_count":     _loop_count,
        "signals_today":  _signals_today,
        "open_trades":    open_trades_count(),
        "mode":           runtime_settings.get_mode(),
        "daily_pnl_usd":  rs["daily_pnl_usd"],
        "running_equity": rs["running_equity"],
    })
    _last_heartbeat = now


# ---------------------------------------------------------------------------
# Weekly auto-restart
# ---------------------------------------------------------------------------

def maybe_weekly_restart() -> bool:
    """Return True if we just triggered a weekly restart."""
    global _weekly_restarted_this_week
    if not runtime_settings.get_weekly_restart_enabled():
        return False
    now = datetime.now(timezone.utc)
    if now.weekday() == WEEKLY_RESTART_DAY and now.hour == 0 and not _weekly_restarted_this_week:
        _weekly_restarted_this_week = True
        log.info("Weekly auto-restart triggered (Sunday 00:00 UTC)")
        send_message("🔄 <b>Weekly auto-restart</b> — flushing memory and restarting…")
        _release_lock()
        perform_restart()
        return True
    if now.weekday() != WEEKLY_RESTART_DAY:
        _weekly_restarted_this_week = False
    return False


# ---------------------------------------------------------------------------
# Alwaysdata 120-day reminder
# ---------------------------------------------------------------------------

def maybe_alwaysdata_reminder() -> None:
    """Warn once when approaching the 120-day login deadline."""
    age_days = (datetime.now(timezone.utc) - _start_time).days
    for warn_day in ALWAYSDATA_WARN_DAYS:
        if age_days == warn_day:
            send_message(
                f"⚠️ <b>Alwaysdata reminder</b>\n"
                f"The bot has been running for {age_days} days.\n"
                "Log in to Alwaysdata to reset the 120-day inactivity timer and prevent account suspension."
            )
            return


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_forever() -> None:
    global _loop_count

    if not _acquire_lock():
        log.error("Refusing to start: another instance appears active.")
        sys.exit(0)

    supabase_ok = supabase_connected() and supabase_ping()
    if supabase_ok:
        log.info("✅ Supabase connected")
    else:
        log.warning("⚠️ Supabase NOT reachable — running with degraded persistence.")

    runtime_settings.load()
    active_symbols = runtime_settings.get_symbols()
    broker = get_broker()
    log.info("Bot starting. Broker=%s Symbols=%s", broker.name, ", ".join(active_symbols))

    rm = get_risk_manager()
    rm.tick()
    set_my_commands()
    start_command_listener()
    start_github_sync()

    mode_display = runtime_settings.get_mode()
    send_message(
        f"🤖 <b>Forex Bot online</b>\n"
        f"Broker: {broker.name.capitalize()}\n"
        f"Pairs: {', '.join(active_symbols)}\n"
        f"Mode: {mode_display.capitalize()}\n"
        f"Score threshold: {SCORE_THRESHOLD_SEND}/{SCORE_MAX}\n"
        f"News shield: {'🛡️ ON' if runtime_settings.get_news_shield_enabled() else '🔓 off'}\n"
        f"Correlation: {runtime_settings.get_correlation_mode()}\n"
        f"Supabase: {'✅' if supabase_ok else '❌'}\n"
        "Send /start to open the control menu."
    )

    try:
        if supabase_ok:
            n = prune_old_zones(days=7)
            if n:
                log.info("Pruned %s old inactive zones", n)
    except Exception as e:  # noqa: BLE001
        log.warning("prune_old_zones failed: %s", e)

    while True:
        if runtime_settings.is_restart_requested():
            send_message("🔄 <b>Restart in progress…</b>")
            _release_lock()
            perform_restart()
            return

        if maybe_weekly_restart():
            return

        if not _acquire_lock():
            log.warning("Lock contention — skipping this loop iteration.")
            time.sleep(LOOP_SLEEP_SECONDS)
            continue
        _refresh_lock()
        _loop_count += 1
        rm.tick()

        if runtime_settings.is_stopped():
            time.sleep(LOOP_SLEEP_SECONDS)
            continue

        symbols = runtime_settings.get_symbols()
        active_mode = runtime_settings.get_mode()
        modes_to_run = ALL_MODES if active_mode == "all" else [active_mode]

        for mode in modes_to_run:
            for symbol in symbols:
                try:
                    process_symbol(symbol, mode)
                except Exception as e:  # noqa: BLE001
                    log.error("process_symbol %s %s: %s\n%s", symbol, mode, e, traceback.format_exc())

        update_state(last_loop_at=datetime.now(timezone.utc).isoformat())
        maybe_send_daily_summary()
        maybe_send_heartbeat()
        maybe_alwaysdata_reminder()

        if supabase_connected():
            try:
                set_bot_state_bulk({
                    "loop_count":           _loop_count,
                    "signals_today":        _signals_today,
                    "signals_today_date":   _signals_today_date,
                    "last_loop_at":         datetime.now(timezone.utc).isoformat(),
                    "last_heartbeat_at":    _last_heartbeat.isoformat(),
                })
            except Exception as e:  # noqa: BLE001
                log.warning("set_bot_state_bulk failed: %s", e)

        time.sleep(LOOP_SLEEP_SECONDS)


if __name__ == "__main__":
    try:
        run_forever()
    except KeyboardInterrupt:
        log.info("Shutting down on KeyboardInterrupt.")
        _release_lock()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()
        log.error("FATAL: %s\n%s", e, tb)
        try:
            send_crash_alert(f"{type(e).__name__}: {e}")
        except Exception:  # noqa: BLE001
            pass
        _release_lock()
        sys.exit(1)
