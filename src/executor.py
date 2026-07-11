"""Execution (feature 6): bracket orders.

A bracket = parent entry + child take-profit (limit) + child stop-loss (stop),
submitted as an OCO group so that whichever child fills first cancels the other.
Every position is protected the instant it opens — you can never end up holding
an unprotected position because the executor forgot the stop.

ib_async provides ib.bracketOrder(...) which builds the three linked orders.
"""
from __future__ import annotations
from .risk import bracket_prices


class Executor:
    def __init__(self, broker, risk_manager, journal, cfg):
        self.broker = broker
        self.rm = risk_manager
        self.journal = journal
        self.cfg = cfg

    def enter_long(self, symbol: str, entry_ref_price: float, shares: int,
                   now_date) -> dict:
        """Place a bracketed long. entry_ref_price is the current price used to
        compute a marketable limit + stop/target. Returns a summary dict."""
        ok, reason = self.rm.can_open(now_date)
        if not ok:
            self.journal.log("blocked", symbol=symbol, mode=self.broker.mode, note=reason)
            return {"placed": False, "reason": reason}
        if shares <= 0:
            return {"placed": False, "reason": "size is 0 shares"}

        bp = bracket_prices(entry_ref_price,
                            self.cfg.risk.per_trade_stop_pct,
                            self.cfg.risk.take_profit_pct)

        contract = self.broker.stock(symbol)
        bracket = self.broker.ib.bracketOrder(
            action="BUY", quantity=shares,
            limitPrice=bp["entry"], takeProfitPrice=bp["take_profit"],
            stopLossPrice=bp["stop"])
        trades = [self.broker.ib.placeOrder(contract, order) for order in bracket]

        self.rm.note_entry(now_date)
        self.journal.log("entry", symbol=symbol, side="BUY", qty=shares,
                         price=bp["entry"], stop=bp["stop"],
                         take_profit=bp["take_profit"],
                         strategy=self.cfg.strategy.name, mode=self.broker.mode,
                         note=f"R:R {bp['reward_risk']}")
        return {"placed": True, **bp, "shares": shares, "contract": contract,
                "trades": trades}
