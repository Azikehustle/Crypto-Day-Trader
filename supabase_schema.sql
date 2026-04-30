-- Crypto bot — Supabase schema
-- Run this once in: Supabase Dashboard → SQL Editor → New query → paste → Run

-- ------------------------------------------------------------------
-- ZONES (supply / demand zones detected by zone_detector)
-- ------------------------------------------------------------------
create table if not exists zones (
    id            bigserial primary key,
    symbol        text not null,
    zone_type     text not null check (zone_type in ('supply', 'demand')),
    price_top     double precision not null,
    price_bottom  double precision not null,
    origin_ts     timestamptz,
    status        text not null default 'active'
                       check (status in ('active', 'swept', 'invalidated')),
    created_at    timestamptz not null default now(),
    swept_at      timestamptz,
    unique (symbol, zone_type, origin_ts)
);
create index if not exists zones_symbol_status_idx on zones (symbol, status);
create index if not exists zones_created_at_idx    on zones (created_at);

-- ------------------------------------------------------------------
-- TRADES (paper-trading journal; every entry + exit lives here)
-- ------------------------------------------------------------------
create table if not exists trades (
    id                  bigserial primary key,
    symbol              text not null,
    direction           text not null check (direction in ('long', 'short')),
    entry_price         double precision not null,
    stop_loss           double precision not null,
    take_profit         double precision not null,
    score               integer,
    confidence          text,
    pair_zone_id        bigint references zones(id) on delete set null,
    notes               jsonb,
    status              text not null default 'open'
                             check (status in ('open', 'closed')),
    exit_price          double precision,
    pnl                 double precision,        -- USDT realised P&L
    pnl_pct             double precision,
    position_size       double precision,        -- base units (e.g. BTC)
    pair_weight         double precision,
    notional_at_entry   double precision,
    risked_usd          double precision,
    result              text,                    -- 'win' | 'loss' | null
    opened_at           timestamptz not null default now(),
    closed_at           timestamptz
);
create index if not exists trades_status_idx     on trades (status);
create index if not exists trades_symbol_idx     on trades (symbol);
create index if not exists trades_opened_at_idx  on trades (opened_at);
create index if not exists trades_closed_at_idx  on trades (closed_at);

-- ------------------------------------------------------------------
-- BOT_STATE  (key/value JSONB store: risk state, counters, heartbeat)
-- ------------------------------------------------------------------
create table if not exists bot_state (
    key         text primary key,
    value       jsonb,
    updated_at  timestamptz not null default now()
);

-- Trigger: bump updated_at on every change
create or replace function bot_state_touch_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists bot_state_touch on bot_state;
create trigger bot_state_touch
before update on bot_state
for each row execute function bot_state_touch_updated_at();
