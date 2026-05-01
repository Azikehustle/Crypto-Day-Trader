"""Market data fetching with forex-first triple-API fallback + CCXT for crypto."""
from typing import Optional, Tuple
import os
import time
import requests
import ccxt
import pandas as pd
import numpy as np

from logger_setup import get_logger
from config import (
    EXCHANGE,
    EXCHANGE_FALLBACKS,
    DATA_FETCH_RETRIES,
    DATA_FETCH_BACKOFF_BASE,
)

log = get_logger("data")

# ---------------------------------------------------------------------------
# Timeframe conversion map  (internal → provider-specific)
# ---------------------------------------------------------------------------

TIMEFRAME_MAP = {
    "1m":  "1min",
    "5m":  "5min",
    "15m": "15min",
    "30m": "30min",
    "45m": "45min",
    "1h":  "1h",
    "2h":  "2h",
    "4h":  "4h",
    "8h":  "8h",
    "1D":  "1day",
    "1d":  "1day",
    "1W":  "1week",
    "1w":  "1week",
}


def convert_timeframe(tf: str) -> str:
    """Convert internal timeframe string to Twelvedata interval format."""
    return TIMEFRAME_MAP.get(tf, tf)


# ---------------------------------------------------------------------------
# Forex detection helper
# ---------------------------------------------------------------------------

_FOREX_CURRENCIES = {
    "EUR", "GBP", "USD", "JPY", "AUD", "CAD", "CHF", "NZD",
    "SEK", "NOK", "DKK", "SGD", "HKD", "MXN", "ZAR", "TRY",
}


def _is_forex(symbol: str) -> bool:
    """Return True if symbol looks like a forex pair (e.g. EUR/USD, GBPUSD)."""
    clean = symbol.replace("/", "").replace("-", "").upper()
    if len(clean) == 6:
        base, quote = clean[:3], clean[3:]
        return base in _FOREX_CURRENCIES and quote in _FOREX_CURRENCIES
    return False


def _forex_symbol_td(symbol: str) -> str:
    """Convert EUR/USD → EUR/USD for Twelvedata (they accept slash format)."""
    return symbol.replace("-", "/")


def _forex_symbol_fcs(symbol: str) -> str:
    """Convert EUR/USD → EUR/USD for FCS API."""
    return symbol.replace("-", "/")


def _forex_symbol_itick(symbol: str) -> str:
    """Convert EUR/USD → EURUSD for iTick."""
    return symbol.replace("/", "").replace("-", "").upper()


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

_TWELVEDATA_KEY = os.getenv("TWELVEDATA_API_KEY", "")
_FCS_KEY = os.getenv("FCS_API_KEY", "")
_ITICK_KEY = os.getenv("ITICK_API_KEY", "")


# ---------------------------------------------------------------------------
# Twelvedata fetch
# ---------------------------------------------------------------------------

# Minimum gap (seconds) between Twelvedata API calls to stay within free-tier rate limits
_TD_MIN_INTERVAL = 8.0
_td_last_call: float = 0.0


