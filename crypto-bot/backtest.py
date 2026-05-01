"""Walk-forward backtester — forex pairs, Polars DataFrames, Twelvedata.

Usage (command line):
    python backtest.py EUR/USD 2024-01-01 2024-04-01 [day|scalp|swing]

Outputs:
    Console summary + data/backtest_equity_<symbol>.png
"""
from __future__ import annotations

import os
import sys
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import polars as pl

from config import HTF_EMA_PERIOD, BACKTEST_DIR
from data_fetcher import fetch_ohlcv, fetch_batch_for_symbols, htf_bias
from zone_detector import detect_zones, filter_active_zones
from signal_engine import evaluate_zone, should_send
from timeframe_manager import timeframes_for_mode
from logger_setup import get_logger

log = get_logger("backtest")

WINDOW = 300  # entry-TF bars per simulation slice


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def fetch_range(
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    limit: int = 2000,
) -> pl.DataFrame:
    """Fetch historical bars via the live data pipeline (Twelvedata → fallback).

    We fetch a large limit; filtering by date is done after.
    """
    df = fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    if df.is_empty():
        return df
    start_utc = start.replace(tzinfo=timezone.utc) if not start.tzinfo else start
    end_utc   = end.replace(tzinfo=timezone.utc)   if not end.tzinfo   else end
    return df.filter(
        (pl.col("ts") >= pl.lit(start_utc).cast(pl.Datetime("us", "UTC"))) &
        (pl.col("ts") <= pl.lit(end_utc).cast(pl.Datetime("us", "UTC")))
    )


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def _sharpe(returns: List[float], periods_per_year: float = 252.0) -> float:
    if len(returns) < 2:
        return 0.0
    import numpy as np
    arr = np.array(returns, dtype=float)
    std = arr.std(ddof=1)
    if std == 0:
        return 0.0
    return float(arr.mean() / std * math.sqrt(periods_per_year))


def _max_drawdown_pct(equity: List[float]) -> float:
    if not equity:
        return 0.0
    import numpy as np
    arr = np.array(equity, dtype=float)
    running_max = np.maximum.accumulate(arr)
    running_max[running_max == 0] = 1.0
    dd = (arr - running_max) / running_max
    return float(dd.min() * 100.0)


def _expectancy(trades: List[Dict[str, Any]]) -> float:
    if not trades:
        return 0.0
    return float(sum(t["r"] for t in trades) / len(trades))


def _save_equity_curve(symbol: str, trades: List[Dict[str, Any]]) -> str:
    if not trades:
        return ""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:  # noqa: BLE001
        log.warning("matplotlib unavailable for equity curve: %s", e)
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
        ("Symbol",            summary["symbol"]),
        ("Period",            f"{summary['start']} → {summary['end']}"),
        ("Mode",              summary.get("mode", "day")),
        ("Trades",            summary["trades"]),
        ("Win rate",          f"{summary['win_rate']:.2f}%"),
        ("Avg R",             f"{summary['avg_R']:+.3f}"),
        ("Expectancy",        f"{summary['expectancy']:+.3f} R / trade"),
        ("Profit factor",     ("inf" if summary["profit_factor"] is None else f"{summary['profit_factor']:.3f}")),
        ("Sharpe (R-stream)", f"{summary['sharpe']:.2f}"),
        ("Max drawdown",      f"{summary['max_drawdown_pct']:.2f}%"),
        ("Equity PNG",        summary.get("equity_png") or "(none)"),
    ]
    width = max(len(k) for k, _ in rows) + 2
    print("\n" + "=" * 60)
    print("BACKTEST SUMMARY".center(60))
    print("=" * 60)
    for k, v in rows:
        print(f"  {k.ljust(width)}{v}")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Main backtest runner
# ---------------------------------------------------------------------------

