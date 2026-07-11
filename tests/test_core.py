#!/usr/bin/env python3
"""Offline sanity tests for the pure-logic core (no network needed).

Validates: indicators, every strategy produces clean 0/1 targets, the
backtester runs and produces coherent stats, and the risk manager's sizing +
kill switch behave correctly. Run:  python tests/test_core.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
import datetime as dt

from src import indicators as ind
from src.strategies import build_strategy, available
from src.backtester import run_backtest
from src.risk import position_size, bracket_prices, RiskManager


def synthetic(days=5, bars_per_day=78, seed=1):
    """Build intraday OHLCV with a DatetimeIndex (5-min bars, ~6.5h sessions)."""
    rng = np.random.default_rng(seed)
    rows, idx = [], []
    price = 100.0
    start = pd.Timestamp("2024-01-02 09:30")
    for d in range(days):
        day0 = start + pd.Timedelta(days=d)
        for b in range(bars_per_day):
            ts = day0 + pd.Timedelta(minutes=5 * b)
            drift = 0.02 * np.sin((d * bars_per_day + b) / 15.0)
            ret = rng.normal(drift, 0.25) / 100.0
            o = price
            c = max(1.0, o * (1 + ret))
            h = max(o, c) * (1 + abs(rng.normal(0, 0.001)))
            l = min(o, c) * (1 - abs(rng.normal(0, 0.001)))
            v = rng.integers(1000, 5000)
            rows.append((o, h, l, c, v)); idx.append(ts)
            price = c
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"],
                      index=pd.DatetimeIndex(idx))
    return df


def test_indicators(df):
    assert ind.ema(df["close"], 9).notna().sum() > 0
    r = ind.rsi(df["close"], 14).dropna()
    assert ((r >= 0) & (r <= 100)).all(), "RSI out of [0,100]"
    lo, mid, up = ind.bollinger(df["close"], 20, 2)
    valid = mid.dropna().index
    assert (lo.loc[valid] <= up.loc[valid]).all(), "Bollinger lower > upper"
    vw = ind.session_vwap(df).dropna()
    assert (vw > 0).all(), "VWAP must be positive"
    oh, ol = ind.opening_range(df, 30)
    assert oh.dropna().ge(ol.dropna()).all(), "OR high < OR low"
    print("  indicators ............ OK")


def test_strategies(df):
    for name in available():
        s = build_strategy(name, {"fast": 9, "slow": 21, "opening_minutes": 30,
                                  "rsi_period": 14, "oversold": 30, "overbought": 70})
        out = s.generate(df)
        assert "target" in out.columns
        assert set(out["target"].unique()).issubset({0, 1}), f"{name}: target not 0/1"
        assert len(out) == len(df)
        print(f"  strategy {name:14s} OK  (long bars: {int(out['target'].sum())})")


def test_backtester(df):
    s = build_strategy("ma_crossover", {"fast": 9, "slow": 21})
    sig = s.generate(df)
    res = run_backtest(df, sig["target"], starting_equity=100000,
                       stop_pct=0.02, take_profit_pct=0.04, max_position_pct=0.10,
                       commission_per_share=0.005, slippage_bps=1.0, intraday_only=True)
    st = res.stats
    assert st["trades"] >= 0
    assert res.equity_curve.notna().all(), "NaN in equity curve"
    assert res.equity_curve.iloc[0] > 0
    # no overnight holds when intraday_only=True: every trade exits same calendar day
    for t in res.trades:
        if t.exit_time is not None:
            assert t.entry_time.normalize() == t.exit_time.normalize(), \
                "intraday_only violated: trade held overnight"
    print(f"  backtester ............ OK  (trades={st['trades']}, "
          f"ret={st['total_return_pct']}%, win={st['win_rate']}%, "
          f"maxDD={st['max_drawdown_pct']}%)")


def test_risk():
    sz = position_size(equity=100000, price=200, max_position_pct=0.10, stop_pct=0.02)
    assert sz["shares"] == 50, sz                      # 10000 / 200
    assert abs(sz["notional"] - 10000) < 1e-6
    assert abs(sz["dollar_risk"] - 200) < 1e-6         # 10000 * 2%
    bp = bracket_prices(100, 0.02, 0.04)
    assert bp["stop"] == 98.0 and bp["take_profit"] == 104.0 and bp["reward_risk"] == 2.0

    rm = RiskManager(start_equity=100000, max_position_pct=0.10,
                     per_trade_stop_pct=0.02, take_profit_pct=0.04,
                     daily_max_loss_pct=0.03, max_open_positions=3,
                     max_trades_per_day=6)
    today = dt.date(2024, 1, 2)
    ok, _ = rm.can_open(today); assert ok
    # Trip the kill switch: lose more than 3% (-3000) in realized P&L.
    rm.record_fill(today); rm.record_close(-3500, today)
    ok, reason = rm.can_open(today)
    assert not ok and "KILL SWITCH" in reason, reason
    # New day resets the halt.
    ok2, _ = rm.can_open(dt.date(2024, 1, 3)); assert ok2
    print("  risk manager .......... OK  (sizing + kill switch verified)")


def test_live_helpers(df):
    # New RiskManager helpers used by the live loop.
    rm = RiskManager(start_equity=100000, max_position_pct=0.10,
                     per_trade_stop_pct=0.02, take_profit_pct=0.04,
                     daily_max_loss_pct=0.03, max_open_positions=3,
                     max_trades_per_day=6)
    today = dt.date(2024, 1, 2)
    rm.note_entry(today); rm.note_entry(today)
    assert rm.trades_today == 2 and rm.open_positions == 0  # note_entry leaves positions
    rm.sync_open_positions(2, today); assert rm.open_positions == 2
    rm.update_daily_pnl(-3500, today)          # past -3000 kill level
    ok, reason = rm.can_open(today)
    assert not ok and "KILL SWITCH" in reason
    rm.update_daily_pnl(-1000, today)          # back above level, but halt latches same day
    assert rm.halted is True

    # LiveTrader pure helpers (no broker needed) via lightweight stubs.
    from src.config import load_config
    from src.live import LiveTrader
    cfg = load_config()
    lt = LiveTrader(broker=None, risk_manager=rm, executor=None, journal=None, cfg=cfg)
    import pandas as pd
    flip = pd.Series([0, 0, 1])      # fresh entry on last bar
    noflip = pd.Series([1, 1, 1])
    assert lt._fresh_entry(flip) is True
    assert lt._fresh_entry(noflip) is False
    assert lt._exit_signal(pd.Series([1, 0])) is True
    assert lt._exit_signal(pd.Series([1, 1])) is False
    # _latest_closed should not drop bars from clearly-historical data
    assert len(lt._latest_closed(df)) in (len(df), len(df) - 1)
    print("  live helpers .......... OK  (kill-switch sync + signal edges)")


if __name__ == "__main__":
    df = synthetic()
    print(f"Synthetic data: {len(df)} bars over "
          f"{df.index.normalize().nunique()} days\n")
    test_indicators(df)
    test_strategies(df)
    test_backtester(df)
    test_risk()
    test_live_helpers(df)
    print("\nALL CORE TESTS PASSED ✅")
