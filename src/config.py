"""Load and validate the YAML config into a simple dotted-access object."""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from typing import Any
import yaml
from ruamel.yaml import YAML as RuamelYAML


class Cfg(dict):
    """Dict that also allows attribute access: cfg.broker.port"""
    def __getattr__(self, k: str) -> Any:
        try:
            v = self[k]
        except KeyError as e:
            raise AttributeError(k) from e
        return Cfg(v) if isinstance(v, dict) else v


def load_config(path: str = "config.yaml") -> Cfg:
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Config '{path}' not found. Copy config.example.yaml to config.yaml first."
        )
    with open(path, "r") as f:
        raw = yaml.safe_load(f)
    cfg = Cfg(raw)
    _validate(cfg)
    return cfg


def save_config(cfg: Cfg, path: str = "config.yaml") -> None:
    """Write cfg back to YAML, preserving comments via ruamel.yaml."""
    ry = RuamelYAML()
    ry.preserve_quotes = True
    with open(path, "r") as f:
        doc = ry.load(f)

    def _deep_update(target, source):
        for k, v in source.items():
            if isinstance(v, dict) and k in target and isinstance(target[k], dict):
                _deep_update(target[k], v)
            else:
                target[k] = v

    for section in ("risk", "strategy", "live", "screener"):
        if section in cfg:
            if section not in doc:
                doc[section] = {}
            _deep_update(doc[section], cfg[section])

    with open(path, "w") as f:
        ry.dump(doc, f)


def normalize_live_symbols(cfg: Cfg) -> None:
    """Ensure live.symbols is a dict mapping symbol -> {strategy, params}.

    Accepts either the old list format (uses global strategy for all) or
    the new per-symbol dict format. Mutates cfg in place.
    """
    from .strategies import available
    syms = cfg["live"]["symbols"]
    if isinstance(syms, list):
        global_name = cfg["strategy"]["name"]
        global_params = dict(cfg["strategy"]["params"])
        cfg["live"]["symbols"] = {
            str(s): {"strategy": global_name, "params": dict(global_params)}
            for s in syms
        }
    elif isinstance(syms, dict):
        valid = set(available())
        for sym, scfg in syms.items():
            if isinstance(scfg, dict):
                name = scfg.get("strategy", cfg["strategy"]["name"])
                assert name in valid, \
                    f"live.symbols.{sym}.strategy '{name}' not in {sorted(valid)}"
                scfg.setdefault("params", {})
            else:
                syms[sym] = {"strategy": cfg["strategy"]["name"],
                             "params": dict(cfg["strategy"]["params"])}


def _validate(cfg: Cfg) -> None:
    r = cfg.risk
    assert 0 < r.max_position_pct <= 1, "risk.max_position_pct must be in (0, 1]"
    assert 0 < r.per_trade_stop_pct < 1, "risk.per_trade_stop_pct must be in (0, 1)"
    assert r.take_profit_pct > 0, "risk.take_profit_pct must be > 0"
    assert 0 < r.daily_max_loss_pct < 1, "risk.daily_max_loss_pct must be in (0, 1)"
    assert r.max_open_positions >= 1
    assert r.max_trades_per_day >= 1
    assert cfg.broker.mode in ("paper", "live"), "broker.mode must be 'paper' or 'live'"
    assert cfg.strategy.name in ("orb", "ma_crossover", "mean_reversion", "vwap"), \
        f"unknown strategy '{cfg.strategy.name}'"
    normalize_live_symbols(cfg)
