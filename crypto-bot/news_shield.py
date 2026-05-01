"""News Shield — high-impact economic event filter for Oracle_v5.

Queries the Finnhub economic calendar for HIGH-impact events within a
configurable window. When a qualifying event is found, sets NEWS_HALT=True
and fires a Telegram alert. Resumes automatically once the window passes.

Config (env vars):
  NEWS_SHIELD_ENABLED   = "true" / "false"   (default: true)
  NEWS_HALT_MINUTES     = 30                  (default: minutes before/after event)
  FINNHUB_API_KEY       = <key>
  NEWS_CURRENCIES       = "USD,EUR,GBP"       (default: USD,EUR,GBP,JPY)

Thread-safe. Call check_high_impact_events() from the main bot loop; it
caches results for NEWS_CACHE_MINUTES (default 15) to avoid hammering the API.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

from logger_setup import get_logger

log = get_logger("news_shield")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NEWS_SHIELD_ENABLED: bool = os.getenv("NEWS_SHIELD_ENABLED", "true").lower() not in (
    "0", "false", "no", "off",
)
NEWS_HALT_MINUTES: int = int(os.getenv("NEWS_HALT_MINUTES", "30"))
NEWS_CACHE_MINUTES: int = int(os.getenv("NEWS_CACHE_MINUTES", "15"))
_FINNHUB_KEY: str = os.getenv("FINNHUB_API_KEY", "")
_NEWS_CURRENCIES: List[str] = [
    c.strip().upper()
    for c in os.getenv("NEWS_CURRENCIES", "USD,EUR,GBP,JPY").split(",")
    if c.strip()
]

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_lock = threading.RLock()
_NEWS_HALT: bool = False
_halt_reason: str = ""
_cache: Optional[List[Dict[str, Any]]] = None
_cache_fetched_at: float = 0.0
_alert_sent_for: set = set()   # event ids already alerted


# ---------------------------------------------------------------------------
# Finnhub helpers
# ---------------------------------------------------------------------------

def _fetch_calendar() -> List[Dict[str, Any]]:
    """Fetch today's + tomorrow's economic calendar from Finnhub."""
    if not _FINNHUB_KEY:
        log.warning("news_shield: FINNHUB_API_KEY not set — shield disabled")
        return []
    try:
        import finnhub  # type: ignore
        client = finnhub.Client(api_key=_FINNHUB_KEY)
        today = datetime.now(timezone.utc)
        tomorrow = today + timedelta(days=1)
        data = client.economic_calendar()
        events = data.get("economicCalendar", []) if isinstance(data, dict) else []
        return events
    except ImportError:
        log.error("news_shield: finnhub-python not installed. Run: pip install finnhub-python")
        return []
    except Exception as e:  # noqa: BLE001
        log.error("news_shield: calendar fetch failed: %s", e)
        return []


def _get_cached_calendar() -> List[Dict[str, Any]]:
    """Return calendar events, refreshing the cache if stale."""
    global _cache, _cache_fetched_at
    with _lock:
        age = time.time() - _cache_fetched_at
        if _cache is None or age > NEWS_CACHE_MINUTES * 60:
            log.debug("news_shield: refreshing calendar cache")
            _cache = _fetch_calendar()
            _cache_fetched_at = time.time()
        return list(_cache)


def _is_high_impact(event: Dict[str, Any]) -> bool:
    """Return True if the event is HIGH impact and for a watched currency."""
    impact = str(event.get("impact", "")).upper()
    currency = str(event.get("unit", event.get("currency", ""))).upper()
    return impact in ("HIGH", "3") and currency in _NEWS_CURRENCIES


def _event_time(event: Dict[str, Any]) -> Optional[datetime]:
    """Parse the event time to a UTC-aware datetime."""
    raw = event.get("time") or event.get("datetime") or event.get("date")
    if not raw:
        return None
    try:
        # Finnhub returns ISO-8601 strings
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except Exception:  # noqa: BLE001
        return None


def _within_window(event_dt: datetime, window_minutes: int) -> bool:
    """Return True if now is within ±window_minutes of event_dt."""
    now = datetime.now(timezone.utc)
    delta = abs((event_dt - now).total_seconds()) / 60.0
    return delta <= window_minutes


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_high_impact_events() -> bool:
    """Check for high-impact events within the halt window.

    Sets the NEWS_HALT flag and sends a Telegram alert when a qualifying
    event is found. Clears the flag automatically once all events pass.

    Returns True if a halt is currently active, False otherwise.
    """
    global _NEWS_HALT, _halt_reason

    if not NEWS_SHIELD_ENABLED:
        return False

    events = _get_cached_calendar()
    now = datetime.now(timezone.utc)
    active_events: List[Dict[str, Any]] = []

    for ev in events:
        if not _is_high_impact(ev):
            continue
        ev_dt = _event_time(ev)
        if ev_dt is None:
            continue
        if _within_window(ev_dt, NEWS_HALT_MINUTES):
            active_events.append(ev)

    with _lock:
        if active_events:
            ev = active_events[0]
            ev_id = str(ev.get("id") or ev.get("event") or ev.get("time", ""))
            ev_name = ev.get("event") or ev.get("name") or "Unknown event"
            ev_currency = ev.get("unit") or ev.get("currency", "?")
            ev_dt = _event_time(ev)
            ev_time_str = ev_dt.strftime("%H:%M UTC") if ev_dt else "?"

            if not _NEWS_HALT:
                _NEWS_HALT = True
                _halt_reason = f"{ev_name} ({ev_currency}) @ {ev_time_str}"
                log.warning("NEWS_SHIELD: halt triggered — %s", _halt_reason)

            if ev_id and ev_id not in _alert_sent_for:
                _alert_sent_for.add(ev_id)
                _send_halt_alert(ev_name, ev_currency, ev_time_str, len(active_events))
        else:
            if _NEWS_HALT:
                _NEWS_HALT = False
                _halt_reason = ""
                log.info("NEWS_SHIELD: halt cleared — no upcoming high-impact events")
                _send_resume_alert()

        return _NEWS_HALT


def is_halted() -> bool:
    """Return True if the news shield is currently blocking signals."""
    with _lock:
        return _NEWS_HALT


def halt_reason() -> str:
    """Return human-readable reason for the current halt, or empty string."""
    with _lock:
        return _halt_reason


def force_clear() -> None:
    """Manually clear the news halt (for operator override)."""
    global _NEWS_HALT, _halt_reason
    with _lock:
        _NEWS_HALT = False
        _halt_reason = ""
    log.info("NEWS_SHIELD: manually cleared by operator")


def status_dict() -> Dict[str, Any]:
    """Return a dict summarising current shield state."""
    with _lock:
        return {
            "enabled": NEWS_SHIELD_ENABLED,
            "halted": _NEWS_HALT,
            "reason": _halt_reason,
            "halt_window_minutes": NEWS_HALT_MINUTES,
            "currencies": _NEWS_CURRENCIES,
        }


# ---------------------------------------------------------------------------
# Telegram alerts
# ---------------------------------------------------------------------------

def _send_halt_alert(event_name: str, currency: str, event_time: str, count: int) -> None:
    try:
        from telegram_bot import send_message
        msg = (
            f"📰 <b>Oracle_v5 News Shield — HALT</b>\n"
            f"High-impact event detected:\n"
            f"<b>{event_name}</b> ({currency}) @ {event_time}\n"
            f"Total events in window: {count}\n"
            f"⛔ New signals blocked for ±{NEWS_HALT_MINUTES} min.\n"
            f"Auto-resumes after the window passes."
        )
        send_message(msg)
    except Exception as e:  # noqa: BLE001
        log.warning("news_shield: Telegram halt alert failed: %s", e)


def _send_resume_alert() -> None:
    try:
        from telegram_bot import send_message
        send_message(
            "✅ <b>Oracle_v5 News Shield — Resumed</b>\n"
            "No high-impact events in window. Scanning resumed."
        )
    except Exception as e:  # noqa: BLE001
        log.warning("news_shield: Telegram resume alert failed: %s", e)
