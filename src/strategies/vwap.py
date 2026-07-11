"""VWAP strategy: long while price holds above session VWAP (optionally by a
band buffer), flat when it loses VWAP. VWAP resets each day."""
from __future__ import annotations
import pandas as pd
from .base import Strategy, register
from ..indicators import session_vwap


@register
class Vwap(Strategy):
    name = "vwap"

    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        band = float(self.params.get("vwap_band_pct", 0.0))
        vw = session_vwap(df)
        upper = vw * (1 + band)
        target = (df["close"] > upper).astype(int)
        target[vw.isna()] = 0
        return self._finish(df, target)
