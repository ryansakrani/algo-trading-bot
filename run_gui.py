#!/usr/bin/env python3
"""Launch the web GUI for the IBKR day trader.

Usage:
    python run_gui.py                    # default: http://127.0.0.1:8000
    python run_gui.py --port 8080
"""
import argparse
import os
import uvicorn


def main():
    p = argparse.ArgumentParser(description="IBKR Day Trader Web GUI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    p.add_argument("--config", default="config.yaml")
    a = p.parse_args()

    from src.web.app import create_app
    app = create_app(cfg_path=a.config)
    uvicorn.run(app, host=a.host, port=a.port)


if __name__ == "__main__":
    main()
