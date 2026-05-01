"""Timeframe manager — orchestrates multi-TF data fetching per mode.

Modes
------
scalp : HTF=1h  / entry=5m
day   : HTF=4h  / entry=15m  ← default
swing : HTF=1D  / entry=1h

All three modes run simultaneously in main.py. This module
provides the pair (htf, entry) for each mode and helper
functions to decide whether a given timeframe needs re-fetching
this iteration.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Tuple

from config import (
    SCALP_HTF, SCALP_ENTRY,
    DAY_HTF, DAY_ENTRY,
    SWING_HTF, SWING_ENTRY,
    HTF_REFRESH_MINUTES,
    TF_CACHE_TTL,
)

# Mode → (htf, entry)
MODE_MAP: Dict[str, Tuple[str, str]] = {
    "scalp": (SCALP_HTF, SCALP_ENTRY),
    "day":   (DAY_HTF,   DAY_ENTRY),
    "swing": (SWING_HTF, SWING_ENTRY),
}

ALL_MODES = list(MODE_MAP.keys())

# Track last HTF fetch per (symbol, htf)
_last_htf_fetch: Dict[Tuple[str, str], datetime] = {}


def timeframes_for_mode(mode: str) -> Tuple[str, str]:
    """Return (htf, entry_tf) for the given mode."""
    return MODE_MAP.get(mode, MODE_MAP["day"])


def needs_htf_refresh(symbol: str, htf: str) -> bool:
    """True if the HTF frame for this symbol hasn't been fetched recently."""
    key = (symbol, htf)
    last = _last_htf_fetch.get(key)
    if last is None:
        return True
    age = (datetime.now(tz=timezone.utc) - last).total_seconds()
    return age >= HTF_REFRESH_MINUTES * 60


def mark_htf_fetched(symbol: str, htf: str) -> None:
    _last_htf_fetch[(symbol, htf)] = datetime.now(tz=timezone.utc)


def entry_tf_limit(mode: str) -> int:
    """Candle limit to request for the entry timeframe of a given mode."""
    limits = {"scalp": 200, "day": 300, "swing": 200}
    return limits.get(mode, 300)


def htf_limit(mode: str) -> int:
    limits = {"scalp": 200, "day": 200, "swing": 100}
    return limits.get(mode, 200)


def mode_label(mode: str) -> str:
    labels = {"scalp": "🏎️ Scalp", "day": "📈 Day", "swing": "🌊 Swing"}
    return labels.get(mode, mode.capitalize())
