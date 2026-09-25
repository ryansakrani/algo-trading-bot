#!/usr/bin/env python3
"""Offline SAFETY tests: paper/live guard, kill switch and caps, bracket
orders, equity handling, live-loop bar timing, market hours, disconnects,
flatten logic, position sizing and backtester next-bar execution.

Everything runs against a fake ib_async.IB that records orders and never opens
a socket — no IB Gateway/TWS, no yfinance, no network. Config comes from
config.example.yaml copied into a temp dir; config.yaml is never read.

Run:  python tests/test_safety.py        (pytest also collects this file)

Tests decorated with @known_bug("<ID>") reproduce a defect written up in
REVIEW.md under that ID. They are EXPECTED TO FAIL until the bug is fixed.
Do not weaken them to make the suite green — fix the code, then remove the
marker.
"""
from __future__ import annotations
import sys, os, io, asyncio, contextlib, datetime as dt, functools, glob
import itertools, math, random, tempfile, traceback
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np
import pandas as pd
import yaml
import ib_async
from ib_async import IB

from src.config import load_config
from src.broker import Broker, SafetyError
from src.async_broker import AsyncBroker
from src.executor import Executor
from src.async_executor import AsyncExecutor
from src.live import LiveTrader, OpenPos
from src.async_live import AsyncLiveTrader
from src.risk import RiskManager, position_size
from src.journal import Journal
from src.backtester import run_backtest
from src.strategies.base import Strategy, register

NY = ZoneInfo("America/New_York")
EXAMPLE_CFG = os.path.join(ROOT, "config.example.yaml")
STEP = pd.Timedelta(minutes=5)
FRESH = [0] * 20 + [1]          # 0 -> 1 on the last bar: fresh entry
EXIT = [1] * 20 + [0]           # strategy says flat on the last bar


# ============================================================================
# Known-bug marker
# ============================================================================
def known_bug(bug_id: str):
    """Tag a test that reproduces REVIEW.md finding `bug_id`. It is expected
    to fail until that finding is fixed."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper():
            try:
                fn()
            except AssertionError as e:
                raise AssertionError(f"KNOWN BUG {bug_id} (see REVIEW.md): {e}") from None
        wrapper.known_bug = bug_id
        return wrapper
    return deco


# ============================================================================
# Test-only strategies (registered into the real registry)
# ============================================================================
@register
class _SigColumn(Strategy):
    """target = the bars' `sig` column, so each test controls the signal."""
    name = "test_sig_column"

    def generate(self, df):
        return self._finish(df, df["sig"])


@register
class _NeverLong(Strategy):
    name = "test_never_long"

    def generate(self, df):
        return self._finish(df, pd.Series(0, index=df.index))


# ============================================================================
# Fake ib_async.IB
# ============================================================================
class FakeTrade:
    def __init__(self, order, status="Submitted", filled=0.0):
        self.order = order
        self.orderStatus = SimpleNamespace(status=status, filled=filled)


class FakeIB:
    """Stands in for ib_async.IB. Records every order; never touches a socket.
    bracketOrder is ib_async's REAL implementation (it only needs getReqId)."""
    bracketOrder = IB.bracketOrder

    def __init__(self, account="DU1234567", netliq=1_000_000.0, ccy="CAD"):
        self.client = SimpleNamespace(getReqId=itertools.count(1).__next__)
        self.accounts = [account] if account else []
        self.connected = False
        self.holdings: dict[str, float] = {}     # what the portfolio feed reports
        self.orders: list[tuple[str, object]] = []   # every placeOrder attempt
        self.cancelled: list = []
        self.disconnects = 0
        self.fill_market_sells = True
        self.account_values = [SimpleNamespace(tag="NetLiquidation", currency=ccy,
                                               value=str(netliq))]
        self.pnl = SimpleNamespace(dailyPnL=0.0)
        self.on_sleep = None
        self.values_arrive_on_sleep = None       # simulate data needing the event loop

    # -- connection --------------------------------------------------------
    def connect(self, host, port, clientId=0, **kw):
        self.connected = True

    async def connectAsync(self, host, port, clientId=0, **kw):
        self.connected = True

    def isConnected(self):
        return self.connected

    def managedAccounts(self):
        return list(self.accounts)

    def disconnect(self):
        self.disconnects += 1
        self.connected = False

    def sleep(self, secs=0.0):
        if self.values_arrive_on_sleep is not None:
            self.account_values = self.values_arrive_on_sleep
        if self.on_sleep:
            self.on_sleep(secs)

    # -- data --------------------------------------------------------------
    def accountValues(self, account=""):
        return list(self.account_values)

    def reqPnL(self, account, modelCode=""):
        self._require_connection()
        return self.pnl

    def portfolio(self, account=""):
        return [SimpleNamespace(contract=SimpleNamespace(symbol=s), position=float(q),
                                averageCost=100.0, marketPrice=100.0, unrealizedPNL=0.0)
                for s, q in self.holdings.items() if q]

    def positions(self, account=""):
        return self.portfolio()

    def qualifyContracts(self, *contracts):
        return list(contracts)

    async def qualifyContractsAsync(self, *contracts):
        return list(contracts)

    # -- orders ------------------------------------------------------------
    def placeOrder(self, contract, order):
        self.orders.append((contract.symbol, order))
        self._require_connection()
        if order.orderType == "MKT" and order.action == "SELL" and self.fill_market_sells:
            self.holdings[contract.symbol] = (self.holdings.get(contract.symbol, 0)
                                              - order.totalQuantity)
        return FakeTrade(order)

    def cancelOrder(self, order):
        self._require_connection()
        self.cancelled.append(order)

    def _require_connection(self):
        if not self.connected:
            raise ConnectionError("Not connected")

    # -- views -------------------------------------------------------------
    def buys(self):
        return [o for _, o in self.orders if o.action == "BUY"]

    def market_sells(self, symbol=None):
        return [o for s, o in self.orders
                if o.orderType == "MKT" and o.action == "SELL"
                and (symbol is None or s == symbol)]


