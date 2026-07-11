"""Trade journal (feature 7): append every signal, order, and fill to CSV."""
from __future__ import annotations
import csv
import os
import datetime as dt

FIELDS = ["ts", "event", "symbol", "side", "qty", "price",
          "stop", "take_profit", "strategy", "mode", "note"]


class Journal:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=FIELDS).writeheader()

    def log(self, event: str, *, symbol="", side="", qty="", price="",
            stop="", take_profit="", strategy="", mode="", note=""):
        row = {
            "ts": dt.datetime.now().isoformat(timespec="seconds"),
            "event": event, "symbol": symbol, "side": side, "qty": qty,
            "price": price, "stop": stop, "take_profit": take_profit,
            "strategy": strategy, "mode": mode, "note": note,
        }
        with open(self.path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writerow(row)
        return row

    def tail(self, n: int = 10) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path) as f:
            rows = list(csv.DictReader(f))
        return rows[-n:]
