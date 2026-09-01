"""Walk-forward backtest engine.

Feeds a strategy plugin the exact same MarketSnapshot/TradeIntent interface
it gets live (src/strategies/base.py), so a strategy needs zero changes to
be backtestable. Simulates fills with Black-Scholes (options_pricing.py)
since no historical option-chain data exists, applies the same cautious-day
position-size halving as live (historical_caution_gate.py + the real
compute_contracts from src/risk/position_sizer.py), and enforces the same
daily-loss-limit / max-trades-per-day guardrails as src/risk/risk_manager.py
(reimplemented here against simulated dates instead of wall-clock date.today()).
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Optional

import pandas as pd

from src.backtest.options_pricing import RealizedVolEstimator, bs_price, select_strike_for_delta
from src.broker.models import Position
from src.engine.trade_logger import TradeLogger
from src.risk.position_sizer import compute_contracts
from src.strategies.base import MarketSnapshot, StrategyBase, TradeIntent

log = logging.getLogger(__name__)


def _synthetic_option_symbol(underlying: str, option_type: str, strike: float) -> str:
    # Mirrors real OCC symbols closely enough for code that reads the
    # option-type char at index -9 (see no_wick_candles.should_exit).
    type_char = "C" if option_type == "call" else "P"
    return f"{underlying}{type_char}{int(round(strike * 1000)):08d}"


class BacktestEngine:
    def __init__(
        self,
        strategy: StrategyBase,
        bars: pd.DataFrame,
        symbol: str,
        daily_mode_map: dict,
        account_cfg: dict,
        trade_logger: TradeLogger,
        warmup_bars: int = 60,
    ):
        self.strategy = strategy
        self.bars = bars
        self.symbol = symbol
        self.daily_mode_map = daily_mode_map
        self.account_cfg = account_cfg
        self.trade_logger = trade_logger
        self.warmup_bars = warmup_bars
        self.vol_estimator = RealizedVolEstimator()

        self.open_trade: Optional[dict] = None
        self.closed_trades: list[dict] = []
        self._trades_today: dict = {}
        self._realized_pnl_today: dict = {}
        self._halted_dates: set = set()

    def _day_mode(self, ts: pd.Timestamp) -> tuple[str, float]:
        return self.daily_mode_map.get(ts.date(), ("normal", 1.0))

    def _can_open(self, day) -> bool:
        if day in self._halted_dates:
            return False
        return self._trades_today.get(day, 0) < self.account_cfg["max_trades_per_day"]

    def _resolve_expiration(self, ts: pd.Timestamp, dte_days: int) -> pd.Timestamp:
        exp_date = ts.date() + timedelta(days=dte_days)
        while exp_date.weekday() >= 5:
            exp_date += timedelta(days=1)
        return pd.Timestamp(datetime.combine(exp_date, time(16, 0)), tz=ts.tzinfo)

    def _current_option_price(self, window: pd.DataFrame) -> float:
        ot = self.open_trade
        ts = window.index[-1]
        S = window["close"].iloc[-1]
        T = max(0.0, (ot["expiration"] - ts).total_seconds() / (365.25 * 24 * 3600))
        return bs_price(S, ot["K"], T, ot["sigma"], ot["option_type"])

    def _enter(self, intent: TradeIntent, window: pd.DataFrame) -> None:
        ts = window.index[-1]
        S = window["close"].iloc[-1]
        option_type = "call" if intent.direction == "long_call" else "put"

        expiration = self._resolve_expiration(ts, intent.dte_min)
        T = max((expiration - ts).total_seconds() / (365.25 * 24 * 3600), 1.0 / (365.25 * 24 * 3600))

        daily_closes = window["close"].resample("1D").last().dropna().tolist()
        sigma = self.vol_estimator.estimate(daily_closes)

        K = select_strike_for_delta(S, T, sigma, option_type, intent.target_delta)
        entry_price = bs_price(S, K, T, sigma, option_type)
        if entry_price <= 0.01:
            return

        day_mode, multiplier = self._day_mode(ts)
        contracts = compute_contracts(
            base_contracts=intent.base_contracts,
            day_multiplier=multiplier,
            account_balance=self.account_cfg["balance"],
            max_risk_pct_per_trade=self.account_cfg["max_risk_pct_per_trade"],
            option_price_per_contract=entry_price,
        )
        if contracts < 1:
            return

        option_symbol = _synthetic_option_symbol(self.symbol, option_type, K)
        trade_id = self.trade_logger.log_open(
            strategy=intent.strategy_name,
            underlying=self.symbol,
            option_symbol=option_symbol,
            direction=intent.direction,
            contracts=contracts,
            entry_price=entry_price,
            day_mode=day_mode,
            opened_at=ts.to_pydatetime(),
        )

        self.open_trade = {
            "trade_id": trade_id,
            "option_type": option_type,
            "K": K,
            "entry_time": ts,
            "expiration": expiration,
            "sigma": sigma,
            "contracts": contracts,
            "entry_price": entry_price,
            "stop_loss_pct": intent.stop_loss_pct,
            "take_profit_pct": intent.take_profit_pct,
            "option_symbol": option_symbol,
            "day_mode": day_mode,
            "notes": intent.notes,
        }
        day = ts.date()
        self._trades_today[day] = self._trades_today.get(day, 0) + 1

    def _close(self, ts: pd.Timestamp, exit_price: float, reason: str) -> None:
        ot = self.open_trade
        pnl = (exit_price - ot["entry_price"]) * ot["contracts"] * 100
        self.trade_logger.log_close(ot["trade_id"], exit_price, pnl, closed_at=ts.to_pydatetime())

        day = ts.date()
        self._realized_pnl_today[day] = self._realized_pnl_today.get(day, 0.0) + pnl
        loss_limit = self.account_cfg["balance"] * self.account_cfg["daily_loss_limit_pct"]
        if self._realized_pnl_today[day] <= -abs(loss_limit):
            self._halted_dates.add(day)

        self.closed_trades.append(
            {
                "entry_time": ot["entry_time"],
                "exit_time": ts,
                "strategy": ot.get("notes", ""),
                "option_type": ot["option_type"],
                "strike": ot["K"],
                "contracts": ot["contracts"],
                "entry_price": ot["entry_price"],
                "exit_price": exit_price,
                "pnl": pnl,
                "reason": reason,
                "day_mode": ot["day_mode"],
                "stop_loss_pct": ot["stop_loss_pct"],
                "take_profit_pct": ot["take_profit_pct"],
            }
        )
        self.open_trade = None

    def _check_exit(self, window: pd.DataFrame) -> None:
        ot = self.open_trade
        ts = window.index[-1]
        S = window["close"].iloc[-1]
        T = max(0.0, (ot["expiration"] - ts).total_seconds() / (365.25 * 24 * 3600))
        current_price = bs_price(S, ot["K"], T, ot["sigma"], ot["option_type"])
        change_pct = (current_price - ot["entry_price"]) / ot["entry_price"] if ot["entry_price"] > 0 else 0.0

        reason = None
        if T <= 0:
            reason = "expired"
        elif change_pct <= -ot["stop_loss_pct"]:
            reason = "stop loss"
        elif change_pct >= ot["take_profit_pct"]:
            reason = "take profit"
        else:
            fake_position = Position(
                option_symbol=ot["option_symbol"],
                underlying=self.symbol,
                quantity=ot["contracts"],
                cost_basis=ot["entry_price"] * ot["contracts"] * 100,
                entry_price=ot["entry_price"],
            )
            snapshot = MarketSnapshot(timestamp=ts, underlying_symbol=self.symbol, underlying_price=S, bars=window, option_chain=[])
            if self.strategy.should_exit(fake_position, snapshot):
                reason = "strategy exit"

        if reason:
            self._close(ts, current_price, reason)

    def run(self) -> dict:
        if len(self.bars) <= self.warmup_bars:
            raise ValueError(f"Not enough bars ({len(self.bars)}) for warmup ({self.warmup_bars})")

        for i in range(self.warmup_bars, len(self.bars)):
            window = self.bars.iloc[: i + 1]
            ts = window.index[-1]

            if self.open_trade is not None:
                self._check_exit(window)

            if self.open_trade is None and self._can_open(ts.date()):
                snapshot = MarketSnapshot(
                    timestamp=ts,
                    underlying_symbol=self.symbol,
                    underlying_price=window["close"].iloc[-1],
                    bars=window,
                    option_chain=[],
                )
                intent = self.strategy.scan(snapshot)
                if intent is not None:
                    self._enter(intent, window)

        if self.open_trade is not None:
            last_window = self.bars
            exit_price = self._current_option_price(last_window)
            self._close(last_window.index[-1], exit_price, "backtest end")

        return self._compute_stats()

    def _compute_stats(self) -> dict:
        trades = self.closed_trades
        n = len(trades)
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        total_pnl = sum(t["pnl"] for t in trades)
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))

        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in trades:
            equity += t["pnl"]
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)

        # Exits are only evaluated once per bar. On a coarse bar interval, a
        # trade can gap straight past the strategy's own stop-loss/take-profit
        # level before the backtest ever checks it -- inflating simulated P&L
        # beyond what a live bot polling every ~60s would actually capture.
        # This clips each trade's simulated pnl to what its own thresholds
        # intended, as a sanity check against that bias.
        capped_total = 0.0
        for t in trades:
            cost = t["entry_price"] * t["contracts"] * 100
            lo, hi = -t["stop_loss_pct"] * cost, t["take_profit_pct"] * cost
            capped_total += max(lo, min(hi, t["pnl"]))

        return {
            "trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": (len(wins) / n * 100) if n else 0.0,
            "total_pnl": total_pnl,
            "avg_win": (gross_win / len(wins)) if wins else 0.0,
            "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0),
            "max_drawdown": max_dd,
            "granularity_corrected_pnl": capped_total,
        }
