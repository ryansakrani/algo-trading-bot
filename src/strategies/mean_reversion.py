"""Mean reversion: go long when RSI is oversold AND price is at/below the lower
Bollinger band; exit when RSI recovers above the midline (overbought level)."""
from __future__ import annotations
import pandas as pd
from .base import Strategy, register
from ..indicators import rsi, bollinger


@register
class MeanReversion(Strategy):
    name = "mean_reversion"

    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        n = int(self.params.get("rsi_period", 14))
        oversold = float(self.params.get("oversold", 30))
        overbought = float(self.params.get("overbought", 70))
        r = rsi(df["close"], n)
        lower, mid, upper = bollinger(df["close"], n=20, k=2.0)

        target = pd.Series(0, index=df.index, dtype=int)
        in_pos = False
        for i in range(len(df)):
            ri = r.iloc[i]
            if pd.isna(ri):
                target.iloc[i] = 0
                continue
            if not in_pos:
                if ri <= oversold and df["close"].iloc[i] <= lower.iloc[i]:
                    in_pos = True
            else:
                if ri >= overbought or df["close"].iloc[i] >= mid.iloc[i]:
                    in_pos = False
            target.iloc[i] = int(in_pos)
        return self._finish(df, target)
