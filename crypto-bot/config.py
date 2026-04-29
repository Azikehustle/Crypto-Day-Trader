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

# Score thresholds. Max possible score is now 15 (was 13) after adding
# Volume Confirmed (+1) and ATR Healthy (+1).
SCORE_THRESHOLD_SEND = 8
SCORE_THRESHOLD_LOG = 6
SCORE_MAX = 15

LONDON_OPEN = (7, 9)
NY_OPEN = (12, 14)

LOOP_SLEEP_SECONDS = 60
HTF_REFRESH_MINUTES = 60

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ---------------------------------------------------------------------------
# Volume confirmation filter
# ---------------------------------------------------------------------------
VOLUME_LOOKBACK = 20
VOLUME_MULTIPLIER = 1.2                # displacement candle vol multiplier
VOLUME_CONFIRMATION_MULTIPLIER = 1.0   # confirmation candle vol multiplier

# ---------------------------------------------------------------------------
# ATR-based volatility filter + dynamic stop loss
# ---------------------------------------------------------------------------
ATR_PERIOD = 14
ATR_PERCENTILE_THRESHOLD = 20          # reject below this percentile
ATR_HEALTHY_THRESHOLD = 30             # bonus score above this percentile
ATR_LOOKBACK = 100
SL_ATR_MULTIPLIER = 0.5                # SL buffer = SL_ATR_MULTIPLIER * ATR

# ---------------------------------------------------------------------------
# Risk manager
# ---------------------------------------------------------------------------
DAILY_LOSS_CAP = -0.03                 # fraction of starting equity
DAILY_STARTING_EQUITY = 10000.0        # USDT
MAX_CONSECUTIVE_LOSSES = 5
CONSECUTIVE_HALT_MINUTES = 1440        # 24 h auto-resume window
PAIR_LOSS_THRESHOLD = 3
PAIR_LOSS_WINDOW_HOURS = 24
PAIR_COOLDOWN_HOURS = 48               # half-weight window
PAIR_BLOCK_HOURS = 24                  # full block window
PAIR_WEIGHT_REDUCED = 0.5
MAX_OPEN_TRADES = 3
QUIET_START_UTC = 21
QUIET_END_UTC = 1                      # crosses midnight: 21..23 + 0..0

# ---------------------------------------------------------------------------
# Heartbeat + crash detection
# ---------------------------------------------------------------------------
HEARTBEAT_INTERVAL_HOURS = 4

# ---------------------------------------------------------------------------
# Position sizing for paper trades
# ---------------------------------------------------------------------------
ACCOUNT_EQUITY = 10000.0                # starting equity in USDT
RISK_PER_TRADE = 0.01                   # 1% of equity per trade

# ---------------------------------------------------------------------------
# Reliability
# ---------------------------------------------------------------------------
DATA_FETCH_RETRIES = 3                  # per exchange
DATA_FETCH_BACKOFF_BASE = 2.0           # seconds: 2, 4, 8
LOCK_STALE_SECONDS = 120                # bot.lock considered stale after 2 min

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
LOG_FILE = os.path.join(DATA_DIR, "bot.log")
TRADES_FILE = os.path.join(DATA_DIR, "trades.json")
RISK_STATE_FILE = os.path.join(DATA_DIR, "risk_state.json")
LOCK_FILE = os.path.join(DATA_DIR, "bot.lock")
CHART_DIR = os.path.join(DATA_DIR, "charts")
BACKTEST_DIR = DATA_DIR

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CHART_DIR, exist_ok=True)
