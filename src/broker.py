"""IBKR connection (feature 1) via ib_async (the maintained successor to
ib_insync). Talks to a locally-running TWS or IB Gateway.

SAFETY GUARDS:
  * Live trading is refused unless BOTH config broker.mode == 'live' AND
    broker.allow_live == true.
  * After connecting, the real IBKR account number is inspected. Paper accounts
    start with 'DU'; live individual accounts start with 'U'. If you asked for
    paper but the connected account is live (or vice-versa), we refuse to
    proceed. This catches "I pointed at the wrong port" mistakes before any
    order is sent.
"""
from __future__ import annotations
import logging
import time
import pandas as pd

log = logging.getLogger(__name__)


class SafetyError(RuntimeError):
    pass


class Broker:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ib = None
        self.account = None
        self._equity_dumped = False

    @property
    def mode(self) -> str:
        return self.cfg.broker.mode

    def connect(self):
        b = self.cfg.broker
        if b.mode == "live" and not b.allow_live:
            raise SafetyError(
                "Refusing to connect in LIVE mode: set broker.allow_live: true "
                "in config.yaml to override (only after extensive paper testing).")
        from ib_async import IB
        ib = IB()
        ib.connect(b.host, int(b.port), clientId=int(b.client_id))
        accounts = ib.managedAccounts()
        if not accounts:
            ib.disconnect()
            raise SafetyError("Connected but no managed accounts returned.")
        acct = accounts[0]
        is_paper = acct.upper().startswith("DU")

        if b.mode == "paper" and not is_paper:
            ib.disconnect()
            raise SafetyError(
                f"Config says PAPER but connected account '{acct}' is not a paper "
                f"(DU...) account. Check your port (paper TWS=7497, Gateway=4002).")
        if b.mode == "live" and is_paper:
            ib.disconnect()
            raise SafetyError(
                f"Config says LIVE but connected account '{acct}' is a paper account.")

        self.ib, self.account = ib, acct
        ib.sleep(4)
        return self

    def disconnect(self):
        if self.ib is not None:
            self.ib.disconnect()

    # -- helpers ------------------------------------------------------------
    def equity(self) -> float:
        vals = self.ib.accountValues(self.account)
        if not self._equity_dumped:
            self._equity_dumped = True
            print("--- accountValues diagnostic dump ---")
            for v in vals:
                print(f"  {v.tag:30s}  currency={v.currency:4s}  value={v.value}")
            print(f"--- end dump ({len(vals)} items) ---")

        configured = self.cfg.account.currency
        first_netliq = None
        first_netliq_ccy = None
        for v in vals:
            if v.tag == "NetLiquidation":
                val = float(v.value)
                if v.currency == configured and val:
                    return val
                if first_netliq is None and val:
                    first_netliq = val
                    first_netliq_ccy = v.currency
        if first_netliq:
            log.warning("NetLiquidation not found for configured currency %s; "
                        "using %s value %.2f instead",
                        configured, first_netliq_ccy, first_netliq)
            return first_netliq
        return 0.0

    def equity_or_raise(self, retries: int = 10, delay: float = 2.0) -> float:
        """Return account equity, retrying until a non-zero value arrives.

        Raises SafetyError after `retries` failed attempts — a zero equity at
        startup is always a data/connection problem, never real.
        """
        for attempt in range(1, retries + 1):
            eq = self.equity()
            if eq:
                log.info("Startup equity: %s %s", f"{eq:,.2f}",
                         self.cfg.account.currency)
                return eq
            log.warning("Equity read as %s (attempt %d/%d), retrying in %ss …",
                        eq, attempt, retries, delay)
            time.sleep(delay)
        raise SafetyError(
            f"Account equity still {self.equity()} after {retries} attempts. "
            "Check that TWS/Gateway is running, the API port is correct, and "
            "your market-data subscriptions are active."
        )

    def stock(self, symbol: str, currency: str = "USD"):
        from ib_async import Stock
        c = Stock(symbol, "SMART", currency)
        self.ib.qualifyContracts(c)
        return c

    def historical(self, symbol: str) -> pd.DataFrame:
        """Real-time-feed historical bars from IBKR (use this for live signals)."""
        from ib_async import util
        c = self.stock(symbol)
        bars = self.ib.reqHistoricalData(
            c, endDateTime="", durationStr=self.cfg.data.lookback,
            barSizeSetting=self.cfg.data.bar_size, whatToShow="TRADES",
            useRTH=self.cfg.data.rth_only, formatDate=1)
        df = util.df(bars)
        if df is None or df.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = df.set_index("date")[["open", "high", "low", "close", "volume"]]
        df.index = pd.to_datetime(df.index)
        return df

    def positions(self) -> list:
        return self.ib.positions(self.account)

    def portfolio(self) -> list:
        return self.ib.portfolio()

    # -- live-loop helpers --------------------------------------------------
    def daily_pnl(self):
        """Account-level daily P&L (realized + unrealized) from IBKR, or None
        until the first update arrives. Requires a subscribed PnL stream."""
        from ib_async import PnL
        if getattr(self, "_pnl", None) is None:
            self._pnl = self.ib.reqPnL(self.account)
        self.ib.sleep(0)  # let an update arrive
        val = getattr(self._pnl, "dailyPnL", None)
        # ib_async uses NaN before the first update; treat that as "unknown".
        if val is None or val != val:
            return None
        return float(val)

    def position_qty(self, symbol: str) -> float:
        for item in self.ib.portfolio():
            if item.contract.symbol == symbol:
                return float(item.position)
        return 0.0

    def cancel_orders(self, trades) -> None:
        """Cancel any still-active orders among the given Trade objects."""
        for tr in trades or []:
            try:
                if tr.orderStatus.status not in ("Filled", "Cancelled", "ApiCancelled",
                                                 "Inactive"):
                    self.ib.cancelOrder(tr.order)
            except Exception:
                pass

    def flatten(self, symbol: str) -> None:
        """Market-close an open long position in `symbol` (then sweep its orders)."""
        from ib_async import MarketOrder
        qty = self.position_qty(symbol)
        if qty > 0:
            c = self.stock(symbol)
            self.ib.placeOrder(c, MarketOrder("SELL", int(qty)))
            self.ib.sleep(1)

    def market_clock(self):
        """Return (is_open, minutes_to_close) for US regular trading hours.

        Wall-clock check in US/Eastern: Mon-Fri 09:30-16:00. Does NOT know
        market holidays/half-days — on those IBKR simply won't fill and RTH
        data won't update, so the practical risk is low, but be aware of it.
        """
        from datetime import datetime, time as dtime
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            now = datetime.now()
        if now.weekday() >= 5:
            return False, 0
        open_t, close_t = dtime(9, 30), dtime(16, 0)
        is_open = open_t <= now.time() < close_t
        mins_to_close = (16 * 60) - (now.hour * 60 + now.minute) if is_open else 0
        return is_open, mins_to_close
