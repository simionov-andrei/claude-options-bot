"""no_wick_candles backtest runner with a proper granularity correction.

Structurally identical to orb_engine.py / sr_engine.py: since the updated
no_wick_candles strategy's stop/target are now underlying-price levels (not
a fixed % of premium), the generic BacktestEngine's premium-pct correction
doesn't apply. On a stop/target exit, recompute the option's Black-Scholes
price using the INTENDED underlying level instead of wherever the bar's
actual price was, holding strike/vol/time fixed. EOD and trend-flip exits
need no correction (no threshold being overshot -- the position is simply
flattened at whatever the current price is).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

import pandas as pd

from src.backtest.options_pricing import RealizedVolEstimator, bs_price, select_strike_for_delta
from src.risk.position_sizer import compute_contracts
from src.strategies.base import MarketSnapshot
from src.strategies.no_wick_candles import NoWickCandlesStrategy


def run_nowick_backtest(
    symbol: str, bars: pd.DataFrame, daily_mode_map: dict, account_cfg: dict, warmup_bars: int = 55,
    return_trades: bool = False,
) -> dict:
    strat = NoWickCandlesStrategy()
    vol_est = RealizedVolEstimator()

    trades_today: dict = {}
    halted_dates: set = set()
    realized_pnl_today: dict = {}
    open_trade = None
    closed: list[dict] = []

    for i in range(warmup_bars, len(bars)):
        window = bars.iloc[: i + 1]
        ts = window.index[-1]
        S = window["close"].iloc[-1]
        day = ts.date()

        if open_trade is not None:
            ot = open_trade
            T = max(0.0, (ot["expiration"] - ts).total_seconds() / (365.25 * 24 * 3600))
            raw_exit_price = bs_price(S, ot["K"], T, ot["sigma"], ot["option_type"])

            direction = ot["direction"]
            hit_stop = (direction == "long_call" and S <= ot["stop_price"]) or (
                direction == "long_put" and S >= ot["stop_price"]
            )
            hit_target = (direction == "long_call" and S >= ot["target_price"]) or (
                direction == "long_put" and S <= ot["target_price"]
            )
            minutes_left = (ts.replace(hour=15, minute=55, second=0, microsecond=0) - ts).total_seconds() / 60

            trend = strat._trend(window)
            trend_flipped = (ot["option_type"] == "call" and trend == "bearish") or (
                ot["option_type"] == "put" and trend == "bullish"
            )
            eod = minutes_left <= 10 or T <= 0

            reason = None
            corrected_exit_price = raw_exit_price
            if hit_stop:
                reason = "stop"
                corrected_exit_price = bs_price(ot["stop_price"], ot["K"], T, ot["sigma"], ot["option_type"])
            elif hit_target:
                reason = "target"
                corrected_exit_price = bs_price(ot["target_price"], ot["K"], T, ot["sigma"], ot["option_type"])
            elif eod:
                reason = "eod"
            elif trend_flipped:
                reason = "trend_flip"

            if reason:
                raw_pnl = (raw_exit_price - ot["entry_price"]) * ot["contracts"] * 100
                corrected_pnl = (corrected_exit_price - ot["entry_price"]) * ot["contracts"] * 100
                closed.append({"date": day, "reason": reason, "raw_pnl": raw_pnl, "corrected_pnl": corrected_pnl})

                realized_pnl_today[day] = realized_pnl_today.get(day, 0.0) + raw_pnl
                loss_limit = account_cfg["balance"] * account_cfg["daily_loss_limit_pct"]
                if realized_pnl_today[day] <= -abs(loss_limit):
                    halted_dates.add(day)
                open_trade = None
                strat._active_trade.pop(symbol, None)

        if open_trade is None:
            can_open = day not in halted_dates and trades_today.get(day, 0) < account_cfg["max_trades_per_day"]
            if can_open:
                snap = MarketSnapshot(timestamp=ts, underlying_symbol=symbol, underlying_price=S, bars=window, option_chain=[])
                intent = strat.scan(snap)
                if intent is not None:
                    trade_meta = strat._active_trade.get(symbol)
                    if trade_meta is not None:
                        option_type = "call" if intent.direction == "long_call" else "put"
                        daily_closes = window["close"].resample("1D").last().dropna().tolist()
                        sigma = vol_est.estimate(daily_closes)

                        exp_date = ts.date() + timedelta(days=intent.dte_min)
                        while exp_date.weekday() >= 5:
                            exp_date += timedelta(days=1)
                        expiration = pd.Timestamp(datetime.combine(exp_date, time(16, 0)), tz=ts.tzinfo)
                        T = max((expiration - ts).total_seconds() / (365.25 * 24 * 3600), 1e-6)

                        K = select_strike_for_delta(S, T, sigma, option_type, intent.target_delta)
                        entry_price = bs_price(S, K, T, sigma, option_type)

                        if entry_price > 0.01:
                            day_mode, mult = daily_mode_map.get(day, ("normal", 1.0))
                            contracts = compute_contracts(
                                intent.base_contracts, mult, account_cfg["balance"],
                                account_cfg["max_risk_pct_per_trade"], entry_price,
                            )
                            if contracts >= 1:
                                open_trade = {
                                    "K": K, "sigma": sigma, "expiration": expiration, "option_type": option_type,
                                    "contracts": contracts, "entry_price": entry_price,
                                    "stop_price": trade_meta["stop_price"], "target_price": trade_meta["target_price"],
                                    "direction": intent.direction,
                                }
                                trades_today[day] = trades_today.get(day, 0) + 1
                            else:
                                strat._active_trade.pop(symbol, None)
                        else:
                            strat._active_trade.pop(symbol, None)

    if open_trade is not None:
        ot = open_trade
        ts = bars.index[-1]
        S = bars["close"].iloc[-1]
        T = max(0.0, (ot["expiration"] - ts).total_seconds() / (365.25 * 24 * 3600))
        exit_price = bs_price(S, ot["K"], T, ot["sigma"], ot["option_type"])
        pnl = (exit_price - ot["entry_price"]) * ot["contracts"] * 100
        closed.append({"date": ts.date(), "reason": "backtest_end", "raw_pnl": pnl, "corrected_pnl": pnl})

    n = len(closed)
    raw_total = sum(c["raw_pnl"] for c in closed)
    corrected_total = sum(c["corrected_pnl"] for c in closed)
    win_rate_raw = (sum(1 for c in closed if c["raw_pnl"] > 0) / n * 100) if n else 0.0
    win_rate_corrected = (sum(1 for c in closed if c["corrected_pnl"] > 0) / n * 100) if n else 0.0

    result = {
        "trades": n,
        "win_rate_raw_pct": win_rate_raw,
        "raw_pnl": raw_total,
        "win_rate_corrected_pct": win_rate_corrected,
        "corrected_pnl": corrected_total,
    }
    if return_trades:
        result["closed_trades"] = closed
    return result
