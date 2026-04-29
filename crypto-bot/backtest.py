"""Walk-forward backtester for the strategy.

Usage:
    python backtest.py BTC/USDT 2024-01-01 2024-04-01
"""
import sys
from datetime import datetime, timezone
import pandas as pd

from config import EXCHANGE, HTF_EMA_PERIOD
from data_fetcher import get_exchange, fetch_ohlcv, ema, htf_bias
from zone_detector import detect_zones, filter_active_zones
from signal_engine import evaluate_zone, should_send
from logger_setup import get_logger

log = get_logger("backtest")

WINDOW = 300  # 15m bars per simulation slice


def fetch_range(symbol: str, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
    ex = get_exchange(EXCHANGE)
    since = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    all_rows = []
    while True:
        chunk = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=1000)
        if not chunk:
            break
        all_rows.extend(chunk)
        since = chunk[-1][0] + 1
        if since >= end_ms or len(chunk) < 1000:
            break
    df = pd.DataFrame(all_rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts").astype(float)
    return df[df.index <= pd.Timestamp(end, tz="UTC")]


def run_backtest(symbol: str, start: datetime, end: datetime) -> dict:
    log.info("Fetching %s 4h...", symbol)
    df_4h_full = fetch_range(symbol, "4h", start, end)
    log.info("Fetching %s 15m...", symbol)
    df_15_full = fetch_range(symbol, "15m", start, end)
    log.info("4h bars=%d  15m bars=%d", len(df_4h_full), len(df_15_full))

    trades = []
    seen_ids = set()

    for i in range(WINDOW, len(df_15_full)):
        slice_15 = df_15_full.iloc[i - WINDOW : i + 1]
        now_ts = slice_15.index[-1]
        # corresponding 4h slice up to now
        df_4h = df_4h_full[df_4h_full.index <= now_ts]
        if len(df_4h) < HTF_EMA_PERIOD + 5:
            continue
        bias = htf_bias(df_4h, HTF_EMA_PERIOD)
        if bias == "flat":
            continue
        zones = detect_zones(slice_15)
        zones = filter_active_zones(zones, slice_15)
        for z in zones[-5:]:
            sig = evaluate_zone(symbol, slice_15, z, bias)
            if sig is None:
                continue
            if not should_send(sig):
                continue
            sid = f"{z.origin_ts}-{sig.direction}"
            if sid in seen_ids:
                continue
            seen_ids.add(sid)
            # walk forward to determine result
            future = df_15_full.iloc[i + 1 :]
            entry = sig.entry
            sl = sig.stop_loss
            tp = sig.take_profit
            result = None
            for _, row in future.iterrows():
                if sig.direction == "long":
                    if row["low"] <= sl:
                        result = "loss"; exit_p = sl; break
                    if row["high"] >= tp:
                        result = "win"; exit_p = tp; break
                else:
                    if row["high"] >= sl:
                        result = "loss"; exit_p = sl; break
                    if row["low"] <= tp:
                        result = "win"; exit_p = tp; break
            if result is None:
                continue
            risk = abs(entry - sl)
            reward = abs(tp - entry)
            r_mult = reward / risk if result == "win" else -1.0
            trades.append({**sig.to_dict(), "result": result, "exit": exit_p, "r": r_mult})

    wins = [t for t in trades if t["result"] == "win"]
    losses = [t for t in trades if t["result"] == "loss"]
    n = len(trades)
    win_rate = len(wins) / n * 100 if n else 0.0
    avg_rr = sum(t["r"] for t in trades) / n if n else 0.0
    gross_win = sum(t["r"] for t in wins)
    gross_loss = abs(sum(t["r"] for t in losses))
    pf = gross_win / gross_loss if gross_loss else float("inf")

    summary = {
        "symbol": symbol,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "trades": n,
        "win_rate": round(win_rate, 2),
        "avg_R": round(avg_rr, 3),
        "profit_factor": round(pf, 3) if gross_loss else None,
    }
    log.info("Backtest summary: %s", summary)
    return summary


def main():
    if len(sys.argv) < 4:
        print("Usage: python backtest.py BTC/USDT 2024-01-01 2024-04-01")
        sys.exit(1)
    symbol = sys.argv[1]
    start = datetime.fromisoformat(sys.argv[2]).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(sys.argv[3]).replace(tzinfo=timezone.utc)
    run_backtest(symbol, start, end)


if __name__ == "__main__":
    main()
