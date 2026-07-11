"""Config API routes: risk parameters and strategy selection."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .state import get_state
from ..config import save_config
from ..strategies import build_strategy, available

router = APIRouter(prefix="/api/config")


RISK_FIELDS = {
    "max_position_pct", "per_trade_stop_pct", "take_profit_pct",
    "daily_max_loss_pct", "max_open_positions", "max_trades_per_day",
}


def _risk_dict(state) -> dict:
    r = state.cfg.risk
    return {f: getattr(r, f) if hasattr(r, f) else r[f] for f in RISK_FIELDS}


@router.get("/risk")
async def get_risk():
    return _risk_dict(get_state())


class RiskUpdate(BaseModel):
    max_position_pct: float | None = None
    per_trade_stop_pct: float | None = None
    take_profit_pct: float | None = None
    daily_max_loss_pct: float | None = None
    max_open_positions: int | None = None
    max_trades_per_day: int | None = None


@router.put("/risk")
async def update_risk(req: RiskUpdate):
    state = get_state()
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(400, "No fields to update")

    if "max_position_pct" in updates:
        v = updates["max_position_pct"]
        if not (0 < v <= 1):
            raise HTTPException(400, "max_position_pct must be in (0, 1]")
    if "per_trade_stop_pct" in updates:
        v = updates["per_trade_stop_pct"]
        if not (0 < v < 1):
            raise HTTPException(400, "per_trade_stop_pct must be in (0, 1)")
    if "take_profit_pct" in updates:
        if updates["take_profit_pct"] <= 0:
            raise HTTPException(400, "take_profit_pct must be > 0")
    if "daily_max_loss_pct" in updates:
        v = updates["daily_max_loss_pct"]
        if not (0 < v < 1):
            raise HTTPException(400, "daily_max_loss_pct must be in (0, 1)")
    if "max_open_positions" in updates:
        if updates["max_open_positions"] < 1:
            raise HTTPException(400, "max_open_positions must be >= 1")
    if "max_trades_per_day" in updates:
        if updates["max_trades_per_day"] < 1:
            raise HTTPException(400, "max_trades_per_day must be >= 1")

    if state.rm:
        for k, v in updates.items():
            setattr(state.rm, k, v)
    for k, v in updates.items():
        state.cfg["risk"][k] = v
    save_config(state.cfg, state.cfg_path)
    return _risk_dict(state)


@router.get("/strategy")
async def get_strategy():
    state = get_state()
    return {
        "name": state.cfg.strategy.name,
        "params": dict(state.cfg.strategy.params),
        "available": available(),
    }


class StrategyUpdate(BaseModel):
    name: str
    params: dict | None = None


@router.put("/strategy")
async def update_strategy(req: StrategyUpdate):
    state = get_state()
    if state.trader and state.trader.status == "running":
        raise HTTPException(409, "Stop the loop before changing strategy")
    if req.name not in available():
        raise HTTPException(400, f"Unknown strategy '{req.name}'. Available: {available()}")

    params = req.params or dict(state.cfg.strategy.params)
    strat = build_strategy(req.name, params)

    state.cfg["strategy"]["name"] = req.name
    if req.params:
        state.cfg["strategy"]["params"].update(req.params)
    save_config(state.cfg, state.cfg_path)

    if state.trader:
        state.trader.strat = strat

    return {
        "name": req.name,
        "params": params,
        "available": available(),
    }
