"""Central configuration for the crypto day trading bot."""
import os

EXCHANGE = os.getenv("EXCHANGE", "kucoin")
EXCHANGE_FALLBACKS = [
    s.strip() for s in os.getenv("EXCHANGE_FALLBACKS", "okx,bybit").split(",") if s.strip()
]
SYMBOLS = [s.strip() for s in os.getenv(
    "SYMBOLS", "BTC/USDT,ETH/USDT,SOL/USDT"
).split(",") if s.strip()]

HTF_TIMEFRAME = "4h"
ENTRY_TIMEFRAME = "15m"

HTF_EMA_PERIOD = 200
SWING_LOOKBACK = 5
DISPLACEMENT_LOOKBACK = 20
DISPLACEMENT_MULT = 1.2
ZONE_PROXIMITY_PCT = 0.005
SL_BUFFER_PCT = 0.001
LIQUIDITY_TOLERANCE = 0.002
EQUAL_LEVEL_TOLERANCE = 0.002

MIN_RR_FOR_TARGET = 1.5
MAX_RR_FOR_TARGET = 3.0
FALLBACK_RR = 2.0

SCORE_THRESHOLD_SEND = 8
SCORE_THRESHOLD_LOG = 6

LONDON_OPEN = (7, 9)
NY_OPEN = (12, 14)

LOOP_SLEEP_SECONDS = 60
HTF_REFRESH_MINUTES = 60

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
LOG_FILE = os.path.join(DATA_DIR, "bot.log")
TRADES_FILE = os.path.join(DATA_DIR, "trades.json")

os.makedirs(DATA_DIR, exist_ok=True)
