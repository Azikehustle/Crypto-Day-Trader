"""Broker abstraction layer.

AbstractBroker defines the interface.
PaperBroker wraps paper_trader (no network trades).
MetaApiBroker stubs MetaAPI live/demo MT5 trading.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from config import TRADING_MODE
from logger_setup import get_logger

log = get_logger("broker")


class AbstractBroker(ABC):
    """Common interface all brokers must implement."""

    @abstractmethod
    def open_position(self, signal: Dict[str, Any], weight: float = 1.0) -> bool: ...

    @abstractmethod
    def close_position(self, trade_id: str, reason: str = "manual") -> bool: ...

    @abstractmethod
    def get_open_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]: ...

    @abstractmethod
    def update_sl(self, trade_id: str, new_sl: float) -> bool: ...

    @property
    @abstractmethod
    def name(self) -> str: ...


# ---------------------------------------------------------------------------
# Paper broker
# ---------------------------------------------------------------------------

class PaperBroker(AbstractBroker):
    """Wraps paper_trader — all logic stays local / Supabase."""

    @property
    def name(self) -> str:
        return "paper"

    def open_position(self, signal: Dict[str, Any], weight: float = 1.0) -> bool:
        from paper_trader import open_trade  # noqa: WPS433
        return open_trade(signal, pair_weight=weight)

    def close_position(self, trade_id: str, reason: str = "manual") -> bool:
        from supabase_client import close_trade  # noqa: WPS433
        try:
            return bool(close_trade(int(trade_id), "closed", 0.0, 0.0, 0.0, reason))
        except Exception as e:  # noqa: BLE001
            log.error("PaperBroker.close_position %s: %s", trade_id, e)
            return False

    def get_open_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        from supabase_client import get_open_trades  # noqa: WPS433
        return get_open_trades(symbol)  # type: ignore[arg-type]

    def update_sl(self, trade_id: str, new_sl: float) -> bool:
        try:
            from supabase_client import _client  # noqa: WPS433
            _client().table("trades").update({"stop_loss": new_sl}).eq("id", int(trade_id)).execute()
            return True
        except Exception as e:  # noqa: BLE001
            log.error("PaperBroker.update_sl %s: %s", trade_id, e)
            return False


# ---------------------------------------------------------------------------
# MetaAPI broker stub
# ---------------------------------------------------------------------------

class MetaApiBroker(AbstractBroker):
    """Stub MetaAPI MT5 broker. Real implementation awaits MetaAPI keys.

    All methods log intent and return False (no-op) until credentials are
    present and metaapi_client is fully implemented.
    """

    @property
    def name(self) -> str:
        return "metaapi"

    def open_position(self, signal: Dict[str, Any], weight: float = 1.0) -> bool:
        from metaapi_client import place_trade  # noqa: WPS433
        return place_trade(signal, weight)

    def close_position(self, trade_id: str, reason: str = "manual") -> bool:
        from metaapi_client import close_trade  # noqa: WPS433
        return close_trade(trade_id, reason)

    def get_open_positions(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        from metaapi_client import get_positions  # noqa: WPS433
        return get_positions(symbol)

    def update_sl(self, trade_id: str, new_sl: float) -> bool:
        from metaapi_client import modify_sl  # noqa: WPS433
        return modify_sl(trade_id, new_sl)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_BROKER: Optional[AbstractBroker] = None


def get_broker() -> AbstractBroker:
    global _BROKER
    if _BROKER is None:
        mode = TRADING_MODE.lower()
        if mode in ("live", "demo"):
            _BROKER = MetaApiBroker()
            log.info("Broker: MetaAPI (%s mode)", mode)
        else:
            _BROKER = PaperBroker()
            log.info("Broker: Paper")
    return _BROKER
