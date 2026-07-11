"""Live-trading API routes: status, SSE stream, connect/disconnect, loop
control, flatten, watchlist."""
from __future__ import annotations
import asyncio
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .state import get_state, do_connect, do_disconnect
from ..config import save_config

router = APIRouter(prefix="/api")


def _build_status(state) -> dict:
    base = {
        "connected": state.connected,
        "mode": state.cfg.broker.mode,
    }
    if not state.connected:
        base.update(account=None, equity=0, daily_pnl=0,
                    kill_switch_level=0, kill_switch_active=False,
                    loop_status="stopped", trades_today=0,
                    max_trades_per_day=int(state.cfg.risk.max_trades_per_day),
                    open_positions_count=0,
                    max_open_positions=int(state.cfg.risk.max_open_positions),
                    market_open=False, minutes_to_close=0,
                    open_positions=[], symbols=dict(state.cfg.live.symbols),
                    strategy=state.cfg.strategy.name)
        return base

    rm = state.rm
    is_open, mins = state.broker.market_clock()
    positions = []
    for item in state.broker.portfolio():
        positions.append({
            "symbol": item.contract.symbol,
            "shares": int(item.position),
            "avg_cost": round(float(item.averageCost), 2),
            "market_price": round(float(item.marketPrice), 2),
            "unrealized_pnl": round(float(item.unrealizedPNL or 0), 2),
        })
    trader_open = {}
    if state.trader:
        for sym, pos in state.trader.open.items():
            trader_open[sym] = {
                "entry_price": pos.entry_price,
                "entry_time": pos.entry_time.isoformat() if pos.entry_time else None,
            }
    for p in positions:
        extra = trader_open.get(p["symbol"], {})
        p["entry_price"] = extra.get("entry_price", p["avg_cost"])
        p["entry_time"] = extra.get("entry_time")

    base.update(
        account=state.broker.account,
        equity=round(state.broker.equity(), 2),
        daily_pnl=round(rm.realized_pnl_today, 2),
        kill_switch_level=round(rm.kill_switch_level(), 2),
        kill_switch_active=rm.halted,
        loop_status=state.trader.status if state.trader else "stopped",
        trades_today=rm.trades_today,
        max_trades_per_day=rm.max_trades_per_day,
        open_positions_count=rm.open_positions,
        max_open_positions=rm.max_open_positions,
        market_open=is_open,
        minutes_to_close=mins,
        open_positions=positions,
        symbols=dict(state.cfg.live.symbols),
        strategy=state.cfg.strategy.name,
    )
    return base


@router.get("/status")
async def status():
    return _build_status(get_state())


@router.get("/stream")
async def event_stream():
    async def generate():
        state = get_state()
        while True:
            data = _build_status(state)
            yield f"data: {json.dumps(data)}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.get("/journal")
async def journal(n: int = 20):
    return get_state().journal.tail(n)


@router.post("/connect")
async def connect():
    state = get_state()
    if state.connected:
        raise HTTPException(409, "Already connected")
    try:
        await do_connect(state)
    except Exception as e:
        raise HTTPException(500, str(e))
    return _build_status(state)


@router.post("/disconnect")
async def disconnect():
    state = get_state()
    if not state.connected:
        raise HTTPException(409, "Not connected")
    await do_disconnect(state)
    return {"status": "disconnected"}


@router.post("/loop/start")
async def loop_start():
    state = get_state()
    if not state.connected:
        raise HTTPException(409, "Not connected — connect first")
    if not state.trader:
        raise HTTPException(409, "Trader not initialized")
    if state.trader.status == "running":
        raise HTTPException(409, "Loop already running")
    await state.trader.start()
    return {"loop_status": state.trader.status}


@router.get("/loop/status")
async def loop_status():
    state = get_state()
    if not state.trader:
        return {"status": "stopped", "task_exists": False,
                "task_done": None, "task_exception": None, "last_error": None}
    return state.trader.task_status()


@router.post("/loop/stop")
async def loop_stop():
    state = get_state()
    if not state.trader:
        raise HTTPException(409, "Trader not initialized")
    state.trader.stop()
    return {"loop_status": "stopping"}


class FlattenRequest(BaseModel):
    confirm: bool = False


@router.post("/flatten-all")
async def flatten_all(req: FlattenRequest):
    state = get_state()
    if not state.trader:
        raise HTTPException(409, "Trader not initialized")
    if not req.confirm:
        raise HTTPException(400, "Confirmation required: send {\"confirm\": true}")
    await state.trader._flatten_all("manual flatten-all from GUI")
    return {"status": "flattened"}


@router.post("/flatten/{symbol}")
async def flatten_symbol(symbol: str):
    state = get_state()
    if not state.trader:
        raise HTTPException(409, "Trader not initialized")
    await state.trader.flatten_single(symbol)
    return {"status": f"flattened {symbol}"}


class SymbolConfig(BaseModel):
    strategy: str
    params: dict = {}


class WatchlistRequest(BaseModel):
    symbols: dict[str, SymbolConfig]


@router.post("/watchlist")
async def update_watchlist(req: WatchlistRequest):
    from ..strategies import available, build_strategy
    state = get_state()
    if not req.symbols:
        raise HTTPException(400, "Watchlist cannot be empty")
    valid = set(available())
    sym_cfg = {}
    for sym, sc in req.symbols.items():
        sym = sym.upper().strip()
        if not sym:
            continue
        if sc.strategy not in valid:
            raise HTTPException(400, f"Unknown strategy '{sc.strategy}' for {sym}")
        sym_cfg[sym] = {"strategy": sc.strategy, "params": sc.params}
    if not sym_cfg:
        raise HTTPException(400, "Watchlist cannot be empty")

    if state.trader:
        state.trader.symbols = list(sym_cfg.keys())
        state.trader.strats = {
            s: build_strategy(c["strategy"], c.get("params", {}))
            for s, c in sym_cfg.items()
        }
    state.cfg["live"]["symbols"] = sym_cfg
    save_config(state.cfg, state.cfg_path)
    return {"symbols": sym_cfg}
