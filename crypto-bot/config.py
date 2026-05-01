"""Central configuration for the forex trading bot."""
import os

# ---------------------------------------------------------------------------
# Trading pairs (Forex only)
# ---------------------------------------------------------------------------
SYMBOLS = [s.strip() for s in os.getenv(
    "SYMBOLS", "EUR/USD,GBP/USD,USD/JPY,AUD/USD,USD/CAD,EUR/GBP"
).split(",") if s.strip()]

EXCHANGE = os.getenv("EXCHANGE", "kucoin")           # kept for CCXT last-resort
EXCHANGE_FALLBACKS = [
    s.strip() for s in os.getenv("EXCHANGE_FALLBACKS", "okx,bybit").split(",") if s.strip()
]

# ---------------------------------------------------------------------------
# Timeframe modes (all active simultaneously; DAY is the default view)
# ---------------------------------------------------------------------------
SCALP_HTF   = "1h"
SCALP_ENTRY = "5m"
DAY_HTF     = "4h"
DAY_ENTRY   = "15m"
SWING_HTF   = "1D"
SWING_ENTRY = "1h"
DEFAULT_MODE = os.getenv("DEFAULT_MODE", "day")      # "scalp" | "day" | "swing"

# HTF legacy aliases (used by backtest and some helpers)
HTF_TIMEFRAME   = DAY_HTF
ENTRY_TIMEFRAME = DAY_ENTRY

# ---------------------------------------------------------------------------
# Data sources — triple fallback
# ---------------------------------------------------------------------------
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY")
FCS_API_KEY        = os.getenv("FCS_API_KEY")
ITICK_API_KEY      = os.getenv("ITICK_API_KEY")
FINNHUB_API_KEY    = os.getenv("FINNHUB_API_KEY")

# Twelvedata interval strings per bot timeframe
TD_INTERVALS = {
    "1m": "1min", "5m": "5min", "15m": "15min",
    "1h": "1h", "4h": "4h", "1D": "1day",
}

# FCS API interval strings
FCS_INTERVALS = {
    "1m": "1m", "5m": "5m", "15m": "15m",
    "1h": "1h", "4h": "4h", "1D": "1D",
}

# Cache TTL per timeframe (seconds) — don't re-fetch until stale
TF_CACHE_TTL = {
    "1m": 60, "5m": 300, "15m": 900,
    "1h": 3600, "4h": 14400, "1D": 86400,
}

# ---------------------------------------------------------------------------
# Technical analysis
# ---------------------------------------------------------------------------
HTF_EMA_PERIOD         = 200
SWING_LOOKBACK         = 5
DISPLACEMENT_LOOKBACK  = 20
DISPLACEMENT_MULT      = 1.2
ZONE_PROXIMITY_PCT     = 0.005
SL_BUFFER_PCT          = 0.001
LIQUIDITY_TOLERANCE    = 0.002
EQUAL_LEVEL_TOLERANCE  = 0.002

MIN_RR_FOR_TARGET = 1.5
MAX_RR_FOR_TARGET = 3.0
FALLBACK_RR       = 2.0

# Score thresholds (max = 15)
SCORE_THRESHOLD_SEND = 8
SCORE_THRESHOLD_LOG  = 6
SCORE_MAX            = 15

# Volume filter
VOLUME_LOOKBACK                  = 20
VOLUME_MULTIPLIER                = 1.2
VOLUME_CONFIRMATION_MULTIPLIER   = 1.0

# ATR filter
ATR_PERIOD             = 14
ATR_PERCENTILE_THRESHOLD = 20
ATR_HEALTHY_THRESHOLD  = 30
ATR_LOOKBACK           = 100
SL_ATR_MULTIPLIER      = 0.5

# ---------------------------------------------------------------------------
# Session rules (UTC hours)
# ---------------------------------------------------------------------------
QUIET_START_UTC = 21
QUIET_END_UTC   = 1     # crosses midnight: 21..23 + 0..0

# Session windows
SESSION_ASIAN   = (0, 7)
SESSION_LONDON  = (7, 12)
SESSION_OVERLAP = (12, 14)
SESSION_NY_OPEN = (12, 16)

# Per-session score minimums & position size multipliers
SESSION_ASIAN_SCORE_MIN  = 10
SESSION_ASIAN_SIZE       = 0.70
SESSION_ASIAN_SL_MULT    = 0.80
SESSION_LONDON_SCORE_MIN = 8
SESSION_NY_SCORE_MIN     = 8
SESSION_NY_SL_MULT       = 1.20

