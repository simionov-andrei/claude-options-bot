"""Turns a TradeIntent + sized contract count into a real order, and
monitors open positions for stop-loss/take-profit/strategy-defined exits.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from src.broker.models import OptionContract
from src.broker.tradier_client import TradierClient
from src.engine.trade_logger import TradeLogger
from src.strategies.base import MarketSnapshot, TradeIntent

log = logging.getLogger(__name__)


@dataclass
class OpenPosition:
    trade_id: int
    strategy_name: str
    underlying_symbol: str
    option_symbol: str
    direction: str
    contracts: int
    entry_price: float
    stop_loss_pct: float
    take_profit_pct: float
    opened_at: datetime


def _pick_from_chain(chain: list[OptionContract], intent: TradeIntent, expiration_label: str) -> Optional[OptionContract]:
    option_type = "call" if intent.direction == "long_call" else "put"
    candidates = [c for c in chain if c.option_type == option_type and c.bid > 0 and c.ask > 0 and c.delta is not None]
    if not candidates:
        log.warning("No liquid %s contracts found for %s exp %s", option_type, intent.underlying_symbol, expiration_label)
        return None
    return min(candidates, key=lambda c: abs(abs(c.delta) - intent.target_delta))


def select_contract(tradier: TradierClient, intent: TradeIntent, snapshot: Optional[MarketSnapshot] = None) -> Optional[OptionContract]:
    # If the caller already has a snapshot with a fetched chain (the engine builds
    # one every poll to feed strategy.scan()), and that chain's expiration still
    # satisfies this intent's DTE window, reuse it instead of re-fetching --
    # saves 2 Tradier API calls with no loss of freshness (same poll, same data).
    if snapshot is not None and snapshot.option_chain:
        chain_expiration = snapshot.option_chain[0].expiration
        dte = (datetime.strptime(chain_expiration, "%Y-%m-%d").date() - date.today()).days
        if intent.dte_min <= dte <= intent.dte_max:
            return _pick_from_chain(snapshot.option_chain, intent, chain_expiration)

    expirations = tradier.get_expirations(intent.underlying_symbol)
    if not expirations:
        log.warning("No expirations found for %s", intent.underlying_symbol)
        return None

    today = date.today()
    in_range = [
        e for e in expirations
        if intent.dte_min <= (datetime.strptime(e, "%Y-%m-%d").date() - today).days <= intent.dte_max
    ]
    target_expiration = in_range[0] if in_range else expirations[0]

    chain = tradier.get_option_chain(intent.underlying_symbol, target_expiration)
    return _pick_from_chain(chain, intent, target_expiration)


class OrderExecutor:
    def __init__(self, tradier: TradierClient, trade_logger: TradeLogger, live_orders: bool):
        self.tradier = tradier
        self.trade_logger = trade_logger
        self.live_orders = live_orders  # if False, logs intended trades without calling the broker (dry run)
        self.open_positions: list[OpenPosition] = []

    def enter(
        self, intent: TradeIntent, contracts: int, day_mode: str, contract: Optional[OptionContract] = None
    ) -> Optional[OpenPosition]:
        # Callers that already selected a contract (e.g. the scheduler, which
        # needs one for position sizing before calling enter()) should pass
        # it here instead of letting this re-select -- same trade, same
        # market moment, no reason to pay for a second round trip.
        if contract is None:
            contract = select_contract(self.tradier, intent)
        if contract is None:
            return None

        entry_price = contract.mid or contract.ask
        side = "buy_to_open"

        if self.live_orders:
            result = self.tradier.place_single_leg_order(
                underlying=intent.underlying_symbol,
                option_symbol=contract.symbol,
                side=side,
                quantity=contracts,
                order_type=intent.order_type,
                limit_price=intent.limit_price,
            )
            if result.status == "error":
                log.error("Failed to open position for %s: %s", intent.strategy_name, result.error)
                return None
        else:
            log.info("[DRY RUN] Would place order: BUY %s x%d %s", contract.symbol, contracts, intent.direction)

        trade_id = self.trade_logger.log_open(
            strategy=intent.strategy_name,
            underlying=intent.underlying_symbol,
            option_symbol=contract.symbol,
            direction=intent.direction,
            contracts=contracts,
            entry_price=entry_price,
            day_mode=day_mode,
        )

        position = OpenPosition(
            trade_id=trade_id,
            strategy_name=intent.strategy_name,
            underlying_symbol=intent.underlying_symbol,
            option_symbol=contract.symbol,
            direction=intent.direction,
            contracts=contracts,
            entry_price=entry_price,
            stop_loss_pct=intent.stop_loss_pct,
            take_profit_pct=intent.take_profit_pct,
            opened_at=datetime.now(),
        )
        self.open_positions.append(position)
        log.info(
            "Opened %s: %s x%d @ %.2f (%s day)",
            intent.strategy_name, contract.symbol, contracts, entry_price, day_mode,
        )
        return position

    def close(self, position: OpenPosition, reason: str) -> float:
        quote = self.tradier.get_quote(position.option_symbol)
        exit_price = quote.last if quote and quote.last else position.entry_price

        if self.live_orders:
            result = self.tradier.place_single_leg_order(
                underlying=position.underlying_symbol,
                option_symbol=position.option_symbol,
                side="sell_to_close",
                quantity=position.contracts,
                order_type="market",
            )
            if result.status == "error":
                log.error("Failed to close position %s: %s", position.option_symbol, result.error)
        else:
            log.info("[DRY RUN] Would close: SELL %s x%d", position.option_symbol, position.contracts)

        pnl = (exit_price - position.entry_price) * position.contracts * 100
        self.trade_logger.log_close(position.trade_id, exit_price, pnl)
        self.open_positions.remove(position)
        log.info(
            "Closed %s (%s): entry %.2f -> exit %.2f, pnl $%.2f",
            position.option_symbol, reason, position.entry_price, exit_price, pnl,
        )
        return pnl

    def check_exits(self, snapshot_by_symbol: dict[str, MarketSnapshot], strategies_by_name) -> list[tuple[OpenPosition, float]]:
        closed = []
        for position in list(self.open_positions):
            quote = self.tradier.get_quote(position.option_symbol)
            if quote is None or not quote.last:
                continue
            current_price = quote.last
            change_pct = (current_price - position.entry_price) / position.entry_price

            reason = None
            if change_pct <= -position.stop_loss_pct:
                reason = "stop loss"
            elif change_pct >= position.take_profit_pct:
                reason = "take profit"
            else:
                strategy = strategies_by_name.get(position.strategy_name)
                snapshot = snapshot_by_symbol.get(position.underlying_symbol)
                if strategy is not None and snapshot is not None:
                    from src.broker.models import Position as BrokerPosition

                    fake_position = BrokerPosition(
                        option_symbol=position.option_symbol,
                        underlying=position.underlying_symbol,
                        quantity=position.contracts,
                        cost_basis=position.entry_price * position.contracts * 100,
                        entry_price=position.entry_price,
                    )
                    if strategy.should_exit(fake_position, snapshot):
                        reason = "strategy exit signal"

            if reason:
                pnl = self.close(position, reason)
                closed.append((position, pnl))
        return closed
