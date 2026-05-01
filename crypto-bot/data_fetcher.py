"""Market data fetcher — Polars DataFrames, triple API fallback.

Provider priority per request:
  1. Twelvedata  (batch, 800 calls/day)
  2. FCS API     (fallback, 500 calls/month)
  3. iTick       (last resort / backtest history)
  4. CCXT        (crypto only, never used for forex in production)
  5. Stale cache (any age)

All public functions return ``pl.DataFrame`` with columns:
  ts (pl.Datetime("us", "UTC")), open, high, low, close, volume (pl.Float64)

The ``ts`` column is sorted ascending and has NO pandas-style index.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional, Dict, Tuple, List

import polars as pl
import numpy as np
import requests

from logger_setup import get_logger
from config import (
    TWELVEDATA_API_KEY, FCS_API_KEY, ITICK_API_KEY,
    TD_INTERVALS, FCS_INTERVALS, TF_CACHE_TTL,
    EXCHANGE, EXCHANGE_FALLBACKS,
    DATA_FETCH_RETRIES, DATA_FETCH_BACKOFF_BASE,
)

log = get_logger("data")

# ---------------------------------------------------------------------------
# Cache  — (symbol, timeframe) → (fetched_at_unix, pl.DataFrame)
# ---------------------------------------------------------------------------
_CACHE: Dict[Tuple[str, str], Tuple[float, pl.DataFrame]] = {}

_SCHEMA = {
    "ts":     pl.Datetime("us", "UTC"),
    "open":   pl.Float64,
    "high":   pl.Float64,
    "low":    pl.Float64,
    "close":  pl.Float64,
    "volume": pl.Float64,
}

_EMPTY = pl.DataFrame(schema=_SCHEMA)


def _cache_ttl(timeframe: str) -> float:
    return float(TF_CACHE_TTL.get(timeframe, 900))


def _try_cache(symbol: str, timeframe: str) -> Optional[pl.DataFrame]:
    rec = _CACHE.get((symbol, timeframe))
    if rec is None:
        return None
    fetched_at, df = rec
    if time.time() - fetched_at < _cache_ttl(timeframe):
        return df
    return None


def _store_cache(symbol: str, timeframe: str, df: pl.DataFrame) -> None:
    _CACHE[(symbol, timeframe)] = (time.time(), df)


def get_cached_ohlcv(symbol: str, timeframe: str) -> Optional[pl.DataFrame]:
    """Return last known good frame regardless of TTL (stale is ok for fallback)."""
    rec = _CACHE.get((symbol, timeframe))
    return rec[1] if rec else None


# ---------------------------------------------------------------------------
# Twelvedata provider
# ---------------------------------------------------------------------------
_TD_BASE = "https://api.twelvedata.com"


def _td_interval(tf: str) -> Optional[str]:
    return TD_INTERVALS.get(tf)


def _td_symbol(symbol: str) -> str:
    """EUR/USD → EUR/USD (Twelvedata uses slash notation)."""
    return symbol


def _parse_td_values(values: list) -> pl.DataFrame:
    """Convert Twelvedata 'values' list (newest-first) to sorted pl.DataFrame."""
    if not values:
        return _EMPTY
    rows = []
    for v in reversed(values):   # Twelvedata returns newest-first
        try:
            ts = datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
            rows.append({
                "ts":     ts,
                "open":   float(v["open"]),
                "high":   float(v["high"]),
                "low":    float(v["low"]),
                "close":  float(v["close"]),
                "volume": float(v.get("volume") or 0.0),
            })
        except Exception:  # noqa: BLE001
            continue
    if not rows:
        return _EMPTY
    df = pl.DataFrame(rows).cast({"ts": pl.Datetime("us", "UTC")})
    return df.sort("ts")


def _fetch_td_single(symbol: str, interval: str, outputsize: int) -> Optional[pl.DataFrame]:
    if not TWELVEDATA_API_KEY:
        return None
    try:
        url = f"{_TD_BASE}/time_series"
        params = {
            "symbol": symbol,
            "interval": interval,
            "outputsize": outputsize,
            "apikey": TWELVEDATA_API_KEY,
            "timezone": "UTC",
        }
        r = requests.get(url, params=params, timeout=20)
        if r.status_code != 200:
            log.warning("Twelvedata HTTP %s for %s", r.status_code, symbol)
            return None
        data = r.json()
        if data.get("status") == "error" or "values" not in data:
            log.warning("Twelvedata error %s: %s", symbol, data.get("message", ""))
            return None
        df = _parse_td_values(data["values"])
        if df.is_empty():
            return None
        return df
    except Exception as e:  # noqa: BLE001
        log.warning("Twelvedata request failed %s: %s", symbol, e)
        return None


def fetch_td_batch(
    symbols: List[str], timeframe: str, limit: int = 300
) -> Dict[str, pl.DataFrame]:
    """Fetch multiple symbols in one Twelvedata API call.
    Returns dict {symbol: df}. Missing symbols will be absent from dict."""
    if not TWELVEDATA_API_KEY:
        return {}
    interval = _td_interval(timeframe)
    if not interval:
        return {}
    try:
        url = f"{_TD_BASE}/time_series"
        params = {
            "symbol": ",".join(symbols),
            "interval": interval,
            "outputsize": limit,
            "apikey": TWELVEDATA_API_KEY,
            "timezone": "UTC",
        }
        r = requests.get(url, params=params, timeout=30)
        if r.status_code != 200:
            log.warning("Twelvedata batch HTTP %s", r.status_code)
            return {}
        data = r.json()
        if len(symbols) == 1:
            # Single symbol still wrapped differently
            if "values" in data:
                df = _parse_td_values(data["values"])
                return {symbols[0]: df} if not df.is_empty() else {}
            return {}
        result: Dict[str, pl.DataFrame] = {}
        for sym in symbols:
            sub = data.get(sym, {})
            if sub.get("status") == "error" or "values" not in sub:
                continue
            df = _parse_td_values(sub["values"])
            if not df.is_empty():
                result[sym] = df
        return result
    except Exception as e:  # noqa: BLE001
        log.warning("Twelvedata batch failed: %s", e)
        return {}


# ---------------------------------------------------------------------------
# FCS API provider  (fallback)
# ---------------------------------------------------------------------------
_FCS_BASE = "https://fcsapi.com/api-v3/forex"


def _fetch_fcs_single(symbol: str, timeframe: str, limit: int) -> Optional[pl.DataFrame]:
    if not FCS_API_KEY:
        return None
    interval = FCS_INTERVALS.get(timeframe)
    if not interval:
        return None
    try:
        # FCS symbol: EUR/USD → EUR/USD
        params = {
            "symbol": symbol,
            "period": interval,
            "access_key": FCS_API_KEY,
            "level": str(min(limit, 200)),
        }
        r = requests.get(f"{_FCS_BASE}/history", params=params, timeout=20)
        if r.status_code != 200:
            log.warning("FCS HTTP %s for %s", r.status_code, symbol)
            return None
        data = r.json()
        resp = data.get("response") or []
        if not resp:
            log.warning("FCS empty response for %s", symbol)
            return None
        rows = []
        for v in resp:
            try:
                ts_str = v.get("tm") or v.get("d") or ""
                if not ts_str:
                    continue
                # "2024-01-01 12:00:00" or "2024-01-01T12:00:00"
                ts_str = ts_str.replace("T", " ")
                ts = datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
                rows.append({
                    "ts":     ts,
                    "open":   float(v.get("o") or v.get("open") or 0),
                    "high":   float(v.get("h") or v.get("high") or 0),
                    "low":    float(v.get("l") or v.get("low") or 0),
                    "close":  float(v.get("c") or v.get("close") or 0),
                    "volume": float(v.get("v") or v.get("volume") or 0),
                })
            except Exception:  # noqa: BLE001
                continue
        if not rows:
            return None
        return pl.DataFrame(rows).cast({"ts": pl.Datetime("us", "UTC")}).sort("ts")
    except Exception as e:  # noqa: BLE001
        log.warning("FCS request failed %s: %s", symbol, e)
        return None


# ---------------------------------------------------------------------------
# iTick provider  (tertiary / backtest history)
# ---------------------------------------------------------------------------
_ITICK_BASE = "https://api.itick.org"

# iTick timeframe → API type string
_ITICK_INTERVALS = {
    "1m": "1", "5m": "5", "15m": "15",
    "1h": "60", "4h": "240", "1D": "1440",
}


def _fetch_itick_single(symbol: str, timeframe: str, limit: int) -> Optional[pl.DataFrame]:
    if not ITICK_API_KEY:
        return None
    tf_code = _ITICK_INTERVALS.get(timeframe)
    if not tf_code:
        return None
    try:
        # iTick forex symbol: EUR/USD → EURUSD
        fx_sym = symbol.replace("/", "")
        params = {
            "token": ITICK_API_KEY,
            "code":  fx_sym,
            "type":  tf_code,
            "count": str(min(limit, 500)),
        }
        r = requests.get(f"{_ITICK_BASE}/instrument/his", params=params, timeout=20)
        if r.status_code != 200:
            log.warning("iTick HTTP %s for %s", r.status_code, symbol)
            return None
        data = r.json()
        bars = data.get("data") or data.get("result") or []
        if not bars:
            return None
        rows = []
        for v in bars:
            try:
                ts_raw = v.get("t") or v.get("time") or v.get("ts") or 0
                if isinstance(ts_raw, (int, float)):
                    # milliseconds or seconds
                    ms = ts_raw if ts_raw > 1e10 else ts_raw * 1000
                    ts = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
                else:
                    ts = datetime.strptime(str(ts_raw)[:19], "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=timezone.utc
                    )
                rows.append({
                    "ts":     ts,
                    "open":   float(v.get("o") or v.get("open") or 0),
                    "high":   float(v.get("h") or v.get("high") or 0),
                    "low":    float(v.get("l") or v.get("low") or 0),
                    "close":  float(v.get("c") or v.get("close") or 0),
                    "volume": float(v.get("v") or v.get("volume") or 0),
                })
            except Exception:  # noqa: BLE001
                continue
        if not rows:
            return None
        return pl.DataFrame(rows).cast({"ts": pl.Datetime("us", "UTC")}).sort("ts")
    except Exception as e:  # noqa: BLE001
        log.warning("iTick request failed %s: %s", symbol, e)
        return None


# ---------------------------------------------------------------------------
# CCXT provider  (crypto last resort / backtest legacy)
# ---------------------------------------------------------------------------
_CCXT_INSTANCES: dict = {}
_CCXT_BAD: set = set()


def _get_ccxt(name: str):
    if name not in _CCXT_INSTANCES:
        import ccxt  # noqa: WPS433
        cls = getattr(ccxt, name)
        ex = cls({"enableRateLimit": True, "timeout": 20_000})
        try:
            ex.load_markets()
        except Exception as e:  # noqa: BLE001
            log.warning("CCXT load_markets failed %s: %s", name, e)
        _CCXT_INSTANCES[name] = ex
    return _CCXT_INSTANCES[name]


def _fetch_ccxt(
    symbol: str, timeframe: str, limit: int, exchange_name: Optional[str] = None
) -> Optional[pl.DataFrame]:
    preferred = exchange_name or EXCHANGE
    seq = [preferred] + [x for x in EXCHANGE_FALLBACKS if x != preferred]
    seq = [x for x in seq if x not in _CCXT_BAD]
    for ex_name in seq:
        for attempt in range(DATA_FETCH_RETRIES):
            try:
                ex = _get_ccxt(ex_name)
                raw = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
                if not raw:
                    raise RuntimeError("empty OHLCV")
                rows = [
                    {
                        "ts":     datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc),
                        "open":   float(r[1]),
                        "high":   float(r[2]),
                        "low":    float(r[3]),
                        "close":  float(r[4]),
                        "volume": float(r[5]),
                    }
                    for r in raw
                ]
                df = pl.DataFrame(rows).cast({"ts": pl.Datetime("us", "UTC")}).sort("ts")
                return df
            except Exception as e:  # noqa: BLE001
                msg = str(e)[:160]
                log.warning("CCXT %s %s %s (try %d): %s", ex_name, symbol, timeframe, attempt + 1, msg)
                low = msg.lower()
                if any(c in msg for c in ("451", "403")) or "restricted" in low:
                    _CCXT_BAD.add(ex_name)
                    break
                if attempt < DATA_FETCH_RETRIES - 1:
                    time.sleep(DATA_FETCH_BACKOFF_BASE * (2 ** attempt))
    return None


# ---------------------------------------------------------------------------
# Unified public API
# ---------------------------------------------------------------------------

def fetch_ohlcv(
    symbol: str,
    timeframe: str = "15m",
    limit: int = 300,
    exchange_name: Optional[str] = None,
    use_cache_on_failure: bool = True,
) -> pl.DataFrame:
    """Fetch OHLCV with triple fallback. Returns pl.DataFrame sorted by ts.

    Provider order: Twelvedata → FCS API → iTick → CCXT → stale cache.
    Telegram fallback alerts are sent automatically.
    """
    # 1. Fresh cache hit
    cached = _try_cache(symbol, timeframe)
    if cached is not None:
        return cached

    # 2. Twelvedata
    if TWELVEDATA_API_KEY:
        interval = _td_interval(timeframe)
        if interval:
            df = _fetch_td_single(symbol, timeframe, limit)
            if df is not None and not df.is_empty():
                _store_cache(symbol, timeframe, df)
                return df
        log.warning("Twelvedata failed for %s %s — trying FCS", symbol, timeframe)
        _send_fallback_alert("FCS API", symbol, "Twelvedata")

    # 3. FCS API
    if FCS_API_KEY:
        df = _fetch_fcs_single(symbol, timeframe, limit)
        if df is not None and not df.is_empty():
            _store_cache(symbol, timeframe, df)
            log.info("FCS API used for %s %s", symbol, timeframe)
            return df
        log.warning("FCS API failed for %s %s — trying iTick", symbol, timeframe)
        _send_fallback_alert("iTick", symbol, "FCS API")

    # 4. iTick
    if ITICK_API_KEY:
        df = _fetch_itick_single(symbol, timeframe, limit)
        if df is not None and not df.is_empty():
            _store_cache(symbol, timeframe, df)
            log.info("iTick used for %s %s", symbol, timeframe)
            return df
        log.warning("iTick failed for %s %s — trying CCXT", symbol, timeframe)

    # 5. CCXT (last resort, crypto symbols only in practice)
    df = _fetch_ccxt(symbol, timeframe, limit, exchange_name)
    if df is not None and not df.is_empty():
        _store_cache(symbol, timeframe, df)
        return df

    # 6. Stale cache
    if use_cache_on_failure:
        stale = get_cached_ohlcv(symbol, timeframe)
        if stale is not None:
            log.warning("Using stale cache for %s %s after all providers failed", symbol, timeframe)
            return stale

    _send_all_fail_alert(symbol, timeframe)
    raise RuntimeError(f"All data providers failed for {symbol} {timeframe}")


def _send_fallback_alert(active_provider: str, symbol: str, failed_provider: str) -> None:
    """Non-blocking Telegram alert on provider fallback."""
    try:
        from telegram_bot import send_message  # noqa: WPS433
        send_message(
            f"⚠️ <b>Data Fallback</b>\n"
            f"{failed_provider} failed for <code>{symbol}</code> — "
            f"switching to <b>{active_provider}</b>."
        )
    except Exception:  # noqa: BLE001
        pass


def _send_all_fail_alert(symbol: str, timeframe: str) -> None:
    try:
        from telegram_bot import send_message  # noqa: WPS433
        send_message(
            f"🔴 <b>DATA FAILURE</b>\n"
            f"All providers failed for <code>{symbol} {timeframe}</code>.\n"
            f"Bot skipping this symbol until next cycle."
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Technical indicators — return pl.Series (aligned to df rows)
# ---------------------------------------------------------------------------

def ema(series: pl.Series, period: int) -> pl.Series:
    """Exponential moving average (EWM span)."""
    return series.ewm_mean(span=period, adjust=False)


def htf_bias(df: pl.DataFrame, period: int = 200) -> str:
    """'bullish' | 'bearish' | 'flat' from close vs EMA-200."""
    if len(df) < period + 5:
        return "flat"
    close = df["close"]
    ema_vals = ema(close, period)
    last_close = float(close[-1])
    last_ema = float(ema_vals[-1])
    slope = float(ema_vals[-1]) - float(ema_vals[-10])
    pct_dist = (last_close - last_ema) / last_ema
    if last_close > last_ema and slope >= 0 and pct_dist > 0.001:
        return "bullish"
    if last_close < last_ema and slope <= 0 and pct_dist < -0.001:
        return "bearish"
    return "flat"


def avg_body(df: pl.DataFrame, lookback: int = 20) -> float:
    tail = df.tail(lookback)
    bodies = (tail["close"] - tail["open"]).abs()
    return float(bodies.mean()) if len(bodies) else 0.0


def candle_body(row: dict) -> float:
    return float(abs(row["close"] - row["open"]))


def is_bullish_engulfing(prev: dict, curr: dict) -> bool:
    return (
        curr["close"] > curr["open"]
        and prev["close"] < prev["open"]
        and curr["close"] >= prev["open"]
        and curr["open"] <= prev["close"]
    )


def is_bearish_engulfing(prev: dict, curr: dict) -> bool:
    return (
        curr["close"] < curr["open"]
        and prev["close"] > prev["open"]
        and curr["open"] >= prev["close"]
        and curr["close"] <= prev["open"]
    )


def is_bull_pin(row: dict) -> bool:
    rng = row["high"] - row["low"]
    if rng <= 0:
        return False
    body = abs(row["close"] - row["open"])
    lower_wick = min(row["open"], row["close"]) - row["low"]
    return body / rng < 0.4 and lower_wick / rng > 0.5 and row["close"] > row["open"]


def is_bear_pin(row: dict) -> bool:
    rng = row["high"] - row["low"]
    if rng <= 0:
        return False
    body = abs(row["close"] - row["open"])
    upper_wick = row["high"] - max(row["open"], row["close"])
    return body / rng < 0.4 and upper_wick / rng > 0.5 and row["close"] < row["open"]


def calculate_atr(df: pl.DataFrame, period: int = 14) -> pl.Series:
    """Wilder's ATR. Returns pl.Series aligned to df rows."""
    if df is None or len(df) == 0:
        return pl.Series("atr", [], dtype=pl.Float64)
    df_with_tr = df.with_columns(
        pl.concat_list([
            (pl.col("high") - pl.col("low")).abs(),
            (pl.col("high") - pl.col("close").shift(1)).abs(),
            (pl.col("low")  - pl.col("close").shift(1)).abs(),
        ]).list.max().alias("_tr")
    )
    atr_series = df_with_tr["_tr"].ewm_mean(alpha=1.0 / period, adjust=False)
    return atr_series.rename("atr")