def run_backtest(
    symbol: str,
    start: datetime,
    end: datetime,
    mode: str = "day",
) -> Dict[str, Any]:
    htf_tf, entry_tf = timeframes_for_mode(mode)
    log.info("Fetching %s %s (HTF)…", symbol, htf_tf)
    df_htf_full   = fetch_range(symbol, htf_tf,   start, end, limit=2000)
    log.info("Fetching %s %s (entry)…", symbol, entry_tf)
    df_entry_full = fetch_range(symbol, entry_tf, start, end, limit=5000)
    log.info("HTF bars=%d  entry bars=%d", len(df_htf_full), len(df_entry_full))
    if len(df_entry_full) < WINDOW + 5:
        log.error("Not enough entry bars for backtest (%d < %d)", len(df_entry_full), WINDOW + 5)
        return {}

    trades: List[Dict[str, Any]] = []
    seen_ids: set = set()
    htf_ts_list = df_htf_full["ts"].to_list() if not df_htf_full.is_empty() else []
    entry_ts_list = df_entry_full["ts"].to_list()

    for i in range(WINDOW, len(df_entry_full)):
        slice_entry = df_entry_full.slice(i - WINDOW, WINDOW + 1)
        now_ts      = entry_ts_list[i]

        # HTF: everything up to now_ts
        if df_htf_full.is_empty():
            bias = "bullish"  # fallback
        else:
            htf_sub = df_htf_full.filter(pl.col("ts") <= pl.lit(now_ts).cast(pl.Datetime("us", "UTC")))
            if len(htf_sub) < HTF_EMA_PERIOD + 5:
                continue
            bias = htf_bias(htf_sub, HTF_EMA_PERIOD)

        if bias == "flat":
            continue

        zones = detect_zones(slice_entry)
        zones = filter_active_zones(zones, slice_entry)

        for z in zones[-5:]:
            sig = evaluate_zone(symbol, slice_entry, z, bias, timeframe=entry_tf, mode=mode)
            if sig is None or not should_send(sig):
                continue
            sid = f"{z.origin_ts}-{sig.direction}"
            if sid in seen_ids:
                continue
            seen_ids.add(sid)

            # Simulate future price action
            future = df_entry_full.slice(i + 1)
            entry_p = sig.entry
            sl      = sig.stop_loss
            tp      = sig.take_profit
            result  = None
            exit_p  = entry_p

            for row in future.iter_rows(named=True):
                if sig.direction == "long":
                    if row["low"] <= sl:  result = "loss"; exit_p = sl; break
                    if row["high"] >= tp: result = "win";  exit_p = tp; break
                else:
                    if row["high"] >= sl: result = "loss"; exit_p = sl; break
                    if row["low"]  <= tp: result = "win";  exit_p = tp; break

            if result is None:
                continue

            risk    = abs(entry_p - sl)
            r_mult  = abs(tp - entry_p) / risk if result == "win" else -1.0
            trades.append({**sig.to_dict(), "result": result, "exit": exit_p, "r": r_mult})

    wins   = [t for t in trades if t["result"] == "win"]
    losses = [t for t in trades if t["result"] == "loss"]
    n       = len(trades)
    win_rate = len(wins) / n * 100 if n else 0.0
    avg_rr  = sum(t["r"] for t in trades) / n if n else 0.0
    gross_win  = sum(t["r"] for t in wins)
    gross_loss = abs(sum(t["r"] for t in losses))
    pf = (gross_win / gross_loss) if gross_loss else None

    equity_curve = []
    running = 1.0
    for t in trades:
        running *= (1.0 + float(t["r"]) * 0.01)
        equity_curve.append(running)

    sharpe     = _sharpe([float(t["r"]) for t in trades])
    mdd        = _max_drawdown_pct(equity_curve)
    expectancy = _expectancy(trades)
    equity_png = _save_equity_curve(symbol, trades)

    summary = {
        "symbol":           symbol,
        "start":            start.isoformat(),
        "end":              end.isoformat(),
        "mode":             mode,
        "trades":           n,
        "win_rate":         round(win_rate, 2),
        "avg_R":            round(avg_rr, 3),
        "expectancy":       round(expectancy, 3),
        "profit_factor":    round(pf, 3) if pf is not None else None,
        "sharpe":           round(sharpe, 3),
        "max_drawdown_pct": round(mdd, 3),
        "equity_png":       equity_png,
    }
    log.info("Backtest complete: %s", summary)
    _print_summary(summary)
    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 4:
        print("Usage: python backtest.py EUR/USD 2024-01-01 2024-04-01 [day|scalp|swing]")
        sys.exit(1)
    symbol = sys.argv[1]
    start  = datetime.fromisoformat(sys.argv[2]).replace(tzinfo=timezone.utc)
    end    = datetime.fromisoformat(sys.argv[3]).replace(tzinfo=timezone.utc)
    mode   = sys.argv[4] if len(sys.argv) > 4 else "day"
    run_backtest(symbol, start, end, mode=mode)


if __name__ == "__main__":
    main()
