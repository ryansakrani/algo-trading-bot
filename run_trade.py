#!/usr/bin/env python3
"""On-demand trade (features 1+5+6+7 together).

Pulls IBKR live-feed bars for ONE symbol, runs the configured strategy, and if
the latest bar is a fresh entry signal, sizes the position under your risk
limits and asks you to CONFIRM before placing a bracket order. Nothing is sent
without an explicit 'yes'.

This is a manual, human-in-the-loop runner — not an always-on auto-trader. You
chose to skip the live loop, so this is how you act on a signal deliberately.

Usage:  python run_trade.py --symbol AAPL
"""
import argparse
import datetime as dt
from src.config import load_config
from src.broker import Broker
from src.risk import RiskManager, position_size, bracket_prices
from src.journal import Journal
from src.executor import Executor
from src.strategies import build_strategy


def main():
    cfg = load_config()
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", required=True)
    p.add_argument("--yes", action="store_true",
                   help="skip the confirmation prompt (not recommended)")
    a = p.parse_args()

    broker = Broker(cfg).connect()
    print(f"Connected to {broker.mode} account {broker.account}")
    journal = Journal(cfg.journal.path)
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

    try:
        df = broker.historical(a.symbol)
        if df.empty:
            print("No bars returned from IBKR. Is the market data subscription active?")
            return
        strat = build_strategy(cfg.strategy.name, dict(cfg.strategy.params))
        sig = strat.generate(df)
        tgt = sig["target"]
        last, prev = int(tgt.iloc[-1]), int(tgt.iloc[-2]) if len(tgt) > 1 else 0
        price = float(df["close"].iloc[-1])
        journal.log("signal", symbol=a.symbol, price=round(price, 2),
                    strategy=cfg.strategy.name, mode=broker.mode,
                    note=f"target={last} prev={prev}")

        print(f"\n{a.symbol}: last close {price:.2f}, target={last} (prev={prev})")
        if not (prev == 0 and last == 1):
            print("No fresh entry signal on the latest bar. Nothing to do.")
            return

        equity = broker.equity()
        sized = position_size(equity, price, cfg.risk.max_position_pct,
                              cfg.risk.per_trade_stop_pct)
        bp = bracket_prices(price, cfg.risk.per_trade_stop_pct,
                           cfg.risk.take_profit_pct)
        ok, reason = rm.can_open(dt.date.today())
        print(f"\nProposed bracket order (PAPER={broker.mode=='paper'}):")
        print(f"  BUY {sized['shares']} {a.symbol} @ ~{bp['entry']}")
        print(f"  stop {bp['stop']}  take-profit {bp['take_profit']}  "
              f"R:R {bp['reward_risk']}")
        print(f"  notional {sized['notional']:,.0f}  dollar risk "
              f"~{sized['dollar_risk']:,.0f}  ({cfg.risk.per_trade_stop_pct*100:.1f}% stop)")
        if not ok:
            print(f"\nRisk manager BLOCKS this trade: {reason}")
            return
        if sized["shares"] <= 0:
            print("\nSize rounds to 0 shares — equity too small for this price. Skipping.")
            return

        if not a.yes:
            ans = input("\nPlace this bracket order? type 'yes' to confirm: ").strip().lower()
            if ans != "yes":
                print("Cancelled. Nothing sent.")
                return

        result = executor.enter_long(a.symbol, price, sized["shares"], dt.date.today())
        if result.get("placed"):
            print(f"\nPlaced bracket: BUY {result['shares']} {a.symbol} "
                  f"@ ~{result['entry']}  stop {result['stop']}  "
                  f"tp {result['take_profit']}")
        else:
            print(f"\nNot placed: {result.get('reason')}")
    finally:
        broker.disconnect()


if __name__ == "__main__":
    main()
