"""Momentum: fast/slow moving-average crossover. Long while fast > slow."""
from __future__ import annotations
import pandas as pd
from .base import Strategy, register
from ..indicators import ema


@register
class MaCrossover(Strategy):
    name = "ma_crossover"

    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        fast = int(self.params.get("fast", 9))
        slow = int(self.params.get("slow", 21))
        ef = ema(df["close"], fast)
        es = ema(df["close"], slow)
        target = (ef > es).astype(int)
        # No position until both EMAs are defined.
        target[ef.isna() | es.isna()] = 0
        return self._finish(df, target)
