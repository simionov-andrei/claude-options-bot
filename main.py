"""Entrypoint. Loads config + env, wires up the broker client, news agent,
risk manager, strategies, and scheduler, then runs the trading loop.

Usage:
    python main.py

Defaults to Tradier's sandbox (paper trading) unless TRADIER_MODE=live is
set in .env AND broker.mode: live is set in config/settings.yaml. Both must
agree to go live, as a deliberate double-check against accidentally trading
real money.
"""
from __future__ import annotations

import logging
import os
import sys

import yaml
from dotenv import load_dotenv

from src.broker.tradier_client import TradierClient
from src.engine.order_executor import OrderExecutor
from src.engine.scheduler import Scheduler
from src.engine.trade_logger import TradeLogger
from src.news_agent.caution_gate import CautionGate
from src.risk.risk_manager import RiskManager
from src.strategies.registry import load_strategies

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("main")


def load_config(path: str = "config/settings.yaml") -> dict:
    with open(path, "r") as f:
        return yaml.safe_load(f)


def main() -> None:
    load_dotenv()
    config = load_config()

    tradier_api_key = os.getenv("TRADIER_API_KEY")
    tradier_account_id = os.getenv("TRADIER_ACCOUNT_ID")
    env_mode = os.getenv("TRADIER_MODE", "paper").lower()
    config_mode = config["broker"]["mode"].lower()

    if not tradier_api_key or not tradier_account_id:
        log.error("TRADIER_API_KEY / TRADIER_ACCOUNT_ID not set. Copy .env.example to .env and fill them in.")
        sys.exit(1)

    # Both .env and settings.yaml must say "live" to actually trade real money.
    mode = "live" if (env_mode == "live" and config_mode == "live") else "paper"
    if config_mode == "live" and env_mode != "live":
        log.warning("config/settings.yaml requests live mode but TRADIER_MODE != live in .env. Running in PAPER mode.")

    tradier = TradierClient(tradier_api_key, tradier_account_id, mode=mode)

    strategies_cfg = config["strategies"]
    strategies = load_strategies(strategies_cfg["active"])
    underlyings = strategies_cfg.get("underlyings", ["SPY"])

    caution_gate = CautionGate(tradier, config["news_agent"])
    risk_manager = RiskManager(
        daily_loss_limit_pct=config["account"]["daily_loss_limit_pct"],
        max_concurrent_positions=config["account"]["max_concurrent_positions"],
        max_trades_per_day=config["account"]["max_trades_per_day"],
    )
    trade_logger = TradeLogger()
    order_executor = OrderExecutor(tradier, trade_logger, live_orders=True)

    scheduler = Scheduler(
        tradier=tradier,
        strategies=strategies,
        underlyings=underlyings,
        caution_gate=caution_gate,
        risk_manager=risk_manager,
        order_executor=order_executor,
        trade_logger=trade_logger,
        config=config,
    )

    if mode == "live":
        log.warning("=" * 60)
        log.warning("LIVE TRADING MODE. Real orders, real money.")
        log.warning("=" * 60)
    else:
        log.info("Running in PAPER (sandbox) mode. No real money is at risk.")

    scheduler.run()


if __name__ == "__main__":
    main()
