#!/usr/bin/env python3
"""Live terminal dashboard (feature 9). Read-only; places no orders.

Usage:  python run_dashboard.py
Requires TWS / IB Gateway running with the API enabled (paper port by default).
"""
from src.config import load_config
from src.broker import Broker
from src.risk import RiskManager
from src.journal import Journal
from src.dashboard import run_dashboard


def main():
    cfg = load_config()
    broker = Broker(cfg).connect()
    print(f"Connected to {broker.mode} account {broker.account}")
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
    journal = Journal(cfg.journal.path)
    try:
        run_dashboard(broker, rm, journal)
    finally:
        broker.disconnect()


if __name__ == "__main__":
    main()
