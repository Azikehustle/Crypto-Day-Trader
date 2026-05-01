"""Swing point + supply/demand zone detection — Polars DataFrames."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

import numpy as np
import polars as pl


@dataclass
class Swing:
    idx: int
    ts: datetime
    price: float
    kind: str   # 'high' | 'low'


@dataclass
class Zone:
    kind: str           # 'supply' | 'demand'
    high: float
    low: float
    origin_idx: int
    origin_ts: datetime
    range_high: float
    range_low: float
    swept: bool = False
    invalidated: bool = False
    sweep_idx: Optional[int] = None
    pre_sweep_swing_high: Optional[float] = None
    pre_sweep_swing_low: Optional[float] = None
    db_id: Optional[int] = None

    def mid(self) -> float:
        return (self.high + self.low) / 2


def find_swings(df: pl.DataFrame, lookback: int = 5) -> List[Swing]:
    """Pivot highs/lows: high/low strictly greatest/least within ±lookback bars."""
    highs = df["high"].to_numpy()
    lows  = df["low"].to_numpy()
    ts_col = df["ts"].to_list()
    out: List[Swing] = []
    n = len(df)
    for i in range(lookback, n - lookback):
        wh = highs[i - lookback: i + lookback + 1]
        wl = lows[i  - lookback: i + lookback + 1]
        if highs[i] == wh.max() and (wh == highs[i]).sum() == 1:
            out.append(Swing(i, ts_col[i], float(highs[i]), "high"))
        if lows[i] == wl.min() and (wl == lows[i]).sum() == 1:
            out.append(Swing(i, ts_col[i], float(lows[i]), "low"))
    out.sort(key=lambda s: s.idx)
    return out


def detect_zones(
    df: pl.DataFrame,
    lookback: int = 5,
    impulse_mult: float = 1.5,
) -> List[Zone]:
    """Supply/demand zones from pivots followed by an impulsive candle."""
    swings = find_swings(df, lookback=lookback)
    zones: List[Zone] = []
    close_arr = df["close"].to_numpy()
    open_arr  = df["open"].to_numpy()
    high_arr  = df["high"].to_numpy()
    low_arr   = df["low"].to_numpy()
    ts_col    = df["ts"].to_list()
    bodies = np.abs(close_arr - open_arr)
    n = len(df)

    for sw in swings:
        i = sw.idx
        if i + 3 >= n:
            continue
        start = max(0, i - 20)
        avg = bodies[start:i].mean() if i > start else 0.0
        if avg <= 0:
            continue
        for j in range(i + 1, min(i + 4, n)):
            body = bodies[j]
            if body < impulse_mult * avg:
                continue
            range_start = max(0, i - 30)
            range_end   = min(n, i + 30)
            range_high  = float(high_arr[range_start:range_end].max())
            range_low   = float(low_arr[range_start:range_end].min())
            origin_row  = {"open": float(open_arr[i]), "close": float(close_arr[i]),
                           "high": float(high_arr[i]), "low": float(low_arr[i])}
            impulse_close = float(close_arr[j])
            impulse_open  = float(open_arr[j])
            if sw.kind == "high" and impulse_close < impulse_open:
                zones.append(Zone(
                    kind="supply",
                    high=origin_row["high"],
                    low=float(min(origin_row["open"], origin_row["close"])),
                    origin_idx=i,
                    origin_ts=ts_col[i],
                    range_high=range_high,
                    range_low=range_low,
                ))
                break
            if sw.kind == "low" and impulse_close > impulse_open:
                zones.append(Zone(
                    kind="demand",
                    high=float(max(origin_row["open"], origin_row["close"])),
                    low=origin_row["low"],
                    origin_idx=i,
                    origin_ts=ts_col[i],
                    range_high=range_high,
                    range_low=range_low,
                ))
                break
    return zones


def filter_active_zones(zones: List[Zone], df: pl.DataFrame) -> List[Zone]:
    """Drop zones that price has fully closed through."""
    out: List[Zone] = []
    for z in zones:
        post = df.slice(z.origin_idx + 1)
        if z.kind == "supply" and (post["close"] > z.high).any():
            z.invalidated = True
            continue
        if z.kind == "demand" and (post["close"] < z.low).any():
            z.invalidated = True
            continue
        out.append(z)
    return out


def find_liquidity_targets(
    df: pl.DataFrame,
    direction: str,
    entry_price: float,
    tolerance: float = 0.002,
) -> List[float]:
    """Candidate liquidity targets in trade direction (equal highs/lows + swings)."""
    swings = find_swings(df, lookback=5)
    candidates: List[float] = []
    if direction == "long":
        highs = [s.price for s in swings if s.kind == "high"]
        for idx_a, h1 in enumerate(highs):
            for h2 in highs[idx_a + 1:]:
                if abs(h1 - h2) / max(h1, 1e-9) < tolerance and h1 > entry_price:
                    candidates.append(max(h1, h2))
        candidates.extend(h for h in highs if h > entry_price)
    else:
        lows = [s.price for s in swings if s.kind == "low"]
        for idx_a, l1 in enumerate(lows):
            for l2 in lows[idx_a + 1:]:
                if abs(l1 - l2) / max(l1, 1e-9) < tolerance and l1 < entry_price:
                    candidates.append(min(l1, l2))
        candidates.extend(l for l in lows if l < entry_price)
    candidates = sorted(set(candidates), reverse=(direction == "short"))
    return candidates
