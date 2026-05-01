"""Signal engine: sweep, MSS, displacement, volume, ATR, scoring."""
from dataclasses import dataclass, asdict, field
from typing import Optional, Dict, Any
import pandas as pd

from data_fetcher import (
    avg_body,
    candle_body,
    is_bullish_engulfing,
    is_bearish_engulfing,
    is_bull_pin,
    is_bear_pin,
    calculate_atr,
    atr_percentile,
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
    SCORE_MAX,
    VOLUME_LOOKBACK,
    VOLUME_MULTIPLIER,
    VOLUME_CONFIRMATION_MULTIPLIER,
    ATR_PERIOD,
    ATR_PERCENTILE_THRESHOLD,
    ATR_HEALTHY_THRESHOLD,
    ATR_LOOKBACK,
    SL_ATR_MULTIPLIER,
)
from logger_setup import get_logger

log = get_logger("signal")


@dataclass
class Signal:
    symbol: str
    direction: str          # 'long' | 'short'
    entry: float
    stop_loss: float
    take_profit: float
    tp_reason: str
    score: int
    score_max: int
    htf_bias: str
    timeframe: str
    timestamp: str
    zone_high: float
    zone_low: float
    rr: float
    # Extras for the chart renderer / risk inspection
    sweep_idx: Optional[int] = None
    confirm_idx: Optional[int] = None
    mss_level: Optional[float] = None
    atr: Optional[float] = None
    atr_pct: Optional[float] = None
    vol_ok: bool = False
    atr_healthy: bool = False

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


def _avg_volume(df: pd.DataFrame, end_idx: int) -> float:
    """Mean volume over the previous VOLUME_LOOKBACK bars (excluding end_idx)."""
    start = max(0, end_idx - VOLUME_LOOKBACK)
    if end_idx <= start:
        return 0.0
    window = df.iloc[start:end_idx]["volume"]
    return float(window.mean()) if len(window) else 0.0


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
    volume_confirmed: bool,
    atr_healthy: bool,
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
    if volume_confirmed: s += 1     # +1 (max becomes 14)
    if atr_healthy: s += 1          # +1 (max becomes 15)
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


def _check_volume(df: pd.DataFrame, displacement_idx: int, confirm_idx: int) -> tuple[bool, str]:
    """Volume gate. Returns (ok, reason). When `displacement_idx == confirm_idx`
    (single MSS candle that is also the confirmation), only one check runs and
    the stricter VOLUME_MULTIPLIER is applied."""
    avg = _avg_volume(df, displacement_idx)
    if avg <= 0:
        return False, "no avg volume"
    disp_vol = float(df.iloc[displacement_idx]["volume"])
    if disp_vol < VOLUME_MULTIPLIER * avg:
        return False, (
            f"displacement vol {disp_vol:.2f} < {VOLUME_MULTIPLIER}x avg {avg:.2f}"
        )
    if confirm_idx != displacement_idx:
        confirm_avg = _avg_volume(df, confirm_idx)
        confirm_vol = float(df.iloc[confirm_idx]["volume"])
        if confirm_avg <= 0:
            return False, "no avg volume at confirm"
        if confirm_vol < VOLUME_CONFIRMATION_MULTIPLIER * confirm_avg:
            return False, (
                f"confirmation vol {confirm_vol:.2f} < "
                f"{VOLUME_CONFIRMATION_MULTIPLIER}x avg {confirm_avg:.2f}"
            )
    return True, "ok"


def evaluate_zone(
    symbol: str,
    df_15m: pd.DataFrame,
    zone: Zone,
    htf_bias_value: str,
) -> Optional[Signal]:
    """Run the full setup pipeline for a single zone. Returns a Signal if score
    meets the LOG threshold (caller filters SEND threshold separately)."""
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

    confirm_idx = mss["confirm_idx"]
    if confirm_idx + 1 >= len(df_15m):
        return None  # no entry candle yet

    # ---- ATR volatility filter ----------------------------------------
    atr_series = calculate_atr(df_15m, period=ATR_PERIOD)
    current_atr = float(atr_series.iloc[confirm_idx]) if len(atr_series) > confirm_idx else 0.0
    atr_pct = atr_percentile(
        df_15m.iloc[: confirm_idx + 1], period=ATR_PERIOD, lookback=ATR_LOOKBACK
    )
    if atr_pct < ATR_PERCENTILE_THRESHOLD:
        log.info(
            "Skip %s %s: ATR %.6f at %.1f pct < %d (dead chop)",
            symbol, direction, current_atr, atr_pct, ATR_PERCENTILE_THRESHOLD,
        )
        return None
    atr_healthy = atr_pct >= ATR_HEALTHY_THRESHOLD

    # ---- Volume confirmation (hard filter) ----------------------------
    vol_ok, vol_reason = _check_volume(df_15m, confirm_idx, confirm_idx)
    if not vol_ok:
        log.info("Skip %s %s: volume filter failed (%s)", symbol, direction, vol_reason)
        return None

    # ---- Entry / stops -----------------------------------------------
    entry_row = df_15m.iloc[confirm_idx + 1]
    entry = float(entry_row["open"])

    # Dynamic SL using ATR. Buffer fallback = SL_BUFFER_PCT for safety when ATR
    # is unusable.
    if current_atr > 0:
        if direction == "long":
            stop = zone.low - SL_ATR_MULTIPLIER * current_atr
        else:
            stop = zone.high + SL_ATR_MULTIPLIER * current_atr
    else:
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
        volume_confirmed=True,        # already passed the gate above
        atr_healthy=atr_healthy,
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
        score_max=SCORE_MAX,
        htf_bias=htf_bias_value,
        timeframe="15m",
        timestamp=entry_row.name.isoformat(),
        zone_high=zone.high,
        zone_low=zone.low,
        rr=round(rr, 2),
        sweep_idx=int(sweep_idx),
        confirm_idx=int(confirm_idx),
        mss_level=float(mss["mss_level"]),
        atr=round(current_atr, 6),
        atr_pct=round(atr_pct, 2),
        vol_ok=True,
        atr_healthy=atr_healthy,
    )
    return sig


def should_send(signal: Signal) -> bool:
    return signal.score >= SCORE_THRESHOLD_SEND
