"""News shield — block trades around high-impact Forex news (Finnhub)."""
from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

import finnhub

from config import FINNHUB_API_KEY, NEWS_SHIELD_ENABLED, NEWS_HALT_MINUTES
from logger_setup import get_logger

log = get_logger("news")

_client: Optional[finnhub.Client] = None
_cache_events: List[Dict[str, Any]] = []
_cache_fetched_at: float = 0.0
_CACHE_TTL = 3600  # 1 h


def _get_client() -> Optional[finnhub.Client]:
    global _client
    if _client is None and FINNHUB_API_KEY:
        _client = finnhub.Client(api_key=FINNHUB_API_KEY)
    return _client


def _fetch_calendar() -> List[Dict[str, Any]]:
    """Fetch next 24h Forex economic calendar from Finnhub."""
    global _cache_events, _cache_fetched_at
    if time.time() - _cache_fetched_at < _CACHE_TTL and _cache_events:
        return _cache_events
    client = _get_client()
    if client is None:
        return []
    try:
        now = datetime.now(tz=timezone.utc)
        tomorrow = now + timedelta(hours=24)
        fmt = "%Y-%m-%d"
        result = client.economic_calendar(  # type: ignore[union-attr]
            _from=now.strftime(fmt),
            to=tomorrow.strftime(fmt),
        )
        events = result.get("economicCalendar") or []
        _cache_events = [
            e for e in events
            if str(e.get("impact") or e.get("importance") or "").lower() in ("high", "3", "2")
        ]
        _cache_fetched_at = time.time()
        log.info("News shield: %d high-impact events loaded", len(_cache_events))
        return _cache_events
    except Exception as e:  # noqa: BLE001
        log.warning("Finnhub calendar fetch failed: %s", e)
        return []


def _affected_pairs(event: Dict[str, Any]) -> List[str]:
    """Map event country → affected Forex pairs."""
    from config import SYMBOLS  # noqa: WPS433
    country = (event.get("country") or "").upper()
    mapping = {
        "US":  ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD"],
        "EU":  ["EUR/USD", "EUR/GBP"],
        "GB":  ["GBP/USD", "EUR/GBP"],
        "JP":  ["USD/JPY"],
        "AU":  ["AUD/USD"],
        "CA":  ["USD/CAD"],
        "EMU": ["EUR/USD", "EUR/GBP"],
    }
    pairs = mapping.get(country, [])
    return [p for p in pairs if p in SYMBOLS]


def is_news_blocked(symbol: str, ts: Optional[datetime] = None) -> bool:
    """Return True if symbol should be blocked due to imminent/recent news."""
    if not NEWS_SHIELD_ENABLED or not FINNHUB_API_KEY:
        return False
    now = ts or datetime.now(tz=timezone.utc)
    events = _fetch_calendar()
    window = timedelta(minutes=NEWS_HALT_MINUTES)
    for event in events:
        # Parse event time
        evt_str = event.get("time") or event.get("date") or ""
        try:
            if "T" in evt_str:
                evt_dt = datetime.fromisoformat(evt_str.replace("Z", "+00:00"))
            else:
                evt_dt = datetime.strptime(evt_str[:10], "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
        except Exception:  # noqa: BLE001
            continue
        # Block in [evt_dt - window, evt_dt + window]
        if (evt_dt - window) <= now <= (evt_dt + window):
            pairs = _affected_pairs(event)
            if symbol in pairs:
                log.info(
                    "NEWS BLOCK %s: %s (%s) impact %s",
                    symbol, event.get("event", "?"), evt_dt.isoformat(), event.get("impact", "?")
                )
                return True
    return False


def upcoming_events(symbol: str, lookahead_hours: int = 4) -> List[Dict[str, Any]]:
    """Return high-impact events affecting `symbol` in the next N hours."""
    now = datetime.now(tz=timezone.utc)
    events = _fetch_calendar()
    out = []
    for event in events:
        pairs = _affected_pairs(event)
        if symbol not in pairs:
            continue
        evt_str = event.get("time") or event.get("date") or ""
        try:
            if "T" in evt_str:
                evt_dt = datetime.fromisoformat(evt_str.replace("Z", "+00:00"))
            else:
                evt_dt = datetime.strptime(evt_str[:10], "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
        except Exception:  # noqa: BLE001
            continue
        if now <= evt_dt <= now + timedelta(hours=lookahead_hours):
            out.append({
                "event": event.get("event", "?"),
                "time":  evt_dt.isoformat(),
                "impact": event.get("impact", "?"),
                "country": event.get("country", "?"),
            })
    return out


def news_summary_text(symbol: str, lookahead_hours: int = 4) -> str:
    """Short one-liner for Telegram messages."""
    events = upcoming_events(symbol, lookahead_hours)
    if not events:
        return "No high-impact news in the next 4h"
    lines = [f"📰 High-impact news for {symbol}:"]
    for e in events[:3]:
        lines.append(f"  • {e['time'][11:16]} UTC — {e['event']} ({e['country']})")
    return "\n".join(lines)


def invalidate_cache() -> None:
    global _cache_events, _cache_fetched_at
    _cache_events = []
    _cache_fetched_at = 0.0
