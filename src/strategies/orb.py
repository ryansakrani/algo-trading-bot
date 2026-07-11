"""Opening Range Breakout (ORB): after the first N minutes, go long if price
breaks above the opening-range high. Stay long until end of day (the backtester
flattens intraday; risk stops handle the rest)."""
from __future__ import annotations
import pandas as pd
from .base import Strategy, register
from ..indicators import opening_range


@register
class OpeningRangeBreakout(Strategy):
    name = "orb"

    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        minutes = int(self.params.get("opening_minutes", 30))
        or_high, or_low = opening_range(df, minutes)
        day = pd.Series(df.index, index=df.index).dt.normalize()

        target = pd.Series(0, index=df.index, dtype=int)
        for d, idx in df.groupby(day).groups.items():
            sub = df.loc[idx]
            hi = or_high.loc[idx].iloc[0] if pd.notna(or_high.loc[idx]).any() else None
            if hi is None:
                continue
            broke = False
            for ts in sub.index:
                # Only consider breakouts AFTER the opening range window.
                after_window = ts >= sub.index[0] + pd.Timedelta(minutes=minutes)
                if not broke and after_window and df["close"].loc[ts] > hi:
                    broke = True
                target.loc[ts] = int(broke)
        return self._finish(df, target)
