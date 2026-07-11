"""Risk management (feature 5) — the seatbelt.

Two responsibilities:
  1. position_size(): how many shares to buy given equity and risk limits.
  2. RiskManager: a live gatekeeper that tracks the trading day and refuses new
     trades when a limit is breached (daily kill switch, max trades, max
     positions). This is pure logic with no broker dependency, so it is fully
     unit-testable offline.
"""
from __future__ import annotations
import logging
import math
from dataclasses import dataclass, field
import datetime as dt

log = logging.getLogger(__name__)


def position_size(equity: float, price: float, max_position_pct: float,
                  stop_pct: float) -> dict:
    """Size a long position by capping notional at max_position_pct of equity.

    Returns a dict with shares, notional, and the dollar risk implied by the
    stop. Never returns a position whose notional exceeds the cap.
    """
    if price <= 0 or equity <= 0:
        return {"shares": 0, "notional": 0.0, "dollar_risk": 0.0}
    max_notional = equity * max_position_pct
    shares = int(math.floor(max_notional / price))
    notional = shares * price
    dollar_risk = notional * stop_pct
    return {"shares": shares, "notional": notional, "dollar_risk": dollar_risk}


def bracket_prices(entry: float, stop_pct: float, take_profit_pct: float) -> dict:
    """Compute stop-loss and take-profit prices for a long entry."""
    return {
        "entry": round(entry, 2),
        "stop": round(entry * (1 - stop_pct), 2),
        "take_profit": round(entry * (1 + take_profit_pct), 2),
        "reward_risk": round(take_profit_pct / stop_pct, 2) if stop_pct else None,
    }


@dataclass
class RiskManager:
    start_equity: float
    max_position_pct: float
    per_trade_stop_pct: float
    take_profit_pct: float
    daily_max_loss_pct: float
    max_open_positions: int
    max_trades_per_day: int

    _day: dt.date = field(default=None)
    realized_pnl_today: float = 0.0
    trades_today: int = 0
    open_positions: int = 0
    halted: bool = False

    def _roll_day(self, now: dt.date) -> None:
        if self._day != now:
            self._day = now
            self.realized_pnl_today = 0.0
            self.trades_today = 0
            self.halted = False
            # open_positions intentionally NOT reset (positions may carry until flat)

    def kill_switch_level(self) -> float:
        return -abs(self.start_equity * self.daily_max_loss_pct)

    def record_fill(self, now: dt.date) -> None:
        self._roll_day(now)
        self.trades_today += 1
        self.open_positions += 1

    def record_close(self, realized: float, now: dt.date) -> None:
        self._roll_day(now)
        self.realized_pnl_today += realized
        self.open_positions = max(0, self.open_positions - 1)
        if self.realized_pnl_today <= self.kill_switch_level():
            self.halted = True

    def can_open(self, now: dt.date) -> tuple[bool, str]:
        self._roll_day(now)
        if self.halted or self.realized_pnl_today <= self.kill_switch_level():
            self.halted = True
            return False, (f"KILL SWITCH: day P&L {self.realized_pnl_today:,.2f} "
                           f"hit limit {self.kill_switch_level():,.2f}")
        if self.trades_today >= self.max_trades_per_day:
            return False, f"max_trades_per_day ({self.max_trades_per_day}) reached"
        if self.open_positions >= self.max_open_positions:
            return False, f"max_open_positions ({self.max_open_positions}) reached"
        return True, "ok"

    # -- live-loop helpers --------------------------------------------------
    def note_entry(self, now: dt.date) -> None:
        """Count an entry without touching open_positions (the live loop syncs
        open_positions from the broker portfolio instead)."""
        self._roll_day(now)
        self.trades_today += 1

    def sync_open_positions(self, n: int, now: dt.date) -> None:
        self._roll_day(now)
        self.open_positions = max(0, int(n))

    def update_daily_pnl(self, daily_pnl: float, now: dt.date) -> None:
        """Drive the kill switch from the broker's authoritative daily P&L
        (realized + unrealized). Trips the halt if past the kill level."""
        self._roll_day(now)
        self.realized_pnl_today = float(daily_pnl)
        if not self.start_equity:
            log.warning("start_equity is zero — skipping kill-switch check "
                        "(this is a data problem, not a real loss)")
            return
        if abs(daily_pnl) < 1.0:
            return
        if self.realized_pnl_today <= self.kill_switch_level():
            self.halted = True
