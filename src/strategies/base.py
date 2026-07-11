"""Strategy framework (feature 3).

Every strategy takes a DataFrame with columns [open, high, low, close, volume]
and a DatetimeIndex, and returns the same DataFrame plus a `target` column:

    target == 1  ->  want to be LONG
    target == 0  ->  want to be FLAT

Strategies are LONG-ONLY by design. Shorting stocks adds borrow/locate and
margin complexity that a beginner should not automate first. The backtester and
executor translate target *changes* into entries/exits, acting on the NEXT bar
to avoid look-ahead bias.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
import pandas as pd


class Strategy(ABC):
    name = "base"

    def __init__(self, params: dict | None = None):
        self.params = params or {}

    @abstractmethod
    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return df with an added integer `target` column in {0, 1}."""
        raise NotImplementedError

    def _finish(self, df: pd.DataFrame, target: pd.Series) -> pd.DataFrame:
        out = df.copy()
        out["target"] = target.fillna(0).astype(int)
        return out


_REGISTRY: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    _REGISTRY[cls.name] = cls
    return cls


def build_strategy(name: str, params: dict | None = None) -> Strategy:
    if name not in _REGISTRY:
        raise KeyError(f"Unknown strategy '{name}'. Available: {sorted(_REGISTRY)}")
    return _REGISTRY[name](params)


def available() -> list[str]:
    return sorted(_REGISTRY)
