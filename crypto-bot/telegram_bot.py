"""Send signals/messages/photos to Telegram via the Bot API (requests-based)."""
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
import os
import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, SCORE_MAX
from logger_setup import get_logger

log = get_logger("telegram")


# ---------------------------------------------------------------------------
# Bot commands menu (BotFather setMyCommands)
# ---------------------------------------------------------------------------

BOT_COMMANDS: List[Dict[str, str]] = [
    {"command": "start",       "description": "Open the main menu"},
    {"command": "status",      "description": "Bot status, HTF bias, open trades"},
    {"command": "performance", "description": "Win rate (7d / 30d / all-time)"},
    {"command": "equity",      "description": "Equity, daily P&L, halt status"},
    {"command": "zones",       "description": "Active supply / demand zones"},
    {"command": "last",        "description": "Last signal per symbol"},
    {"command": "summary",     "description": "Daily paper-trading summary"},
    {"command": "config",      "description": "Live tuning of risk settings"},
    {"command": "pairs",       "description": "Manage scanned trading pairs"},
    {"command": "log",         "description": "Recent log lines"},
    {"command": "chart",       "description": "Render a chart for a symbol"},
    {"command": "backtest",    "description": "How to run a historical backtest"},
    {"command": "stop",        "description": "Pause new trade scanning"},
    {"command": "restart",     "description": "Restart the bot process"},
    {"command": "help",        "description": "Show this menu"},
]


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


def send_message(
    text: str,
    chat_id: Optional[str] = None,
    reply_markup: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Send a text message. Returns the Telegram message dict on success, else None."""
    if not _credentials_ok(chat_id):
        return None
    try:
        payload: Dict[str, Any] = {
            "chat_id": chat_id or TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        r = requests.post(_api_url("sendMessage"), json=payload, timeout=15)
        if r.status_code != 200:
            log.error("Telegram error %s: %s", r.status_code, r.text)
            return None
        data = r.json()
        return data.get("result") if data.get("ok") else None
    except Exception as e:  # noqa: BLE001
        log.error("Telegram send failed: %s", e)
        return None


def edit_message(
    chat_id: str,
    message_id: int,
    text: str,
    reply_markup: Optional[Dict[str, Any]] = None,
) -> bool:
    """Edit an existing message in place using editMessageText."""
    if not TELEGRAM_BOT_TOKEN:
        return False
    try:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        r = requests.post(_api_url("editMessageText"), json=payload, timeout=15)
        if r.status_code == 200:
            return True
        # 400 "message is not modified" is harmless
        body = r.text or ""
        if "message is not modified" in body:
            return True
        log.error("Telegram editMessageText %s: %s", r.status_code, body)
        return False
    except Exception as e:  # noqa: BLE001
        log.error("Telegram editMessageText failed: %s", e)
        return False


def answer_callback_query(
    callback_query_id: str,
    text: str = "",
    show_alert: bool = False,
) -> bool:
    """Acknowledge a button press so Telegram stops the loading spinner."""
    if not TELEGRAM_BOT_TOKEN:
        return False
    try:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:200]
        if show_alert:
            payload["show_alert"] = True
        r = requests.post(_api_url("answerCallbackQuery"), json=payload, timeout=10)
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        log.error("answerCallbackQuery failed: %s", e)
        return False


def set_my_commands() -> bool:
    """Register the /commands menu so it appears as suggestions in Telegram."""
    if not TELEGRAM_BOT_TOKEN:
        return False
    try:
        r = requests.post(
            _api_url("setMyCommands"),
            json={"commands": BOT_COMMANDS},
            timeout=15,
        )
        if r.status_code == 200 and r.json().get("ok"):
            log.info("Registered %d commands with BotFather menu.", len(BOT_COMMANDS))
            return True
        log.warning("setMyCommands failed: %s", r.text)
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("setMyCommands error: %s", e)
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
