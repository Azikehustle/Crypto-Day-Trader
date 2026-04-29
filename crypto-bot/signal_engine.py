"""Signal engine: sweep, MSS, displacement, scoring."""
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
import pandas as pd

from data_fetcher import (
    avg_body,
    candle_body,
    is_bullish_engulfing,
    is_bearish_engulfing,
    is_bull_pin,
    is_bear_pin,
)
from zone_detector import Zone, find_swings, find_liquidity_targets
from config import (
    DISPLACEMENT_LOOKBACK,
    DISPLACEMENT_MULT,
    SL_BUFFER_PCT,
    MIN_RR_FOR_TARGET,
    MAX_RR_FOR_TARGET,
    FALLBACK_RR,
    LONDON_OPEN,
    NY_OPEN,
    SCORE_THRESHOLD_SEND,
    SCORE_THRESHOLD_LOG,
)


@dataclass
class Signal:
    symbol: str
    direction: str        # 'long' | 'short'
    entry: float
    stop_loss: float
    take_profit: float
    tp_reason: str
    score: int
    htf_bias: str
    timeframe: str
    timestamp: str
    zone_high: float
    zone_low: float
    rr: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _in_session(ts: pd.Timestamp) -> bool:
    h = ts.tz_convert("UTC").hour if ts.tzinfo else ts.hour
    return LONDON_OPEN[0] <= h < LONDON_OPEN[1] or NY_OPEN[0] <= h < NY_OPEN[1]


def _last_pre_sweep_swing(
    df: pd.DataFrame, sweep_idx: int, kind: str
) -> Optional[float]:
    """Most recent pivot of `kind` ('high'/'low') strictly before sweep_idx."""
    swings = find_swings(df.iloc[: sweep_idx + 1], lookback=3)
    relevant = [s for s in swings if s.kind == kind and s.idx < sweep_idx]
    return relevant[-1].price if relevant else None


def detect_sweep(df: pd.DataFrame, zone: Zone, direction: str) -> Optional[int]:
    """Find index of a sweep candle that wicked beyond the zone but closed back
    inside, occurring after the zone's origin."""
    post = df.iloc[zone.origin_idx + 1 :]
    for i, (ts, row) in enumerate(post.iterrows()):
        abs_idx = zone.origin_idx + 1 + i
        if direction == "long":
            if row["low"] < zone.low and row["close"] > zone.low:
                return abs_idx
        else:
            if row["high"] > zone.high and row["close"] < zone.high:
                return abs_idx
    return None


def check_mss_and_displacement(
    df: pd.DataFrame, sweep_idx: int, direction: str
) -> Optional[Dict[str, Any]]:
    """Check for MSS confirmed by a strong displacement & confirmation candle
    after the sweep. Returns details if valid."""
    if sweep_idx + 1 >= len(df):
        return None

    if direction == "long":
        target_swing = _last_pre_sweep_swing(df, sweep_idx, "high")
    else:
        target_swing = _last_pre_sweep_swing(df, sweep_idx, "low")
    if target_swing is None:
        return None

    avg = avg_body(df.iloc[max(0, sweep_idx - DISPLACEMENT_LOOKBACK) : sweep_idx])
    if avg <= 0:
        return None

    # Iterate post-sweep candles looking for the MSS break with strong close.
    post = df.iloc[sweep_idx + 1 :]
    for j, (ts, row) in enumerate(post.iterrows()):
        abs_j = sweep_idx + 1 + j
        body = candle_body(row)
        displacement_ok = body >= DISPLACEMENT_MULT * avg

        if direction == "long":
            broke = row["close"] > target_swing
            if not broke:
                continue
            prev = df.iloc[abs_j - 1]
            pattern_ok = is_bullish_engulfing(prev, row) or is_bull_pin(row)
        else:
            broke = row["close"] < target_swing
            if not broke:
                continue
            prev = df.iloc[abs_j - 1]
            pattern_ok = is_bearish_engulfing(prev, row) or is_bear_pin(row)

        return {
            "confirm_idx": abs_j,
            "confirm_ts": ts,
            "displacement_ok": displacement_ok,
            "pattern_ok": pattern_ok,
            "mss_level": target_swing,
            "body_to_avg": body / avg,
        }
    return None


