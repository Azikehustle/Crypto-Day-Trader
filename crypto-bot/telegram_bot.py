"""Send signals/messages to Telegram via the Bot API (requests-based)."""
from typing import Optional
import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from logger_setup import get_logger

log = get_logger("telegram")


def _api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"


def send_message(text: str, chat_id: Optional[str] = None) -> bool:
    if not TELEGRAM_BOT_TOKEN or not (chat_id or TELEGRAM_CHAT_ID):
        log.warning("Telegram credentials missing; skipping send.")
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


def format_signal(signal_dict: dict) -> str:
    arrow = "📈" if signal_dict["direction"] == "long" else "📉"
    side = signal_dict["direction"].upper()
    return (
        f"📊 <b>Signal: {signal_dict['symbol']} {side}</b> {arrow}\n"
        f"Entry: <code>{signal_dict['entry']:.4f}</code>\n"
        f"SL: <code>{signal_dict['stop_loss']:.4f}</code>\n"
        f"TP: <code>{signal_dict['take_profit']:.4f}</code>"
        f" ({signal_dict['tp_reason']})\n"
        f"RR: {signal_dict['rr']}\n"
        f"Confidence: {signal_dict['score']}/13\n"
        f"Timeframe: {signal_dict['timeframe']} | "
        f"HTF: {signal_dict['htf_bias'].title()}\n"
        f"Time: {signal_dict['timestamp']}"
    )


def send_signal(signal_dict: dict) -> bool:
    return send_message(format_signal(signal_dict))
