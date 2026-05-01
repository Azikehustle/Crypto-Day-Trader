"""Render annotated signal charts as PNGs for Telegram.

Accepts either pl.DataFrame or pd.DataFrame.
Converts Polars → pandas only for mplfinance (minimal bridge).
mplfinance requires a DatetimeIndex with OHLCV columns.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Union

from logger_setup import get_logger
from config import CHART_DIR

log = get_logger("chart")

CANDLES_TO_SHOW = 60


def _safe_ts(ts: str) -> str:
    return ts.replace(":", "-").replace("/", "-").replace(" ", "_")


def _to_pandas(df):
    """Convert pl.DataFrame or pd.DataFrame → pandas with DatetimeIndex."""
    import pandas as pd
    try:
        import polars as pl
        if isinstance(df, pl.DataFrame):
            pdf = df.to_pandas()
            if "ts" in pdf.columns:
                pdf["ts"] = pd.to_datetime(pdf["ts"], utc=True)
                pdf = pdf.set_index("ts")
            return pdf
    except ImportError:
        pass
    # Already pandas
    if hasattr(df, "iloc"):
        return df
    raise TypeError(f"Cannot convert {type(df)} to pandas DataFrame")


def render_signal_chart(
    df_entry,
    signal_dict: Dict[str, Any],
) -> Optional[str]:
    """Render a chart for the given signal. Returns PNG path or None."""
    try:
        if df_entry is None or len(df_entry) < 5:
            return None
        os.makedirs(CHART_DIR, exist_ok=True)
        symbol = signal_dict.get("symbol", "UNKNOWN").replace("/", "-")
        ts     = signal_dict.get("timestamp") or datetime.now(timezone.utc).isoformat()
        out    = os.path.join(CHART_DIR, f"{symbol}_{_safe_ts(ts)}.png")

        # Build pandas view
        pdf = _to_pandas(df_entry)
        confirm_idx = signal_dict.get("confirm_idx")
        if isinstance(confirm_idx, int) and confirm_idx > 0:
            end = min(len(pdf), confirm_idx + 3)
        else:
            end = len(pdf)
        start  = max(0, end - CANDLES_TO_SHOW)
        view   = pdf.iloc[start:end].copy()
        if view.empty:
            return None

        try:
            return _render_with_mplfinance(view, signal_dict, start, out)
        except Exception as e:  # noqa: BLE001
            log.warning("mplfinance render failed (%s); falling back", e)
            return _render_with_matplotlib(view, signal_dict, start, out)
    except Exception as e:  # noqa: BLE001
        log.error("render_signal_chart failed: %s", e)
        return None


def _render_with_mplfinance(view, sig: Dict[str, Any], offset: int, out: str) -> Optional[str]:
    import mplfinance as mpf
    import matplotlib.pyplot as plt
    import pandas as pd

    direction = sig.get("direction", "long")
    entry     = float(sig.get("entry") or 0.0)
    sl        = float(sig.get("stop_loss") or 0.0)
    tp        = float(sig.get("take_profit") or 0.0)
    zhi       = float(sig.get("zone_high") or 0.0)
    zlo       = float(sig.get("zone_low") or 0.0)
    mss       = sig.get("mss_level")
    session   = sig.get("session", "")
    mode      = sig.get("mode", "")

    sweep_series   = pd.Series(float("nan"), index=view.index)
    confirm_series = pd.Series(float("nan"), index=view.index)

    if isinstance(sig.get("sweep_idx"), int):
        rel = sig["sweep_idx"] - offset
        if 0 <= rel < len(view):
            row = view.iloc[rel]
            sweep_series.iloc[rel] = row["low"] * 0.999 if direction == "long" else row["high"] * 1.001

    if isinstance(sig.get("confirm_idx"), int):
        rel = sig["confirm_idx"] - offset
        if 0 <= rel < len(view):
            row = view.iloc[rel]
            confirm_series.iloc[rel] = row["high"] * 1.001 if direction == "long" else row["low"] * 0.999

    addplots = []
    if sweep_series.notna().any():
        addplots.append(mpf.make_addplot(
            sweep_series, type="scatter",
            marker="^" if direction == "long" else "v",
            markersize=140, color="#ff9800",
        ))
    if confirm_series.notna().any():
        addplots.append(mpf.make_addplot(
            confirm_series, type="scatter",
            marker="^" if direction == "long" else "v",
            markersize=180,
            color="#2e7d32" if direction == "long" else "#c62828",
        ))

    hlines = dict(
        hlines=[entry, sl, tp] + ([float(mss)] if mss else []),
        colors=["#1976d2", "#c62828", "#2e7d32"] + (["#9c27b0"] if mss else []),
        linestyle=["--", ":", ":"] + (["-."] if mss else []),
        linewidths=[1.2, 1.0, 1.0] + ([1.0] if mss else []),
    )

    tags = " | ".join(filter(None, [
        mode.capitalize() if mode else "",
        session if session else "",
        f"RR {sig.get('rr', '?')}",
    ]))
    title = (
        f"{sig.get('symbol', '')} {direction.upper()}  "
        f"score {sig.get('score', '?')}/{sig.get('score_max', 15)}  {tags}"
    )
    fig, axes = mpf.plot(
        view, type="candle", style="charles",
        addplot=addplots if addplots else None,
        hlines=hlines, volume=True,
        figratio=(16, 9), figscale=1.1,
        returnfig=True, title=title,
    )
    ax = axes[0]
    if zhi > 0 and zlo > 0:
        color = "#2e7d32" if direction == "long" else "#c62828"
        ax.axhspan(zlo, zhi, color=color, alpha=0.12, zorder=0)
    fig.savefig(out, dpi=110, bbox_inches="tight")
    plt.close(fig)
    return out


def _render_with_matplotlib(view, sig: Dict[str, Any], offset: int, out: str) -> Optional[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    fig, ax = plt.subplots(figsize=(11, 6))
    x     = np.arange(len(view))
    width = 0.6

    for i, (_, row) in enumerate(view.iterrows()):
        color = "#2e7d32" if row["close"] >= row["open"] else "#c62828"
        ax.vlines(i, row["low"], row["high"], color=color, linewidth=1)
        ax.add_patch(plt.Rectangle(
            (i - width / 2, min(row["open"], row["close"])), width,
            abs(row["close"] - row["open"]) or (row["high"] - row["low"]) * 0.05,
            color=color, alpha=0.85,
        ))

    direction = sig.get("direction", "long")
    entry     = float(sig.get("entry") or 0.0)
    sl        = float(sig.get("stop_loss") or 0.0)
    tp        = float(sig.get("take_profit") or 0.0)
    zhi       = float(sig.get("zone_high") or 0.0)
    zlo       = float(sig.get("zone_low") or 0.0)
    mss       = sig.get("mss_level")

    if entry: ax.axhline(entry, color="#1976d2", linestyle="--", linewidth=1.2, label=f"Entry {entry:.5f}")
    if sl:    ax.axhline(sl,    color="#c62828", linestyle=":",  linewidth=1.0, label=f"SL {sl:.5f}")
    if tp:    ax.axhline(tp,    color="#2e7d32", linestyle=":",  linewidth=1.0, label=f"TP {tp:.5f}")
    if mss:   ax.axhline(float(mss), color="#9c27b0", linestyle="-.", linewidth=1.0, label=f"MSS {float(mss):.5f}")
    if zhi > 0 and zlo > 0:
        c = "#2e7d32" if direction == "long" else "#c62828"
        ax.axhspan(zlo, zhi, color=c, alpha=0.12)

    if isinstance(sig.get("sweep_idx"), int):
        rel = sig["sweep_idx"] - offset
        if 0 <= rel < len(view):
            row = view.iloc[rel]
            y = row["low"] if direction == "long" else row["high"]
            ax.annotate("sweep", xy=(rel, y),
                        xytext=(rel, y * (0.997 if direction == "long" else 1.003)),
                        arrowprops=dict(arrowstyle="->", color="#ff9800"),
                        color="#ff9800", fontsize=9, ha="center")

    if isinstance(sig.get("confirm_idx"), int):
        rel = sig["confirm_idx"] - offset
        if 0 <= rel < len(view):
            row = view.iloc[rel]
            y = row["high"] if direction == "long" else row["low"]
            color = "#2e7d32" if direction == "long" else "#c62828"
            ax.annotate("entry", xy=(rel, y),
                        xytext=(rel, y * (1.004 if direction == "long" else 0.996)),
                        arrowprops=dict(arrowstyle="->", color=color),
                        color=color, fontsize=9, ha="center")

    ax.set_xticks(x[:: max(1, len(x) // 8)])
    ax.set_xticklabels(
        [view.index[i].strftime("%m-%d %H:%M") for i in x[:: max(1, len(x) // 8)]],
        rotation=30, fontsize=8,
    )
    ax.set_title(
        f"{sig.get('symbol', '')} {direction.upper()}  "
        f"score {sig.get('score', '?')}/{sig.get('score_max', 15)}  "
        f"RR {sig.get('rr', '?')}"
    )
    ax.legend(loc="best", fontsize=8)
    ax.grid(alpha=0.2)
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    plt.close(fig)
    return out