def score_setup(
    in_zone: bool,
    sweep: bool,
    mss: bool,
    displacement_ok: bool,
    pattern_ok: bool,
    htf_aligned: bool,
    in_premium_discount: bool,
    in_session: bool,
) -> int:
    s = 0
    if in_zone: s += 1
    if sweep: s += 2
    if mss: s += 3
    if displacement_ok: s += 2
    if pattern_ok: s += 1
    if htf_aligned: s += 2
    if in_premium_discount: s += 1
    if in_session: s += 1
    return s


def _premium_discount_ok(zone: Zone, entry: float, direction: str) -> bool:
    rng = zone.range_high - zone.range_low
    if rng <= 0:
        return True
    midpoint = zone.range_low + rng / 2
    if direction == "long":
        return entry <= midpoint  # discount
    return entry >= midpoint      # premium


def _pick_take_profit(
    df: pd.DataFrame, direction: str, entry: float, stop: float
) -> tuple[float, str]:
    risk = abs(entry - stop)
    if risk <= 0:
        return (entry * (1.02 if direction == "long" else 0.98), "fallback 2R")
    targets = find_liquidity_targets(df, direction, entry)
    for t in targets:
        if direction == "long":
            rr = (t - entry) / risk
        else:
            rr = (entry - t) / risk
        if MIN_RR_FOR_TARGET <= rr <= MAX_RR_FOR_TARGET:
            return (t, "liquidity pool" if rr < MAX_RR_FOR_TARGET else "swing")
    # fallback
    tp = entry + FALLBACK_RR * risk if direction == "long" else entry - FALLBACK_RR * risk
    return (tp, f"fallback {FALLBACK_RR}R")


def evaluate_zone(
    symbol: str,
    df_15m: pd.DataFrame,
    zone: Zone,
    htf_bias_value: str,
) -> Optional[Signal]:
    """Run the full setup pipeline for a single zone. Returns a Signal if score
    meets the SEND threshold; logs a partial dict if 6-7 (handled by caller)."""
    if zone.kind == "demand" and htf_bias_value != "bullish":
        return None
    if zone.kind == "supply" and htf_bias_value != "bearish":
        return None

    direction = "long" if zone.kind == "demand" else "short"
    sweep_idx = detect_sweep(df_15m, zone, direction)
    if sweep_idx is None:
        return None

    mss = check_mss_and_displacement(df_15m, sweep_idx, direction)
    if mss is None:
        return None
    if not mss["pattern_ok"] or not mss["displacement_ok"]:
        # still partial — let scorer decide
        pass

    confirm_idx = mss["confirm_idx"]
    if confirm_idx + 1 >= len(df_15m):
        return None  # no entry candle yet

    entry_row = df_15m.iloc[confirm_idx + 1]
    entry = float(entry_row["open"])

    if direction == "long":
        stop = zone.low * (1 - SL_BUFFER_PCT)
    else:
        stop = zone.high * (1 + SL_BUFFER_PCT)

    tp, tp_reason = _pick_take_profit(df_15m, direction, entry, stop)
    risk = abs(entry - stop)
    rr = abs(tp - entry) / risk if risk > 0 else 0.0

    in_pd = _premium_discount_ok(zone, entry, direction)
    in_sess = _in_session(entry_row.name)

    score = score_setup(
        in_zone=True,
        sweep=True,
        mss=True,
        displacement_ok=bool(mss["displacement_ok"]),
        pattern_ok=bool(mss["pattern_ok"]),
        htf_aligned=True,
        in_premium_discount=in_pd,
        in_session=in_sess,
    )

    if score < SCORE_THRESHOLD_LOG:
        return None

    sig = Signal(
        symbol=symbol,
        direction=direction,
        entry=entry,
        stop_loss=float(stop),
        take_profit=float(tp),
        tp_reason=tp_reason,
        score=score,
        htf_bias=htf_bias_value,
        timeframe="15m",
        timestamp=entry_row.name.isoformat(),
        zone_high=zone.high,
        zone_low=zone.low,
        rr=round(rr, 2),
    )
    return sig


def should_send(signal: Signal) -> bool:
    return signal.score >= SCORE_THRESHOLD_SEND
