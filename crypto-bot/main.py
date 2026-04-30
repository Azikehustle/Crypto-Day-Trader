"""Main loop: orchestrate the full bot pipeline.

Adds: risk-manager gating, heartbeat pings, crash alerts, file lock to prevent
duplicate instances, and dynamic SL/volume/ATR-aware signal pipeline.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from typing import Dict

from config import (
    EXCHANGE,
    SYMBOLS,
    HTF_TIMEFRAME,
    ENTRY_TIMEFRAME,
    HTF_EMA_PERIOD,
    HTF_REFRESH_MINUTES,
    LOOP_SLEEP_SECONDS,
    SCORE_THRESHOLD_SEND,
    SCORE_THRESHOLD_LOG,
    SCORE_MAX,
    ZONE_PROXIMITY_PCT,
    HEARTBEAT_INTERVAL_HOURS,
    LOCK_FILE,
    LOCK_STALE_SECONDS,
)
from logger_setup import get_logger
from data_fetcher import fetch_ohlcv, htf_bias
from zone_detector import detect_zones, filter_active_zones
from signal_engine import evaluate_zone, should_send
from telegram_bot import (
    send_message,
    send_signal,
    send_heartbeat,
    send_crash_alert,
    send_risk_alert,
)
from paper_trader import open_trade, update_trades_with_price, daily_summary, open_trades_count
from command_handler import (
    start_in_background as start_command_listener,
    update_state,
    update_symbol_state,
)
from github_sync import start_in_background as start_github_sync
from risk_manager import get_risk_manager
from chart_renderer import render_signal_chart
from supabase_client import (
    is_connected as supabase_connected,
    ping as supabase_ping,
    insert_zone,
    sweep_zone,
    prune_old_zones,
    set_bot_state_bulk,
)

log = get_logger("main")

# Per-symbol caches
_htf_cache: Dict[str, dict] = {}
_last_signal_ts: Dict[str, str] = {}
_last_summary_day: Dict[str, str] = {"day": ""}

# Heartbeat / counters
_loop_count: int = 0
_signals_today: int = 0
_signals_today_date: str = ""
_last_heartbeat: datetime = datetime.now(timezone.utc) - timedelta(hours=HEARTBEAT_INTERVAL_HOURS)


# ---------------------------------------------------------------------------
# File lock
# ---------------------------------------------------------------------------

def _acquire_lock() -> bool:
    """Returns True if we hold the lock, False if another fresh instance does."""
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
                    log.warning(
                        "Another instance holds %s (pid=%s, age=%.0fs). Backing off.",
                        LOCK_FILE, other, age,
                    )
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


# ---------------------------------------------------------------------------
# HTF bias caching
# ---------------------------------------------------------------------------

def get_htf_bias_cached(symbol: str) -> str:
    rec = _htf_cache.get(symbol)
    now = datetime.now(timezone.utc)
    if rec and now - rec["fetched_at"] < timedelta(minutes=HTF_REFRESH_MINUTES):
        return rec["bias"]
    df_4h = fetch_ohlcv(
        symbol, timeframe=HTF_TIMEFRAME, limit=HTF_EMA_PERIOD + 50,
        exchange_name=EXCHANGE,
    )
    bias = htf_bias(df_4h, HTF_EMA_PERIOD)
    _htf_cache[symbol] = {"bias": bias, "fetched_at": now}
    update_symbol_state(symbol, htf_bias=bias)
    log.info("HTF bias %s: %s", symbol, bias)
    return bias


# ---------------------------------------------------------------------------
# Per-symbol processing
# ---------------------------------------------------------------------------

def _bump_signal_counter() -> None:
    global _signals_today, _signals_today_date
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today != _signals_today_date:
        _signals_today = 0
        _signals_today_date = today
    _signals_today += 1


def process_symbol(symbol: str) -> None:
    bias = get_htf_bias_cached(symbol)
    df = fetch_ohlcv(symbol, timeframe=ENTRY_TIMEFRAME, limit=300, exchange_name=EXCHANGE)
    if df.empty:
        return

    last_high = float(df["high"].iloc[-1])
    last_low = float(df["low"].iloc[-1])
    last_close = float(df["close"].iloc[-1])

    # --- Manage open paper trades first ---
    rm = get_risk_manager()
    closed = update_trades_with_price(symbol, last_high, last_low)
    for c in closed:
        emoji = "✅" if c["result"] == "win" else "❌"
        send_message(
            f"{emoji} <b>Closed {c['symbol']} {c['direction'].upper()}</b>\n"
            f"Exit: {c['exit_price']:.4f} | "
            f"P&L: {c['pnl_pct']:+.2f}% ({(c.get('pnl_usd') or 0.0):+.2f} USDT)"
        )
        events = rm.register_trade_close(
            c["symbol"], float(c.get("pnl_usd") or 0.0), c["result"]
        )
        for ev in events:
            send_risk_alert(ev)

    if bias == "flat":
        update_symbol_state(symbol, active_zones=[])
        return

    zones = detect_zones(df, lookback=5)
    zones = filter_active_zones(zones, df)

    # Only consider zones price is currently near
    nearby = []
    for z in zones:
        ref = z.high if z.kind == "supply" else z.low
        if abs(last_close - ref) / ref <= max(ZONE_PROXIMITY_PCT * 5, 0.02):
            nearby.append(z)

    update_symbol_state(symbol, active_zones=[
        {"kind": z.kind, "high": z.high, "low": z.low} for z in nearby[-5:]
    ])

    # Persist nearby zones to Supabase (idempotent on symbol+type+origin_ts)
    if supabase_connected():
        for z in nearby[-10:]:
            try:
                z.db_id = insert_zone(
                    symbol=symbol,
                    zone_type=z.kind,
                    price_top=float(z.high),
                    price_bottom=float(z.low),
                    origin_ts=z.origin_ts.isoformat()
                    if hasattr(z.origin_ts, "isoformat")
                    else str(z.origin_ts),
                )
            except Exception as e:  # noqa: BLE001
                log.warning("insert_zone failed for %s: %s", symbol, e)

    open_n = open_trades_count()

    for zone in nearby[-10:]:  # cap work
        try:
            sig = evaluate_zone(symbol, df, zone, bias)
        except Exception as e:  # noqa: BLE001
            log.error("evaluate_zone error %s: %s", symbol, e)
            continue
        if sig is None:
            continue

        sig_id = f"{symbol}-{zone.origin_ts}-{sig.direction}"
        if _last_signal_ts.get(symbol) == sig_id:
            continue

        signal_dict = sig.to_dict()
        update_symbol_state(symbol, last_signal=signal_dict)

        if not should_send(sig):
            if sig.score >= SCORE_THRESHOLD_LOG:
                log.info(
                    "Partial setup %s score=%d/%d (no alert)",
                    symbol, sig.score, SCORE_MAX,
                )
            _last_signal_ts[symbol] = sig_id
            continue

        # Risk manager gate (halt, max open, quiet hours, blocked pair)
        blocked, reason = rm.should_block_signal(symbol, open_n)
        if blocked:
            log.info(
                "Skip signal %s %s (score %d/%d): %s",
                symbol, sig.direction, sig.score, SCORE_MAX, reason,
            )
            _last_signal_ts[symbol] = sig_id
            continue

        # Pair weight (1.0, 0.5 for cooldown)
        weight = rm.get_pair_weight(symbol)
        if weight <= 0:
            _last_signal_ts[symbol] = sig_id
            continue

        log.info(
            "SIGNAL %s %s score=%d/%d weight=%.2f",
            symbol, sig.direction, sig.score, SCORE_MAX, weight,
        )
        # Render chart, then send (chart is best-effort)
        chart_path = None
        try:
            chart_path = render_signal_chart(df, signal_dict)
        except Exception as e:  # noqa: BLE001
            log.warning("Chart render failed: %s", e)
        send_signal(signal_dict, chart_path=chart_path)
        if open_trade(signal_dict, pair_weight=weight, pair_zone_id=zone.db_id):
            open_n += 1
            _bump_signal_counter()
            # Mark zone as swept now that it produced a confirmed signal
            if zone.db_id is not None and signal_dict.get("sweep_confirmed"):
                try:
                    sweep_zone(zone.db_id)
                except Exception as e:  # noqa: BLE001
                    log.warning("sweep_zone failed: %s", e)
        _last_signal_ts[symbol] = sig_id


# ---------------------------------------------------------------------------
# Daily summary + heartbeat
# ---------------------------------------------------------------------------

def maybe_send_daily_summary() -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    hour = datetime.now(timezone.utc).hour
    if hour == 0 and _last_summary_day["day"] != today:
        send_message(daily_summary())
        _last_summary_day["day"] = today


def maybe_send_heartbeat() -> None:
    global _last_heartbeat
    now = datetime.now(timezone.utc)
    if now - _last_heartbeat < timedelta(hours=HEARTBEAT_INTERVAL_HOURS):
        return
    rs = get_risk_manager().status_dict()
    state = {
        "loop_count": _loop_count,
        "signals_today": _signals_today,
        "open_trades": open_trades_count(),
        "exchange": EXCHANGE,
        "daily_pnl_usd": rs["daily_pnl_usd"],
        "running_equity": rs["running_equity"],
    }
    if send_heartbeat(state):
        _last_heartbeat = now


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_forever() -> None:
    global _loop_count
    log.info("Starting bot. Exchange=%s Symbols=%s", EXCHANGE, ", ".join(SYMBOLS))

    if not _acquire_lock():
        log.error("Refusing to start: another instance appears active.")
        sys.exit(0)

    # Verify Supabase connectivity
    if supabase_connected() and supabase_ping():
        log.info("✅ Supabase connected")
        supabase_ok = True
    else:
        log.warning(
            "⚠️ Supabase NOT reachable — bot will run with degraded persistence."
        )
        supabase_ok = False

    # Warm-start risk manager (loads state, runs daily reset / auto-resume)
    rm = get_risk_manager()
    rm.tick()

    start_command_listener()
    start_github_sync()
    send_message(
        "🤖 <b>Crypto bot online</b>\n"
        f"Exchange: {EXCHANGE}\n"
        f"Symbols: {', '.join(SYMBOLS)}\n"
        f"HTF: {HTF_TIMEFRAME} | Entry: {ENTRY_TIMEFRAME}\n"
        f"Score threshold: {SCORE_THRESHOLD_SEND}/{SCORE_MAX}\n"
        f"Supabase: {'✅' if supabase_ok else '❌'}\n"
        "Send /help to see commands."
    )

    # Best-effort prune of old (>7d) inactive zones on startup
    try:
        if supabase_ok:
            n = prune_old_zones(days=7)
            if n:
                log.info("Pruned %s old inactive zones", n)
    except Exception as e:  # noqa: BLE001
        log.warning("prune_old_zones failed: %s", e)

    while True:
        if not _acquire_lock():
            log.warning("Lock contention; skipping this loop iteration.")
            time.sleep(LOOP_SLEEP_SECONDS)
            continue
        _refresh_lock()
        _loop_count += 1
        rm.tick()

        for symbol in SYMBOLS:
            try:
                process_symbol(symbol)
            except Exception as e:  # noqa: BLE001
                log.error(
                    "process_symbol %s error: %s\n%s",
                    symbol, e, traceback.format_exc(),
                )
        update_state(last_loop_at=datetime.now(timezone.utc).isoformat())
        maybe_send_daily_summary()
        maybe_send_heartbeat()

        # Sync runtime counters to Supabase bot_state
        if supabase_connected():
            try:
                set_bot_state_bulk({
                    "loop_count": _loop_count,
                    "signals_today": _signals_today,
                    "signals_today_date": _signals_today_date,
                    "last_loop_at": datetime.now(timezone.utc).isoformat(),
                    "last_heartbeat_at": _last_heartbeat.isoformat(),
                })
            except Exception as e:  # noqa: BLE001
                log.warning("set_bot_state_bulk failed: %s", e)

        time.sleep(LOOP_SLEEP_SECONDS)


def _release_lock() -> None:
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE) as f:
                if f.read().strip() == str(os.getpid()):
                    os.remove(LOCK_FILE)
    except Exception:  # noqa: BLE001
        pass


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
        log.error("FATAL: uncaught exception: %s\n%s", e, tb)
        try:
            send_crash_alert(f"{type(e).__name__}: {e}")
        except Exception:  # noqa: BLE001
            pass
        _release_lock()
        sys.exit(1)
