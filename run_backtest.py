#!/usr/bin/env python3
"""Backtest the configured strategy on yfinance history (feature 2).

Usage:
    python run_backtest.py                  # uses config.yaml
    python run_backtest.py --symbol MSFT --strategy orb --interval 5m --period 30d
"""
import argparse
from src.config import load_config
from src.data import fetch_yf
from src.strategies import build_strategy, available
from src.backtester import run_backtest


def main():
    cfg = load_config()
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default=cfg.backtest.symbol)
    p.add_argument("--strategy", default=cfg.strategy.name, choices=available())
    p.add_argument("--period", default=cfg.backtest.period)
    p.add_argument("--interval", default=cfg.backtest.interval)
    a = p.parse_args()

    print(f"Fetching {a.symbol} {a.period}/{a.interval} from Yahoo...")
    df = fetch_yf(a.symbol, a.period, a.interval)
    strat = build_strategy(a.strategy, dict(cfg.strategy.params))
    sig = strat.generate(df)

    res = run_backtest(
        df, sig["target"],
        starting_equity=cfg.account.backtest_starting_equity,
        stop_pct=cfg.risk.per_trade_stop_pct,
        take_profit_pct=cfg.risk.take_profit_pct,
        max_position_pct=cfg.risk.max_position_pct,
        commission_per_share=cfg.backtest.commission_per_share,
        slippage_bps=cfg.backtest.slippage_bps,
        intraday_only=cfg.backtest.intraday_only,
    )
    print(f"\n=== Backtest: {a.strategy} on {a.symbol} "
          f"({len(df)} bars, {df.index[0]} -> {df.index[-1]}) ===")
    for k, v in res.stats.items():
        print(f"  {k:18s}: {v}")
    print("\nFirst few trades:")
    for t in res.trades[:8]:
        print(f"  {t.entry_time} -> {t.exit_time}  {t.shares} sh  "
              f"{t.reason:12s}  pnl {t.pnl:,.2f}")
    print("\nReminder: backtest results are NOT a promise of live performance. "
          "Slippage, fills, and data gaps differ in reality.")


if __name__ == "__main__":
    main()
