"""Market data fetching via CCXT and basic indicators."""
from typing import Optional
import time
import ccxt
import pandas as pd
import numpy as np

from logger_setup import get_logger
from config import EXCHANGE, EXCHANGE_FALLBACKS

log = get_logger("data")

_EXCHANGES: dict = {}
_EXCHANGE_BAD: set = set()


def get_exchange(name: str):
    """Return a cached, rate-limited ccxt exchange instance."""
    if name not in _EXCHANGES:
        cls = getattr(ccxt, name)
        ex = cls({"enableRateLimit": True, "timeout": 20000})
        try:
            ex.load_markets()
        except Exception as e:  # noqa: BLE001
            log.warning("load_markets failed for %s: %s", name, e)
        _EXCHANGES[name] = ex
    return _EXCHANGES[name]


def _candidate_exchanges(preferred: str):
    """Return preferred + fallbacks, skipping ones already known bad."""
    seq = [preferred] + [x for x in EXCHANGE_FALLBACKS if x != preferred]
    return [x for x in seq if x not in _EXCHANGE_BAD]


def fetch_ohlcv(
    symbol: str,
    timeframe: str = "15m",
    limit: int = 300,
    exchange_name: Optional[str] = None,
    since: Optional[int] = None,
) -> pd.DataFrame:
    """Fetch OHLCV with retries across the preferred exchange + fallbacks."""
    preferred = exchange_name or EXCHANGE
    last_err: Optional[Exception] = None
    for ex_name in _candidate_exchanges(preferred):
        for attempt in range(2):
            try:
                ex = get_exchange(ex_name)
                raw = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=limit)
                if not raw:
                    raise RuntimeError("empty OHLCV response")
                df = pd.DataFrame(
                    raw, columns=["ts", "open", "high", "low", "close", "volume"]
                )
                df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
                df = df.set_index("ts")
                return df.astype(float)
            except Exception as e:  # noqa: BLE001
                last_err = e
                msg = str(e)[:160]
                log.warning(
                    "fetch_ohlcv %s %s on %s failed (try %d): %s",
                    symbol, timeframe, ex_name, attempt + 1, msg,
                )
                # Geo / region blocks → skip this exchange entirely
                low = msg.lower()
                if "451" in msg or "403" in msg or "restricted location" in low or "block access from your country" in low:
                    _EXCHANGE_BAD.add(ex_name)
                    log.warning("Marking %s as unusable from this region", ex_name)
                    break
                time.sleep(1.5 ** attempt)
    raise RuntimeError(
        f"fetch_ohlcv failed for {symbol} {timeframe} on all exchanges: {last_err}"
    )


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def htf_bias(df_4h: pd.DataFrame, period: int = 200) -> str:
    """Return 'bullish', 'bearish', or 'flat' bias from 4h close vs EMA."""
    if len(df_4h) < period + 5:
        return "flat"
    e = ema(df_4h["close"], period)
    last_close = df_4h["close"].iloc[-1]
    last_ema = e.iloc[-1]
    # Slope check over last 10 bars to filter chop
    slope = e.iloc[-1] - e.iloc[-10]
    pct_dist = (last_close - last_ema) / last_ema
    if last_close > last_ema and slope >= 0 and pct_dist > 0.001:
        return "bullish"
    if last_close < last_ema and slope <= 0 and pct_dist < -0.001:
        return "bearish"
    return "flat"


def avg_body(df: pd.DataFrame, lookback: int = 20) -> float:
    bodies = (df["close"] - df["open"]).abs().tail(lookback)
    return float(bodies.mean()) if len(bodies) else 0.0


def candle_body(row) -> float:
    return float(abs(row["close"] - row["open"]))


def is_bullish_engulfing(prev, curr) -> bool:
    return (
        curr["close"] > curr["open"]
        and prev["close"] < prev["open"]
        and curr["close"] >= prev["open"]
        and curr["open"] <= prev["close"]
    )


def is_bearish_engulfing(prev, curr) -> bool:
    return (
        curr["close"] < curr["open"]
        and prev["close"] > prev["open"]
        and curr["open"] >= prev["close"]
        and curr["close"] <= prev["open"]
    )


def is_bull_pin(row) -> bool:
    rng = row["high"] - row["low"]
    if rng <= 0:
        return False
    body = abs(row["close"] - row["open"])
    lower_wick = min(row["open"], row["close"]) - row["low"]
    return body / rng < 0.4 and lower_wick / rng > 0.5 and row["close"] > row["open"]


def is_bear_pin(row) -> bool:
    rng = row["high"] - row["low"]
    if rng <= 0:
        return False
    body = abs(row["close"] - row["open"])
    upper_wick = row["high"] - max(row["open"], row["close"])
    return body / rng < 0.4 and upper_wick / rng > 0.5 and row["close"] < row["open"]
