"""Async version of LiveTrader for use as a background task inside FastAPI.

Mirrors src/live.py line-by-line but replaces all blocking ib_async calls with
their async equivalents. The original LiveTrader stays untouched so the CLI
entry point (run_live.py) keeps working.
"""
from __future__ import annotations
import asyncio
import datetime as dt
import sys
import traceback

import pandas as pd

from .live import OpenPos
from .strategies import build_strategy
from .risk import position_size


_C_RESET = "\033[0m"
_C_CYAN = "\033[36m"
_C_GREEN = "\033[32m"
_C_YELLOW = "\033[33m"
_C_RED = "\033[31m"
_C_DIM = "\033[2m"


def _log(msg: str, color: str = _C_CYAN) -> None:
    print(f"{color}[LOOP]{_C_RESET} {msg}", file=sys.stdout, flush=True)


class AsyncLiveTrader:
    def __init__(self, broker, risk_manager, executor, journal, cfg):
        self.broker = broker
        self.rm = risk_manager
        self.executor = executor
        self.journal = journal
        self.cfg = cfg
        sym_cfg = dict(cfg.live.symbols)
        self.symbols: list[str] = list(sym_cfg.keys())
        self.strats: dict[str, object] = {
            sym: build_strategy(sc["strategy"], sc.get("params", {}))
            for sym, sc in sym_cfg.items()
        }
        self.poll = float(cfg.live.poll_seconds)
        self.flatten_before_close = int(cfg.live.flatten_before_close_min)
        self.open: dict[str, OpenPos] = {}
        self.last_bar_acted: dict[str, pd.Timestamp] = {}
        self._stop = False
        self._task: asyncio.Task | None = None
        self.status: str = "stopped"
        self.last_error: str | None = None

    def _latest_closed(self, df: pd.DataFrame):
        if len(df) < 3:
            return df
        try:
            step = df.index[-1] - df.index[-2]
            last_bar = df.index[-1]
            bar_end = last_bar + step

            if last_bar.tz is not None:
                now = pd.Timestamp.now(tz=last_bar.tz)
            else:
                try:
                    from zoneinfo import ZoneInfo
                    now = pd.Timestamp.now(tz=ZoneInfo("America/New_York"))
                    last_bar = last_bar.tz_localize(ZoneInfo("America/New_York"))
                    bar_end = last_bar + step
                except Exception:
                    now = pd.Timestamp.now()

            remaining = (bar_end - now).total_seconds()
            still_forming = remaining > 60
            print(f"[HIST] last bar={df.index[-1]} step={step} "
                  f"bar_end={bar_end} now={now} "
                  f"remaining={remaining:.0f}s dropping={still_forming}",
                  file=sys.stdout, flush=True)
            if still_forming:
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
        for sym in list(self.open.keys()):
            if self.broker.position_qty(sym) <= 0:
                pos = self.open.pop(sym)
                self.broker.cancel_orders(pos.trades)
                self.journal.log("closed", symbol=sym, mode=self.broker.mode,
                                 note="position no longer held (stop/target/flatten)")
                _log(f"[{sym}] position closed; tidied up remaining order.", _C_YELLOW)

    async def _flatten_all(self, reason: str):
        for sym in list(self.open.keys()):
            pos = self.open[sym]
            self.broker.cancel_orders(pos.trades)
            await self.broker.flatten(sym)
            self.journal.log("flatten", symbol=sym, side="SELL", qty=pos.shares,
                             mode=self.broker.mode, note=reason)
            _log(f"[{sym}] flattened ({reason}).", _C_YELLOW)
        await asyncio.sleep(1)
        self._reconcile_closed()

    async def start(self):
        if self._task and not self._task.done():
            return
        self._stop = False
        self.status = "running"
        self.last_error = None
        self._task = asyncio.create_task(self._run_loop())

    def stop(self):
        self._stop = True

    def task_status(self) -> dict:
        """Return the real asyncio task state for diagnostics."""
        info = {
            "status": self.status,
            "task_exists": self._task is not None,
            "task_done": self._task.done() if self._task else None,
            "last_error": self.last_error,
        }
        if self._task and self._task.done():
            exc = self._task.exception() if not self._task.cancelled() else None
            info["task_exception"] = str(exc) if exc else None
        else:
            info["task_exception"] = None
        return info

    async def flatten_single(self, symbol: str):
        """Close a single position, routed through the trader so self.open stays consistent."""
        pos = self.open.get(symbol)
        if pos:
            self.broker.cancel_orders(pos.trades)
        await self.broker.flatten(symbol)
        if pos:
            self.journal.log("flatten", symbol=symbol, side="SELL", qty=pos.shares,
                             mode=self.broker.mode, note="manual close from GUI")
            self.open.pop(symbol, None)
            _log(f"[{symbol}] manually closed from GUI.", _C_YELLOW)
        self._reconcile_closed()

    async def _run_loop(self):
        try:
            eq = self.broker.equity()
            _log(f"startup initiated, equity={eq:,.2f}", _C_GREEN)
            sym_strats = {s: st.name for s, st in self.strats.items()}
            _log(f"mode={self.broker.mode} account={self.broker.account} "
                 f"symbols={sym_strats}")
            _log(f"poll every {self.poll:.0f}s, kill switch at "
                 f"{self.rm.kill_switch_level():,.0f}")

            test_df = await self.broker.historical(self.symbols[0] if self.symbols else "AAPL")
            if not test_df.empty:
                last_ts = test_df.index[-1]
                try:
                    from zoneinfo import ZoneInfo
                    wall = dt.datetime.now(ZoneInfo("America/New_York"))
                except Exception:
                    wall = dt.datetime.now()
                lag = (wall - last_ts.to_pydatetime().replace(
                    tzinfo=last_ts.tz if hasattr(last_ts, 'tz') and last_ts.tz else None
                )).total_seconds() if hasattr(last_ts, 'tz') and last_ts.tz else None
                if lag and lag > 600:
                    _log(f"DATA IS DELAYED: last bar {last_ts} is {lag/60:.0f}m behind "
                         f"wall clock {wall:%H:%M}. Paper accounts without real-time "
                         f"subscriptions get ~15m delayed data.", _C_YELLOW)

            while not self._stop:
                today = dt.date.today()
                is_open, mins_to_close = self.broker.market_clock()

                if not is_open:
                    _log(f"{dt.datetime.now():%H:%M:%S} market closed -- idling.", _C_DIM)
                    await asyncio.sleep(min(self.poll * 4, 120))
                    continue

                dpnl = await self.broker.daily_pnl()
                if dpnl is not None:
                    self.rm.update_daily_pnl(dpnl, today)

                self._reconcile_closed()
                self.rm.sync_open_positions(len(self.broker.portfolio()), today)

                if mins_to_close <= self.flatten_before_close:
                    if self.open:
                        await self._flatten_all("end-of-day flatten")
                    _log(f"{dt.datetime.now():%H:%M:%S} within "
                         f"{self.flatten_before_close}m of close -- no new entries.", _C_YELLOW)
                    await asyncio.sleep(self.poll)
                    continue

                if self.rm.halted:
                    self.status = "halted"
                    _log(f"{dt.datetime.now():%H:%M:%S} KILL SWITCH active "
                         f"(dayPnL {self.rm.realized_pnl_today:,.0f}). No new entries.", _C_RED)
                    if self.cfg.live.flatten_on_kill and self.open:
                        await self._flatten_all("kill switch")
                    await asyncio.sleep(self.poll)
                    continue

                if self.status == "halted" and not self.rm.halted:
                    self.status = "running"

                dpnl_str = f"{self.rm.realized_pnl_today:,.0f}" if dpnl is not None else "n/a"
                _log(f"{dt.datetime.now():%H:%M:%S} poll | "
                     f"eq={self.broker.equity():,.0f} "
                     f"dayPnL={dpnl_str} "
                     f"open={len(self.open)}/{self.rm.max_open_positions} "
                     f"trades={self.rm.trades_today}/{self.rm.max_trades_per_day} "
                     f"close_in={mins_to_close}m")

                for sym in list(self.symbols):
                    try:
                        await self._evaluate(sym, today)
                    except Exception as e:
                        _log(f"[{sym}] error: {e}", _C_RED)

                await asyncio.sleep(self.poll)

        except asyncio.CancelledError:
            _log("task cancelled.", _C_YELLOW)
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            _log(f"FATAL ERROR: {self.last_error}", _C_RED)
        finally:
            if self.cfg.live.flatten_on_exit and self.open:
                _log("flattening open positions before exit...")
                await self._flatten_all("shutdown")
            self.status = "stopped"
            _log("loop stopped.")

    async def _evaluate(self, sym: str, today: dt.date):
        df = await self.broker.historical(sym)
        if df.empty or len(df) < 3:
            _log(f"  [{sym}] {len(df)} bars (need >=3), skipping", _C_DIM)
            return
        df = self._latest_closed(df)
        bar_ts = df.index[-1]
        if self.last_bar_acted.get(sym) == bar_ts:
            return

        strat = self.strats.get(sym)
        if strat is None:
            _log(f"  [{sym}] no strategy assigned, skipping", _C_YELLOW)
            return
        sig = strat.generate(df)
        target = sig["target"]
        tgt_now = int(target.iloc[-1])
        tgt_prev = int(target.iloc[-2]) if len(target) > 1 else 0
        price = float(df["close"].iloc[-1])
        holding = self.broker.position_qty(sym) > 0
        _log(f"  [{sym}] {len(df)} bars, last={bar_ts}, "
             f"price={price:.2f}, target={tgt_prev}->{tgt_now}, "
             f"strat={strat.name}, holding={holding}", _C_DIM)

        if holding and self._exit_signal(target):
            pos = self.open.get(sym)
            if pos:
                self.broker.cancel_orders(pos.trades)
            await self.broker.flatten(sym)
            self.journal.log("exit", symbol=sym, side="SELL", price=round(price, 2),
                             strategy=strat.name, mode=self.broker.mode,
                             note="strategy exit signal")
            _log(f"[{sym}] strategy exit @ ~{price:.2f} -- flattened.", _C_YELLOW)
            self.last_bar_acted[sym] = bar_ts
            self.open.pop(sym, None)
            return

        if (not holding) and self._fresh_entry(target):
            ok, reason = self.rm.can_open(today)
            if not ok:
                self.journal.log("blocked", symbol=sym, mode=self.broker.mode, note=reason)
                _log(f"[{sym}] entry blocked: {reason}", _C_YELLOW)
                self.last_bar_acted[sym] = bar_ts
                return
            equity = self.broker.equity()
            sized = position_size(equity, price, self.cfg.risk.max_position_pct,
                                  self.cfg.risk.per_trade_stop_pct)
            if sized["shares"] <= 0:
                self.last_bar_acted[sym] = bar_ts
                return
            res = await self.executor.enter_long(sym, price, sized["shares"], today)
            if res.get("placed"):
                self.open[sym] = OpenPos(symbol=sym, shares=res["shares"],
                                         entry_price=res["entry"], trades=res["trades"],
                                         entry_time=dt.datetime.now())
                _log(f"[{sym}] ENTRY: BUY {res['shares']} @ ~{res['entry']} "
                     f"stop {res['stop']} tp {res['take_profit']}", _C_GREEN)
            self.last_bar_acted[sym] = bar_ts
