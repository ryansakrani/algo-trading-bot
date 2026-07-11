"""Screener (feature 8): scan a watchlist and report symbols whose configured
strategy is signalling a FRESH entry on the most recent bar (target flips 0->1).

Uses yfinance, so it is delayed data — meant for a morning/pre-trade scan to
build a shortlist, not for firing live orders."""
from __future__ import annotations
import pandas as pd
from .data import fetch_yf
from .strategies import build_strategy


def scan(cfg) -> pd.DataFrame:
    strat = build_strategy(cfg.strategy.name, dict(cfg.strategy.params))
    rows = []
    for sym in cfg.screener.watchlist:
        try:
            df = fetch_yf(sym, cfg.screener.yf_period, cfg.screener.yf_interval)
            sig = strat.generate(df)
            tgt = sig["target"]
            last = int(tgt.iloc[-1])
            prev = int(tgt.iloc[-2]) if len(tgt) > 1 else 0
            fresh = (prev == 0 and last == 1)
            rows.append({
                "symbol": sym,
                "last_close": round(float(df["close"].iloc[-1]), 2),
                "target": last,
                "fresh_entry": fresh,
                "as_of": df.index[-1],
            })
        except Exception as e:  # keep scanning the rest of the list
            rows.append({"symbol": sym, "last_close": None, "target": None,
                         "fresh_entry": False, "as_of": None, "error": str(e)[:60]})
    out = pd.DataFrame(rows)
    return out.sort_values(["fresh_entry", "target"], ascending=False).reset_index(drop=True)
