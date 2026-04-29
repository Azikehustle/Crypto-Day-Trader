"""Main loop: orchestrate the full bot pipeline."""
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
    ZONE_PROXIMITY_PCT,
)
from logger_setup import get_logger
from data_fetcher import fetch_ohlcv, htf_bias
from zone_detector import detect_zones, filter_active_zones
from signal_engine import evaluate_zone, should_send
from telegram_bot import send_message, send_signal, format_signal
from paper_trader import open_trade, update_trades_with_price, daily_summary
from command_handler import (
    start_in_background as start_command_listener,
    update_state,
    update_symbol_state,
)
from github_sync import start_in_background as start_github_sync

log = get_logger("main")

# Per-symbol caches
_htf_cache: Dict[str, dict] = {}
_last_signal_ts: Dict[str, str] = {}
_last_summary_day: Dict[str, str] = {"day": ""}


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


def process_symbol(symbol: str) -> None:
    bias = get_htf_bias_cached(symbol)
    df = fetch_ohlcv(symbol, timeframe=ENTRY_TIMEFRAME, limit=300, exchange_name=EXCHANGE)
    if df.empty:
        return

    last_high = float(df["high"].iloc[-1])
    last_low = float(df["low"].iloc[-1])
    last_close = float(df["close"].iloc[-1])

    # Manage open paper trades first
    closed = update_trades_with_price(symbol, last_high, last_low)
    for c in closed:
        emoji = "✅" if c["result"] == "win" else "❌"
        send_message(
            f"{emoji} <b>Closed {c['symbol']} {c['direction'].upper()}</b>\n"
            f"Exit: {c['exit_price']:.4f} | P&L: {c['pnl_pct']:+.2f}%"
        )

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
        if should_send(sig):
            log.info("SIGNAL %s score=%d", symbol, sig.score)
            send_signal(signal_dict)
            open_trade(signal_dict)
        elif sig.score >= SCORE_THRESHOLD_LOG:
            log.info(
                "Partial setup %s score=%d (no alert)", symbol, sig.score
            )
        _last_signal_ts[symbol] = sig_id


def maybe_send_daily_summary() -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    hour = datetime.now(timezone.utc).hour
    if hour == 0 and _last_summary_day["day"] != today:
        send_message(daily_summary())
        _last_summary_day["day"] = today


def run_forever() -> None:
    log.info("Starting bot. Exchange=%s Symbols=%s", EXCHANGE, ", ".join(SYMBOLS))
    start_command_listener()
    start_github_sync()
    send_message(
        "🤖 <b>Crypto bot online</b>\n"
        f"Exchange: {EXCHANGE}\n"
        f"Symbols: {', '.join(SYMBOLS)}\n"
        f"HTF: {HTF_TIMEFRAME} | Entry: {ENTRY_TIMEFRAME}\n"
        f"Score threshold: {SCORE_THRESHOLD_SEND}/13\n"
        "Send /help to see commands."
    )
    while True:
        for symbol in SYMBOLS:
            try:
                process_symbol(symbol)
            except Exception as e:  # noqa: BLE001
                log.error("process_symbol %s error: %s\n%s", symbol, e, traceback.format_exc())
        update_state(last_loop_at=datetime.now(timezone.utc).isoformat())
        maybe_send_daily_summary()
        time.sleep(LOOP_SLEEP_SECONDS)


if __name__ == "__main__":
    try:
        run_forever()
    except KeyboardInterrupt:
        log.info("Shutting down on KeyboardInterrupt.")