def atr_percentile(df: pl.DataFrame, period: int = 14, lookback: int = 100) -> float:
    """Percentile rank (0-100) of current ATR within the last `lookback` ATR values."""
    if df is None or len(df) < period + 5:
        return 50.0
    atr = calculate_atr(df, period=period).drop_nulls()
    if len(atr) < 5:
        return 50.0
    window = atr.tail(lookback)
    current = float(atr[-1])
    rank = float((window <= current).sum()) / len(window) * 100.0
    return rank


# ---------------------------------------------------------------------------
# Batch fetch helper for main loop (minimises Twelvedata API calls)
# ---------------------------------------------------------------------------

def fetch_batch_for_symbols(
    symbols: List[str],
    timeframe: str,
    limit: int = 300,
) -> Dict[str, pl.DataFrame]:
    """Fetch multiple symbols via the Twelvedata batch endpoint (1 API call).
    Falls back to per-symbol fetch for symbols not returned by batch."""
    result: Dict[str, pl.DataFrame] = {}

    # Check cache first
    to_fetch = []
    for sym in symbols:
        cached = _try_cache(sym, timeframe)
        if cached is not None:
            result[sym] = cached
        else:
            to_fetch.append(sym)

    if not to_fetch:
        return result

    # One batch Twelvedata call for all missing symbols
    if TWELVEDATA_API_KEY and _td_interval(timeframe):
        batch = fetch_td_batch(to_fetch, timeframe, limit)
        for sym, df in batch.items():
            if not df.is_empty():
                _store_cache(sym, timeframe, df)
                result[sym] = df
        still_missing = [s for s in to_fetch if s not in result]
    else:
        still_missing = to_fetch

    # Per-symbol fallback for anything batch missed
    for sym in still_missing:
        try:
            df = fetch_ohlcv(sym, timeframe, limit)
            result[sym] = df
        except Exception as e:  # noqa: BLE001
            log.error("fetch_ohlcv %s %s failed: %s", sym, timeframe, e)

    return result
