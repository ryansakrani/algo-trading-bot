"""Backtester (feature 2). Event-driven over OHLCV bars. Long-only.

Key correctness choices:
  * Acts on the bar AFTER a signal change (entry/exit at next bar's open) to
    avoid look-ahead bias.
  * Models per-trade stop-loss and take-profit intrabar using each bar's
    high/low (the same bracket the live executor places). If both the stop and
    target are touched in one bar, assumes the STOP filled first (conservative).
  * Optionally flattens at the last bar of each day (day-trading: no overnight).
  * Charges commission per share and slippage in basis points on every fill.

The engine is pure pandas — no network — so it runs in CI and offline tests.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import numpy as np
import pandas as pd

from .risk import position_size


@dataclass
class Trade:
    entry_time: pd.Timestamp
    entry_price: float
    shares: int
    exit_time: pd.Timestamp = None
    exit_price: float = None
    reason: str = ""
    pnl: float = 0.0


@dataclass
class BacktestResult:
    trades: list = field(default_factory=list)
    equity_curve: pd.Series = None
    stats: dict = field(default_factory=dict)


def _apply_slippage(price: float, bps: float, side: str) -> float:
    adj = price * bps / 10_000.0
    return price + adj if side == "buy" else price - adj


def run_backtest(df: pd.DataFrame, target: pd.Series, *,
                 starting_equity: float,
                 stop_pct: float,
                 take_profit_pct: float,
                 max_position_pct: float,
                 commission_per_share: float = 0.005,
                 slippage_bps: float = 1.0,
                 intraday_only: bool = True) -> BacktestResult:
    df = df.copy()
    target = target.reindex(df.index).fillna(0).astype(int)
    # Trade on the NEXT bar -> shift the desired position forward by one.
    desired = target.shift(1).fillna(0).astype(int)

    has_dt = isinstance(df.index, pd.DatetimeIndex)
    day = (pd.Series(df.index, index=df.index).dt.normalize()
           if has_dt else pd.Series(0, index=df.index))

    equity = starting_equity
    cash = starting_equity
    in_pos = False
    shares = 0
    entry_price = stop = tp = 0.0
    cur_trade = None
    trades: list[Trade] = []
    eq_points = []

    idx = list(df.index)
    for i, ts in enumerate(idx):
        o = df["open"].iloc[i]
        h = df["high"].iloc[i]
        l = df["low"].iloc[i]
        c = df["close"].iloc[i]
        want = desired.iloc[i]
        last_bar_of_day = intraday_only and has_dt and (
            i == len(idx) - 1 or day.iloc[i + 1] != day.iloc[i])

        # ---- manage an open position: check stop/target intrabar ----
        if in_pos:
            exit_price = None
            reason = ""
            if l <= stop:                       # stop first (conservative)
                exit_price, reason = stop, "stop"
            elif h >= tp:
                exit_price, reason = tp, "take_profit"
            elif want == 0:                     # strategy says exit -> next open
                exit_price, reason = o, "signal"
            elif last_bar_of_day:
                exit_price, reason = c, "eod_flatten"

            if exit_price is not None:
                fill = _apply_slippage(exit_price, slippage_bps, "sell")
                proceeds = shares * fill - commission_per_share * shares
                cost_basis = shares * entry_price
                cur_trade.exit_time = ts
                cur_trade.exit_price = fill
                cur_trade.reason = reason
                cur_trade.pnl = proceeds - cost_basis
                cash += proceeds
                trades.append(cur_trade)
                in_pos, shares, cur_trade = False, 0, None

        # ---- consider a new entry ----
        if (not in_pos) and want == 1 and not last_bar_of_day:
            sized = position_size(equity, o, max_position_pct, stop_pct)
            if sized["shares"] > 0:
                fill = _apply_slippage(o, slippage_bps, "buy")
                shares = sized["shares"]
                cost = shares * fill + commission_per_share * shares
                cash -= cost
                entry_price = fill
                stop = entry_price * (1 - stop_pct)
                tp = entry_price * (1 + take_profit_pct)
                in_pos = True
                cur_trade = Trade(entry_time=ts, entry_price=fill, shares=shares)

        # mark-to-market equity
        mtm = cash + (shares * c if in_pos else 0.0)
        equity = mtm
        eq_points.append((ts, mtm))

    eq = pd.Series({t: v for t, v in eq_points})
    return BacktestResult(trades=trades, equity_curve=eq,
                          stats=_stats(trades, eq, starting_equity))


def _stats(trades: list, eq: pd.Series, start: float) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "total_return_pct": 0.0, "win_rate": 0.0,
                "profit_factor": 0.0, "max_drawdown_pct": 0.0, "sharpe": 0.0,
                "final_equity": float(start)}
    pnls = np.array([t.pnl for t in trades], dtype=float)
    wins = pnls[pnls > 0].sum()
    losses = -pnls[pnls < 0].sum()
    final = float(eq.iloc[-1])
    roll_max = eq.cummax()
    dd = (eq - roll_max) / roll_max
    rets = eq.pct_change().dropna()
    sharpe = float(np.sqrt(252) * rets.mean() / rets.std()) if rets.std() > 0 else 0.0
    return {
        "trades": n,
        "total_return_pct": round((final / start - 1) * 100, 2),
        "win_rate": round(float((pnls > 0).mean()) * 100, 1),
        "profit_factor": round(float(wins / losses), 2) if losses > 0 else float("inf"),
        "max_drawdown_pct": round(float(dd.min()) * 100, 2),
        "sharpe": round(sharpe, 2),
        "final_equity": round(final, 2),
        "avg_win": round(float(pnls[pnls > 0].mean()), 2) if (pnls > 0).any() else 0.0,
        "avg_loss": round(float(pnls[pnls < 0].mean()), 2) if (pnls < 0).any() else 0.0,
    }
