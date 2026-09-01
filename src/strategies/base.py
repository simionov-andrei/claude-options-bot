"""The strategy plugin interface.

To add your own strategy: drop a new file in src/strategies/ that defines a
class inheriting StrategyBase, then add its filename (without .py) to
strategies.active in config/settings.yaml.

The engine calls scan() on every poll while the market is open and no
position for this strategy is open. It calls should_exit() every poll while
a position from this strategy is open. That's the whole contract — how you
decide entries/exits inside those two methods is entirely up to you, which
is the point: the engine, risk management, and position sizing are generic;
the edge lives in your strategy code.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

import pandas as pd

from src.broker.models import OptionContract, Position

TradeDirection = Literal["long_call", "long_put"]


@dataclass
class MarketSnapshot:
    """Everything a strategy gets to look at on a given poll."""

    timestamp: datetime
    underlying_symbol: str
    underlying_price: float
    bars: pd.DataFrame  # OHLCV history, indexed by datetime, ascending
    option_chain: list[OptionContract]  # chain for the nearest tradable expiration(s)


@dataclass
class TradeIntent:
    """What a strategy wants to do. The engine turns this into an actual
    order after risk checks and news-agent-driven sizing are applied."""

    strategy_name: str
    underlying_symbol: str
    direction: TradeDirection
    # Target delta for the contract to select, e.g. 0.40-0.60 for near-the-money.
    target_delta: float
    # Acceptable days-to-expiration range for the contract to select.
    dte_min: int = 0
    dte_max: int = 2
    # Exit rules, as a fraction of entry premium (0.5 = 50%).
    stop_loss_pct: float = 0.5
    take_profit_pct: float = 1.0
    # "Base" contract count before the news agent's cautious-day multiplier
    # and account-balance risk cap are applied.
    base_contracts: int = 1
    order_type: Literal["market", "limit"] = "market"
    limit_price: Optional[float] = None
    notes: str = ""


class StrategyBase(ABC):
    name: str = "unnamed_strategy"

    @abstractmethod
    def scan(self, snapshot: MarketSnapshot) -> Optional[TradeIntent]:
        """Return a TradeIntent to enter a new trade, or None to do nothing."""
        raise NotImplementedError

    @abstractmethod
    def should_exit(self, position: Position, snapshot: MarketSnapshot) -> bool:
        """Return True if an open position from this strategy should be closed.

        Note: stop_loss_pct/take_profit_pct on the original TradeIntent are
        already enforced by the engine automatically. Implement this only for
        extra exit logic (e.g. time-based exits, signal reversal).
        """
        raise NotImplementedError
