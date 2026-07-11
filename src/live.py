"""Live intraday auto-trading loop (feature 4).

THE RISKIEST PIECE IN THIS PROJECT. It evaluates the configured strategy on
IBKR's live-feed bars for each symbol in `live.symbols` and, on a fresh entry
signal, places a bracketed long automatically — no human keypress per trade.
Run it ONLY on a paper account for a long time before considering anything else.

What it does each poll:
  1. Refresh account daily P&L -> feed the KILL SWITCH (halts new entries when
     the day's loss passes risk.daily_max_loss_pct).
  2. Sync open-position count from the broker portfolio (source of truth).
  3. For each symbol: pull bars, compute the strategy target on the latest
     CLOSED bar, and:
        - fresh entry (0 -> 1) & flat & risk allows  -> place a bracket long
        - strategy exit (1 -> 0) & holding           -> cancel bracket, flatten
  4. Detect positions that closed on their own (stop/target hit) and tidy up.
  5. Near the close, flatten everything and stop entering (no overnight risk).

Safety: refuses to start unless the broker is connected in a verified mode
(the paper/live guard in broker.connect already ran). Graceful Ctrl-C flattens
open positions if `live.flatten_on_exit` is true.
"""
from __future__ import annotations
import datetime as dt
from dataclasses import dataclass, field

import pandas as pd

from .strategies import build_strategy


@dataclass
class OpenPos:
    symbol: str
    shares: int
    entry_price: float
    trades: list           # bracket Trade objects (parent, tp, stop)
    entry_time: dt.datetime


