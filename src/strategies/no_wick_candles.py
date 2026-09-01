"""'No wick candle' retest strategy.

Pattern: in a bullish trend, a bullish candle whose low equals its open (no
bottom wick) marks a level. If price later dips back down and taps that
level while the trend is still bullish, buy. Mirror image in a bearish
trend: a bearish candle whose high equals its open (no top wick) marks a
level; a later retest from below while still bearish triggers a sell.

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

    def __init__(self):
        self._zones: dict[str, list[WickZone]] = {}

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
                return TradeIntent(
                    strategy_name=self.name,
                    underlying_symbol=symbol,
                    direction=direction,
                    target_delta=0.45,
                    dte_min=0,
                    dte_max=3,
                    stop_loss_pct=0.4,
                    take_profit_pct=0.8,
                    base_contracts=1,
                    order_type="market",
                    notes=f"no-wick retest @ {zone.level:.2f} ({zone.direction}, marked {zone.created_at})",
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
        # Stop-loss / take-profit are enforced by the engine already.
        # Extra rule: if the trend that justified this trade has flipped,
        # the setup is invalidated -- exit rather than hope it comes back.
        bars = snapshot.bars
        if bars is None or len(bars) < self.trend_slow + 2:
            return False
        trend = self._trend(bars)
        if trend is None:
            return False

        option_type = "call" if position.option_symbol[-9] == "C" else "put"
        return (option_type == "call" and trend == "bearish") or (option_type == "put" and trend == "bullish")