# ============================================================================
# Fixtures
# ============================================================================
def make_cfg(tmp, *, risk=None, symbols=None, broker=None, strategy="test_sig_column"):
    """Config built from config.example.yaml in a temp dir (CAD account)."""
    with open(EXAMPLE_CFG) as f:
        raw = yaml.safe_load(f)
    raw["account"]["currency"] = "CAD"
    raw["journal"]["path"] = os.path.join(tmp, "journal.csv")
    if isinstance(symbols, dict):
        raw["live"]["symbols"] = symbols
    else:
        raw["live"]["symbols"] = {s: {"strategy": "test_sig_column", "params": {}}
                                  for s in (symbols or ["AAPL"])}
    raw["risk"].update(risk or {})
    raw["broker"].update(broker or {})
    path = os.path.join(tmp, "config.yaml")
    with open(path, "w") as f:
        yaml.safe_dump(raw, f)
    cfg = load_config(path)
    # load_config only accepts the four built-in names for the global strategy.
    cfg["strategy"]["name"] = strategy
    cfg["strategy"]["params"] = {}
    return cfg


def make_rm(cfg, start_equity=1_000_000.0):
    r = cfg.risk
    return RiskManager(start_equity=start_equity, max_position_pct=r.max_position_pct,
                       per_trade_stop_pct=r.per_trade_stop_pct,
                       take_profit_pct=r.take_profit_pct,
                       daily_max_loss_pct=r.daily_max_loss_pct,
                       max_open_positions=r.max_open_positions,
                       max_trades_per_day=r.max_trades_per_day)


def make_bars(sigs, last_start, price=100.0):
    n = len(sigs)
    idx = pd.DatetimeIndex([last_start - STEP * (n - 1 - i) for i in range(n)])
    return pd.DataFrame({"open": price, "high": price + 0.1, "low": price - 0.1,
                         "close": price, "volume": 1000, "sig": list(sigs)}, index=idx)


def now_ny():
    return pd.Timestamp.now(tz=NY)


def closed_bars(sigs):
    """Bars whose last one closed at the most recent 5-minute boundary."""
    return make_bars(sigs, now_ny().floor("5min") - STEP)


def prev_session_bars(sigs):
    """Bars ending with yesterday's 15:55 bar (what IB returns just after the open)."""
    last = now_ny().normalize() - pd.Timedelta(days=1) + pd.Timedelta(hours=15, minutes=55)
    return make_bars(sigs, last)


class _Rig:
    def _init_common(self, tmp, bars, clock, start_equity, cfg_kw, broker_cls):
        self.cfg = make_cfg(tmp, **cfg_kw)
        self.ib = FakeIB()
        self.ib.connected = True
        b = broker_cls(self.cfg)
        b.ib, b.account = self.ib, self.ib.accounts[0]
        self.bars = dict(bars or {})
        self.clock = clock
        self.historical_calls = 0
        b.market_clock = lambda: self.clock
        self.broker = b
        self.rm = make_rm(self.cfg, start_equity)
        self.journal = Journal(self.cfg.journal.path)

    def _historical(self, sym):
        self.historical_calls += 1
        v = self.bars.get(sym)
        if isinstance(v, Exception):
            raise v
        if v is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        return v.copy()

    def track(self, sym, qty):
        """Pretend the bot opened `qty` shares of `sym` with a live bracket."""
        trades = [FakeTrade(SimpleNamespace(orderId=901), "Filled", qty),
                  FakeTrade(SimpleNamespace(orderId=902), "Submitted"),
                  FakeTrade(SimpleNamespace(orderId=903), "PreSubmitted")]
        self.trader.open[sym] = OpenPos(sym, qty, 100.0, trades, dt.datetime.now())
        self.ib.holdings[sym] = qty

    def journal_events(self, sym=None):
        return [r["event"] for r in self.journal.tail(1000)
                if sym is None or r["symbol"] == sym]


class SyncRig(_Rig):
    """Real Broker/Executor/LiveTrader wired to a FakeIB."""
    def __init__(self, tmp, bars=None, clock=(True, 180), start_equity=1_000_000.0,
                 **cfg_kw):
        self._init_common(tmp, bars, clock, start_equity, cfg_kw, Broker)
        self.broker.historical = self._historical
        self.executor = Executor(self.broker, self.rm, self.journal, self.cfg)
        self.trader = LiveTrader(self.broker, self.rm, self.executor, self.journal,
                                 self.cfg)

    def evaluate(self, sym="AAPL"):
        self.trader._evaluate(sym, dt.date.today(), position_size)

    def run_iterations(self, n):
        """Run LiveTrader.run() for n poll iterations (end-of-iteration sleeps)."""
        count = [0]

        def on_sleep(secs):
            if secs >= self.trader.poll:
                count[0] += 1
                if count[0] >= n:
                    self.trader._stop = True
        self.ib.on_sleep = on_sleep
        self.trader.run()


class AsyncRig(_Rig):
    """Real AsyncBroker/AsyncExecutor/AsyncLiveTrader wired to a FakeIB."""
    def __init__(self, tmp, bars=None, clock=(True, 180), start_equity=1_000_000.0,
                 **cfg_kw):
        self._init_common(tmp, bars, clock, start_equity, cfg_kw, AsyncBroker)

        async def historical(sym):
            return self._historical(sym)
        self.broker.historical = historical
        self.executor = AsyncExecutor(self.broker, self.rm, self.journal, self.cfg)
        self.trader = AsyncLiveTrader(self.broker, self.rm, self.executor,
                                      self.journal, self.cfg)

    async def evaluate(self, sym="AAPL"):
        await self.trader._evaluate(sym, dt.date.today())

    async def run_for(self, seconds):
        self.trader.poll = 0.02
        await self.trader.start()
        await asyncio.sleep(seconds)
        self.trader.stop()
        await asyncio.wait_for(self.trader._task, timeout=15)


