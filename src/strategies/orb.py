"""Opening Range Breakout (ORB), 5-minute variant.

Published rules (Zarattini & Aziz, "Can Day Trading Really Be Profitable?"):
if the first 5-minute bar of the session closes up, buy at the start of the
second 5-minute bar; if it closes down, sell short. Skip the day if the
first bar's open and close are about the same (no real breakout to follow).
Stop-loss at the low (long) / high (short) of that first bar. Take-profit
at 10x the entry-to-stop risk. If neither is hit, flatten at the close.

One trade per symbol per day, decided entirely by the first bar -- this is
a day-trading strategy, not a multi-day one.

Note: the published rule's stop/target are defined on the UNDERLYING price,
not on option premium. This strategy implements them that way via
should_exit() (which sees the live underlying price every poll), rather
than through the engine's generic stop_loss_pct/take_profit_pct -- those are
set to wide backstop values here and aren't the real exit rule. That also
means the backtester's usual "granularity-corrected PnL" (which caps a
trade at its own stop_loss_pct/take_profit_pct) isn't a meaningful number
for this strategy; judge it on raw PnL/win-rate instead, keeping in mind
exits are still only checked once per bar like everything else here.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from src.broker.models import Position
from src.strategies.base import MarketSnapshot, StrategyBase, TradeIntent


class ORBStrategy(StrategyBase):
    name = "orb"

    take_profit_r_multiple = 10.0
    # Skip the day if the first bar's |close-open| is smaller than this
    # fraction of its own high-low range (treated as "no real breakout").
    flat_threshold_pct = 0.05

    def __init__(self):
        self._decided_today: dict[str, date] = {}
        self._active_trade: dict[str, dict] = {}

    def scan(self, snapshot: MarketSnapshot) -> Optional[TradeIntent]:
        bars = snapshot.bars
        symbol = snapshot.underlying_symbol
        if bars is None or bars.empty:
            return None

        today = snapshot.timestamp.date()
        todays_bars = bars[bars.index.date == today]

        if self._decided_today.get(symbol) == today:
            return None
        if len(todays_bars) < 2:
            return None
        if len(todays_bars) > 2:
            # Missed the exact bar-2 window (e.g. bot started mid-session) --
            # the rule is specifically "at the start of the second bar", so
            # don't chase a late entry. Mark today done and move on.
            self._decided_today[symbol] = today
            return None

        self._decided_today[symbol] = today

        opening = todays_bars.iloc[0]
        o, c, h, l = opening["open"], opening["close"], opening["high"], opening["low"]
        opening_range = h - l
        if opening_range <= 0 or abs(c - o) < self.flat_threshold_pct * opening_range:
            return None  # flat open -- no breakout to follow today

        entry_price = snapshot.underlying_price
        if c > o:
            direction = "long_call"
            stop_price = l
        else:
            direction = "long_put"
            stop_price = h

        risk = abs(entry_price - stop_price)
        if risk <= 0:
            return None
        target_price = (
            entry_price + self.take_profit_r_multiple * risk
            if direction == "long_call"
            else entry_price - self.take_profit_r_multiple * risk
        )

        self._active_trade[symbol] = {
            "date": today,
            "direction": direction,
            "stop_price": stop_price,
            "target_price": target_price,
        }

        return TradeIntent(
            strategy_name=self.name,
            underlying_symbol=symbol,
            direction=direction,
            target_delta=0.45,
            dte_min=0,
            dte_max=1,
            # Backstop only -- real exit rule is in should_exit() below.
            stop_loss_pct=0.9,
            take_profit_pct=5.0,
            base_contracts=1,
            order_type="market",
            notes=f"ORB {direction} stop_u={stop_price:.2f} target_u={target_price:.2f} (10R)",
        )

    def should_exit(self, position: Position, snapshot: MarketSnapshot) -> bool:
        symbol = snapshot.underlying_symbol
        trade = self._active_trade.get(symbol)
        if trade is None or trade["date"] != snapshot.timestamp.date():
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
