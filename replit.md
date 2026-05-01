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
  `signal_engine`, `telegram_bot`, `paper_trader`, `main`, `backtest`.