@contextlib.contextmanager
def patched_ib(fake):
    """Make `from ib_async import IB` return `fake`; skip AsyncBroker's 4s sleep.
    Yields a list that records every IB() construction."""
    import src.async_broker as ab
    made = []
    orig_ib, orig_asyncio = ib_async.IB, ab.asyncio

    def factory(*a, **k):
        made.append(1)
        return fake

    async def no_sleep(*a, **k):
        return None
    ib_async.IB = factory
    ab.asyncio = SimpleNamespace(sleep=no_sleep)
    try:
        yield made
    finally:
        ib_async.IB, ab.asyncio = orig_ib, orig_asyncio


@contextlib.contextmanager
def no_time_sleep():
    import src.broker as bm
    orig = bm.time
    bm.time = SimpleNamespace(sleep=lambda s: None)
    try:
        yield
    finally:
        bm.time = orig


@contextlib.contextmanager
def frozen_ny_clock(y, mo, d, h, mi):
    """Freeze datetime.now() (as seen by Broker.market_clock) to a NY time."""
    import datetime as dt_mod
    real = dt_mod.datetime
    fixed = real(y, mo, d, h, mi, tzinfo=NY)

    class FrozenDT(real):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
    dt_mod.datetime = FrozenDT
    try:
        yield
    finally:
        dt_mod.datetime = real


