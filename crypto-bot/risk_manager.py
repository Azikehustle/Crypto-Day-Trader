"""Risk manager: persistent rules that gate paper trading.

Rules (see config.py for tunables):
  A) Daily loss cap                — halt all signals when daily_pnl ≤ cap
  B) Consecutive loss halt         — halt after N losing trades in a row
  C) Pair cooldown                 — half weight after 3 losses / 24 h, full
                                     block after another loss inside cooldown
  D) Max open trades               — skip new signals when at cap
  E) Quiet hours (21..01 UTC)      — no new trades, manage existing only

State persists in `data/risk_state.json` and survives restarts.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple, List

from config import (
    RISK_STATE_FILE,
    DAILY_LOSS_CAP,
    DAILY_STARTING_EQUITY,
    MAX_CONSECUTIVE_LOSSES,
    CONSECUTIVE_HALT_MINUTES,
    PAIR_LOSS_THRESHOLD,
    PAIR_LOSS_WINDOW_HOURS,
    PAIR_COOLDOWN_HOURS,
    PAIR_BLOCK_HOURS,
    PAIR_WEIGHT_REDUCED,
    MAX_OPEN_TRADES,
    QUIET_START_UTC,
    QUIET_END_UTC,
    ACCOUNT_EQUITY,
)
from logger_setup import get_logger

log = get_logger("risk")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today_str() -> str:
    return _now().strftime("%Y-%m-%d")


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:  # noqa: BLE001
        return None


def in_quiet_hours(dt: Optional[datetime] = None) -> bool:
    """Quiet window crosses midnight: 21..23 UTC OR 0..(QUIET_END_UTC-1)."""
    h = (dt or _now()).hour
    if QUIET_START_UTC <= QUIET_END_UTC:
        return QUIET_START_UTC <= h < QUIET_END_UTC
    # wraps midnight (default 21 .. 1)
    return h >= QUIET_START_UTC or h < QUIET_END_UTC


_DEFAULT_STATE: Dict[str, Any] = {
    "daily_pnl_usd": 0.0,
    "daily_reset_date": "",
    "consecutive_losses": 0,
    "halt_signals": False,
    "halt_reason": "",
    "halt_started_at": "",
    "pair_loss_history": {},      # symbol -> list of ISO timestamps of losses
    "pair_cooldowns": {},         # symbol -> ISO end timestamp (half weight)
    "pair_blocks": {},            # symbol -> ISO end timestamp (full block)
    "running_equity": ACCOUNT_EQUITY,
    "lifetime_realised_pnl_usd": 0.0,
}


class RiskManager:
    """Singleton-ish risk gate. Construct once per process."""

    def __init__(self, state_path: str = RISK_STATE_FILE):
        self.state_path = state_path
        self._lock = threading.Lock()
        self.state: Dict[str, Any] = self._load()
        self._migrate_defaults()
        self._maybe_daily_reset()
        self._save()

    # ---------------- persistence ------------------------------------------
    def _load(self) -> Dict[str, Any]:
        if not os.path.exists(self.state_path):
            return dict(_DEFAULT_STATE)
        try:
            with open(self.state_path) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else dict(_DEFAULT_STATE)
        except Exception as e:  # noqa: BLE001
            log.error("risk_state.json unreadable (%s); resetting", e)
            return dict(_DEFAULT_STATE)

    def _save(self) -> None:
        try:
            with open(self.state_path, "w") as f:
                json.dump(self.state, f, indent=2, default=str)
        except Exception as e:  # noqa: BLE001
            log.error("Failed saving risk state: %s", e)

    def _migrate_defaults(self) -> None:
        for k, v in _DEFAULT_STATE.items():
            self.state.setdefault(k, v)

    # ---------------- daily reset ------------------------------------------
    def _maybe_daily_reset(self) -> None:
        today = _today_str()
        if self.state.get("daily_reset_date") != today:
            log.info("RiskManager: rolling daily P&L (%s -> %s)",
                     self.state.get("daily_reset_date") or "n/a", today)
            self.state["daily_pnl_usd"] = 0.0
            self.state["daily_reset_date"] = today
            self._save()

    def tick(self) -> None:
        """Called from the main loop. Handles daily resets and auto-resume."""
        with self._lock:
            self._maybe_daily_reset()
            self._maybe_auto_resume()

    def _maybe_auto_resume(self) -> None:
        if not self.state.get("halt_signals"):
            return
        # Only auto-resume the consecutive-loss halt; daily-cap halt resets
        # at 00:00 UTC implicitly via _maybe_daily_reset clearing daily_pnl.
        reason = self.state.get("halt_reason", "")
        if not reason.startswith("consecutive_losses"):
            # If daily cap, lift the flag once daily_pnl is 0 again
            if reason.startswith("daily_loss_cap") and self.state.get("daily_pnl_usd", 0.0) == 0.0:
                self._clear_halt("auto: new UTC day")
            return
        started = _parse_iso(self.state.get("halt_started_at", ""))
        if started is None:
            return
        if _now() - started >= timedelta(minutes=CONSECUTIVE_HALT_MINUTES):
            self._clear_halt("auto: 24h elapsed")

    # ---------------- halts ------------------------------------------------
    def _set_halt(self, reason: str) -> None:
        if self.state.get("halt_signals") and self.state.get("halt_reason") == reason:
            return
        self.state["halt_signals"] = True
        self.state["halt_reason"] = reason
        self.state["halt_started_at"] = _iso(_now())
        log.warning("RiskManager halt: %s", reason)
        self._save()

    def _clear_halt(self, note: str = "") -> None:
        if not self.state.get("halt_signals"):
            return
        log.info("RiskManager: clearing halt (%s)", note or "manual")
        self.state["halt_signals"] = False
        self.state["halt_reason"] = ""
        self.state["halt_started_at"] = ""
        self.state["consecutive_losses"] = 0
        self._save()

    def clear_halts(self) -> str:
        """Manual /resume. Clears halt flag, consecutive losses, and pair blocks."""
        with self._lock:
            self.state["pair_blocks"] = {}
            self.state["pair_cooldowns"] = {}
            self._clear_halt("manual /resume")
            return "All halts cleared. Trading resumed."

    # ---------------- queries ----------------------------------------------
    def status_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "halt_signals": bool(self.state.get("halt_signals")),
                "halt_reason": self.state.get("halt_reason", ""),
                "halt_started_at": self.state.get("halt_started_at", ""),
                "daily_pnl_usd": float(self.state.get("daily_pnl_usd", 0.0)),
                "consecutive_losses": int(self.state.get("consecutive_losses", 0)),
                "running_equity": float(self.state.get("running_equity", ACCOUNT_EQUITY)),
                "lifetime_realised_pnl_usd": float(
                    self.state.get("lifetime_realised_pnl_usd", 0.0)
                ),
                "pair_cooldowns": dict(self.state.get("pair_cooldowns", {})),
                "pair_blocks": dict(self.state.get("pair_blocks", {})),
            }

    def get_pair_weight(self, symbol: str) -> float:
        """1.0 = full size, 0.5 = reduced, 0.0 = blocked."""
        with self._lock:
            now = _now()
            blocks = self.state.get("pair_blocks", {})
            until = _parse_iso(blocks.get(symbol, ""))
            if until and now < until:
                return 0.0
            if until and now >= until:
                blocks.pop(symbol, None)
                self.state["pair_blocks"] = blocks
                self._save()
            cooldowns = self.state.get("pair_cooldowns", {})
            until = _parse_iso(cooldowns.get(symbol, ""))
            if until and now < until:
                return PAIR_WEIGHT_REDUCED
            if until and now >= until:
                cooldowns.pop(symbol, None)
                self.state["pair_cooldowns"] = cooldowns
                self._save()
            return 1.0

    # ---------------- main gate -------------------------------------------
    def should_block_signal(self, symbol: str, open_trades: int) -> Tuple[bool, str]:
        """Return (blocked, reason). Reason is empty when blocked is False."""
        with self._lock:
            self._maybe_daily_reset()
            self._maybe_auto_resume()
            if self.state.get("halt_signals"):
                return True, f"halted ({self.state.get('halt_reason', 'unknown')})"
            if open_trades >= MAX_OPEN_TRADES:
                return True, f"max open trades ({MAX_OPEN_TRADES}) reached"
            if in_quiet_hours():
                return True, "quiet hours (21:00–01:00 UTC)"
            blocks = self.state.get("pair_blocks", {})
            until = _parse_iso(blocks.get(symbol, ""))
            if until and _now() < until:
                return True, f"{symbol} blocked until {until.strftime('%Y-%m-%d %H:%M UTC')}"
        return False, ""

    # ---------------- trade close hook ------------------------------------
    def register_trade_close(
        self, symbol: str, pnl_usd: float, result: str
    ) -> List[str]:
        """Update state when a trade closes. Returns list of human-readable
        risk events the caller should alert on (Telegram)."""
        events: List[str] = []
        with self._lock:
            self._maybe_daily_reset()
            now = _now()

            # Equity / lifetime / daily P&L
            self.state["daily_pnl_usd"] = float(self.state.get("daily_pnl_usd", 0.0)) + float(pnl_usd)
            self.state["lifetime_realised_pnl_usd"] = float(
                self.state.get("lifetime_realised_pnl_usd", 0.0)
            ) + float(pnl_usd)
            self.state["running_equity"] = float(
                self.state.get("running_equity", ACCOUNT_EQUITY)
            ) + float(pnl_usd)

            # Consecutive loss tracking
            if result == "loss":
                self.state["consecutive_losses"] = int(
                    self.state.get("consecutive_losses", 0)
                ) + 1

                # Pair-level loss history
                hist = self.state.setdefault("pair_loss_history", {})
                ts_list: List[str] = list(hist.get(symbol, []))
                ts_list.append(_iso(now))
                # Trim to PAIR_LOSS_WINDOW_HOURS
                cutoff = now - timedelta(hours=PAIR_LOSS_WINDOW_HOURS)
                ts_list = [t for t in ts_list if (_parse_iso(t) or now) >= cutoff]
                hist[symbol] = ts_list

                # Trigger pair cooldown / block
                cooldowns = self.state.setdefault("pair_cooldowns", {})
                blocks = self.state.setdefault("pair_blocks", {})
                in_cooldown = bool(_parse_iso(cooldowns.get(symbol, "")))
                if in_cooldown:
                    until = _iso(now + timedelta(hours=PAIR_BLOCK_HOURS))
                    blocks[symbol] = until
                    cooldowns.pop(symbol, None)
                    events.append(
                        f"⛔ {symbol} fully blocked for {PAIR_BLOCK_HOURS}h "
                        f"(another loss during cooldown)"
                    )
                elif len(ts_list) >= PAIR_LOSS_THRESHOLD:
                    until = _iso(now + timedelta(hours=PAIR_COOLDOWN_HOURS))
                    cooldowns[symbol] = until
                    events.append(
                        f"⚠️ {symbol} in cooldown (50% weight) for "
                        f"{PAIR_COOLDOWN_HOURS}h after {PAIR_LOSS_THRESHOLD} losses"
                    )

                # Consecutive loss halt
                if int(self.state["consecutive_losses"]) >= MAX_CONSECUTIVE_LOSSES:
                    self.state["halt_signals"] = True
                    self.state["halt_reason"] = (
                        f"consecutive_losses ({self.state['consecutive_losses']})"
                    )
                    self.state["halt_started_at"] = _iso(now)
                    events.append(
                        f"🛑 Halted: {self.state['consecutive_losses']} consecutive losses. "
                        f"Auto-resume in {CONSECUTIVE_HALT_MINUTES // 60}h or via /resume."
                    )
            else:
                self.state["consecutive_losses"] = 0

            # Daily loss cap
            cap_usd = DAILY_LOSS_CAP * DAILY_STARTING_EQUITY
            if (
                not self.state.get("halt_signals")
                and self.state.get("daily_pnl_usd", 0.0) <= cap_usd
            ):
                self.state["halt_signals"] = True
                self.state["halt_reason"] = "daily_loss_cap"
                self.state["halt_started_at"] = _iso(now)
                events.append(
                    f"🛑 Daily loss cap hit "
                    f"(P&L {self.state['daily_pnl_usd']:+.2f} ≤ {cap_usd:+.2f} USDT). "
                    f"Resumes at 00:00 UTC."
                )

            self._save()
        return events


# Module-level singleton
_RISK_MANAGER: Optional[RiskManager] = None


def get_risk_manager() -> RiskManager:
    global _RISK_MANAGER
    if _RISK_MANAGER is None:
        _RISK_MANAGER = RiskManager()
    return _RISK_MANAGER