class LiveTrader:
    def __init__(self, broker, risk_manager, executor, journal, cfg):
        self.broker = broker
        self.rm = risk_manager
        self.executor = executor
        self.journal = journal
        self.cfg = cfg
        self.strat = build_strategy(cfg.strategy.name, dict(cfg.strategy.params))
        self.symbols = list(cfg.live.symbols)
        self.poll = float(cfg.live.poll_seconds)
        self.flatten_before_close = int(cfg.live.flatten_before_close_min)
        self.open: dict[str, OpenPos] = {}
        self.last_bar_acted: dict[str, pd.Timestamp] = {}
        self._stop = False

    # ---- helpers ----------------------------------------------------------
    def _latest_closed(self, df: pd.DataFrame):
        """Drop a still-forming final bar if its period hasn't elapsed yet."""
        if len(df) < 3:
            return df
        try:
            step = df.index[-1] - df.index[-2]
            now = pd.Timestamp.now(tz=df.index.tz) if df.index.tz else pd.Timestamp.now()
            if df.index[-1] + step > now:     # last bar still forming
                return df.iloc[:-1]
        except Exception:
            pass
        return df

    def _fresh_entry(self, target: pd.Series) -> bool:
        if len(target) < 2:
            return False
        return int(target.iloc[-1]) == 1 and int(target.iloc[-2]) == 0

    def _exit_signal(self, target: pd.Series) -> bool:
        return len(target) >= 1 and int(target.iloc[-1]) == 0

    def _reconcile_closed(self):
        """Forget positions that the broker no longer reports (stop/TP filled)."""
        for sym in list(self.open.keys()):
            if self.broker.position_qty(sym) <= 0:
                pos = self.open.pop(sym)
                self.broker.cancel_orders(pos.trades)   # cancel the leftover child
                self.journal.log("closed", symbol=sym, mode=self.broker.mode,
                                 note="position no longer held (stop/target/flatten)")
                print(f"  [{sym}] position closed; tidied up remaining order.")

    def _flatten_all(self, reason: str):
        for sym in list(self.open.keys()):
            pos = self.open[sym]
            self.broker.cancel_orders(pos.trades)
            self.broker.flatten(sym)
            self.journal.log("flatten", symbol=sym, side="SELL", qty=pos.shares,
                             mode=self.broker.mode, note=reason)
            print(f"  [{sym}] flattened ({reason}).")
        self.broker.ib.sleep(1)
        self._reconcile_closed()

    # ---- main loop --------------------------------------------------------
    def run(self):
        from src.risk import position_size  # local import keeps module load light
        print(f"\nLIVE LOOP starting | mode={self.broker.mode} "
              f"account={self.broker.account} | strategy={self.cfg.strategy.name} | "
              f"symbols={self.symbols}")
        print(f"Poll every {self.poll:.0f}s. Kill switch at "
              f"{self.rm.kill_switch_level():,.0f}. Ctrl-C to stop.\n")
        try:
            while not self._stop:
                today = dt.date.today()
                is_open, mins_to_close = self.broker.market_clock()

                if not is_open:
                    print(f"[{dt.datetime.now():%H:%M:%S}] market closed — idling.")
                    self.broker.ib.sleep(min(self.poll * 4, 120))
                    continue

                # 1) kill switch from authoritative daily P&L
                dpnl = self.broker.daily_pnl()
                if dpnl is not None:
                    self.rm.update_daily_pnl(dpnl, today)

                # 2) sync open positions from broker
                self._reconcile_closed()
                self.rm.sync_open_positions(len(self.broker.portfolio()), today)

                # near the close: flatten + stop entering
                if mins_to_close <= self.flatten_before_close:
                    if self.open:
                        self._flatten_all("end-of-day flatten")
                    print(f"[{dt.datetime.now():%H:%M:%S}] within "
                          f"{self.flatten_before_close}m of close — no new entries.")
                    self.broker.ib.sleep(self.poll)
                    continue

                if self.rm.halted:
                    print(f"[{dt.datetime.now():%H:%M:%S}] KILL SWITCH active "
                          f"(dayPnL {self.rm.realized_pnl_today:,.0f}). No new entries.")
                    if self.cfg.live.flatten_on_kill and self.open:
                        self._flatten_all("kill switch")
                    self.broker.ib.sleep(self.poll)
                    continue

                # 3) evaluate each symbol
                for sym in self.symbols:
                    try:
                        self._evaluate(sym, today, position_size)
                    except Exception as e:
                        print(f"  [{sym}] error: {e}")

                self.broker.ib.sleep(self.poll)

        except KeyboardInterrupt:
            print("\nCtrl-C received.")
        finally:
            if self.cfg.live.flatten_on_exit and self.open:
                print("Flattening open positions before exit...")
                self._flatten_all("shutdown")
            print("Live loop stopped.")

    def _evaluate(self, sym: str, today: dt.date, position_size):
        df = self.broker.historical(sym)
        if df.empty or len(df) < 3:
            return
        df = self._latest_closed(df)
        bar_ts = df.index[-1]
        # act at most once per closed bar per symbol
        if self.last_bar_acted.get(sym) == bar_ts:
            return

        sig = self.strat.generate(df)
        target = sig["target"]
        price = float(df["close"].iloc[-1])
        holding = self.broker.position_qty(sym) > 0

        if holding and self._exit_signal(target):
            pos = self.open.get(sym)
            if pos:
                self.broker.cancel_orders(pos.trades)
            self.broker.flatten(sym)
            self.journal.log("exit", symbol=sym, side="SELL", price=round(price, 2),
                             strategy=self.cfg.strategy.name, mode=self.broker.mode,
                             note="strategy exit signal")
            print(f"  [{sym}] strategy exit @ ~{price:.2f} — flattened.")
            self.last_bar_acted[sym] = bar_ts
            self.open.pop(sym, None)
            return

        if (not holding) and self._fresh_entry(target):
            ok, reason = self.rm.can_open(today)
            if not ok:
                self.journal.log("blocked", symbol=sym, mode=self.broker.mode, note=reason)
                print(f"  [{sym}] entry blocked: {reason}")
                self.last_bar_acted[sym] = bar_ts
                return
            equity = self.broker.equity()
            sized = position_size(equity, price, self.cfg.risk.max_position_pct,
                                  self.cfg.risk.per_trade_stop_pct)
            if sized["shares"] <= 0:
                self.last_bar_acted[sym] = bar_ts
                return
            res = self.executor.enter_long(sym, price, sized["shares"], today)
            if res.get("placed"):
                self.open[sym] = OpenPos(symbol=sym, shares=res["shares"],
                                         entry_price=res["entry"], trades=res["trades"],
                                         entry_time=dt.datetime.now())
                print(f"  [{sym}] ENTRY: BUY {res['shares']} @ ~{res['entry']} "
                      f"stop {res['stop']} tp {res['take_profit']}")
            self.last_bar_acted[sym] = bar_ts
