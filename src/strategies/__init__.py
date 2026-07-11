from .base import Strategy, build_strategy, available, register  # noqa: F401
# Import each module so its @register decorator runs.
from . import ma_crossover, mean_reversion, orb, vwap  # noqa: F401
