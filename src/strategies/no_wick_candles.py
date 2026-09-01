"""'No wick candle' retest strategy.

Entry (unchanged): in a bullish trend, a bullish candle whose low equals its
open (no bottom wick) marks a level. If price later dips back down and taps
that level while the trend is still bullish, buy. Mirror image in a
bearish trend: a bearish candle whose high equals its open (no top wick)
marks a level; a later retest from below while still bearish triggers a
put.

Exit (researched and updated -- this is the part that changed from the
first version, which used a fixed 40%/80% of premium). Sources on this
specific pattern (no-wick / Marubozu-style candles) consistently describe a
structural exit: stop beyond the signal candle's own extreme -- which here
is just the marked zone level itself, since "no wick" means that level IS
the candle's low/high -- and a take-profit at a reward:risk multiple off
that same risk distance, with sources citing 2-3x ATR for this pattern
specifically. This mirrors the same family of fix already applied to ORB
(10R) and support_resistance (next-level/2R): a rule defined on the
underlying price, enforced in should_exit() against the live underlying
price, not through the engine's generic premium-pct
stop_loss_pct/take_profit_pct (set to wide backstop values below and not
the real rule).

Sources: Medium ("No Upper Wick Bullish K-Line Breakout Strategy" -- stop
at the signal candle's opposite extreme, take-profit at the resulting
risk:reward multiple), PriceActionNinja on Marubozu (2-3x ATR profit
target), FTMO's "No Wick Strategy" writeup. Same original sourcing as
before for the entry-side pattern definition.

Note on the claimed win rate: that number comes from a promotional video and
isn't something this code can verify on its own -- treat it as an untested
claim. Run this in paper mode and check src/engine/trade_logger's daily
summary / data/trades.db before trusting it with size.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

import pandas as pd

from src.broker.models import Position
from src.strategies.base import MarketSnapshot, StrategyBase, TradeIntent


@dataclass
class WickZone:
    level: float
    direction: Literal["bullish", "bearish"]
    created_at: pd.Timestamp
    expires_at: pd.Timestamp


class NoWickCandlesStrategy(StrategyBase):
    name = "no_wick_candles"

    # Trend filter: fast/slow EMA on closes.
    trend_fast = 20
    trend_slow = 50

    # A candle counts as "no wick" if its wick on the trend side is at most
    # this fraction of the candle's full high-low range.
    wick_tolerance_pct = 0.10

    # How close a later candle must come to a marked level to count as a tap,
    # as a fraction of that candle's own high-low range.
    retest_tolerance_pct = 0.10

    # Forget a zone if it hasn't been retested within this many bars.
    zone_max_age_bars = 30
    max_tracked_zones = 20

    # Place the stop this % beyond the zone level itself, not exactly on it.
    stop_buffer_pct = 0.05
    # Take-profit as a multiple of the entry-to-stop risk distance. Sources
    # cite 2-3x ATR for this pattern; 3R is the upper end of that translated
    # into risk-multiple terms.
    risk_reward_multiple = 3.0

    def __init__(self):
        self._zones: dict[str, list[WickZone]] = {}
        self._active_trade: dict[str, dict] = {}

    def _trend(self, bars: pd.DataFrame) -> Optional[str]:
        ema_fast = bars["close"].ewm(span=self.trend_fast, adjust=False).mean()
        ema_slow = bars["close"].ewm(span=self.trend_slow, adjust=False).mean()
        if ema_fast.iloc[-1] > ema_slow.iloc[-1]:
            return "bullish"
        if ema_fast.iloc[-1] < ema_slow.iloc[-1]:
            return "bearish"
        return None

    def scan(self, snapshot: MarketSnapshot) -> Optional[TradeIntent]:
        bars = snapshot.bars
        if bars is None or len(bars) < self.trend_slow + 2:
            return None

        symbol = snapshot.underlying_symbol
        zones = self._zones.setdefault(symbol, [])

        trend = self._trend(bars)
        if trend is None:
            return None

        last = bars.iloc[-1]

        # 1. Check existing zones for a retest, oldest first.
        for zone in list(zones):
            if last.name <= zone.created_at:
                continue
            if last.name > zone.expires_at:
                zones.remove(zone)
                continue

            candle_range = last["high"] - last["low"]
            tolerance = max(candle_range * self.retest_tolerance_pct, zone.level * 0.0005)

            tapped = (
                zone.direction == "bullish" and last["low"] <= zone.level + tolerance
            ) or (
                zone.direction == "bearish" and last["high"] >= zone.level - tolerance
            )
            if tapped and trend == zone.direction:
                zones.remove(zone)
                direction = "long_call" if zone.direction == "bullish" else "long_put"
                entry_price = snapshot.underlying_price

                if zone.direction == "bullish":
                    stop_price = zone.level * (1 - self.stop_buffer_pct / 100)
                    risk = entry_price - stop_price
                    target_price = entry_price + self.risk_reward_multiple * risk
                else:
                    stop_price = zone.level * (1 + self.stop_buffer_pct / 100)
                    risk = stop_price - entry_price
                    target_price = entry_price - self.risk_reward_multiple * risk

                if risk <= 0:
                    continue  # degenerate (entry already past the level) -- skip

                self._active_trade[symbol] = {
                    "direction": direction, "stop_price": stop_price, "target_price": target_price,
                }
                return TradeIntent(
                    strategy_name=self.name,
                    underlying_symbol=symbol,
                    direction=direction,
                    target_delta=0.45,
                    dte_min=0,
                    dte_max=3,
                    stop_loss_pct=0.9,  # backstop only -- real rule is in should_exit()
                    take_profit_pct=5.0,
                    base_contracts=1,
                    order_type="market",
                    notes=f"no-wick retest @ {zone.level:.2f} ({zone.direction}) stop_u={stop_price:.2f} target_u={target_price:.2f}",
                )
            if tapped:
                # Price tapped the level but the trend has since flipped -- the
                # setup is invalidated, drop the zone rather than trade it.
                zones.remove(zone)

        # 2. Look for a new no-wick candle to mark, on the just-closed bar.
        body_high = max(last["open"], last["close"])
        body_low = min(last["open"], last["close"])
        candle_range = last["high"] - last["low"]
        if candle_range <= 0:
            return None

        is_bullish_candle = last["close"] > last["open"]
        is_bearish_candle = last["close"] < last["open"]
        bottom_wick = body_low - last["low"]
        top_wick = last["high"] - body_high

        if len(bars.index) >= 2:
            bar_interval = bars.index.to_series().diff().median()
        else:
            bar_interval = pd.Timedelta(minutes=5)
        expires_at = last.name + bar_interval * self.zone_max_age_bars

        if trend == "bullish" and is_bullish_candle and bottom_wick <= candle_range * self.wick_tolerance_pct:
            zones.append(WickZone(level=last["low"], direction="bullish", created_at=last.name, expires_at=expires_at))
        elif trend == "bearish" and is_bearish_candle and top_wick <= candle_range * self.wick_tolerance_pct:
            zones.append(WickZone(level=last["high"], direction="bearish", created_at=last.name, expires_at=expires_at))

        if len(zones) > self.max_tracked_zones:
            zones.pop(0)

        return None

    def should_exit(self, position: Position, snapshot: MarketSnapshot) -> bool:
        symbol = snapshot.underlying_symbol
        trade = self._active_trade.get(symbol)

        if trade is not None:
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

        # Extra rule, independent of the price-based exit above: if the
        # trend that justified this trade has flipped, the setup is
        # invalidated -- exit rather than hope it comes back.
        bars = snapshot.bars
        if bars is None or len(bars) < self.trend_slow + 2:
            return False
        trend = self._trend(bars)
        if trend is None:
            return False

        option_type = "call" if position.option_symbol[-9] == "C" else "put"
        trend_flipped = (option_type == "call" and trend == "bearish") or (option_type == "put" and trend == "bullish")
        if trend_flipped:
            self._active_trade.pop(symbol, None)
        return trend_flipped
