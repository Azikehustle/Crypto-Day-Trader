"""Send signals/messages/photos to Telegram via the Bot API (requests-based)."""
from typing import Optional, Dict, Any
from datetime import datetime, timezone
import os
import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, SCORE_MAX
from logger_setup import get_logger

log = get_logger("telegram")


def _api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"


def should_listen() -> bool:
    """Whether this instance should poll Telegram for incoming /commands.

    Controlled by the TELEGRAM_LISTEN env var. Default is 1 (listen) so
    behaviour is unchanged for single-instance setups. Set TELEGRAM_LISTEN=0
    on a secondary/dev instance to keep signal sending active while
    avoiding 409 Conflict errors from concurrent getUpdates polling.
    """
    raw = os.getenv("TELEGRAM_LISTEN", "1").strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def _credentials_ok(chat_id: Optional[str] = None) -> bool:
    if not TELEGRAM_BOT_TOKEN or not (chat_id or TELEGRAM_CHAT_ID):
        log.warning("Telegram credentials missing; skipping send.")
        return False
    return True


def send_message(text: str, chat_id: Optional[str] = None) -> bool:
    if not _credentials_ok(chat_id):
        return False
    try:
        r = requests.post(
            _api_url("sendMessage"),
            json={
                "chat_id": chat_id or TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if r.status_code != 200:
            log.error("Telegram error %s: %s", r.status_code, r.text)
            return False
        return True
    except Exception as e:  # noqa: BLE001
        log.error("Telegram send failed: %s", e)
        return False


def send_photo(
    photo_path: str,
    caption: str = "",
    chat_id: Optional[str] = None,
) -> bool:
    """Send an image with optional caption."""
    if not _credentials_ok(chat_id):
        return False
    if not photo_path or not os.path.exists(photo_path):
        log.warning("send_photo: file not found %s", photo_path)
        return False
    try:
        with open(photo_path, "rb") as f:
            r = requests.post(
                _api_url("sendPhoto"),
                data={
                    "chat_id": chat_id or TELEGRAM_CHAT_ID,
                    "caption": caption[:1024],
                    "parse_mode": "HTML",
                },
                files={"photo": f},
                timeout=30,
            )
        if r.status_code != 200:
            log.error("Telegram sendPhoto error %s: %s", r.status_code, r.text)
            return False
        return True
    except Exception as e:  # noqa: BLE001
        log.error("Telegram sendPhoto failed: %s", e)
        return False


def format_signal(signal_dict: Dict[str, Any]) -> str:
    arrow = "📈" if signal_dict["direction"] == "long" else "📉"
    side = signal_dict["direction"].upper()
    score_max = signal_dict.get("score_max", SCORE_MAX)
    extras = []
    if signal_dict.get("atr_pct") is not None:
        extras.append(f"ATR pct: {signal_dict['atr_pct']:.0f}")
    if signal_dict.get("vol_ok"):
        extras.append("Vol ✓")
    extras_line = (" | " + " | ".join(extras)) if extras else ""
    return (
        f"📊 <b>Signal: {signal_dict['symbol']} {side}</b> {arrow}\n"
        f"Entry: <code>{signal_dict['entry']:.4f}</code>\n"
        f"SL: <code>{signal_dict['stop_loss']:.4f}</code>\n"
        f"TP: <code>{signal_dict['take_profit']:.4f}</code>"
        f" ({signal_dict['tp_reason']})\n"
        f"RR: {signal_dict['rr']}\n"
        f"Confidence: {signal_dict['score']}/{score_max}{extras_line}\n"
        f"Timeframe: {signal_dict['timeframe']} | "
        f"HTF: {signal_dict['htf_bias'].title()}\n"
        f"Time: {signal_dict['timestamp']}"
    )


def send_signal(signal_dict: Dict[str, Any], chart_path: Optional[str] = None) -> bool:
    """Send a signal as a photo (if chart available) or as a text message."""
    caption = format_signal(signal_dict)
    if chart_path and os.path.exists(chart_path):
        if send_photo(chart_path, caption=caption):
            return True
        log.warning("Chart send failed; falling back to text-only signal.")
    return send_message(caption)


def send_heartbeat(state: Dict[str, Any]) -> bool:
    """Heartbeat ping with concise live state."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    text = (
        "🟢 <b>Bot Alive</b>\n"
        f"{ts}\n"
        f"Loop: {state.get('loop_count', 0)}\n"
        f"Signals today: {state.get('signals_today', 0)}\n"
        f"Open trades: {state.get('open_trades', 0)}\n"
        f"Daily P&amp;L: {state.get('daily_pnl_usd', 0.0):+.2f} USDT\n"
        f"Equity: {state.get('running_equity', 0.0):.2f} USDT\n"
        f"Exchange: {state.get('exchange', '?')}"
    )
    return send_message(text)


def send_crash_alert(error_msg: str) -> bool:
    return send_message(
        f"🚨 <b>CRASH</b>: <code>{(error_msg or '')[:500]}</code>\nCheck logs."
    )


def send_risk_alert(message: str) -> bool:
    return send_message(f"<b>Risk event</b>\n{message}")
