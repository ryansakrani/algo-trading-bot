"""FastAPI application factory for the IBKR day-trader web GUI."""
from __future__ import annotations
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .state import init_state, get_state, do_disconnect
from .routes_live import router as live_router
from .routes_config import router as config_router
from .routes_research import router as research_router

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_state(app.state.cfg_path)
    yield
    state = get_state()
    if state.connected:
        await do_disconnect(state)


def create_app(cfg_path: str = "config.yaml") -> FastAPI:
    app = FastAPI(title="IBKR Day Trader", lifespan=lifespan)
    app.state.cfg_path = cfg_path

    app.include_router(live_router)
    app.include_router(config_router)
    app.include_router(research_router)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(
            str(STATIC_DIR / "index.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

    return app
