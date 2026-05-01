"""Signal engine: sweep, MSS, displacement, volume, ATR, scoring.

Uses Polars DataFrames throughout. The ``df`` argument everywhere is
``pl.DataFrame`` with columns (ts, open, high, low, close, volume).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple

import polars as pl

from data_fetcher import (
    avg_body, candle_body,
    is_bullish_engulfing, is_bearish_engulfing,
    is_bull_pin, is_bear_pin,
    calculate_atr, atr_percentile,
)
from zone_detector import Zone, find_swings, find_liquidity_targets
from config import (
    DISPLACEMENT_LOOKBACK, DISPLACEMENT_MULT,
    SL_BUFFER_PCT, MIN_RR_FOR_TARGET, MAX_RR_FOR_TARGET, FALLBACK_RR,
    SCORE_THRESHOLD_SEND, SCORE_THRESHOLD_LOG, SCORE_MAX,
    VOLUME_LOOKBACK, VOLUME_MULTIPLIER, VOLUME_CONFIRMATION_MULTIPLIER,
    ATR_PERIOD, ATR_PERCENTILE_THRESHOLD, ATR_HEALTHY_THRESHOLD, ATR_LOOKBACK,
    SL_ATR_MULTIPLIER,
    SESSION_ASIAN, SESSION_LONDON, SESSION_OVERLAP, SESSION_NY_OPEN,
    SESSION_ASIAN_SCORE_MIN, SESSION_ASIAN_SIZE, SESSION_ASIAN_SL_MULT,
    SESSION_LONDON_SCORE_MIN, SESSION_NY_SCORE_MIN, SESSION_NY_SL_MULT,
    QUIET_START_UTC, QUIET_END_UTC,
    CORRELATION_MODE, CORRELATION_RELAXED_SIZE,
)
from logger_setup import get_logger

log = get_logger("signal")


# ---------------------------------------------------------------------------
# Signal dataclass
# ---------------------------------------------------------------------------

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
    mode: str               # 'scalp' | 'day' | 'swing'
    timestamp: str
    zone_high: float
    zone_low: float
    rr: float
    session: str            # 'ASIAN' | 'LONDON' | 'OVERLAP' | 'NY' | 'QUIET' | 'OFF'
    size_mult: float = 1.0  # position size multiplier from session rules
    sweep_idx: Optional[int] = None
    confirm_idx: Optional[int] = None
    mss_level: Optional[float] = None
    atr: Optional[float] = None
    atr_pct: Optional[float] = None
    vol_ok: bool = False
    atr_healthy: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Session detection
# ---------------------------------------------------------------------------

def _get_session(ts: datetime) -> Tuple[str, int, float, float]:
    """Return (session_name, min_score, size_mult, sl_mult) for UTC hour."""
    h = ts.hour  # ts is already UTC from Polars datetime column
    # QUIET hours (no trading)
    if QUIET_START_UTC <= QUIET_END_UTC:
        in_quiet = QUIET_START_UTC <= h < QUIET_END_UTC
    else:
        in_quiet = h >= QUIET_START_UTC or h < QUIET_END_UTC
    if in_quiet:
        return "QUIET", 999, 0.0, 1.0  # blocked

    # Overlap is a sub-session of London+NY
    if SESSION_OVERLAP[0] <= h < SESSION_OVERLAP[1]:
        return "OVERLAP", SESSION_LONDON_SCORE_MIN, 1.0, 1.0
    if SESSION_NY_OPEN[0] <= h < SESSION_NY_OPEN[1]:
        return "NY", SESSION_NY_SCORE_MIN, 1.0, SESSION_NY_SL_MULT
    if SESSION_LONDON[0] <= h < SESSION_LONDON[1]:
        return "LONDON", SESSION_LONDON_SCORE_MIN, 1.0, 1.0
    if SESSION_ASIAN[0] <= h < SESSION_ASIAN[1]:
        return "ASIAN", SESSION_ASIAN_SCORE_MIN, SESSION_ASIAN_SIZE, SESSION_ASIAN_SL_MULT
    return "OFF", SCORE_THRESHOLD_SEND, 1.0, 1.0


def _in_session(ts: datetime) -> bool:
    """True when in London or NY open window."""
    h = ts.hour
    from config import LONDON_OPEN, NY_OPEN  # noqa: WPS433
    return LONDON_OPEN[0] <= h < LONDON_OPEN[1] or NY_OPEN[0] <= h < NY_OPEN[1]


# ---------------------------------------------------------------------------
# Correlation filter
# ---------------------------------------------------------------------------

def check_correlation(
    symbol: str,
    direction: str,
    open_trades: List[Dict[str, Any]],
    mode: str = CORRELATION_MODE,
) -> Tuple[bool, float, str]:
    """Check correlation gate against open trades.

    Returns (blocked, size_mult, reason).
      blocked=True + size_mult=0 → skip signal entirely (strict mode hit)
      blocked=False + size_mult<1 → allow with reduced size (relaxed mode)
    """
    same_dir_trades = [
        t for t in open_trades
        if t.get("direction") == direction and t.get("symbol") != symbol
    ]
    if not same_dir_trades:
        return False, 1.0, ""

    if mode == "strict":
        pairs = ", ".join(t["symbol"] for t in same_dir_trades[:3])
        return True, 0.0, f"correlation block (already {direction} on {pairs})"

    # Relaxed: allow with reduced size
    return False, CORRELATION_RELAXED_SIZE, "relaxed correlation (50% size)"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _last_pre_sweep_swing(df: pl.DataFrame, sweep_idx: int, kind: str) -> Optional[float]:
    """Most recent pivot of `kind` strictly before sweep_idx."""
    sub = df.slice(0, sweep_idx + 1)
    swings = find_swings(sub, lookback=3)
    relevant = [s for s in swings if s.kind == kind and s.idx < sweep_idx]
    return relevant[-1].price if relevant else None


def detect_sweep(df: pl.DataFrame, zone: Zone, direction: str) -> Optional[int]:
    """Find index where price wicked beyond the zone but closed back inside."""
    post = df.slice(zone.origin_idx + 1)
    for i, row in enumerate(post.iter_rows(named=True)):
        abs_idx = zone.origin_idx + 1 + i
        if direction == "long":
            if row["low"] < zone.low and row["close"] > zone.low:
                return abs_idx
        else:
            if row["high"] > zone.high and row["close"] < zone.high:
                return abs_idx
    return None


def _avg_volume(df: pl.DataFrame, end_idx: int) -> float:
    start = max(0, end_idx - VOLUME_LOOKBACK)
    if end_idx <= start:
        return 0.0
    window = df.slice(start, end_idx - start)["volume"]
    return float(window.mean()) if len(window) else 0.0


def check_mss_and_displacement(
    df: pl.DataFrame, sweep_idx: int, direction: str
) -> Optional[Dict[str, Any]]:
    """MSS confirmed by displacement & confirmation candle post-sweep."""
    if sweep_idx + 1 >= len(df):
        return None
    if direction == "long":
        target_swing = _last_pre_sweep_swing(df, sweep_idx, "high")
    else:
        target_swing = _last_pre_sweep_swing(df, sweep_idx, "low")
    if target_swing is None:
        return None

    pre_start = max(0, sweep_idx - DISPLACEMENT_LOOKBACK)
    pre = df.slice(pre_start, sweep_idx - pre_start)
    avg = avg_body(pre)
    if avg <= 0:
        return None

    post = df.slice(sweep_idx + 1)
    for j, row in enumerate(post.iter_rows(named=True)):
        abs_j = sweep_idx + 1 + j
        body = candle_body(row)
        displacement_ok = body >= DISPLACEMENT_MULT * avg
        if direction == "long":
            broke = row["close"] > target_swing
            if not broke:
                continue
            prev = df.row(abs_j - 1, named=True)
            pattern_ok = is_bullish_engulfing(prev, row) or is_bull_pin(row)
        else:
            broke = row["close"] < target_swing
            if not broke:
                continue
            prev = df.row(abs_j - 1, named=True)
            pattern_ok = is_bearish_engulfing(prev, row) or is_bear_pin(row)
        return {
            "confirm_idx":    abs_j,
            "confirm_ts":     row["ts"],
            "displacement_ok": displacement_ok,
            "pattern_ok":      pattern_ok,
            "mss_level":       target_swing,
            "body_to_avg":     body / avg,
        }
    return None


def score_setup(
    in_zone: bool, sweep: bool, mss: bool,
    displacement_ok: bool, pattern_ok: bool, htf_aligned: bool,
    in_premium_discount: bool, in_session: bool,
    volume_confirmed: bool, atr_healthy: bool,
) -> int:
    s = 0
    if in_zone:             s += 1
    if sweep:               s += 2
    if mss:                 s += 3
    if displacement_ok:     s += 2
    if pattern_ok:          s += 1
    if htf_aligned:         s += 2
    if in_premium_discount: s += 1
    if in_session:          s += 1
    if volume_confirmed:    s += 1
    if atr_healthy:         s += 1
    return s


def _premium_discount_ok(zone: Zone, entry: float, direction: str) -> bool:
    rng = zone.range_high - zone.range_low
    if rng <= 0:
        return True
    midpoint = zone.range_low + rng / 2
    if direction == "long":
        return entry <= midpoint
    return entry >= midpoint


def _pick_take_profit(
    df: pl.DataFrame, direction: str, entry: float, stop: float
) -> Tuple[float, str]:
    risk = abs(entry - stop)
    if risk <= 0:
        return (entry * (1.02 if direction == "long" else 0.98), "fallback 2R")
    targets = find_liquidity_targets(df, direction, entry)
    for t in targets:
        rr = (t - entry) / risk if direction == "long" else (entry - t) / risk
        if MIN_RR_FOR_TARGET <= rr <= MAX_RR_FOR_TARGET:
            return (t, "liquidity pool" if rr < MAX_RR_FOR_TARGET else "swing")
    tp = entry + FALLBACK_RR * risk if direction == "long" else entry - FALLBACK_RR * risk
    return (tp, f"fallback {FALLBACK_RR}R")


def _check_volume(df: pl.DataFrame, displacement_idx: int, confirm_idx: int) -> Tuple[bool, str]:
    avg = _avg_volume(df, displacement_idx)
    if avg <= 0:
        return False, "no avg volume"
    disp_vol = float(df.row(displacement_idx, named=True)["volume"])
    if disp_vol < VOLUME_MULTIPLIER * avg:
        return False, f"displacement vol {disp_vol:.2f} < {VOLUME_MULTIPLIER}x avg {avg:.2f}"
    if confirm_idx != displacement_idx:
        confirm_avg = _avg_volume(df, confirm_idx)
        confirm_vol = float(df.row(confirm_idx, named=True)["volume"])
        if confirm_avg <= 0:
            return False, "no avg volume at confirm"
        if confirm_vol < VOLUME_CONFIRMATION_MULTIPLIER * confirm_avg:
            return False, (
                f"confirmation vol {confirm_vol:.2f} < "
                f"{VOLUME_CONFIRMATION_MULTIPLIER}x avg {confirm_avg:.2f}"
            )
    return True, "ok"


# ---------------------------------------------------------------------------
# Main evaluation entry point
# ---------------------------------------------------------------------------

def evaluate_zone(
    symbol: str,
    df: pl.DataFrame,
    zone: Zone,
    htf_bias_value: str,
    timeframe: str = "15m",
    mode: str = "day",
    open_trades: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Signal]:
    """Run the full setup pipeline for one zone. Returns Signal or None."""
    if zone.kind == "demand" and htf_bias_value != "bullish":
        return None
    if zone.kind == "supply" and htf_bias_value != "bearish":
        return None

    direction = "long" if zone.kind == "demand" else "short"
    sweep_idx = detect_sweep(df, zone, direction)
    if sweep_idx is None:
        return None

    mss = check_mss_and_displacement(df, sweep_idx, direction)
    if mss is None:
        return None

    confirm_idx = mss["confirm_idx"]
    if confirm_idx + 1 >= len(df):
        return None

    # ── ATR filter ──────────────────────────────────────────────────────────
    atr_series = calculate_atr(df, period=ATR_PERIOD)
    current_atr = float(atr_series[confirm_idx]) if len(atr_series) > confirm_idx else 0.0
    atr_pct = atr_percentile(
        df.slice(0, confirm_idx + 1), period=ATR_PERIOD, lookback=ATR_LOOKBACK
    )
    if atr_pct < ATR_PERCENTILE_THRESHOLD:
        log.info("Skip %s %s: ATR %.6f at %.1fpct < %d", symbol, direction, current_atr, atr_pct, ATR_PERCENTILE_THRESHOLD)
        return None
    atr_healthy = atr_pct >= ATR_HEALTHY_THRESHOLD

    # ── Volume filter ───────────────────────────────────────────────────────
    vol_ok, vol_reason = _check_volume(df, confirm_idx, confirm_idx)
    if not vol_ok:
        log.info("Skip %s %s: volume (%s)", symbol, direction, vol_reason)
        return None

    # ── Entry / stops ───────────────────────────────────────────────────────
    entry_row = df.row(confirm_idx + 1, named=True)
    entry = float(entry_row["open"])
    entry_ts: datetime = entry_row["ts"]

    if current_atr > 0:
        stop = zone.low - SL_ATR_MULTIPLIER * current_atr if direction == "long" \
               else zone.high + SL_ATR_MULTIPLIER * current_atr
    else:
        stop = zone.low * (1 - SL_BUFFER_PCT) if direction == "long" \
               else zone.high * (1 + SL_BUFFER_PCT)

    # ── Session rules ───────────────────────────────────────────────────────
    session_name, session_min_score, size_mult, sl_mult = _get_session(entry_ts)
    if session_name == "QUIET":
        log.info("Skip %s %s: quiet hours", symbol, direction)
        return None

    # Apply session SL multiplier (wider in NY)
    if sl_mult != 1.0 and current_atr > 0:
        stop = zone.low - SL_ATR_MULTIPLIER * current_atr * sl_mult if direction == "long" \
               else zone.high + SL_ATR_MULTIPLIER * current_atr * sl_mult

    tp, tp_reason = _pick_take_profit(df, direction, entry, stop)
    risk = abs(entry - stop)
    rr = abs(tp - entry) / risk if risk > 0 else 0.0

    # ── Scoring ─────────────────────────────────────────────────────────────
    in_pd   = _premium_discount_ok(zone, entry, direction)
    in_sess = _in_session(entry_ts)
    score = score_setup(
        in_zone=True, sweep=True, mss=True,
        displacement_ok=bool(mss["displacement_ok"]),
        pattern_ok=bool(mss["pattern_ok"]),
        htf_aligned=True,
        in_premium_discount=in_pd, in_session=in_sess,
        volume_confirmed=True, atr_healthy=atr_healthy,
    )

    if score < SCORE_THRESHOLD_LOG:
        return None

    # ── Correlation filter ──────────────────────────────────────────────────
    if open_trades is not None:
        corr_blocked, corr_size, corr_reason = check_correlation(
            symbol, direction, open_trades
        )
        if corr_blocked:
            log.info("🚫 %s %s blocked: %s", symbol, direction, corr_reason)
            try:
                from telegram_bot import send_message  # noqa: WPS433
                send_message(f"🚫 <b>{symbol} {direction.upper()} blocked</b>\n{corr_reason}")
            except Exception:  # noqa: BLE001
                pass
            return None
        if corr_size < 1.0:
            size_mult = min(size_mult, corr_size)

    return Signal(
        symbol=symbol,
        direction=direction,
        entry=entry,
        stop_loss=float(stop),
        take_profit=float(tp),
        tp_reason=tp_reason,
        score=score,
        score_max=SCORE_MAX,
        htf_bias=htf_bias_value,
        timeframe=timeframe,
        mode=mode,
        timestamp=entry_ts.isoformat(),
        zone_high=zone.high,
        zone_low=zone.low,
        rr=round(rr, 2),
        session=session_name,
        size_mult=round(size_mult, 2),
        sweep_idx=int(sweep_idx),
        confirm_idx=int(confirm_idx),
        mss_level=float(mss["mss_level"]),
        atr=round(current_atr, 6),
        atr_pct=round(atr_pct, 2),
        vol_ok=True,
        atr_healthy=atr_healthy,
    )


def should_send(signal: Signal) -> bool:
    # Also enforce session-specific score minimums
    _, session_min, _, _ = _get_session(
        datetime.fromisoformat(signal.timestamp.replace("Z", "+00:00"))
    )
    return signal.score >= max(SCORE_THRESHOLD_SEND, session_min)
