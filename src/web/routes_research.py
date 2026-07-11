"""Research API routes: backtester and screener (run off the event loop)."""
from __future__ import annotations
import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .state import get_state
from ..data import fetch_yf
from ..strategies import build_strategy, available
from ..backtester import run_backtest
from ..screener import scan
from ..config import Cfg

router = APIRouter(prefix="/api")


class BacktestRequest(BaseModel):
    symbol: str = "AAPL"
    strategy: str = "ma_crossover"
    period: str = "60d"
    interval: str = "5m"
    params: dict | None = None


def _do_backtest(req: BacktestRequest, cfg: Cfg) -> dict:
    if req.strategy not in available():
        raise ValueError(f"Unknown strategy '{req.strategy}'")
    df = fetch_yf(req.symbol, req.period, req.interval)
    params = req.params or dict(cfg.strategy.params)
    strat = build_strategy(req.strategy, params)
    sig = strat.generate(df)
    result = run_backtest(
        df, sig["target"],
        starting_equity=cfg.account.backtest_starting_equity,
        stop_pct=cfg.risk.per_trade_stop_pct,
        take_profit_pct=cfg.risk.take_profit_pct,
        max_position_pct=cfg.risk.max_position_pct,
        commission_per_share=cfg.backtest.commission_per_share,
        slippage_bps=cfg.backtest.slippage_bps,
        intraday_only=cfg.backtest.intraday_only,
    )
    return {
        "stats": result.stats,
        "equity_curve": [{"time": str(t), "equity": round(v, 2)}
                         for t, v in result.equity_curve.items()],
        "trades": [{"entry_time": str(t.entry_time), "exit_time": str(t.exit_time),
                     "shares": t.shares, "entry_price": round(t.entry_price, 2),
                     "exit_price": round(t.exit_price, 2) if t.exit_price else None,
                     "reason": t.reason, "pnl": round(t.pnl, 2)}
                    for t in result.trades],
    }


@router.post("/backtest")
async def backtest_endpoint(req: BacktestRequest):
    state = get_state()
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _do_backtest, req, state.cfg)
    except Exception as e:
        raise HTTPException(500, str(e))
    return result


@router.get("/strategies")
async def strategies():
    return {"strategies": available()}


class ScreenerRequest(BaseModel):
    watchlist: list[str] | None = None
    yf_period: str | None = None
    yf_interval: str | None = None


def _do_scan(cfg: Cfg, watchlist, yf_period, yf_interval) -> list[dict]:
    override = Cfg(dict(cfg))
    if watchlist:
        override["screener"]["watchlist"] = watchlist
    if yf_period:
        override["screener"]["yf_period"] = yf_period
    if yf_interval:
        override["screener"]["yf_interval"] = yf_interval
    result = scan(override)
    records = []
    for _, row in result.iterrows():
        rec = {}
        for col in result.columns:
            val = row[col]
            if hasattr(val, "isoformat"):
                rec[col] = val.isoformat()
            else:
                rec[col] = val
        records.append(rec)
    return records


@router.post("/screener")
async def screener_endpoint(req: ScreenerRequest):
    state = get_state()
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(
            None, _do_scan, state.cfg,
            req.watchlist, req.yf_period, req.yf_interval)
    except Exception as e:
        raise HTTPException(500, str(e))
    return result
