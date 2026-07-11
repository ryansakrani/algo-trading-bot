"""Async version of Broker for use inside a running asyncio event loop (FastAPI).

Overrides every blocking ib_async call with its native async equivalent.
The original Broker class stays untouched so CLI scripts keep working.
"""
from __future__ import annotations
import asyncio
import datetime as dt
import logging
import sys
import pandas as pd
from .broker import Broker, SafetyError

log = logging.getLogger(__name__)


class AsyncBroker(Broker):

    def __init__(self, cfg):
        super().__init__(cfg)
        self._contracts: dict[str, object] = {}

    async def connect(self):
        b = self.cfg.broker
        if b.mode == "live" and not b.allow_live:
            raise SafetyError(
                "Refusing to connect in LIVE mode: set broker.allow_live: true "
                "in config.yaml to override (only after extensive paper testing).")
        from ib_async import IB
        ib = IB()
        await ib.connectAsync(b.host, int(b.port), clientId=int(b.client_id))
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
        await asyncio.sleep(4)
        return self

    async def disconnect(self):
        if self.ib is not None:
            self.ib.disconnect()

    async def stock(self, symbol: str, currency: str = "USD"):
        from ib_async import Stock
        if symbol in self._contracts:
            return self._contracts[symbol]
        c = Stock(symbol, "SMART", currency)
        await self.ib.qualifyContractsAsync(c)
        self._contracts[symbol] = c
        return c

    async def historical(self, symbol: str) -> pd.DataFrame:
        """Fresh historical bars with a UTC endDateTime to bust IBKR's cache."""
        from ib_async import util
        c = await self.stock(symbol)
        now_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d %H:%M:%S UTC")
        bars = await self.ib.reqHistoricalDataAsync(
            c, endDateTime=now_utc, durationStr=self.cfg.data.lookback,
            barSizeSetting=self.cfg.data.bar_size, whatToShow="TRADES",
            useRTH=self.cfg.data.rth_only, formatDate=1)
        df = util.df(bars)
        if df is None or df.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = df.set_index("date")[["open", "high", "low", "close", "volume"]]
        df.index = pd.to_datetime(df.index)
        return df

    async def equity_or_raise(self, retries: int = 10, delay: float = 2.0) -> float:
        for attempt in range(1, retries + 1):
            eq = self.equity()
            if eq:
                log.info("Startup equity: %s %s", f"{eq:,.2f}",
                         self.cfg.account.currency)
                return eq
            log.warning("Equity read as %s (attempt %d/%d), retrying in %ss …",
                        eq, attempt, retries, delay)
            await asyncio.sleep(delay)
        raise SafetyError(
            f"Account equity still {self.equity()} after {retries} attempts. "
            "Check that TWS/Gateway is running, the API port is correct, and "
            "your market-data subscriptions are active."
        )

    async def daily_pnl(self):
        from ib_async import PnL
        if getattr(self, "_pnl", None) is None:
            self._pnl = self.ib.reqPnL(self.account)
        await asyncio.sleep(0)
        val = getattr(self._pnl, "dailyPnL", None)
        if val is None or val != val:
            return None
        return float(val)

    async def flatten(self, symbol: str) -> None:
        from ib_async import MarketOrder
        qty = self.position_qty(symbol)
        if qty > 0:
            c = await self.stock(symbol)
            self.ib.placeOrder(c, MarketOrder("SELL", int(qty)))
            await asyncio.sleep(1)
