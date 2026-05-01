"""MetaAPI MT5 client stub.

Provides the public API used by MetaApiBroker. All functions are no-ops
until the user supplies METAAPI_TOKEN + METAAPI_ACCOUNT_ID and installs
the metaapi-cloud-sdk package.

Once keys are available, replace stub bodies with real MetaAPI calls.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from config import METAAPI_TOKEN, METAAPI_ACCOUNT_ID, METAAPI_SYMBOL_MAP
from logger_setup import get_logger

log = get_logger("metaapi")

_READY = bool(METAAPI_TOKEN and METAAPI_ACCOUNT_ID)


def _mt5_symbol(bot_symbol: str) -> str:
    return METAAPI_SYMBOL_MAP.get(bot_symbol, bot_symbol.replace("/", ""))


def place_trade(signal: Dict[str, Any], weight: float = 1.0) -> bool:
    """Open a market order on MT5 via MetaAPI."""
    if not _READY:
        log.warning("MetaAPI keys not configured — place_trade is a no-op")
        return False
    try:
        log.info(
            "MetaAPI place_trade: %s %s entry=%.5f sl=%.5f tp=%.5f",
            signal.get("symbol"), signal.get("direction"),
            signal.get("entry", 0), signal.get("stop_loss", 0),
            signal.get("take_profit", 0),
        )
        # TODO: implement with metaapi_cloud_sdk once keys are available
        # from metaapi_cloud_sdk import MetaApi
        # api = MetaApi(METAAPI_TOKEN)
        # account = await api.metatrader_account_api.get_account(METAAPI_ACCOUNT_ID)
        # connection = account.get_rpc_connection()
        # await connection.connect()
        # await connection.wait_synchronized()
        # mt_sym = _mt5_symbol(signal["symbol"])
        # if signal["direction"] == "long":
        #     await connection.create_market_buy_order(mt_sym, volume, signal["stop_loss"], signal["take_profit"])
        # else:
        #     await connection.create_market_sell_order(mt_sym, volume, signal["stop_loss"], signal["take_profit"])
        return False  # stub
    except Exception as e:  # noqa: BLE001
        log.error("MetaAPI place_trade error: %s", e)
        return False


def close_trade(trade_id: str, reason: str = "manual") -> bool:
    if not _READY:
        log.warning("MetaAPI keys not configured — close_trade is a no-op")
        return False
    log.info("MetaAPI close_trade stub: %s (%s)", trade_id, reason)
    return False


def get_positions(symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    if not _READY:
        return []
    log.debug("MetaAPI get_positions stub")
    return []


def modify_sl(trade_id: str, new_sl: float) -> bool:
    if not _READY:
        return False
    log.info("MetaAPI modify_sl stub: %s → %.5f", trade_id, new_sl)
    return False
