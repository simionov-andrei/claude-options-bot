"""Plain data models shared across the broker client, strategies, and engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional


@dataclass
class Quote:
    symbol: str
    last: float
    bid: float
    ask: float
    prev_close: float
    timestamp: datetime


@dataclass
class OptionContract:
    symbol: str  # OCC symbol, e.g. AAPL240621C00195000
    underlying: str
    strike: float
    expiration: str  # YYYY-MM-DD
    option_type: Literal["call", "put"]
    bid: float
    ask: float
    last: Optional[float]
    volume: int
    open_interest: int
    delta: Optional[float] = None
    gamma: Optional[float] = None
    theta: Optional[float] = None
    vega: Optional[float] = None
    iv: Optional[float] = None

    @property
    def mid(self) -> float:
        if self.bid and self.ask:
            return round((self.bid + self.ask) / 2, 4)
        return self.last or 0.0


OrderSide = Literal[
    "buy_to_open", "sell_to_close", "sell_to_open", "buy_to_close"
]


@dataclass
class OrderLeg:
    option_symbol: str
    side: OrderSide
    quantity: int


@dataclass
class OrderResult:
    order_id: Optional[str]
    status: str  # e.g. "ok", "filled", "rejected", "error"
    raw: dict = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class AccountBalance:
    total_equity: float
    cash: float
    option_buying_power: float


@dataclass
class Position:
    option_symbol: str
    underlying: str
    quantity: int  # positive = long
    cost_basis: float  # total dollars paid (per contract * 100 * qty)
    entry_price: float  # per-contract premium at entry