LONDON_OPEN = (7, 9)
NY_OPEN     = (12, 14)

# ---------------------------------------------------------------------------
# Correlation filter
# ---------------------------------------------------------------------------
CORRELATION_MODE         = os.getenv("CORRELATION_MODE", "strict")  # "strict"|"relaxed"
CORRELATION_RELAXED_SIZE = 0.50   # size multiplier when relaxed

# ---------------------------------------------------------------------------
# Trailing stop loss
# ---------------------------------------------------------------------------
BREAKEVEN_R             = 1.5
TRAILING_ACTIVATE_R     = 2.0
TRAILING_DISTANCE_R     = 0.5
TRAILING_TIGHTEN_R      = 3.0
TRAILING_TIGHT_DISTANCE_R = 0.3

# ---------------------------------------------------------------------------
# Partial take profits
# ---------------------------------------------------------------------------
PARTIAL_TP_ENABLED = True
# List of (R_multiple, fraction_to_close) in ascending R order
PARTIAL_TP_LEVELS  = [(2.0, 0.50), (3.0, 0.25)]   # remaining 25% trails

# ---------------------------------------------------------------------------
# News shield (Finnhub)
# ---------------------------------------------------------------------------
NEWS_SHIELD_ENABLED = True
NEWS_HALT_MINUTES   = 30

# ---------------------------------------------------------------------------
# Risk manager
# ---------------------------------------------------------------------------
DAILY_LOSS_CAP             = -0.03       # fraction of starting equity
DAILY_STARTING_EQUITY      = 10_000.0   # USDT / USD
MAX_CONSECUTIVE_LOSSES     = 5
CONSECUTIVE_HALT_MINUTES   = 1440       # 24h auto-resume
PAIR_LOSS_THRESHOLD        = 3
PAIR_LOSS_WINDOW_HOURS     = 24
PAIR_COOLDOWN_HOURS        = 48
PAIR_BLOCK_HOURS           = 24
PAIR_WEIGHT_REDUCED        = 0.5
MAX_OPEN_TRADES            = 3

# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------
ACCOUNT_EQUITY  = 10_000.0
RISK_PER_TRADE  = 0.01         # 1 % of equity per trade

# ---------------------------------------------------------------------------
# Broker / trading mode
# ---------------------------------------------------------------------------
TRADING_MODE       = os.getenv("TRADING_MODE", "paper")   # paper | demo | live
METAAPI_TOKEN      = os.getenv("METAAPI_TOKEN")
METAAPI_ACCOUNT_ID = os.getenv("METAAPI_ACCOUNT_ID")

# MetaAPI symbol mapping (bot → MT5)
METAAPI_SYMBOL_MAP = {
    "EUR/USD": "EURUSD", "GBP/USD": "GBPUSD", "USD/JPY": "USDJPY",
    "AUD/USD": "AUDUSD", "USD/CAD": "USDCAD", "EUR/GBP": "EURGBP",
}

# ---------------------------------------------------------------------------
# Loop timing
# ---------------------------------------------------------------------------
LOOP_SLEEP_SECONDS   = 60
HTF_REFRESH_MINUTES  = 60
HEARTBEAT_INTERVAL_HOURS = 4

# ---------------------------------------------------------------------------
# Weekly auto-restart (Alwaysdata RAM flush)
# ---------------------------------------------------------------------------
WEEKLY_RESTART_ENABLED = True
WEEKLY_RESTART_DAY     = 6   # Sunday (0=Mon … 6=Sun)

# ---------------------------------------------------------------------------
# Alwaysdata 120-day login reminder
# ---------------------------------------------------------------------------
ALWAYSDATA_WARN_DAYS = [110, 115, 120]

# ---------------------------------------------------------------------------
# Reliability
# ---------------------------------------------------------------------------
DATA_FETCH_RETRIES      = 3
DATA_FETCH_BACKOFF_BASE = 2.0
LOCK_STALE_SECONDS      = 120

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DATA_DIR      = os.path.join(os.path.dirname(__file__), "data")
LOG_FILE      = os.path.join(DATA_DIR, "bot.log")
TRADES_FILE   = os.path.join(DATA_DIR, "trades.json")
RISK_STATE_FILE = os.path.join(DATA_DIR, "risk_state.json")
LOCK_FILE     = os.path.join(DATA_DIR, "bot.lock")
CHART_DIR     = os.path.join(DATA_DIR, "charts")
BACKTEST_DIR  = DATA_DIR

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CHART_DIR, exist_ok=True)