def expect_raises(exc_type, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc_type as e:
        return e
    raise AssertionError(f"expected {exc_type.__name__}, nothing raised")


def tmpdir():
    return tempfile.TemporaryDirectory()


# ============================================================================
# 1. Paper/live guard
# ============================================================================
def test_guard_paper_rejects_non_du_account_sync():
    with tmpdir() as tmp:
        cfg = make_cfg(tmp)
        fake = FakeIB(account="U7654321")
        b = Broker(cfg)
        with patched_ib(fake):
            e = expect_raises(SafetyError, b.connect)
        assert "not a paper" in str(e)
        assert fake.disconnects == 1, "must disconnect after rejecting the account"
        assert b.ib is None and b.account is None, "broker must not keep the connection"
        # With the connection refused, the executor cannot reach any order call.
        ex = Executor(b, make_rm(cfg), Journal(cfg.journal.path), cfg)
        expect_raises(AttributeError, ex.enter_long, "AAPL", 100.0, 10, dt.date.today())
        assert fake.orders == []


def test_guard_paper_rejects_non_du_account_async():
    with tmpdir() as tmp:
        cfg = make_cfg(tmp)
        fake = FakeIB(account="U7654321")
        b = AsyncBroker(cfg)
        with patched_ib(fake):
            try:
                asyncio.run(b.connect())
            except SafetyError as e:
                assert "not a paper" in str(e)
            else:
                raise AssertionError("AsyncBroker accepted a non-DU account in paper mode")
        assert fake.disconnects == 1 and b.ib is None and fake.orders == []


def test_guard_no_managed_accounts_refused():
    with tmpdir() as tmp:
        cfg = make_cfg(tmp)
        fake = FakeIB(account=None)
        with patched_ib(fake):
            expect_raises(SafetyError, Broker(cfg).connect)
        assert fake.disconnects == 1


def test_guard_live_mode_refused_without_allow_live():
    with tmpdir() as tmp:
        cfg = make_cfg(tmp, broker={"mode": "live", "allow_live": False})
        for broker_cls in (Broker, AsyncBroker):
            fake = FakeIB(account="U7654321")
            b = broker_cls(cfg)
            with patched_ib(fake) as made:
                try:
                    r = b.connect()
                    if asyncio.iscoroutine(r):
                        asyncio.run(r)
                except SafetyError as e:
                    assert "allow_live" in str(e)
                else:
                    raise AssertionError(f"{broker_cls.__name__} connected in live mode "
                                         f"with allow_live false")
            assert made == [], "must refuse BEFORE creating an IB connection"


def test_guard_live_mode_rejects_paper_account():
    with tmpdir() as tmp:
        cfg = make_cfg(tmp, broker={"mode": "live", "allow_live": True})
        fake = FakeIB(account="DU1234567")
        with patched_ib(fake):
            expect_raises(SafetyError, Broker(cfg).connect)
        assert fake.disconnects == 1


def test_guard_live_mode_both_flags_and_live_account_connects():
    """Documents the intended opt-in: mode=live + allow_live=true + U... account."""
    with tmpdir() as tmp:
        cfg = make_cfg(tmp, broker={"mode": "live", "allow_live": True})
        fake = FakeIB(account="U7654321")
        with patched_ib(fake):
            b = Broker(cfg).connect()
        assert b.account == "U7654321" and fake.orders == []


@known_bug("L1")
def test_l1_allow_live_string_false_is_not_treated_as_true():
    """A quoted `allow_live: "false"` is a truthy string; it must not unlock live."""
    with tmpdir() as tmp:
        refused = False
        try:
            cfg = make_cfg(tmp, broker={"mode": "live", "allow_live": "false"})
        except AssertionError:
            refused = True          # config validation caught it: good
        if not refused:
            with patched_ib(FakeIB(account="U7654321")):
                try:
                    Broker(cfg).connect()
                except SafetyError:
                    refused = True
        assert refused, 'allow_live: "false" (a string) enabled LIVE trading'


def test_guard_web_connect_rejects_non_du_account():
    from fastapi import HTTPException
    from src.web import state as web_state, routes_live

    async def go(path):
        state = web_state.init_state(path)
        fake = FakeIB(account="U7654321")
        with patched_ib(fake):
            try:
                await routes_live.connect()
            except HTTPException as e:
                assert e.status_code == 500 and "not a paper" in str(e.detail)
            else:
                raise AssertionError("/api/connect accepted a non-DU account")
        assert not state.connected and state.trader is None and state.executor is None
        assert fake.orders == []

    with tmpdir() as tmp:
        make_cfg(tmp)
        asyncio.run(go(os.path.join(tmp, "config.yaml")))


# ============================================================================
# 2. Kill switch and risk caps
# ============================================================================
def test_kill_switch_blocks_entries_and_flattens_sync():
    with tmpdir() as tmp:
        rig = SyncRig(tmp, bars={"AAPL": closed_bars(FRESH)})
        rig.track("MSFT", 500)
        rig.ib.pnl.dailyPnL = -40_000.0          # limit is -3% of 1,000,000 = -30,000
        rig.run_iterations(2)
        assert rig.rm.halted, "kill switch did not trip"
        assert rig.ib.buys() == [], "entry placed while the kill switch was active"
        sells = rig.ib.market_sells("MSFT")
        assert len(sells) == 1 and sells[0].totalQuantity == 500, \
            "flatten_on_kill should market-sell the tracked position exactly once"


def test_kill_switch_blocks_entries_async():
    async def go(tmp):
        rig = AsyncRig(tmp, bars={"AAPL": closed_bars(FRESH)})
        rig.ib.pnl.dailyPnL = -40_000.0
        await rig.run_for(0.3)
        assert rig.trader.last_error is None, rig.trader.last_error
        assert rig.rm.halted and rig.ib.buys() == []
    with tmpdir() as tmp:
        asyncio.run(go(tmp))


def test_kill_switch_latches_and_resets_next_day():
    with tmpdir() as tmp:
        rm = make_rm(make_cfg(tmp))
        today = dt.date(2026, 9, 23)
        rm.update_daily_pnl(-30_001, today)
        assert not rm.can_open(today)[0]
        rm.update_daily_pnl(+5_000, today)       # recovers intraday: stays halted
        assert rm.halted and not rm.can_open(today)[0]
        assert rm.can_open(today + dt.timedelta(days=1))[0]


@known_bug("H3")
def test_h3_no_pnl_data_must_not_allow_entries():
    """reqPnL never delivers (NaN) and NetLiq is down 10%: the kill switch
    cannot be evaluated, so the loop must not open new positions."""
    with tmpdir() as tmp:
        rig = SyncRig(tmp, bars={"AAPL": closed_bars(FRESH)})
        rig.ib.pnl.dailyPnL = float("nan")
        rig.ib.account_values[0].value = "900000"
        rig.run_iterations(1)
        assert rig.ib.buys() == [], \
            "entry placed with no daily P&L available (kill switch silently off)"


def test_max_trades_per_day_enforced_in_loop():
    with tmpdir() as tmp:
        syms = ["AAPL", "MSFT", "NVDA"]
        rig = SyncRig(tmp, bars={s: closed_bars(FRESH) for s in syms}, symbols=syms,
                      risk={"max_trades_per_day": 2, "max_open_positions": 10})
        for s in syms:
            rig.evaluate(s)
        assert len(rig.ib.buys()) == 2, f"expected 2 entries, got {len(rig.ib.buys())}"
        assert "blocked" in rig.journal_events("NVDA")


def test_max_open_positions_counts_existing_holdings():
    with tmpdir() as tmp:
        rig = SyncRig(tmp, bars={"AAPL": closed_bars(FRESH)},
                      risk={"max_open_positions": 2})
        rig.ib.holdings.update({"XOM": 10, "IBM": 10})
        rig.run_iterations(1)
        assert rig.ib.buys() == []


@known_bug("H1")
def test_h1_max_open_positions_counts_same_poll_entries():
    with tmpdir() as tmp:
        syms = ["AAPL", "MSFT", "NVDA"]
        rig = SyncRig(tmp, bars={s: closed_bars(FRESH) for s in syms}, symbols=syms,
                      risk={"max_open_positions": 2, "max_trades_per_day": 10})
        for s in syms:
            rig.evaluate(s)
        n = len(rig.ib.buys())
        assert n <= 2, f"{n} entries placed in one poll with max_open_positions=2"


def test_risk_manager_caps_unit():
    with tmpdir() as tmp:
        cfg = make_cfg(tmp, risk={"max_trades_per_day": 2, "max_open_positions": 1})
        rm, d = make_rm(cfg), dt.date(2026, 9, 23)
        rm.note_entry(d)
        rm.sync_open_positions(1, d)
        ok, why = rm.can_open(d)
        assert not ok and "max_open_positions" in why
        rm.sync_open_positions(0, d)
        rm.note_entry(d)
        ok, why = rm.can_open(d)
        assert not ok and "max_trades_per_day" in why
        rm.note_rejected(d)                      # refund of a never-filled entry
        assert rm.can_open(d)[0]


# ============================================================================
# 3. Bracket orders: every entry is parent + TP + stop, linked, transmit-last
# ============================================================================
def _assert_bracket(ib, qty, entry, tp_px, stop_px):
    orders = [o for _, o in ib.orders]
    assert len(orders) == 3, f"expected 3 orders (parent/TP/stop), got {len(orders)}"
    parent, tp, stop = orders
    assert (parent.action, parent.orderType, parent.lmtPrice) == ("BUY", "LMT", entry)
    assert parent.parentId == 0 and parent.transmit is False
    assert (tp.action, tp.orderType, tp.lmtPrice) == ("SELL", "LMT", tp_px)
    assert (stop.action, stop.orderType, stop.auxPrice) == ("SELL", "STP", stop_px)
    # IB treats children of one parent as a single OCA group.
    assert tp.parentId == parent.orderId and stop.parentId == parent.orderId
    # Only the LAST leg transmits, so nothing goes live until all three are in.
    assert tp.transmit is False and stop.transmit is True
    assert all(o.totalQuantity == qty and o.tif == "DAY" for o in orders)
    assert stop_px < entry < tp_px


def test_every_entry_is_a_linked_bracket_sync():
    with tmpdir() as tmp:
        rig = SyncRig(tmp)
        res = rig.executor.enter_long("AAPL", 100.0, 10, dt.date.today())
        assert res["placed"]
        _assert_bracket(rig.ib, 10, 100.0, 104.0, 98.0)


def test_every_entry_is_a_linked_bracket_async():
    with tmpdir() as tmp:
        rig = AsyncRig(tmp)
        res = asyncio.run(rig.executor.enter_long("AAPL", 100.0, 10, dt.date.today()))
        assert res["placed"]
        _assert_bracket(rig.ib, 10, 100.0, 104.0, 98.0)


def test_live_loop_entry_goes_out_as_bracket():
    with tmpdir() as tmp:
        rig = SyncRig(tmp, bars={"AAPL": closed_bars(FRESH)})
        rig.evaluate()
        _assert_bracket(rig.ib, 1000, 100.0, 104.0, 98.0)   # 10% of 1M / $100


def test_executor_places_nothing_when_blocked_or_zero_size():
    with tmpdir() as tmp:
        for rig in (SyncRig(tmp), AsyncRig(tmp)):
            def enter(*a):
                r = rig.executor.enter_long(*a)
                return asyncio.run(r) if asyncio.iscoroutine(r) else r
            assert not enter("AAPL", 100.0, 0, dt.date.today())["placed"]
            rig.rm.halted = True
            rig.rm._day = dt.date.today()
            assert not enter("AAPL", 100.0, 10, dt.date.today())["placed"]
            assert rig.ib.orders == []


def test_only_executors_send_entries_and_flatten_only_sells():
    """Static check: placeOrder appears only in the executors (brackets) and in
    Broker/AsyncBroker.flatten (a SELL MarketOrder). No route or script sends
    orders directly."""
    allowed = {"src/executor.py", "src/async_executor.py",
               "src/broker.py", "src/async_broker.py"}
    files = glob.glob(os.path.join(ROOT, "src", "**", "*.py"), recursive=True)
    files += glob.glob(os.path.join(ROOT, "run_*.py"))
    offenders = []
    for path in files:
        rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if "placeOrder(" not in line:
                    continue
                if rel not in allowed:
                    offenders.append(f"{rel}:{n}")
                elif "broker" in rel and 'MarketOrder("SELL"' not in line:
                    offenders.append(f"{rel}:{n} (non-SELL order in broker)")
    assert not offenders, f"unexpected order paths: {offenders}"


# ============================================================================
# 4. Equity: zero / missing / CAD base currency
# ============================================================================
def test_cad_base_currency_equity_regression():
    with tmpdir() as tmp:
        cfg = make_cfg(tmp)                      # account.currency: CAD
        b = Broker(cfg)
        b.ib, b.account = FakeIB(), "DU1234567"
        b.ib.account_values = [
            SimpleNamespace(tag="NetLiquidationByCurrency", currency="BASE", value="1000123.45"),
            SimpleNamespace(tag="NetLiquidationByCurrency", currency="USD", value="0.00"),
            SimpleNamespace(tag="NetLiquidation", currency="CAD", value="1000123.45"),
        ]
        assert b.equity() == 1000123.45
        with no_time_sleep():
            assert b.equity_or_raise(retries=2, delay=0) == 1000123.45


def test_missing_or_zero_equity_raises_at_startup():
    with tmpdir() as tmp:
        cfg = make_cfg(tmp)
        for values in ([], [SimpleNamespace(tag="NetLiquidation", currency="CAD", value="0")]):
            b = Broker(cfg)
            b.ib, b.account = FakeIB(), "DU1234567"
            b.ib.account_values = values
            assert b.equity() == 0.0
            with no_time_sleep():
                expect_raises(SafetyError, b.equity_or_raise, retries=3, delay=0)
            ab = AsyncBroker(cfg)
            ab.ib, ab.account = b.ib, "DU1234567"
            try:
                asyncio.run(ab.equity_or_raise(retries=2, delay=0))
            except SafetyError:
                pass
            else:
                raise AssertionError("AsyncBroker.equity_or_raise accepted zero equity")


@known_bug("M3")
def test_m3_equity_retry_lets_ib_deliver_account_values():
    """Account values only arrive while ib_async's event loop runs. The sync
    retry uses time.sleep, which blocks that loop, so retries can never succeed."""
    with tmpdir() as tmp:
        b = Broker(make_cfg(tmp))
        b.ib, b.account = FakeIB(), "DU1234567"
        arriving = list(b.ib.account_values)
        b.ib.account_values = []
        b.ib.values_arrive_on_sleep = arriving   # only an ib.sleep() pumps them in
        with no_time_sleep():
            try:
                eq = b.equity_or_raise(retries=3, delay=0)
            except SafetyError:
                eq = None
        assert eq == 1_000_000.0, "retry loop never pumped the IB event loop"


def test_zero_start_equity_fails_closed():
    with tmpdir() as tmp:
        rm = make_rm(make_cfg(tmp), start_equity=0.0)
        ok, why = rm.can_open(dt.date.today())
        assert not ok and "KILL SWITCH" in why


def test_zero_equity_mid_session_places_no_order():
    with tmpdir() as tmp:
        rig = SyncRig(tmp, bars={"AAPL": closed_bars(FRESH)})
        rig.ib.account_values = []               # equity read comes back 0.0
        rig.evaluate()
        assert rig.ib.orders == []


@known_bug("M2")
def test_m2_zero_equity_signal_is_not_silently_dropped():
    with tmpdir() as tmp:
        rig = SyncRig(tmp, bars={"AAPL": closed_bars(FRESH)})
        rig.ib.account_values = []
        rig.evaluate()
        assert rig.journal_events("AAPL"), \
            "fresh entry signal consumed with equity=0 and nothing journaled"


# ============================================================================
# 5. Live loop: once per bar, closed bars only, stale bars
# ============================================================================
def test_once_per_bar_entry_sync():
    with tmpdir() as tmp:
        rig = SyncRig(tmp, bars={"AAPL": closed_bars(FRESH)})
        rig.evaluate()
        rig.evaluate()
        rig.evaluate()
        assert len(rig.ib.buys()) == 1


def test_once_per_bar_entry_async():
    async def go(tmp):
        rig = AsyncRig(tmp, bars={"AAPL": closed_bars(FRESH)})
        for _ in range(3):
            await rig.evaluate()
        assert len(rig.ib.buys()) == 1
    with tmpdir() as tmp:
        asyncio.run(go(tmp))


def test_once_per_bar_exit_sync():
    with tmpdir() as tmp:
        rig = SyncRig(tmp, bars={"AAPL": closed_bars(EXIT)})
        rig.track("AAPL", 1000)
        rig.ib.fill_market_sells = False         # still held on the second look
        rig.evaluate()
        rig.evaluate()
        assert len(rig.ib.market_sells("AAPL")) == 1


def test_forming_bar_ignored_sync_and_async():
    with tmpdir() as tmp:
        df = make_bars(FRESH, now_ny() - pd.Timedelta(minutes=2))   # 3 min left
        rig = SyncRig(tmp, bars={"AAPL": df})
        rig.evaluate()
        assert rig.ib.orders == [], "sync loop acted on a still-forming bar"
        arig = AsyncRig(tmp, bars={"AAPL": df})
        asyncio.run(arig.evaluate())
        assert arig.ib.orders == [], "async loop acted on a still-forming bar"


def test_forming_bar_final_seconds_ignored_sync():
    with tmpdir() as tmp:
        df = make_bars(FRESH, now_ny() - pd.Timedelta(minutes=4, seconds=30))
        rig = SyncRig(tmp, bars={"AAPL": df})
        rig.evaluate()
        assert rig.ib.orders == []


@known_bug("H2")
def test_h2_forming_bar_final_minute_ignored_async():
    with tmpdir() as tmp:
        df = make_bars(FRESH, now_ny() - pd.Timedelta(minutes=4, seconds=30))  # 30 s left
        rig = AsyncRig(tmp, bars={"AAPL": df})
        asyncio.run(rig.evaluate())
        assert rig.ib.buys() == [], \
            "async loop entered on a bar with 30 s still to run (treated as closed)"


@known_bug("C1")
def test_c1_previous_session_bar_not_traded_at_open_sync():
    with tmpdir() as tmp:
        rig = SyncRig(tmp, bars={"AAPL": prev_session_bars(FRESH)}, clock=(True, 389))
        rig.evaluate()
        assert rig.ib.buys() == [], \
            "entered at the open on yesterday's 15:55 bar (stale price & signal)"


@known_bug("C1")
def test_c1_previous_session_bar_not_traded_at_open_async():
    with tmpdir() as tmp:
        rig = AsyncRig(tmp, bars={"AAPL": prev_session_bars(FRESH)}, clock=(True, 389))
        asyncio.run(rig.evaluate())
        assert rig.ib.buys() == [], \
            "entered at the open on yesterday's 15:55 bar (stale price & signal)"


@known_bug("H6")
def test_h6_sync_loop_uses_per_symbol_strategy():
    """live.symbols assigns AAPL a strategy; the CLI loop must use it, like the GUI."""
    with tmpdir() as tmp:
        rig = SyncRig(tmp, bars={"AAPL": closed_bars(FRESH)}, strategy="test_never_long")
        rig.evaluate()
        assert len(rig.ib.buys()) == 1, \
            "sync LiveTrader ignored live.symbols.AAPL.strategy and used strategy.name"


def test_async_loop_uses_per_symbol_strategy():
    with tmpdir() as tmp:
        rig = AsyncRig(tmp, bars={"AAPL": closed_bars(FRESH)}, strategy="test_never_long")
        asyncio.run(rig.evaluate())
        assert len(rig.ib.buys()) == 1


# ============================================================================
# 6. Market hours
# ============================================================================
def test_market_closed_no_evaluation_or_orders_sync():
    with tmpdir() as tmp:
        rig = SyncRig(tmp, bars={"AAPL": closed_bars(FRESH)}, clock=(False, 0))
        rig.run_iterations(3)
        assert rig.historical_calls == 0 and rig.ib.orders == []


def test_market_closed_no_orders_async():
    async def go(tmp):
        rig = AsyncRig(tmp, bars={"AAPL": closed_bars(FRESH)}, clock=(False, 0))
        await rig.run_for(0.3)
        assert rig.trader.last_error is None, rig.trader.last_error
        assert rig.ib.orders == [] and rig.trader.open == {}
    with tmpdir() as tmp:
        asyncio.run(go(tmp))


def test_near_close_blocks_entries_and_flattens_once():
    with tmpdir() as tmp:
        rig = SyncRig(tmp, bars={"AAPL": closed_bars(FRESH)}, clock=(True, 5))
        rig.track("MSFT", 300)
        rig.run_iterations(2)
        assert rig.ib.buys() == []
        assert [o.totalQuantity for o in rig.ib.market_sells("MSFT")] == [300]


def test_market_clock_regular_hours():
    with tmpdir() as tmp:
        b = Broker(make_cfg(tmp))
        cases = [((2026, 9, 23, 10, 0), (True, 360)), ((2026, 9, 23, 15, 55), (True, 5)),
                 ((2026, 9, 23, 16, 0), (False, 0)), ((2026, 9, 23, 9, 29), (False, 0)),
                 ((2026, 9, 26, 11, 0), (False, 0))]            # Saturday
        for when, expected in cases:
            with frozen_ny_clock(*when):
                got = b.market_clock()
            assert got == expected, f"{when}: expected {expected}, got {got}"


@known_bug("H7")
def test_h7_market_clock_knows_holidays_and_half_days():
    with tmpdir() as tmp:
        b = Broker(make_cfg(tmp))
        problems = []
        with frozen_ny_clock(2026, 11, 26, 10, 0):              # Thanksgiving
            if b.market_clock()[0]:
                problems.append("Thanksgiving 2026-11-26 reported OPEN")
        with frozen_ny_clock(2026, 11, 27, 12, 0):              # 13:00 early close
            if b.market_clock() != (True, 60):
                problems.append(f"2026-11-27 12:00 half-day -> {b.market_clock()}, "
                                f"expected (True, 60)")
        with frozen_ny_clock(2026, 12, 24, 13, 30):             # 13:00 early close
            if b.market_clock()[0]:
                problems.append("2026-12-24 13:30 (after early close) reported OPEN")
        assert not problems, "; ".join(problems)


# ============================================================================
# 7. Disconnect / reconcile / flatten
# ============================================================================
def _disconnected_rig(tmp):
    rig = SyncRig(tmp, bars={"AAPL": ConnectionError("Not connected")})
    rig.track("AAPL", 1000)
    rig.broker._pnl = rig.ib.pnl                 # P&L was subscribed before the drop
    rig.ib.connected = False
    rig.ib.holdings = {}                         # ib_async wrapper.reset() on socket loss
    return rig


def test_disconnect_sends_no_orders():
    with tmpdir() as tmp:
        rig = _disconnected_rig(tmp)
        rig.run_iterations(2)
        assert rig.ib.orders == [], "order attempted while disconnected"


@known_bug("C3")
def test_c3_disconnect_does_not_forget_open_positions():
    with tmpdir() as tmp:
        rig = _disconnected_rig(tmp)
        rig.run_iterations(2)
        assert "AAPL" in rig.trader.open, \
            "position dropped from tracking because the portfolio feed went empty on " \
            "disconnect (journaled as 'closed'); EOD flatten will now skip it"


@known_bug("C3")
def test_c3_unfilled_entry_bracket_not_cancelled_by_reconcile():
    with tmpdir() as tmp:
        rig = SyncRig(tmp, bars={"AAPL": closed_bars(FRESH)})
        rig.evaluate()                           # bracket placed; parent still working
        assert len(rig.ib.buys()) == 1
        rig.trader._reconcile_closed()           # next poll: portfolio still shows 0
        problems = []
        if rig.ib.cancelled:
            problems.append(f"cancelled {len(rig.ib.cancelled)} legs of a working bracket")
        if "AAPL" not in rig.trader.open:
            problems.append("stopped tracking the pending entry")
        assert not problems, "; ".join(problems)


@known_bug("C2")
def test_c2_exit_signal_does_not_sell_untracked_position_sync():
    with tmpdir() as tmp:
        rig = SyncRig(tmp, bars={"AAPL": closed_bars(EXIT)})
        rig.ib.holdings["AAPL"] = 100            # shares the bot did not buy
        rig.evaluate()
        assert rig.ib.market_sells() == [], \
            "strategy exit market-sold 100 AAPL the loop never bought"


@known_bug("C2")
def test_c2_exit_signal_does_not_sell_untracked_position_async():
    with tmpdir() as tmp:
        rig = AsyncRig(tmp, bars={"AAPL": closed_bars(EXIT)})
        rig.ib.holdings["AAPL"] = 100
        asyncio.run(rig.evaluate())
        assert rig.ib.market_sells() == [], \
            "strategy exit market-sold 100 AAPL the loop never bought"


@known_bug("C4")
def test_c4_flatten_does_not_stack_market_sells():
    with tmpdir() as tmp:
        rig = SyncRig(tmp, clock=(True, 5))      # inside the pre-close flatten window
        rig.track("AAPL", 1000)
        rig.ib.fill_market_sells = False         # e.g. halted stock / order still working
        rig.run_iterations(3)
        sells = rig.ib.market_sells("AAPL")
        total = sum(o.totalQuantity for o in sells)
        assert total <= 1000, (f"{len(sells)} market SELLs totalling {total} shares "
                               f"against a 1000-share long -> ends short if they fill")


# ============================================================================
# 8. Position sizing
# ============================================================================
def test_position_size_edge_cases():
    assert position_size(100_000, 200, 0.10, 0.02)["shares"] == 50
    assert position_size(100_000, 100, 0.10, 0.02)["shares"] == 100       # exactly at cap
    assert position_size(100_000, 333.33, 0.10, 0.02)["shares"] == 30     # floors
    assert position_size(1_000, 500, 0.10, 0.02)["shares"] == 0           # price > cap
    assert position_size(100_000, 50, 1.0, 0.02)["shares"] == 2000
    for eq, px in [(0, 100), (-5, 100), (100_000, 0), (100_000, -1)]:
        r = position_size(eq, px, 0.10, 0.02)
        assert r == {"shares": 0, "notional": 0.0, "dollar_risk": 0.0}, (eq, px, r)
    # Non-finite inputs must never produce a positive size (raising is acceptable).
    for eq, px in [(100_000, float("nan")), (float("nan"), 100),
                   (100_000, float("inf")), (float("inf"), 100)]:
        try:
            shares = position_size(eq, px, 0.10, 0.02)["shares"]
        except (ValueError, OverflowError):
            continue
        assert shares == 0, (eq, px, shares)


def test_position_size_never_exceeds_cap():
    rng = random.Random(7)
    for _ in range(5000):
        eq = rng.uniform(1, 5_000_000)
        px = rng.uniform(0.01, 5_000)
        pct = rng.uniform(0.001, 1.0)
        r = position_size(eq, px, pct, 0.02)
        assert isinstance(r["shares"], int) and r["shares"] >= 0
        assert r["notional"] <= eq * pct + 1e-6, (eq, px, pct, r)
        assert (r["shares"] + 1) * px > eq * pct - 1e-6, "left a whole share unused"


# ============================================================================
# 9. Backtester: next-bar execution, no look-ahead
# ============================================================================
def _bt(df, target, **kw):
    args = dict(starting_equity=100_000, stop_pct=0.02, take_profit_pct=0.5,
                max_position_pct=0.10, commission_per_share=0.0, slippage_bps=0.0,
                intraday_only=False)
    args.update(kw)
    return run_backtest(df, target, **args)


def test_backtester_signal_executes_next_bar_open():
    idx = pd.date_range("2024-06-03 09:30", periods=10, freq="5min")
    opens = np.arange(100.0, 110.0)
    df = pd.DataFrame({"open": opens, "high": opens + 0.05, "low": opens - 0.05,
                       "close": opens + 0.02, "volume": 1000}, index=idx)
    target = pd.Series([0, 0, 0, 1, 1, 1, 0, 0, 0, 0], index=idx)
    res = _bt(df, target)
    assert len(res.trades) == 1, res.trades
    t = res.trades[0]
    assert t.entry_time == idx[4] and t.entry_price == opens[4], "entry not at next open"
    assert t.exit_time == idx[7] and t.exit_price == opens[7] and t.reason == "signal"
    # A flip on the final bar has no next bar to trade on.
    last_only = pd.Series([0] * 9 + [1], index=idx)
    assert _bt(df, last_only).trades == []


def test_backtester_future_bars_do_not_change_the_past():
    from src.strategies import build_strategy
    rng = np.random.default_rng(3)
    idx = pd.date_range("2024-06-03 09:30", periods=240, freq="5min")

    def walk(seed_rets):
        c = 100 * np.cumprod(1 + seed_rets)
        return pd.DataFrame({"open": c, "high": c * 1.001, "low": c * 0.999,
                             "close": c, "volume": 1000}, index=idx)
    rets = rng.normal(0, 0.003, len(idx))
    df1 = walk(rets)
    k = 150
    rets2 = rets.copy()
    rets2[k:] = rng.normal(0, 0.01, len(idx) - k)
    df2 = walk(rets2)
    strat = build_strategy("ma_crossover", {"fast": 5, "slow": 13})
    r1 = _bt(df1, strat.generate(df1)["target"], take_profit_pct=0.04)
    r2 = _bt(df2, strat.generate(df2)["target"], take_profit_pct=0.04)
    assert r1.equity_curve.iloc[:k].equals(r2.equity_curve.iloc[:k]), \
        "equity before bar k changed when only bars >= k changed (look-ahead)"
    closed1 = [(t.entry_time, t.exit_time, t.pnl) for t in r1.trades if t.exit_time < idx[k]]
    closed2 = [(t.entry_time, t.exit_time, t.pnl) for t in r2.trades if t.exit_time < idx[k]]
    assert closed1 == closed2


@known_bug("M4")
def test_m4_backtester_no_reentry_in_the_bar_that_stopped_out():
    idx = pd.date_range("2024-06-03 09:30", periods=8, freq="5min")
    o = np.full(8, 100.0)
    low = o - 0.1
    low[3] = 97.0                                # bar 3 trades through the 98 stop
    df = pd.DataFrame({"open": o, "high": o + 0.1, "low": low, "close": o,
                       "volume": 1000}, index=idx)
    # Target drops at the end so the re-entered trade closes and gets recorded.
    target = pd.Series([0, 1, 1, 1, 1, 1, 0, 0], index=idx)
    res = _bt(df, target, take_profit_pct=0.04)
    stop_exits = {t.exit_time for t in res.trades if t.reason in ("stop", "take_profit")}
    reentries = [t.entry_time for t in res.trades if t.entry_time in stop_exits]
    assert not reentries, (f"re-entered at the OPEN of bar {reentries[0]} after being "
                           f"stopped out later in that same bar (fill before the exit)")


# ============================================================================
# 10. Web GUI: cross-site requests
# ============================================================================
async def _asgi_post(app, path, headers):
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": b"", "root_path": "",
             "headers": [(k.encode(), v.encode()) for k, v in headers],
             "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8000)}
    sent_body = False
    messages = []

    async def receive():
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(msg):
        messages.append(msg)
    await app(scope, receive, send)
    return next(m["status"] for m in messages if m["type"] == "http.response.start")


@known_bug("H5")
def test_h5_gui_rejects_cross_site_loop_start():
    """A page on any other site can fire a no-CORS POST at 127.0.0.1:8000. The
    browser sends it without a preflight; it must not start the live loop."""
    from src.web import state as web_state
    from src.web.app import create_app
    started = []

    class StubTrader:
        status, _task = "stopped", None

        async def start(self):
            started.append(1)
            self.status = "running"

    async def go(path):
        state = web_state.init_state(path)
        state.connected, state.trader = True, StubTrader()
        app = create_app(path)
        return await _asgi_post(app, "/api/loop/start",
                                [("host", "127.0.0.1:8000"),
                                 ("origin", "https://evil.example"),
                                 ("content-type", "text/plain")])

    with tmpdir() as tmp:
        make_cfg(tmp)
        status = asyncio.run(go(os.path.join(tmp, "config.yaml")))
    assert not started, f"cross-site POST started the live loop (HTTP {status})"


# ============================================================================
# Runner
# ============================================================================
def main() -> int:
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    print(f"Safety tests ({len(tests)})\n")
    ok = known = unexpected = fixed = 0
    for name, fn in tests:
        bug = getattr(fn, "known_bug", None)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                fn()
        except Exception as e:
            msg = str(e).splitlines()[0] if str(e) else type(e).__name__
            if bug:
                known += 1
                print(f"  FAIL [known bug {bug:3s}] {name}\n        {msg}")
            else:
                unexpected += 1
                print(f"  FAIL [UNEXPECTED]    {name}\n        {type(e).__name__}: {msg}")
                print(textwrap_indent(traceback.format_exc()))
                print(textwrap_indent("captured output (tail):\n"
                                      + "\n".join(buf.getvalue().splitlines()[-15:])))
        else:
            if bug:
                fixed += 1
                print(f"  PASS [bug {bug} FIXED?] {name}  <- remove @known_bug and "
                      f"update REVIEW.md")
            else:
                ok += 1
                print(f"  PASS                 {name}")
    print(f"\n{ok} passed, {known} failed on known bugs (REVIEW.md), "
          f"{unexpected} unexpected failures, {fixed} known-bug tests now passing")
    return 0 if (known == 0 and unexpected == 0) else 1


def textwrap_indent(s: str) -> str:
    return "\n".join("        " + line for line in s.splitlines())


if __name__ == "__main__":
    sys.exit(main())
