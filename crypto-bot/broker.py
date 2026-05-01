"""Broker abstraction layer for Oracle_v5.

AbstractBroker defines the contract.  Two concrete implementations:

  PaperBroker     — current paper-trading behaviour: logs to Supabase.
  MetaApiBroker   — placeholder for live MetaAPI / MT5 trading.

The active broker is selected by the TRADING_MODE env var:
  TRADING_MODE=paper   → PaperBroker   (default)
  TRADING_MODE=live    → MetaApiBroker
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from logger_setup import get_logger

log = get_logger("broker")


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class AbstractBroker(ABC):
    """Common interface that all broker implementations must satisfy."""

    @abstractmethod
    def connect(self) -> bool:
        """Establish the broker connection. Returns True on success."""

    @abstractmethod
    def place_order(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        position_size: float,
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        """Submit a new order. Returns an order-info dict or None on failure."""

    @abstractmethod
    def close_order(
        self,
        order_id: Any,
        exit_price: float,
        reason: str = "manual",
    ) -> bool:
        """Close an open position. Returns True on success."""

    @abstractmethod
    def modify_sl(self, order_id: Any, new_sl: float) -> bool:
        """Move stop-loss to new_sl. Returns True on success."""

    @abstractmethod
    def modify_tp(self, order_id: Any, new_tp: float) -> bool:
        """Move take-profit to new_tp. Returns True on success."""

    @abstractmethod
    def get_open_positions(self) -> List[Dict[str, Any]]:
        """Return a list of all open position dicts."""

    @abstractmethod
    def get_account_info(self) -> Dict[str, Any]:
        """Return account balance / equity information."""


# ---------------------------------------------------------------------------
# PaperBroker
# ---------------------------------------------------------------------------

class PaperBroker(AbstractBroker):
    """Simulated broker that mirrors calls to the Supabase paper-trade tables.

    All execution is virtual — prices are taken from market data on hit, not
    executed with any real exchange.
    """

    def connect(self) -> bool:
        try:
            from supabase_client import is_connected, ping
            ok = is_connected() and ping()
            if ok:
                log.info("PaperBroker: connected to Supabase")
            else:
                log.warning("PaperBroker: Supabase unavailable — using local JSON fallback")
            return True  # paper broker always 'works'
        except Exception as e:  # noqa: BLE001
            log.error("PaperBroker.connect error: %s", e)
            return True

    def place_order(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        position_size: float,
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        try:
            from paper_trader import open_trade
            signal_dict = {
                "symbol": symbol,
                "direction": direction,
                "entry": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "score": kwargs.get("score", 0),
                "score_max": kwargs.get("score_max", 15),
                "confidence": kwargs.get("confidence", "medium"),
                "timeframe": kwargs.get("timeframe", "15m"),
                "htf_bias": kwargs.get("htf_bias", "flat"),
                "rr": kwargs.get("rr", 2.0),
                "tp_reason": kwargs.get("tp_reason", "target"),
                "timestamp": kwargs.get("timestamp", ""),
            }
            opened = open_trade(
                signal_dict,
                pair_weight=kwargs.get("pair_weight", 1.0),
                pair_zone_id=kwargs.get("pair_zone_id"),
            )
            if opened:
                log.info("PaperBroker: opened %s %s @ %.5f", symbol, direction, entry_price)
                return {"symbol": symbol, "direction": direction, "entry_price": entry_price,
                        "stop_loss": stop_loss, "take_profit": take_profit,
                        "position_size": position_size}
            return None
        except Exception as e:  # noqa: BLE001
            log.error("PaperBroker.place_order failed: %s", e)
            return None

    def close_order(self, order_id: Any, exit_price: float, reason: str = "manual") -> bool:
        try:
            from supabase_client import close_trade, is_connected
            if not is_connected():
                log.warning("PaperBroker.close_order: Supabase unavailable")
                return False
            ok = close_trade(
                trade_id=int(order_id),
                status="closed",
                exit_price=exit_price,
                pnl=0.0,
                pnl_pct=0.0,
                result=reason,
            )
            if ok:
                log.info("PaperBroker: closed trade %s @ %.5f (%s)", order_id, exit_price, reason)
            return ok
        except Exception as e:  # noqa: BLE001
            log.error("PaperBroker.close_order failed: %s", e)
            return False

    def modify_sl(self, order_id: Any, new_sl: float) -> bool:
        try:
            from supabase_client import is_connected, supabase as _supa
            if not is_connected() or _supa is None:
                return False
            _supa.table("trades").update({"stop_loss": new_sl}).eq("id", int(order_id)).execute()
            log.info("PaperBroker: modified SL for trade %s → %.5f", order_id, new_sl)
            return True
        except Exception as e:  # noqa: BLE001
            log.error("PaperBroker.modify_sl failed: %s", e)
            return False

    def modify_tp(self, order_id: Any, new_tp: float) -> bool:
        try:
            from supabase_client import is_connected, supabase as _supa
            if not is_connected() or _supa is None:
                return False
            _supa.table("trades").update({"take_profit": new_tp}).eq("id", int(order_id)).execute()
            log.info("PaperBroker: modified TP for trade %s → %.5f", order_id, new_tp)
            return True
        except Exception as e:  # noqa: BLE001
            log.error("PaperBroker.modify_tp failed: %s", e)
            return False

    def get_open_positions(self) -> List[Dict[str, Any]]:
        try:
            from supabase_client import get_open_trades
            return get_open_trades()
        except Exception as e:  # noqa: BLE001
            log.error("PaperBroker.get_open_positions failed: %s", e)
            return []

    def get_account_info(self) -> Dict[str, Any]:
        try:
            from risk_manager import get_risk_manager
            rs = get_risk_manager().status_dict()
            return {
                "equity": rs.get("running_equity", 0.0),
                "daily_pnl_usd": rs.get("daily_pnl_usd", 0.0),
                "lifetime_pnl_usd": rs.get("lifetime_realised_pnl_usd", 0.0),
                "mode": "paper",
            }
        except Exception as e:  # noqa: BLE001
            log.error("PaperBroker.get_account_info failed: %s", e)
            return {"equity": 0.0, "mode": "paper"}


# ---------------------------------------------------------------------------
# MetaApiBroker (placeholder)
# ---------------------------------------------------------------------------

class MetaApiBroker(AbstractBroker):
    """Live broker implementation using MetaAPI (MT5).

    Delegates to metaapi_client.py which handles the MetaAPI SDK,
    WebSocket connection, and Supabase sync.
    """

    def __init__(self) -> None:
        self._client = None

    def connect(self) -> bool:
        try:
            from metaapi_client import MetaApiClient
            self._client = MetaApiClient()
            return self._client.connect()
        except ImportError:
            log.error("MetaApiBroker: metaapi_client module not available")
            return False
        except Exception as e:  # noqa: BLE001
            log.error("MetaApiBroker.connect failed: %s", e)
            return False

    def place_order(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        position_size: float,
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        if not self._client:
            log.error("MetaApiBroker: not connected")
            return None
        return self._client.place_order(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            volume=position_size,
        )

    def close_order(self, order_id: Any, exit_price: float, reason: str = "manual") -> bool:
        if not self._client:
            return False
        return self._client.close_order(str(order_id), exit_price)

    def modify_sl(self, order_id: Any, new_sl: float) -> bool:
        if not self._client:
            return False
        return self._client.modify_sl(str(order_id), new_sl)

    def modify_tp(self, order_id: Any, new_tp: float) -> bool:
        if not self._client:
            return False
        return self._client.modify_tp(str(order_id), new_tp)

    def get_open_positions(self) -> List[Dict[str, Any]]:
        if not self._client:
            return []
        return self._client.get_open_positions()

    def get_account_info(self) -> Dict[str, Any]:
        if not self._client:
            return {"equity": 0.0, "mode": "live"}
        return self._client.get_account_info()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_broker_instance: Optional[AbstractBroker] = None
_broker_lock = threading.Lock() if False else __import__("threading").Lock()


def get_broker() -> AbstractBroker:
    """Return the singleton broker instance, creating it if needed."""
    global _broker_instance
    with _broker_lock:
        if _broker_instance is None:
            mode = os.getenv("TRADING_MODE", "paper").strip().lower()
            if mode == "live":
                log.info("Broker mode: LIVE (MetaAPI)")
                _broker_instance = MetaApiBroker()
            else:
                log.info("Broker mode: PAPER")
                _broker_instance = PaperBroker()
            _broker_instance.connect()
    return _broker_instance
