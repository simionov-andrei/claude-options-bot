"""Reference strategy: EMA crossover momentum, calls/puts, short-dated.

This is a STARTING POINT so the engine has something runnable out of the
box, not a profitable strategy. Copy this file and rewrite scan()/should_exit()
with your own rules -- that's the whole point of the plugin system.
"""
from __future__ import annotations

from typing import Optional

from src.broker.models import Position
from src.strategies.base import MarketSnapshot, StrategyBase, TradeIntent


class ExampleMomentumStrategy(StrategyBase):
    name = "example_momentum"

    fast_period = 9
    slow_period = 21

    def scan(self, snapshot: MarketSnapshot) -> Optional[TradeIntent]:
        bars = snapshot.bars
        if bars is None or len(bars) < self.slow_period + 1:
            return None

        closes = bars["close"]
        fast_ema = closes.ewm(span=self.fast_period, adjust=False).mean()
        slow_ema = closes.ewm(span=self.slow_period, adjust=False).mean()

        crossed_up = fast_ema.iloc[-2] <= slow_ema.iloc[-2] and fast_ema.iloc[-1] > slow_ema.iloc[-1]
        crossed_down = fast_ema.iloc[-2] >= slow_ema.iloc[-2] and fast_ema.iloc[-1] < slow_ema.iloc[-1]

        if not (crossed_up or crossed_down):
            return None

        direction = "long_call" if crossed_up else "long_put"

        return TradeIntent(
            strategy_name=self.name,
            underlying_symbol=snapshot.underlying_symbol,
            direction=direction,
            target_delta=0.45,
            dte_min=0,
            dte_max=2,
            stop_loss_pct=0.5,
            take_profit_pct=1.0,
            base_contracts=1,
            order_type="market",
            notes=f"EMA{self.fast_period}/{self.slow_period} crossover",
        )

    def should_exit(self, position: Position, snapshot: MarketSnapshot) -> bool:
        # Stop-loss / take-profit are enforced by the engine already.
        # Extra rule here: flatten with 10 minutes left before the trading
        # window closes so nothing rides into the last few volatile minutes.
        minutes_left = (
            snapshot.timestamp.replace(hour=15, minute=55, second=0, microsecond=0) - snapshot.timestamp
        ).total_seconds() / 60
        return minutes_left <= 10
