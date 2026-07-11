"""Shared application state for the web GUI.

Holds references to the broker, risk manager, live trader, journal, and config.
Everything lives in one process on one event loop — no locking needed.
"""
from __future__ import annotations
from dataclasses import dataclass, field

from ..config import load_config, Cfg
from ..async_broker import AsyncBroker
from ..async_executor import AsyncExecutor
from ..async_live import AsyncLiveTrader
from ..risk import RiskManager
from ..journal import Journal


@dataclass
class AppState:
    cfg: Cfg
    cfg_path: str
    broker: AsyncBroker
    journal: Journal
    rm: RiskManager | None = None
    executor: AsyncExecutor | None = None
    trader: AsyncLiveTrader | None = None
    connected: bool = False


_state: AppState | None = None


def init_state(cfg_path: str = "config.yaml") -> AppState:
    global _state
    cfg = load_config(cfg_path)
    broker = AsyncBroker(cfg)
    journal = Journal(cfg.journal.path)
    _state = AppState(cfg=cfg, cfg_path=cfg_path, broker=broker, journal=journal)
    return _state


def get_state() -> AppState:
    if _state is None:
        raise RuntimeError("AppState not initialized — call init_state() first")
    return _state


async def do_connect(state: AppState) -> None:
    """Connect broker and build the live-trading object graph."""
    await state.broker.connect()
    eq = await state.broker.equity_or_raise()
    r = state.cfg.risk
    state.rm = RiskManager(
        start_equity=eq,
        max_position_pct=r.max_position_pct,
        per_trade_stop_pct=r.per_trade_stop_pct,
        take_profit_pct=r.take_profit_pct,
        daily_max_loss_pct=r.daily_max_loss_pct,
        max_open_positions=r.max_open_positions,
        max_trades_per_day=r.max_trades_per_day,
    )
    state.executor = AsyncExecutor(state.broker, state.rm, state.journal, state.cfg)
    state.trader = AsyncLiveTrader(state.broker, state.rm, state.executor,
                                    state.journal, state.cfg)
    state.connected = True


async def do_disconnect(state: AppState) -> None:
    """Stop the loop and disconnect."""
    if state.trader and state.trader.status != "stopped":
        state.trader.stop()
        if state.trader._task:
            try:
                await state.trader._task
            except Exception:
                pass
    await state.broker.disconnect()
    state.connected = False
    state.rm = None
    state.executor = None
    state.trader = None
