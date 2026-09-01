"""Support & Resistance bounce/rejection strategy.

Entry (unchanged from the first version): a bar is a swing high/low if it's
the extreme within `swing_lookback` bars on each side (fractal method).
Nearby swing points get clustered into levels; a level only counts once
it's been touched at least `min_touches` times. Entry fires when price
retests a validated level and closes back away from it (a rejection, not
just a touch) -- buy a call on a support bounce, buy a put on a resistance
rejection.

Exit (researched and updated -- this is the part that changed): standard
S/R practice is a STRUCTURAL exit, not a fixed % of premium --
  - stop-loss just beyond the level that generated the entry (a break
    through it invalidates the setup)
  - take-profit at the next validated opposing level in the trade's
    direction (the next resistance above, for a support bounce; the next
    support below, for a resistance rejection)
  - if no further level exists yet in the detected set, fall back to a
    fixed risk-reward multiple -- sources are consistent that 1:2 is the
    commonly cited minimum, so that's the fallback here.
This mirrors ORB's approach: the rule is defined on the underlying price,
so it's enforced in should_exit() against the live underlying price, not
through the engine's generic premium-pct stop_loss_pct/take_profit_pct
(set to wide backstop values below and not the real rule).

Sources: TradingWithRayner, FXOpen, ChartsWatcher, and other price-action
guides on take-profit-at-next-level and minimum 1:2 risk-reward for S/R
trades; same swing-detection sourcing as before for the entry side.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd

from src.broker.models import Position
from src.strategies.base import MarketSnapshot, StrategyBase, TradeIntent


class SupportResistanceStrategy(StrategyBase):
    name = "support_resistance"

    swing_lookback = 5
    level_lookback_bars = 150
    level_tolerance_pct = 0.15  # cluster swing points within this % of each other into one level
    min_touches = 2
    retest_tolerance_pct = 0.10  # how close a bar must come to a level, as a fraction of its own range

    # Place the stop this % beyond the level itself, not exactly on it.
    stop_buffer_pct = 0.05
    # Used only when no further validated level exists yet in the current
    # detected set to serve as the take-profit target.
    fallback_risk_reward = 2.0

    def __init__(self):
        self._last_signal_bar: dict[str, pd.Timestamp] = {}
        self._active_trade: dict[str, dict] = {}

    def _find_swing_points(self, bars: pd.DataFrame) -> tuple[list[float], list[float]]:
        # Vectorized fractal detection: bar i is a swing high/low if it's the
        # strict extreme among the 2n+1 bars centered on it. A rolling
        # window max/min over the raw numpy arrays (not pandas .iloc in a
        # Python loop) makes this cheap enough to call on every bar.
        n = self.swing_lookback
        highs = bars["high"].to_numpy()
        lows = bars["low"].to_numpy()
        length = len(bars)
        if length < 2 * n + 1:
            return [], []

        window = 2 * n + 1
        h_series = pd.Series(highs)
        l_series = pd.Series(lows)
        rolling_max = h_series.rolling(window, center=True).max().to_numpy()
        rolling_min = l_series.rolling(window, center=True).min().to_numpy()

        is_swing_high = highs == rolling_max
        is_swing_low = lows == rolling_min
        valid = slice(n, length - n)

        swing_highs = highs[valid][is_swing_high[valid]].tolist()
        swing_lows = lows[valid][is_swing_low[valid]].tolist()
        return swing_highs, swing_lows

    def _cluster_levels(self, prices: list[float]) -> list[float]:
        if not prices:
            return []
        prices = sorted(prices)
        clusters = [[prices[0]]]
        for p in prices[1:]:
            if abs(p - clusters[-1][-1]) <= self.level_tolerance_pct / 100 * clusters[-1][-1]:
                clusters[-1].append(p)
            else:
                clusters.append([p])
        return [sum(c) / len(c) for c in clusters if len(c) >= self.min_touches]

    def scan(self, snapshot: MarketSnapshot) -> Optional[TradeIntent]:
        bars = snapshot.bars
        symbol = snapshot.underlying_symbol
        if bars is None or len(bars) < self.level_lookback_bars:
            return None
        if self._last_signal_bar.get(symbol) == bars.index[-1]:
            return None

        recent = bars.iloc[-self.level_lookback_bars :]
        swing_highs, swing_lows = self._find_swing_points(recent)
        resistance_levels = self._cluster_levels(swing_highs)
        support_levels = self._cluster_levels(swing_lows)

        last = bars.iloc[-1]
        price = last["close"]
        candle_range = last["high"] - last["low"]
        if candle_range <= 0:
            return None
        tolerance = max(candle_range * self.retest_tolerance_pct, price * 0.0005)

        for level in support_levels:
            touched = last["low"] <= level + tolerance
            rejected = last["close"] > level
            if touched and rejected:
                self._last_signal_bar[symbol] = bars.index[-1]
                stop_price = level * (1 - self.stop_buffer_pct / 100)
                above = [r for r in resistance_levels if r > price]
                target_price = min(above) if above else price + self.fallback_risk_reward * (price - stop_price)

                self._active_trade[symbol] = {
                    "direction": "long_call", "stop_price": stop_price, "target_price": target_price,
                }
                return TradeIntent(
                    strategy_name=self.name,
                    underlying_symbol=symbol,
                    direction="long_call",
                    target_delta=0.45,
                    dte_min=0,
                    dte_max=3,
                    stop_loss_pct=0.9,  # backstop only -- real rule is in should_exit()
                    take_profit_pct=5.0,
                    base_contracts=1,
                    order_type="market",
                    notes=f"support bounce @ {level:.2f} stop_u={stop_price:.2f} target_u={target_price:.2f}",
                )

        for level in resistance_levels:
            touched = last["high"] >= level - tolerance
            rejected = last["close"] < level
            if touched and rejected:
                self._last_signal_bar[symbol] = bars.index[-1]
                stop_price = level * (1 + self.stop_buffer_pct / 100)
                below = [s for s in support_levels if s < price]
                target_price = max(below) if below else price - self.fallback_risk_reward * (stop_price - price)

                self._active_trade[symbol] = {
                    "direction": "long_put", "stop_price": stop_price, "target_price": target_price,
                }
                return TradeIntent(
                    strategy_name=self.name,
                    underlying_symbol=symbol,
                    direction="long_put",
                    target_delta=0.45,
                    dte_min=0,
                    dte_max=3,
                    stop_loss_pct=0.9,
                    take_profit_pct=5.0,
                    base_contracts=1,
                    order_type="market",
                    notes=f"resistance rejection @ {level:.2f} stop_u={stop_price:.2f} target_u={target_price:.2f}",
                )

        return None

    def should_exit(self, position: Position, snapshot: MarketSnapshot) -> bool:
        symbol = snapshot.underlying_symbol
        trade = self._active_trade.get(symbol)
        if trade is None:
            return False

        price = snapshot.underlying_price
        direction = trade["direction"]
        hit_stop = (direction == "long_call" and price <= trade["stop_price"]) or (
            direction == "long_put" and price >= trade["stop_price"]
        )
        hit_target = (direction == "long_call" and price >= trade["target_price"]) or (
            direction == "long_put" and price <= trade["target_price"]
        )
        minutes_left = (
            snapshot.timestamp.replace(hour=15, minute=55, second=0, microsecond=0) - snapshot.timestamp
        ).total_seconds() / 60
        eod = minutes_left <= 10

        if hit_stop or hit_target or eod:
            del self._active_trade[symbol]
            return True
        return False
