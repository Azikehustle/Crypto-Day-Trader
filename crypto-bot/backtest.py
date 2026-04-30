"""Walk-forward backtester for the strategy.

Usage:
    python backtest.py BTC/USDT 2024-01-01 2024-04-01

Outputs:
    - Console summary: trades, win rate, avg R, Sharpe, max DD, profit factor,
      expectancy
    - PNG: data/backtest_equity_<symbol>.png  (cumulative R-multiple equity)
"""
import os
import sys
import math
from datetime import datetime, timezone
from typing import List, Dict, Any
import pandas as pd

from config import EXCHANGE, HTF_EMA_PERIOD, BACKTEST_DIR
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
    end_ts = pd.Timestamp(end) if end.tzinfo else pd.Timestamp(end, tz="UTC")
    return df[df.index <= end_ts]


def _sharpe(returns: List[float], periods_per_year: float = 252.0) -> float:
    if len(returns) < 2:
        return 0.0
    s = pd.Series(returns)
    if s.std(ddof=1) == 0:
        return 0.0
    return float(s.mean() / s.std(ddof=1) * math.sqrt(periods_per_year))


def _max_drawdown_pct(equity: List[float]) -> float:
    if not equity:
        return 0.0
    s = pd.Series(equity)
    running_max = s.cummax()
    dd = (s - running_max) / running_max.replace(0, 1.0)
    return float(dd.min() * 100.0)


def _expectancy(trades: List[Dict[str, Any]]) -> float:
    if not trades:
        return 0.0
    return float(sum(t["r"] for t in trades) / len(trades))


def _save_equity_curve(symbol: str, trades: List[Dict[str, Any]]) -> str:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:  # noqa: BLE001
        log.warning("matplotlib not available, skipping equity curve: %s", e)
        return ""
    if not trades:
        return ""
    cum = []
    running = 0.0
    for t in trades:
        running += float(t["r"])
        cum.append(running)
    safe = symbol.replace("/", "-")
    out = os.path.join(BACKTEST_DIR, f"backtest_equity_{safe}.png")
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.plot(range(1, len(cum) + 1), cum, color="#1976d2", linewidth=1.6)
    ax.fill_between(range(1, len(cum) + 1), cum, color="#1976d2", alpha=0.12)
    ax.axhline(0, color="#888", linewidth=0.6)
    ax.set_title(f"{symbol} — Equity curve (cumulative R)")
    ax.set_xlabel("Trade #")
    ax.set_ylabel("Cumulative R")
    ax.grid(alpha=0.2)
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return out


def _print_summary(summary: Dict[str, Any]) -> None:
    rows = [
        ("Symbol",         summary["symbol"]),
        ("Period",         f"{summary['start']} → {summary['end']}"),
        ("Trades",         summary["trades"]),
        ("Win rate",       f"{summary['win_rate']:.2f}%"),
        ("Avg R",          f"{summary['avg_R']:+.3f}"),
        ("Expectancy",     f"{summary['expectancy']:+.3f} R / trade"),
        ("Profit factor",  ("inf" if summary['profit_factor'] is None
                            else f"{summary['profit_factor']:.3f}")),
        ("Sharpe (R-stream)", f"{summary['sharpe']:.2f}"),
        ("Max drawdown",   f"{summary['max_drawdown_pct']:.2f}%"),
        ("Equity PNG",     summary.get("equity_png") or "(none)"),
    ]
    width = max(len(k) for k, _ in rows) + 2
    print("\n" + "=" * 60)
    print("BACKTEST SUMMARY".center(60))
    print("=" * 60)
    for k, v in rows:
        print(f"  {k.ljust(width)}{v}")
    print("=" * 60 + "\n")


def run_backtest(symbol: str, start: datetime, end: datetime) -> dict:
    log.info("Fetching %s 4h...", symbol)
    df_4h_full = fetch_range(symbol, "4h", start, end)
    log.info("Fetching %s 15m...", symbol)
    df_15_full = fetch_range(symbol, "15m", start, end)
    log.info("4h bars=%d  15m bars=%d", len(df_4h_full), len(df_15_full))

    trades: List[Dict[str, Any]] = []
    seen_ids = set()

    for i in range(WINDOW, len(df_15_full)):
        slice_15 = df_15_full.iloc[i - WINDOW : i + 1]
        now_ts = slice_15.index[-1]
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
            future = df_15_full.iloc[i + 1 :]
            entry = sig.entry
            sl = sig.stop_loss
            tp = sig.take_profit
            result = None
            exit_p = entry
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
    pf = (gross_win / gross_loss) if gross_loss else None

    # Equity curve in R-multiples
    equity_curve = []
    running = 1.0
    for t in trades:
        running *= (1.0 + float(t["r"]) * 0.01)  # 1% notional per R for DD %
        equity_curve.append(running)

    sharpe = _sharpe([float(t["r"]) for t in trades])
    mdd = _max_drawdown_pct(equity_curve)
    expectancy = _expectancy(trades)
    equity_png = _save_equity_curve(symbol, trades)

    summary = {
        "symbol": symbol,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "trades": n,
        "win_rate": round(win_rate, 2),
        "avg_R": round(avg_rr, 3),
        "expectancy": round(expectancy, 3),
        "profit_factor": round(pf, 3) if pf is not None else None,
        "sharpe": round(sharpe, 3),
        "max_drawdown_pct": round(mdd, 3),
        "equity_png": equity_png,
    }
    log.info("Backtest summary: %s", summary)
    _print_summary(summary)
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
