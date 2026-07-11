#!/usr/bin/env python3
"""Morning screener (feature 8): scan the watchlist for fresh entry signals.

Usage:  python run_screener.py
"""
from src.config import load_config
from src.screener import scan


def main():
    cfg = load_config()
    print(f"Scanning {len(cfg.screener.watchlist)} symbols with strategy "
          f"'{cfg.strategy.name}' ({cfg.screener.yf_interval} bars)...\n")
    table = scan(cfg)
    with_cols = [c for c in ["symbol", "last_close", "target", "fresh_entry",
                             "as_of", "error"] if c in table.columns]
    print(table[with_cols].to_string(index=False))
    fresh = table[table["fresh_entry"] == True]  # noqa: E712
    if len(fresh):
        print("\nFresh entry signals:", ", ".join(fresh["symbol"].tolist()))
    else:
        print("\nNo fresh entry signals on the latest bar.")
    print("\n(Delayed Yahoo data — use this to build a shortlist, then confirm "
          "on IBKR's live feed before trading.)")


if __name__ == "__main__":
    main()
