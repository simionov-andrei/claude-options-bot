"""Backtest a strategy plugin against real historical price data.

Usage:
    python backtest.py --strategy no_wick_candles --symbol SPY
    python backtest.py --strategy example_momentum --symbol SPY --range 60d --account-balance 25000

Pulls historical bars from Yahoo Finance (free, no API key -- Tradier has no
historical options data of its own). Simulates option premiums with
Black-Scholes since no historical options-chain data exists anywhere for
free; see src/backtest/options_pricing.py for the honest caveats on that
approximation. Applies the same cautious-day contract-halving and risk
limits as live trading. See README's Backtesting section for details.
"""
from __future__ import annotations

import argparse
import logging

import yaml

from src.backtest.data_provider import get_daily_bars, get_intraday_bars
from src.backtest.engine import BacktestEngine
from src.backtest.historical_caution_gate import build_daily_mode_map
from src.engine.trade_logger import TradeLogger
from src.strategies.registry import load_strategies

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backtest")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--strategy", required=True, help="Strategy filename under src/strategies/, without .py")
    parser.add_argument("--symbol", default="SPY")
    parser.add_argument("--interval", default="5m", help="Bar interval (Yahoo limits how far back each interval goes)")
    parser.add_argument("--range", dest="range_", default="60d", help="How far back to pull (Yahoo max for 5m is ~60d)")
    parser.add_argument("--account-balance", type=float, default=25000.0, help="Simulated account balance for position sizing")
    parser.add_argument("--max-risk-pct", type=float, default=None, help="Max fraction of account balance risked per trade (overrides config/settings.yaml's account.max_risk_pct_per_trade, e.g. 0.35 for 35%%)")
    parser.add_argument("--no-caution-gate", action="store_true", help="Disable the VIX-based cautious-day contract halving")
    parser.add_argument("--db", default=None, help="SQLite path for trade records (defaults to data/backtest_<strategy>_<symbol>.db)")
    args = parser.parse_args()

    with open("config/settings.yaml") as f:
        config = yaml.safe_load(f)

    strategy = load_strategies([args.strategy])[0]

    log.info("Fetching %s %s bars over %s from Yahoo Finance...", args.symbol, args.interval, args.range_)
    bars = get_intraday_bars(args.symbol, interval=args.interval, range_=args.range_)
    if bars.empty:
        log.error("No bar data returned for %s", args.symbol)
        return
    log.info("Got %d bars: %s -> %s", len(bars), bars.index[0], bars.index[-1])

    interval_minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60, "1h": 60}.get(args.interval)
    if interval_minutes and interval_minutes >= 30:
        log.warning(
            "Bar interval is %s. Exits (stop-loss/take-profit) are only evaluated once per bar, and "
            "0-3 DTE options have very high gamma -- a single %s gap can blow straight past the intended "
            "exit level before the backtest ever checks it, inflating simulated wins well beyond what a "
            "live bot polling every ~60s would actually capture. Treat P&L totals from this run with "
            "real skepticism; win rate is more trustworthy than dollar amounts here.",
            args.interval, args.interval,
        )

    news_cfg = config["news_agent"]
    if args.no_caution_gate:
        daily_mode_map = {}
    else:
        log.info("Fetching VIX daily history for the cautious-day gate...")
        vix_daily = get_daily_bars("^VIX", range_="2y")
        daily_mode_map = build_daily_mode_map(
            vix_daily,
            news_cfg.get("vix_caution_level", 25),
            news_cfg.get("vix_caution_change_pct", 8),
            news_cfg.get("cautious_contract_multiplier", 0.5),
        )
        cautious_days = sum(1 for mode, _ in daily_mode_map.values() if mode == "cautious")
        log.info("VIX gate: %d cautious day(s) out of %d in range", cautious_days, len(daily_mode_map))

    account_cfg = dict(config["account"])
    account_cfg["balance"] = args.account_balance
    if args.max_risk_pct is not None:
        account_cfg["max_risk_pct_per_trade"] = args.max_risk_pct

    db_path = args.db or f"data/backtest_{args.strategy}_{args.symbol}.db"
    trade_logger = TradeLogger(db_path=db_path)

    engine = BacktestEngine(
        strategy=strategy,
        bars=bars,
        symbol=args.symbol,
        daily_mode_map=daily_mode_map,
        account_cfg=account_cfg,
        trade_logger=trade_logger,
    )
    stats = engine.run()

    print()
    print(f"=== Backtest: {args.strategy} on {args.symbol} ({bars.index[0].date()} -> {bars.index[-1].date()}) ===")
    print(f"Account balance (simulated): ${args.account_balance:,.2f}   Max risk/trade: {account_cfg['max_risk_pct_per_trade']*100:.0f}%   Cautious-day gate: {'off' if args.no_caution_gate else 'on'}")
    print()
    trade_logger.print_summary_table(days=365)
    print()
    print(f"Trades: {stats['trades']}   Wins: {stats['wins']}   Losses: {stats['losses']}   Win rate: {stats['win_rate_pct']:.1f}%")
    print(f"Total PnL: ${stats['total_pnl']:,.2f}   Avg win: ${stats['avg_win']:,.2f}   Avg loss: ${stats['avg_loss']:,.2f}")
    pf = stats["profit_factor"]
    print(f"Profit factor: {'inf' if pf == float('inf') else f'{pf:.2f}'}   Max drawdown: ${stats['max_drawdown']:,.2f}")
    print(f"Granularity-corrected PnL (each trade capped at its own stop/take-profit): ${stats['granularity_corrected_pnl']:,.2f}")
    print()
    print("NOTE: fills are simulated with Black-Scholes over a realized-volatility")
    print("proxy for IV, not real historical option prices. Treat this as a read on")
    print("the entry/exit logic, not a precise forecast of live P&L.")


if __name__ == "__main__":
    main()
