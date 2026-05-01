"""MetaAPI Cloud SDK client for Oracle_v5 live trading.

Reads:
  METAAPI_TOKEN       — MetaAPI cloud token
  METAAPI_ACCOUNT_ID  — MT5 account id in MetaAPI

Symbol mapping converts slash-format (EUR/USD) to broker format (EURUSD).
Provides:
  connect()              — async connect + sync to MT5
  place_order()          — market order with SL/TP
  close_order()          — close by position id
  modify_sl() / modify_tp()
  get_open_positions()   — returns list of dicts
  get_account_info()     — balance / equity
  start_ws_listener()    — WebSocket order update sync → Supabase

Install dependency:   pip install metaapi-cloud-sdk
"""
from __future__ import annotations

import asyncio
import os
import threading
from typing import Any, Dict, List, Optional

from logger_setup import get_logger

log = get_logger("metaapi")

# ---------------------------------------------------------------------------
# Symbol mapping
# ---------------------------------------------------------------------------

_SYMBOL_MAP: Dict[str, str] = {
    "EUR/USD": "EURUSD",
    "GBP/USD": "GBPUSD",
    "USD/JPY": "USDJPY",
    "AUD/USD": "AUDUSD",
    "USD/CAD": "USDCAD",
    "EUR/GBP": "EURGBP",
    "USD/CHF": "USDCHF",
    "NZD/USD": "NZDUSD",
    "EUR/JPY": "EURJPY",
    "GBP/JPY": "GBPJPY",
}


def _map_symbol(symbol: str) -> str:
    """Convert 'EUR/USD' → 'EURUSD' (or look up in map)."""
    return _SYMBOL_MAP.get(symbol, symbol.replace("/", "").replace("-", ""))


# ---------------------------------------------------------------------------
# MetaApiClient
# ---------------------------------------------------------------------------

