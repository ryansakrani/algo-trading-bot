"""Terminal dashboard (feature 9): a live view of account equity, day P&L vs the
kill-switch level, open positions with unrealized P&L, risk-manager status, and
the most recent journal entries. Refreshes on an interval.

Read-only: the dashboard never places orders. Run it alongside paper trading to
watch what's happening. Requires `rich`.
"""
from __future__ import annotations
import time
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.console import Group
from rich import box


def _positions_table(broker) -> Table:
    t = Table(title="Open Positions", box=box.SIMPLE, expand=True)
    for col in ("Symbol", "Qty", "Avg Cost", "Mkt Price", "Unreal P&L"):
        t.add_column(col, justify="right")
    t.columns[0].justify = "left"
    total = 0.0
    for item in broker.portfolio():
        upnl = float(item.unrealizedPNL or 0.0)
        total += upnl
        color = "green" if upnl >= 0 else "red"
        t.add_row(item.contract.symbol, str(int(item.position)),
                  f"{item.averageCost:,.2f}", f"{item.marketPrice:,.2f}",
                  f"[{color}]{upnl:,.2f}[/{color}]")
    t.caption = f"Total unrealized: {total:,.2f}"
    return t


def _status_panel(broker, rm) -> Panel:
    eq = broker.equity()
    kill = rm.kill_switch_level()
    day = rm.realized_pnl_today
    day_color = "green" if day >= 0 else "red"
    halted = "[bold red]HALTED[/bold red]" if rm.halted else "[green]active[/green]"
    body = (
        f"Mode: [bold]{broker.mode.upper()}[/bold]   Account: {broker.account}\n"
        f"Equity (NetLiq): [bold]{eq:,.2f}[/bold]\n"
        f"Day realized P&L: [{day_color}]{day:,.2f}[/{day_color}]   "
        f"Kill-switch at: {kill:,.2f}\n"
        f"Trades today: {rm.trades_today}/{rm.max_trades_per_day}   "
        f"Open: {rm.open_positions}/{rm.max_open_positions}   "
        f"Status: {halted}"
    )
    return Panel(body, title="Account / Risk", border_style="cyan")


def _journal_table(journal) -> Table:
    t = Table(title="Recent Journal", box=box.SIMPLE, expand=True)
    for col in ("Time", "Event", "Symbol", "Side", "Qty", "Price", "Note"):
        t.add_column(col)
    for r in journal.tail(8):
        t.add_row(r.get("ts", "")[11:], r.get("event", ""), r.get("symbol", ""),
                  r.get("side", ""), str(r.get("qty", "")), str(r.get("price", "")),
                  r.get("note", "")[:30])
    return t


def run_dashboard(broker, rm, journal, refresh_secs: float = 3.0):
    def render():
        return Group(_status_panel(broker, rm),
                     _positions_table(broker),
                     _journal_table(journal))
    with Live(render(), refresh_per_second=4, screen=False) as live:
        try:
            while True:
                broker.ib.sleep(refresh_secs)   # pumps the ib_async event loop
                live.update(render())
        except KeyboardInterrupt:
            pass
