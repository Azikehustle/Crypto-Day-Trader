#!/usr/bin/env bash
# ---------------------------------------------------------------
# Alwaysdata one-shot deployment for the crypto bot.
#
# Run this on YOUR LOCAL MACHINE (or paste it into Replit shell and
# follow the SSH prompts) — it cannot be executed for you because
# the SSH password must be typed manually.
#
# After SSH connects you to ssh-cryptobot.alwaysdata.net:
#   1. paste the BLOCK marked "RUN ON ALWAYSDATA" below
#   2. when it finishes, the bot will be running in a `screen` session
#      named `bot`. Detach with Ctrl-A then D.
# ---------------------------------------------------------------

# Step 1 — open the SSH connection (run on your laptop):
#   ssh cryptobot@ssh-cryptobot.alwaysdata.net
# (type 'yes' on the fingerprint prompt, then enter your password)

# ===============================================================
# RUN ON ALWAYSDATA — copy from here ↓
# ===============================================================
set -e

REPO_URL="https://github.com/Azikehustle/Crypto-Day-Trader.git"
REPO_DIR="Crypto-Day-Trader"

echo "🐍 Python:"; python3 --version
echo "💾 Disk:";   df -h | head -2

# Clone or update
cd ~
if [ -d "$REPO_DIR" ]; then
    echo "📥 Updating existing checkout..."
    cd "$REPO_DIR"
    git pull
else
    echo "📥 Cloning repo..."
    git clone "$REPO_URL"
    cd "$REPO_DIR"
fi

# Install Python deps (Alwaysdata pip3 uses --user automatically)
echo "📦 Installing dependencies..."
pip3 install --user -r requirements.txt

# Build .env if missing — EDIT the placeholders before running this block!
if [ ! -f .env ]; then
    cat > .env <<'EOF'
TELEGRAM_BOT_TOKEN=PASTE_FROM_REPLIT
TELEGRAM_CHAT_ID=PASTE_FROM_REPLIT
SUPABASE_URL=PASTE_FROM_REPLIT
SUPABASE_SERVICE_ROLE_KEY=PASTE_FROM_REPLIT
EXCHANGE=kucoin
TRADING_PAIRS=BTC/USDT,ETH/USDT,SOL/USDT
EOF
    echo "❗ .env created with placeholders. Edit it now:"
    echo "    nano ~/$REPO_DIR/.env"
    echo "Then re-run this script."
    exit 1
fi

mkdir -p crypto-bot/data/charts

# Smoke test — start the bot, wait 8s, kill it, show last 30 log lines
echo "🧪 Smoke test (8s)..."
( python3 crypto-bot/main.py & echo $! > /tmp/bot.pid ) >/tmp/bot.smoke 2>&1
sleep 8
kill "$(cat /tmp/bot.pid)" 2>/dev/null || true
echo "------- smoke test output -------"
tail -30 crypto-bot/data/bot.log 2>/dev/null || tail -30 /tmp/bot.smoke
echo "---------------------------------"

# Launch in detached screen
if screen -list | grep -q "\.bot"; then
    echo "🛑 Existing 'bot' screen session — re-attach with: screen -r bot"
else
    echo "🚀 Starting bot in detached screen (name: bot)..."
    screen -dmS bot python3 crypto-bot/main.py
fi

echo
echo "✅ Done. Useful commands:"
echo "   screen -r bot         # attach to live bot output"
echo "   Ctrl-A then D         # detach without killing the bot"
echo "   tail -f ~/${REPO_DIR}/crypto-bot/data/bot.log   # tail logs"
echo "   cd ~/${REPO_DIR} && git pull && screen -r bot   # update & restart"
# ===============================================================
# RUN ON ALWAYSDATA — copy until here ↑
# ===============================================================
