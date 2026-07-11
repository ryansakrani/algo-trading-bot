#!/usr/bin/env python3
"""Run the live auto-trading loop (feature 4).

Usage:
    python run_live.py            # asks you to confirm before it starts
    python run_live.py --yes      # skip the confirmation (use with care)

Requires TWS / IB Gateway running with the API enabled. Keep mode: paper.
"""
import argparse
import datetime as dt
from src.config import load_config
from src.broker import Broker
from src.risk import RiskManager
from src.journal import Journal
from src.executor import Executor
from src.live import LiveTrader


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--yes", action="store_true", help="skip the start confirmation")
    a = p.parse_args()

    cfg = load_config()
    broker = Broker(cfg).connect()   # paper/live guard runs here
    journal = Journal(cfg.journal.path)
    print(f"Account equity read as: {broker.equity()}")
    eq = broker.equity_or_raise()
    rm = RiskManager(
        start_equity=eq,
        max_position_pct=cfg.risk.max_position_pct,
        per_trade_stop_pct=cfg.risk.per_trade_stop_pct,
        take_profit_pct=cfg.risk.take_profit_pct,
        daily_max_loss_pct=cfg.risk.daily_max_loss_pct,
        max_open_positions=cfg.risk.max_open_positions,
        max_trades_per_day=cfg.risk.max_trades_per_day,
    )
    executor = Executor(broker, rm, journal, cfg)
    trader = LiveTrader(broker, rm, executor, journal, cfg)

    print("=" * 64)
    print(f"  AUTO-TRADING LOOP")
    print(f"  Mode      : {broker.mode.upper()}   Account: {broker.account}")
    print(f"  Equity    : {eq:,.2f} {cfg.account.currency}")
    print(f"  Strategy  : {cfg.strategy.name}  params={dict(cfg.strategy.params)}")
    print(f"  Symbols   : {cfg.live.symbols}")
    print(f"  Risk      : pos<= {cfg.risk.max_position_pct*100:.0f}% | "
          f"stop {cfg.risk.per_trade_stop_pct*100:.1f}% | "
          f"tp {cfg.risk.take_profit_pct*100:.1f}% | "
          f"kill at -{cfg.risk.daily_max_loss_pct*100:.1f}% "
          f"({-eq*cfg.risk.daily_max_loss_pct:,.0f})")
    print(f"  Caps      : {cfg.risk.max_trades_per_day} trades/day, "
          f"{cfg.risk.max_open_positions} open")
    print("=" * 64)

    if broker.mode != "paper":
        print("WARNING: this is NOT a paper account. This loop will trade REAL money.")

    try:
        if not a.yes:
            ans = input("Type 'start' to begin auto-trading: ").strip().lower()
            if ans != "start":
                print("Not started.")
                return
        trader.run()
    finally:
        broker.disconnect()


if __name__ == "__main__":
    main()
