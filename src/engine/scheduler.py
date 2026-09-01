"""The main loop: polls the market, refreshes the day's caution assessment,
runs each active strategy, sizes and places trades, and monitors exits.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta

import pytz

from src.broker.tradier_client import TradierClient
from src.engine.order_executor import OrderExecutor, select_contract
from src.engine.trade_logger import TradeLogger
from src.news_agent.caution_gate import CautionGate
from src.news_agent.models import DayAssessment
from src.risk.position_sizer import compute_contracts
from src.risk.risk_manager import RiskManager
from src.strategies.base import MarketSnapshot, StrategyBase

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        tradier: TradierClient,
        strategies: list[StrategyBase],
        underlyings: list[str],
        caution_gate: CautionGate,
        risk_manager: RiskManager,
        order_executor: OrderExecutor,
        trade_logger: TradeLogger,
        config: dict,
    ):
        self.tradier = tradier
        self.strategies = strategies
        self.strategies_by_name = {s.name: s for s in strategies}
        self.underlyings = underlyings
        self.caution_gate = caution_gate
        self.risk_manager = risk_manager
        self.order_executor = order_executor
        self.trade_logger = trade_logger
        self.config = config

        window = config["trading_window"]
        self.tz = pytz.timezone(window["timezone"])
        self.start_time = window["start"]
        self.end_time = window["end"]
        self.poll_interval = window["poll_interval_seconds"]

        self.account_cfg = config["account"]
        self.reassess_interval = timedelta(minutes=config["news_agent"].get("reassess_interval_minutes", 30))

        self._day_assessment: DayAssessment | None = None
        self._day_assessment_time: datetime | None = None

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    def _within_trading_window(self, now: datetime) -> bool:
        start_h, start_m = map(int, self.start_time.split(":"))
        end_h, end_m = map(int, self.end_time.split(":"))
        start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
        end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        return start <= now <= end and now.weekday() < 5

    def _refresh_day_assessment_if_needed(self) -> DayAssessment:
        now = datetime.now()
        if (
            self._day_assessment is None
            or self._day_assessment_time is None
            or now - self._day_assessment_time >= self.reassess_interval
        ):
            self._day_assessment = self.caution_gate.assess()
            self._day_assessment_time = now
        return self._day_assessment

    def _get_account_balance(self) -> float:
        if self.account_cfg.get("use_live_balance", True):
            bal = self.tradier.get_balance()
            if bal is not None and bal.total_equity:
                return bal.total_equity
        return self.account_cfg["balance"]

    def _build_snapshot(self, symbol: str, now: datetime) -> MarketSnapshot | None:
        bars = self.tradier.get_intraday_bars(symbol, interval="5min", lookback_days=5)
        if bars.empty:
            log.warning("No bar data for %s; skipping this poll", symbol)
            return None
        quote = self.tradier.get_quote(symbol)
        underlying_price = quote.last if quote else bars["close"].iloc[-1]

        expirations = self.tradier.get_expirations(symbol)
        chain = self.tradier.get_option_chain(symbol, expirations[0]) if expirations else []

        return MarketSnapshot(
            timestamp=now,
            underlying_symbol=symbol,
            underlying_price=underlying_price,
            bars=bars,
            option_chain=chain,
        )

    def _run_one_poll(self) -> None:
        now = self._now()
        day_assessment = self._refresh_day_assessment_if_needed()

        snapshots = {}
        for symbol in self.underlyings:
            snap = self._build_snapshot(symbol, now)
            if snap is not None:
                snapshots[symbol] = snap

        closed = self.order_executor.check_exits(snapshots, self.strategies_by_name)
        if closed:
            account_balance = self._get_account_balance()
            for _position, pnl in closed:
                self.risk_manager.record_realized_pnl(pnl, account_balance)

        strategies_with_open_position = {p.strategy_name for p in self.order_executor.open_positions}
        for strategy in self.strategies:
            if strategy.name in strategies_with_open_position:
                continue  # one open position per strategy at a time, keeps this simple and safe

            can_trade, block_reason = self.risk_manager.can_open_new_trade(len(self.order_executor.open_positions))
            if not can_trade:
                log.debug("Skipping %s: %s", strategy.name, block_reason)
                continue

            for symbol, snapshot in snapshots.items():
                intent = strategy.scan(snapshot)
                if intent is None:
                    continue

                account_balance = self._get_account_balance()
                contract = select_contract(self.tradier, intent, snapshot)
                if contract is None:
                    continue

                # Capital already spent on other open positions (any strategy)
                # isn't available for this trade -- without this, every
                # strategy would size independently against the full account
                # balance, letting combined exposure exceed it when several
                # strategies have positions open at once.
                capital_committed = sum(
                    p.entry_price * p.contracts * 100 for p in self.order_executor.open_positions
                )
                available_balance = max(0.0, account_balance - capital_committed)

                contracts = compute_contracts(
                    base_contracts=intent.base_contracts,
                    day_multiplier=day_assessment.contract_multiplier,
                    account_balance=available_balance,
                    max_risk_pct_per_trade=self.account_cfg["max_risk_pct_per_trade"],
                    option_price_per_contract=contract.mid or contract.ask,
                )
                if contracts < 1:
                    log.info("Signal from %s on %s sized to 0 contracts; skipping", strategy.name, symbol)
                    continue

                position = self.order_executor.enter(
                    intent, contracts, day_assessment.mode, contract=contract,
                    underlying_price=snapshot.underlying_price, account_balance=account_balance,
                )
                if position is not None:
                    self.risk_manager.record_trade_opened()
                break  # one new entry per strategy per poll

    def run(self) -> None:
        log.info(
            "Scheduler starting. Underlyings=%s Strategies=%s Mode=%s",
            self.underlyings,
            [s.name for s in self.strategies],
            self.config["broker"]["mode"],
        )
        last_summary_date = None
        while True:
            now = self._now()
            if self._within_trading_window(now):
                try:
                    self._run_one_poll()
                except Exception:
                    log.exception("Error during poll; continuing")
                time.sleep(self.poll_interval)
            else:
                if last_summary_date != now.date():
                    self.trade_logger.print_summary_table()
                    last_summary_date = now.date()
                log.debug("Outside trading window (%s). Sleeping.", now.strftime("%H:%M %Z"))
                time.sleep(60)
