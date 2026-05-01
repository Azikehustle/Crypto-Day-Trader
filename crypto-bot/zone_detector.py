"""Swing point + supply/demand zone detection."""
from dataclasses import dataclass, field
from typing import List, Optional
import pandas as pd
import numpy as np


@dataclass
class Swing:
    idx: int
    ts: pd.Timestamp
    price: float
    kind: str  # 'high' or 'low'


@dataclass
class Zone:
    kind: str           # 'supply' or 'demand'
    high: float
    low: float
    origin_idx: int
    origin_ts: pd.Timestamp
    range_high: float   # surrounding range for premium/discount calc
    range_low: float
    swept: bool = False
    invalidated: bool = False
    sweep_idx: Optional[int] = None
    pre_sweep_swing_high: Optional[float] = None
    pre_sweep_swing_low: Optional[float] = None
    db_id: Optional[int] = None   # Supabase zones.id (set by main.py after persist)

    def mid(self) -> float:
        return (self.high + self.low) / 2


def find_swings(df: pd.DataFrame, lookback: int = 5) -> List[Swing]:
    """Pivot highs/lows: high/low strictly greater/less than `lookback` bars on
    each side."""
    highs = df["high"].values
    lows = df["low"].values
    out: List[Swing] = []
    n = len(df)
    for i in range(lookback, n - lookback):
        wh = highs[i - lookback : i + lookback + 1]
        wl = lows[i - lookback : i + lookback + 1]
        if highs[i] == wh.max() and (wh == highs[i]).sum() == 1:
            out.append(Swing(i, df.index[i], float(highs[i]), "high"))
        if lows[i] == wl.min() and (wl == lows[i]).sum() == 1:
            out.append(Swing(i, df.index[i], float(lows[i]), "low"))
    out.sort(key=lambda s: s.idx)
    return out


def detect_zones(
    df: pd.DataFrame,
    lookback: int = 5,
    impulse_mult: float = 1.5,
) -> List[Zone]:
    """Detect supply/demand zones from pivots followed by an impulsive move.

    Supply: a pivot high immediately followed (within 3 candles) by a bearish
    candle whose body is at least `impulse_mult` x the average body of the prior
    20 candles.

    Demand: a pivot low followed by a similarly impulsive bullish candle.
    """
    swings = find_swings(df, lookback=lookback)
    zones: List[Zone] = []
    bodies = (df["close"] - df["open"]).abs().values
    for sw in swings:
        i = sw.idx
        if i + 3 >= len(df):
            continue
        start = max(0, i - 20)
        avg = bodies[start:i].mean() if i > start else 0.0
        if avg <= 0:
            continue
        # check next 3 candles for impulse
        for j in range(i + 1, min(i + 4, len(df))):
            body = bodies[j]
            row = df.iloc[j]
            if body < impulse_mult * avg:
                continue
            origin = df.iloc[i]
            range_window = df.iloc[max(0, i - 30) : i + 30]
            range_high = float(range_window["high"].max())
            range_low = float(range_window["low"].min())
            if sw.kind == "high" and row["close"] < row["open"]:
                zones.append(
                    Zone(
                        kind="supply",
                        high=float(origin["high"]),
                        low=float(min(origin["open"], origin["close"])),
                        origin_idx=i,
                        origin_ts=df.index[i],
                        range_high=range_high,
                        range_low=range_low,
                    )
                )
                break
            if sw.kind == "low" and row["close"] > row["open"]:
                zones.append(
                    Zone(
                        kind="demand",
                        high=float(max(origin["open"], origin["close"])),
                        low=float(origin["low"]),
                        origin_idx=i,
                        origin_ts=df.index[i],
                        range_high=range_high,
                        range_low=range_low,
                    )
                )
                break
    return zones


def filter_active_zones(zones: List[Zone], df: pd.DataFrame) -> List[Zone]:
    """Drop zones that price has fully traded through (invalidated)."""
    out: List[Zone] = []
    for z in zones:
        post = df.iloc[z.origin_idx + 1 :]
        if z.kind == "supply" and (post["close"] > z.high).any():
            z.invalidated = True
            continue
        if z.kind == "demand" and (post["close"] < z.low).any():
            z.invalidated = True
            continue
        out.append(z)
    return out


def find_liquidity_targets(
    df: pd.DataFrame,
    direction: str,
    entry_price: float,
    tolerance: float = 0.002,
) -> List[float]:
    """Return candidate liquidity targets in trade direction.

    Equal highs (longs) / equal lows (shorts) and prior swing extremes.
    """
    swings = find_swings(df, lookback=5)
    candidates: List[float] = []
    if direction == "long":
        highs = [s.price for s in swings if s.kind == "high"]
        # equal highs: cluster within tolerance
        for i, h1 in enumerate(highs):
            for h2 in highs[i + 1 :]:
                if abs(h1 - h2) / max(h1, 1e-9) < tolerance and h1 > entry_price:
                    candidates.append(max(h1, h2))
        candidates.extend(h for h in highs if h > entry_price)
    else:
        lows = [s.price for s in swings if s.kind == "low"]
        for i, l1 in enumerate(lows):
            for l2 in lows[i + 1 :]:
                if abs(l1 - l2) / max(l1, 1e-9) < tolerance and l1 < entry_price:
                    candidates.append(min(l1, l2))
        candidates.extend(l for l in lows if l < entry_price)
    candidates = sorted(set(candidates), reverse=(direction == "short"))
    return candidates
