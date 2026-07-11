"""Technical indicators. Pure pandas/numpy so they can be unit-tested offline.

Every function takes/returns pandas Series or a DataFrame with OHLCV columns:
    open, high, low, close, volume  (lowercase)
"""
from __future__ import annotations
import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    out[avg_loss == 0] = 100.0   # no losses -> maximally overbought
    return out


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    return mid - k * sd, mid, mid + k * sd   # lower, mid, upper


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    prev_c = c.shift(1)
    tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP reset each calendar day (intraday benchmark)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    pv = tp * df["volume"]
    day = pd.Series(df.index, index=df.index).dt.normalize() if isinstance(
        df.index, pd.DatetimeIndex) else pd.Series(0, index=df.index)
    cum_pv = pv.groupby(day).cumsum()
    cum_v = df["volume"].groupby(day).cumsum().replace(0.0, np.nan)
    return cum_pv / cum_v


def opening_range(df: pd.DataFrame, minutes: int = 30):
    """Per-day high/low of the first `minutes` of the session.

    Returns two Series (or_high, or_low) aligned to df, forward-filled within
    the day. Requires a DatetimeIndex.
    """
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError("opening_range requires a DatetimeIndex")
    day = pd.Series(df.index, index=df.index).dt.normalize()
    or_high = pd.Series(index=df.index, dtype=float)
    or_low = pd.Series(index=df.index, dtype=float)
    for d, idx in df.groupby(day).groups.items():
        sub = df.loc[idx]
        start = sub.index[0]
        window = sub[sub.index < start + pd.Timedelta(minutes=minutes)]
        if len(window) == 0:
            continue
        or_high.loc[idx] = window["high"].max()
        or_low.loc[idx] = window["low"].min()
    return or_high, or_low
