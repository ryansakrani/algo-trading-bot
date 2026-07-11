"""Data layer.

yfinance is used for RESEARCH / BACKTESTING / SCREENING only. It returns
DELAYED data (typically ~15 min for US stocks), is rate-limited, and is an
unofficial Yahoo scraper — never use it for live execution decisions. The live
loop (not built in this package) should use IBKR's real-time feed via broker.py.

All loaders normalize to a DatetimeIndex and lowercase columns:
    open, high, low, close, volume
"""
from __future__ import annotations
import pandas as pd


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    # yfinance sometimes returns a column MultiIndex for a single ticker.
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[keep].dropna()
    df.index = pd.to_datetime(df.index)
    return df


def fetch_yf(symbol: str, period: str = "60d", interval: str = "5m") -> pd.DataFrame:
    """Historical OHLCV from Yahoo for research/backtest. Imports yfinance lazily."""
    import yfinance as yf
    raw = yf.download(symbol, period=period, interval=interval,
                      auto_adjust=False, progress=False, prepost=False)
    df = _normalize(raw)
    if df.empty:
        raise RuntimeError(
            f"No data for {symbol} ({period}/{interval}). Note: Yahoo caps intraday "
            f"history (e.g. ~60d for 5m, ~7d for 1m).")
    return df
