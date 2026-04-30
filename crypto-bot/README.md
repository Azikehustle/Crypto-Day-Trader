# Crypto Day Trading Bot

Free-tools-only signal bot implementing a strict price-action setup:
HTF bias → Supply/Demand zone → Liquidity sweep → MSS → Displacement →
Confirmation candle → Entry → Liquidity-target TP. High-confidence signals
(≥ 8/13) are sent to Telegram and paper-traded into a local journal.

## Layout

| File | Purpose |
| --- | --- |
| `config.py` | Strategy + runtime config (env-driven) |
| `data_fetcher.py` | CCXT OHLCV fetcher, EMA, candle helpers |
| `zone_detector.py` | Swing pivots, supply/demand zones, liquidity targets |
| `signal_engine.py` | Sweep, MSS, displacement, scoring, signal builder |
| `telegram_bot.py` | Telegram sender (Bot API via `requests`) |
| `paper_trader.py` | Paper trade journal + win/loss tracking |
| `main.py` | The 24/7 loop |
| `backtest.py` | Walk-forward backtester |

## Required env vars

- `TELEGRAM_BOT_TOKEN` — from BotFather
- `TELEGRAM_CHAT_ID` — chat / group id
- `EXCHANGE` *(optional, default `binance`)*
- `SYMBOLS` *(optional CSV, default `BTC/USDT,ETH/USDT,SOL/USDT`)*

## Run

```bash
python crypto-bot/main.py
```

## Backtest

```bash
python crypto-bot/backtest.py BTC/USDT 2024-01-01 2024-04-01
```
