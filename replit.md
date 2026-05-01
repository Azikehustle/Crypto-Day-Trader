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

## Oracle_v5 (`crypto-bot/`)

Forex/crypto day-trading signal bot. Pipeline: HTF bias → S/D zone →
liquidity sweep → MSS → displacement → confirmation → entry → TP.
Sends Telegram signals, paper-trades via Supabase, and optionally live-trades
through MetaAPI (MT5).

- **Workflow**: `Oracle_v5` (`cd crypto-bot && python main.py`)
- **Symbols** (env `SYMBOLS`): `EUR/USD,GBP/USD,USD/JPY,AUD/USD,USD/CAD,EUR/GBP`
- **Data** (env `EXCHANGE`): Twelvedata → FCS → iTick triple-fallback for forex
- **Trading mode** (env `TRADING_MODE`): `paper` (default) or `live` (MetaAPI)
- **Required secrets**: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `SUPABASE_URL`,
  `SUPABASE_SERVICE_ROLE_KEY`, `TWELVEDATA_API_KEY`, `FINNHUB_API_KEY`
- **Optional secrets**: `METAAPI_TOKEN`, `METAAPI_ACCOUNT_ID` (live trading)

### Core modules
`config`, `logger_setup`, `data_fetcher`, `zone_detector`, `signal_engine`,
`telegram_bot`, `command_handler`, `paper_trader`, `supabase_client`,
`risk_manager`, `main`, `backtest`

### Phase 3-4 modules
- `trailing_stop.py` — R-multiple trailing stop engine
- `timeframe_manager.py` — SCALP / DAY / SWING mode selector
- `broker.py` — AbstractBroker → PaperBroker / MetaApiBroker
- `metaapi_client.py` — MetaAPI Cloud SDK wrapper (live MT5 orders + WS sync)
- `news_shield.py` — Finnhub economic calendar halt filter