def _fetch_twelvedata(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """Fetch OHLCV from Twelvedata REST API. Returns DataFrame or None."""
    global _td_last_call
    if not _TWELVEDATA_KEY:
        log.debug("Twelvedata key not set, skipping")
        return None
    # Rate-limit: free plan allows 8 calls/minute (1 per ~7.5s)
    elapsed = time.time() - _td_last_call
    if elapsed < _TD_MIN_INTERVAL:
        wait = _TD_MIN_INTERVAL - elapsed
        log.debug("Twelvedata rate-limit pause: %.1fs", wait)
        time.sleep(wait)
    _td_last_call = time.time()

    tf = convert_timeframe(timeframe)
    sym = _forex_symbol_td(symbol)
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": sym,
        "interval": tf,
        "outputsize": limit,
        "apikey": _TWELVEDATA_KEY,
        "format": "JSON",
        "order": "ASC",
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "error" or "values" not in data:
            log.warning("Twelvedata error for %s: %s", symbol, data.get("message", data))
            return None
        rows = data["values"]
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df = df.rename(columns={"datetime": "ts"})
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = df.set_index("ts")
        for col in ["open", "high", "low", "close"]:
            df[col] = df[col].astype(float)
        if "volume" in df.columns:
            df["volume"] = df["volume"].astype(float)
        else:
            df["volume"] = 0.0
        df = df[["open", "high", "low", "close", "volume"]].sort_index()
        log.info("Twelvedata OK: %s %s (%d bars)", symbol, timeframe, len(df))
        return df
    except Exception as e:  # noqa: BLE001
        log.warning("Twelvedata fetch failed for %s: %s", symbol, e)
        return None


# ---------------------------------------------------------------------------
# FCS API fetch
# ---------------------------------------------------------------------------

def _fetch_fcs(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """Fetch OHLCV from FCS API. Returns DataFrame or None."""
    if not _FCS_KEY:
        log.debug("FCS key not set, skipping")
        return None
    tf = convert_timeframe(timeframe)
    sym = _forex_symbol_fcs(symbol)
    url = "https://fcsapi.com/api-v3/forex/history"
    params = {
        "symbol": sym,
        "period": tf,
        "access_key": _FCS_KEY,
        "level": 1,
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("status") or not data.get("response"):
            log.warning("FCS API error for %s: %s", symbol, data.get("msg", data))
            return None
        rows = data["response"]
        if not rows:
            return None
        records = []
        for r in rows:
            records.append({
                "ts": pd.to_datetime(r.get("tm") or r.get("t"), utc=True),
                "open":  float(r.get("o", 0)),
                "high":  float(r.get("h", 0)),
                "low":   float(r.get("l", 0)),
                "close": float(r.get("c", 0)),
                "volume": 0.0,
            })
        df = pd.DataFrame(records).set_index("ts").sort_index()
        df = df.tail(limit)
        log.info("FCS API OK: %s %s (%d bars)", symbol, timeframe, len(df))
        return df
    except Exception as e:  # noqa: BLE001
        log.warning("FCS API fetch failed for %s: %s", symbol, e)
        return None


# ---------------------------------------------------------------------------
# iTick fetch
# ---------------------------------------------------------------------------

def _fetch_itick(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """Fetch OHLCV from iTick API. Returns DataFrame or None."""
    if not _ITICK_KEY:
        log.debug("iTick key not set, skipping")
        return None
    tf = convert_timeframe(timeframe)
    sym = _forex_symbol_itick(symbol)
    url = "https://api.itick.org/forex/kline"
    params = {
        "symbol": sym,
        "type": tf,
        "limit": limit,
    }
    try:
        resp = requests.get(url, params=params, headers={"token": _ITICK_KEY}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        rows = data if isinstance(data, list) else data.get("data", [])
        if not rows:
            log.warning("iTick empty response for %s", symbol)
            return None
        records = []
        for r in rows:
            if isinstance(r, list):
                ts, o, h, l, c = r[0], r[1], r[2], r[3], r[4]
            else:
                ts = r.get("t") or r.get("ts") or r.get("time")
                o, h, l, c = float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"])
            records.append({
                "ts":     pd.to_datetime(ts, unit="ms", utc=True) if isinstance(ts, (int, float)) else pd.to_datetime(ts, utc=True),
                "open":   float(o),
                "high":   float(h),
                "low":    float(l),
                "close":  float(c),
                "volume": 0.0,
            })
        df = pd.DataFrame(records).set_index("ts").sort_index()
        df = df.tail(limit)
        log.info("iTick OK: %s %s (%d bars)", symbol, timeframe, len(df))
        return df
    except Exception as e:  # noqa: BLE001
        log.warning("iTick fetch failed for %s: %s", symbol, e)
        return None


# ---------------------------------------------------------------------------
# CCXT (crypto only)
# ---------------------------------------------------------------------------

_EXCHANGES: dict = {}
_EXCHANGE_BAD: set = set()

# Last-good cache: (symbol, timeframe) -> (timestamp, DataFrame)
_OHLCV_CACHE: dict = {}


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


def _cache_key(symbol: str, timeframe: str) -> Tuple[str, str]:
    return (symbol, timeframe)


def get_cached_ohlcv(symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
    """Return last known good OHLCV frame for (symbol, timeframe), if any."""
    rec = _OHLCV_CACHE.get(_cache_key(symbol, timeframe))
    return rec[1].copy() if rec else None


def _fetch_ccxt(
    symbol: str,
    timeframe: str,
    limit: int,
    preferred: str,
    since: Optional[int],
) -> Optional[pd.DataFrame]:
    """Fetch OHLCV via CCXT across preferred exchange + fallbacks."""
    last_err: Optional[Exception] = None
    for ex_name in _candidate_exchanges(preferred):
        for attempt in range(DATA_FETCH_RETRIES):
            try:
                ex = get_exchange(ex_name)
                raw = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=limit)
                if not raw:
                    raise RuntimeError("empty OHLCV response")
                df = pd.DataFrame(
                    raw, columns=["ts", "open", "high", "low", "close", "volume"]
                )
                df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
                df = df.set_index("ts").astype(float)
                log.info("CCXT %s OK: %s %s (%d bars)", ex_name, symbol, timeframe, len(df))
                return df
            except Exception as e:  # noqa: BLE001
                last_err = e
                msg = str(e)[:160]
                log.warning(
                    "fetch_ohlcv %s %s on %s failed (try %d/%d): %s",
                    symbol, timeframe, ex_name, attempt + 1, DATA_FETCH_RETRIES, msg,
                )
                low = msg.lower()
                if "451" in msg or "403" in msg or "restricted location" in low or "block access from your country" in low:
                    _EXCHANGE_BAD.add(ex_name)
                    log.warning("Marking %s as unusable from this region", ex_name)
                    break
                if attempt < DATA_FETCH_RETRIES - 1:
                    time.sleep(DATA_FETCH_BACKOFF_BASE * (2 ** attempt))
    log.warning("CCXT all exchanges failed for %s: %s", symbol, last_err)
    return None


# ---------------------------------------------------------------------------
# Public fetch_ohlcv (unified entry point)
# ---------------------------------------------------------------------------

def fetch_ohlcv(
    symbol: str,
    timeframe: str = "15m",
    limit: int = 300,
    exchange_name: Optional[str] = None,
    since: Optional[int] = None,
    use_cache_on_failure: bool = True,
) -> pd.DataFrame:
    """Fetch OHLCV data.

    - Forex pairs (containing '/'  between two fiat currencies):
        Twelvedata → FCS API → iTick → cache → raise
    - Crypto pairs:
        CCXT (preferred exchange + fallbacks) → cache → raise
    """
    preferred = exchange_name or EXCHANGE
    df: Optional[pd.DataFrame] = None

    if _is_forex(symbol):
        # --- Forex: triple REST API fallback, no CCXT ---
        df = _fetch_twelvedata(symbol, timeframe, limit)
        if df is None or df.empty:
            log.info("Twelvedata unavailable for %s, trying FCS API", symbol)
            df = _fetch_fcs(symbol, timeframe, limit)
        if df is None or df.empty:
            log.info("FCS API unavailable for %s, trying iTick", symbol)
            df = _fetch_itick(symbol, timeframe, limit)
    else:
        # --- Crypto: CCXT only ---
        df = _fetch_ccxt(symbol, timeframe, limit, preferred, since)

    if df is not None and not df.empty:
        if since is None:
            _OHLCV_CACHE[_cache_key(symbol, timeframe)] = (time.time(), df.copy())
        return df

    # All sources failed — try stale cache
    if use_cache_on_failure:
        cached = get_cached_ohlcv(symbol, timeframe)
        if cached is not None:
            log.warning(
                "fetch_ohlcv %s %s: using stale cached data after all sources failed",
                symbol, timeframe,
            )
            return cached

    raise RuntimeError(
        f"fetch_ohlcv failed for {symbol} {timeframe}: all sources exhausted"
    )


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def htf_bias(df_4h: pd.DataFrame, period: int = 200) -> str:
    """Return 'bullish', 'bearish', or 'flat' bias from 4h close vs EMA."""
    if len(df_4h) < period + 5:
        return "flat"
    e = ema(df_4h["close"], period)
    last_close = df_4h["close"].iloc[-1]
    last_ema = e.iloc[-1]
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


# ---------------------------------------------------------------------------
# ATR (Average True Range)
# ---------------------------------------------------------------------------

def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ATR over `period` bars."""
    if df is None or len(df) == 0:
        return pd.Series(dtype=float)
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    return atr


def atr_percentile(df: pd.DataFrame, period: int = 14, lookback: int = 100) -> float:
    """Return the percentile rank (0-100) of the most recent ATR value."""
    if df is None or len(df) < period + 5:
        return 50.0
    atr = calculate_atr(df, period=period).dropna()
    if len(atr) < 5:
        return 50.0
    window = atr.tail(lookback)
    current = float(atr.iloc[-1])
    rank = float((window <= current).sum()) / len(window) * 100.0
    return rank
