# Workspace

## Overview

pnpm workspace monorepo using TypeScript. Each package manages its own dependencies.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)

## Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- `pnpm --filter @workspace/api-server run dev` — run API server locally

See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details.

## Crypto Trading Bot (`crypto-bot/`)

Standalone Python signal bot (HTF bias → S/D zone → liquidity sweep → MSS →
displacement → confirmation → entry → liquidity-target TP). Sends scored
signals to Telegram and paper-trades them in `crypto-bot/data/trades.json`.

- Run: `python crypto-bot/main.py` (managed by the `Crypto Bot` workflow)
- Backtest: `python crypto-bot/backtest.py BTC/USDT 2024-01-01 2024-04-01`
- Default exchange: KuCoin (Binance/Bybit are geo-blocked from this region).
  Override with `EXCHANGE` env var; fallbacks via `EXCHANGE_FALLBACKS`.
- Default symbols: `BTC/USDT, ETH/USDT, SOL/USDT` (override via `SYMBOLS`).
- Required secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
- Modules: `config`, `logger_setup`, `data_fetcher`, `zone_detector`,
  `signal_engine`, `telegram_bot`, `paper_trader`, `main`, `backtest`,
  `command_handler`, `runtime_settings`, `risk_manager`, `github_sync`.

## Telegram UI (BotFather-style)
- `telegram_bot.set_my_commands()` registers all 15 commands with the
  BotFather native menu on every startup.
- `command_handler.py` renders inline keyboards instead of plain text. Each
  button taps a `callback_data` like `nav:status`, `cfg:max_trades:+`, or
  `confirm:stop`. `editMessageText` updates the message in place.
- Dangerous actions (Stop, Restart, Remove Pair, Clear Halts) always go
  through a confirmation screen.
- `runtime_settings.py` holds live-tunable overrides for `MAX_OPEN_TRADES`,
  `DAILY_LOSS_CAP`, `RISK_PER_TRADE`, and the `SYMBOLS` list. Overrides are
  persisted to Supabase `bot_state` under key `runtime_settings`. The main
  loop reads `runtime_settings.get_symbols()` so pair changes take effect on
  the next iteration. Stop/restart flags are honored at the top of the loop.

## Two-instance topology
- Replit dev instance has `TELEGRAM_LISTEN=0` — sends signal alerts but does
  not poll for `/commands`.
- Alwaysdata production (`screen -S bot`) has `TELEGRAM_LISTEN=1` and is the
  sole responder to user commands. Avoids 409 conflicts on `getUpdates`.
- Deploy to Alwaysdata: SCP the changed files into
  `/home/cryptobot/Crypto-Day-Trader/crypto-bot/` (the remote copy is **not**
  a git repo), then `screen -S bot -X quit && screen -dmS bot bash -lc "cd
  ~/Crypto-Day-Trader/crypto-bot && set -a && source ~/Crypto-Day-Trader/.env
  && set +a && python3 main.py 2>&1 | tee -a data/screen.log"`.