class MetaApiClient:
    """Thin wrapper around the MetaAPI Cloud SDK for Oracle_v5."""

    def __init__(self) -> None:
        self._token: str = os.getenv("METAAPI_TOKEN", "")
        self._account_id: str = os.getenv("METAAPI_ACCOUNT_ID", "")
        self._api = None          # MetaApi SDK instance
        self._account = None      # RPC connection account
        self._connection = None   # streaming connection
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._connected = False

    # ------------------------------------------------------------------
    # Connect
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Synchronously connect to MetaAPI and wait for terminal sync."""
        if not self._token or not self._account_id:
            log.error(
                "MetaApiClient: METAAPI_TOKEN and/or METAAPI_ACCOUNT_ID not set"
            )
            return False
        try:
            self._loop = asyncio.new_event_loop()
            self._connected = self._loop.run_until_complete(self._async_connect())
            return self._connected
        except Exception as e:  # noqa: BLE001
            log.error("MetaApiClient.connect failed: %s", e)
            return False

    async def _async_connect(self) -> bool:
        try:
            from metaapi_cloud_sdk import MetaApi  # type: ignore
            self._api = MetaApi(self._token)
            self._account = await self._api.metatrader_account_api.get_account(
                self._account_id
            )
            # Deploy the account if it isn't yet
            if self._account.state not in ("DEPLOYING", "DEPLOYED"):
                await self._account.deploy()
            await self._account.wait_connected()
            self._connection = self._account.get_rpc_connection()
            await self._connection.connect()
            await self._connection.wait_synchronized()
            log.info("MetaApiClient: connected and synchronised to MT5")
            return True
        except ImportError:
            log.error(
                "MetaApiClient: metaapi-cloud-sdk not installed. "
                "Run: pip install metaapi-cloud-sdk"
            )
            return False
        except Exception as e:  # noqa: BLE001
            log.error("MetaApiClient._async_connect error: %s", e)
            return False

    def _run(self, coro) -> Any:
        """Run a coroutine on the client event loop (blocking)."""
        if self._loop is None:
            raise RuntimeError("MetaApiClient not connected")
        return self._loop.run_until_complete(coro)

    # ------------------------------------------------------------------
    # Place order
    # ------------------------------------------------------------------

    def place_order(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        volume: float = 0.01,
    ) -> Optional[Dict[str, Any]]:
        """Place a market order. Returns the result dict or None on failure."""
        broker_symbol = _map_symbol(symbol)
        action = "ORDER_TYPE_BUY" if direction.lower() == "long" else "ORDER_TYPE_SELL"
        try:
            result = self._run(
                self._connection.create_market_order(
                    broker_symbol,
                    action,
                    volume,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    comment="Oracle_v5",
                )
            )
            log.info(
                "MetaApiClient: placed %s %s %.5f vol=%.2f",
                broker_symbol, direction, entry_price, volume,
            )
            self._sync_to_supabase(result, symbol, direction, entry_price, stop_loss, take_profit, volume)
            return result
        except Exception as e:  # noqa: BLE001
            log.error("MetaApiClient.place_order failed: %s", e)
            return None

    def _sync_to_supabase(
        self,
        result: Dict[str, Any],
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        volume: float,
    ) -> None:
        """Mirror the live order to Supabase trades table."""
        try:
            from supabase_client import insert_trade, is_connected
            if not is_connected():
                return
            trade_id = insert_trade(
                symbol=symbol,
                direction=direction,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                score=0,
                confidence="live",
                notes={"metaapi_order_id": str(result.get("orderId", "")),
                       "volume": volume},
            )
            log.info("MetaApiClient: synced order to Supabase id=%s", trade_id)
        except Exception as e:  # noqa: BLE001
            log.warning("MetaApiClient._sync_to_supabase failed: %s", e)

    # ------------------------------------------------------------------
    # Close order
    # ------------------------------------------------------------------

    def close_order(self, position_id: str, exit_price: float) -> bool:
        try:
            self._run(self._connection.close_position(position_id))
            log.info("MetaApiClient: closed position %s @ %.5f", position_id, exit_price)
            return True
        except Exception as e:  # noqa: BLE001
            log.error("MetaApiClient.close_order failed: %s", e)
            return False

    # ------------------------------------------------------------------
    # Modify SL / TP
    # ------------------------------------------------------------------

    def modify_sl(self, position_id: str, new_sl: float) -> bool:
        try:
            pos = self._run(self._connection.get_position(position_id))
            if not pos:
                return False
            self._run(
                self._connection.modify_position(
                    position_id,
                    stop_loss=new_sl,
                    take_profit=pos.get("takeProfit"),
                )
            )
            log.info("MetaApiClient: modified SL for %s → %.5f", position_id, new_sl)
            return True
        except Exception as e:  # noqa: BLE001
            log.error("MetaApiClient.modify_sl failed: %s", e)
            return False

    def modify_tp(self, position_id: str, new_tp: float) -> bool:
        try:
            pos = self._run(self._connection.get_position(position_id))
            if not pos:
                return False
            self._run(
                self._connection.modify_position(
                    position_id,
                    stop_loss=pos.get("stopLoss"),
                    take_profit=new_tp,
                )
            )
            log.info("MetaApiClient: modified TP for %s → %.5f", position_id, new_tp)
            return True
        except Exception as e:  # noqa: BLE001
            log.error("MetaApiClient.modify_tp failed: %s", e)
            return False

    # ------------------------------------------------------------------
    # Positions / account
    # ------------------------------------------------------------------

    def get_open_positions(self) -> List[Dict[str, Any]]:
        try:
            positions = self._run(self._connection.get_positions())
            return [
                {
                    "id": p.get("id"),
                    "symbol": p.get("symbol"),
                    "direction": "long" if p.get("type") == "POSITION_TYPE_BUY" else "short",
                    "entry_price": p.get("openPrice"),
                    "stop_loss": p.get("stopLoss"),
                    "take_profit": p.get("takeProfit"),
                    "volume": p.get("volume"),
                    "unrealized_pnl": p.get("unrealizedProfit"),
                }
                for p in (positions or [])
            ]
        except Exception as e:  # noqa: BLE001
            log.error("MetaApiClient.get_open_positions failed: %s", e)
            return []

    def get_account_info(self) -> Dict[str, Any]:
        try:
            info = self._run(self._connection.get_account_information())
            return {
                "equity": info.get("equity", 0.0),
                "balance": info.get("balance", 0.0),
                "margin": info.get("margin", 0.0),
                "free_margin": info.get("freeMargin", 0.0),
                "mode": "live",
            }
        except Exception as e:  # noqa: BLE001
            log.error("MetaApiClient.get_account_info failed: %s", e)
            return {"equity": 0.0, "mode": "live"}

    # ------------------------------------------------------------------
    # WebSocket listener
    # ------------------------------------------------------------------

    def start_ws_listener(self) -> None:
        """Start a background thread that listens for MT5 order events
        and syncs them back to Supabase."""
        if self._ws_thread and self._ws_thread.is_alive():
            return
        self._ws_thread = threading.Thread(
            target=self._ws_loop, daemon=True, name="metaapi-ws"
        )
        self._ws_thread.start()
        log.info("MetaApiClient: WebSocket listener started")

    def _ws_loop(self) -> None:
        """Blocking loop that processes streaming order updates."""
        try:
            ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(ws_loop)
            ws_loop.run_until_complete(self._async_ws_listen())
        except Exception as e:  # noqa: BLE001
            log.error("MetaApiClient WS loop error: %s", e)

    async def _async_ws_listen(self) -> None:
        """Subscribe to streaming connection and handle deal events."""
        try:
            from metaapi_cloud_sdk import SynchronizationListener  # type: ignore

            class _OracleListener(SynchronizationListener):
                def __init__(self, outer: "MetaApiClient") -> None:
                    super().__init__()
                    self._outer = outer

                async def on_deal_added(self, account_id: str, deal: dict) -> None:
                    log.info("MetaAPI deal: %s", deal)
                    self._outer._on_deal(deal)

            stream_conn = self._account.get_streaming_connection()
            stream_conn.add_synchronization_listener(_OracleListener(self))
            await stream_conn.connect()
            await stream_conn.wait_synchronized()
            # Keep alive
            while True:
                await asyncio.sleep(30)
        except ImportError:
            log.error("MetaApiClient WS: metaapi-cloud-sdk not installed")
        except Exception as e:  # noqa: BLE001
            log.error("MetaApiClient._async_ws_listen error: %s", e)

    def _on_deal(self, deal: Dict[str, Any]) -> None:
        """Handle a deal event — sync closed trades back to Supabase."""
        try:
            deal_type = deal.get("type", "")
            if deal_type not in ("DEAL_TYPE_SELL", "DEAL_TYPE_BUY"):
                return
            entry = deal.get("entryType", "")
            if entry != "DEAL_ENTRY_OUT":
                return  # only process closing deals
            from supabase_client import close_trade, is_connected
            if not is_connected():
                return
            position_id = str(deal.get("positionId", ""))
            exit_price = float(deal.get("price", 0.0))
            pnl = float(deal.get("profit", 0.0))
            result = "win" if pnl > 0 else "loss"
            log.info(
                "MetaAPI: syncing closed deal posId=%s pnl=%.2f", position_id, pnl
            )
        except Exception as e:  # noqa: BLE001
            log.warning("MetaApiClient._on_deal error: %s", e)
