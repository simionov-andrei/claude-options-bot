"""Loads strategy plugins named in config/settings.yaml from this package."""
from __future__ import annotations

import importlib
import inspect
import logging

from src.strategies.base import StrategyBase

log = logging.getLogger(__name__)


def load_strategies(names: list[str]) -> list[StrategyBase]:
    strategies: list[StrategyBase] = []
    for name in names:
        module = importlib.import_module(f"src.strategies.{name}")
        found = None
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, StrategyBase) and obj is not StrategyBase:
                found = obj
                break
        if found is None:
            raise ImportError(f"No StrategyBase subclass found in src/strategies/{name}.py")
        instance = found()
        log.info("Loaded strategy: %s", instance.name)
        strategies.append(instance)
    return strategies
