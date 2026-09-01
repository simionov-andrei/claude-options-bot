"""Account-level guardrails: daily loss limit, max concurrent positions,
max trades per day. These are checked before every new entry.
"""
from __future__ import annotations

import logging
from datetime import date

log = logging.getLogger(__name__)


class RiskManager:
    def __init__(
        self,
        daily_loss_limit_pct: float,
        max_concurrent_positions: int,
        max_trades_per_day: int,
    ):
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.max_concurrent_positions = max_concurrent_positions
        self.max_trades_per_day = max_trades_per_day
        self._trading_day: date = date.today()
        self._trades_today = 0
        self._realized_pnl_today = 0.0
        self._halted = False

    def _roll_day_if_needed(self) -> None:
        today = date.today()
        if today != self._trading_day:
            self._trading_day = today
            self._trades_today = 0
            self._realized_pnl_today = 0.0
            self._halted = False

    def record_trade_opened(self) -> None:
        self._roll_day_if_needed()
        self._trades_today += 1

    def record_realized_pnl(self, pnl: float, account_balance: float) -> None:
        self._roll_day_if_needed()
        self._realized_pnl_today += pnl
        loss_limit_dollars = account_balance * self.daily_loss_limit_pct
        if self._realized_pnl_today <= -abs(loss_limit_dollars):
            self._halted = True
            log.warning(
                "Daily loss limit hit (%.2f <= -%.2f). Halting new entries for today.",
                self._realized_pnl_today,
                loss_limit_dollars,
            )

    def can_open_new_trade(self, current_open_positions: int) -> tuple[bool, str]:
        self._roll_day_if_needed()
        if self._halted:
            return False, "daily loss limit reached"
        if self._trades_today >= self.max_trades_per_day:
            return False, "max trades per day reached"
        if current_open_positions >= self.max_concurrent_positions:
            return False, "max concurrent positions reached"
        return True, ""

    @property
    def realized_pnl_today(self) -> float:
        return self._realized_pnl_today

    @property
    def trades_today(self) -> int:
        return self._trades_today
