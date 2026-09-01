"""Black-Scholes option pricing used to *simulate* premiums for backtesting,
since no historical options chain data is available (Tradier only exposes
current chains; there's no free historical-options-chain source).

This is a deliberate, documented approximation: real premiums also reflect
volatility skew/smile, bid-ask spread, and liquidity effects that a flat-IV
Black-Scholes model doesn't capture. Treat backtest results as directional
evidence about the entry/exit logic, not a precise P&L forecast.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

RISK_FREE_RATE = 0.045
# Short-dated options tend to trade at a premium to trailing realized vol
# (event/gamma risk, skew). This is a rough, fixed adjustment.
IV_MARKUP_OVER_REALIZED = 1.15


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S: float, K: float, T: float, sigma: float, option_type: Literal["call", "put"], r: float = RISK_FREE_RATE) -> float:
    if T <= 0:
        return max(0.0, S - K) if option_type == "call" else max(0.0, K - S)
    if sigma <= 0:
        sigma = 1e-4

    d1 = (math.log(S / K) + (r + sigma**2 / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)

    if option_type == "call":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_delta(S: float, K: float, T: float, sigma: float, option_type: Literal["call", "put"], r: float = RISK_FREE_RATE) -> float:
    if T <= 0:
        itm = (S > K) if option_type == "call" else (S < K)
        return (1.0 if option_type == "call" else -1.0) if itm else 0.0
    if sigma <= 0:
        sigma = 1e-4

    d1 = (math.log(S / K) + (r + sigma**2 / 2) * T) / (sigma * math.sqrt(T))
    return _norm_cdf(d1) if option_type == "call" else _norm_cdf(d1) - 1.0


def select_strike_for_delta(S: float, T: float, sigma: float, option_type: Literal["call", "put"], target_delta: float) -> float:
    """Brute-force search over a strike grid for the strike whose delta is
    closest to target_delta (magnitude), mirroring how the live engine picks
    a contract from a real chain in src/engine/order_executor.py."""
    step = max(0.5, round(S * 0.005, 2))
    best_strike, best_diff = S, float("inf")
    K = max(0.5, S * 0.7)
    upper = S * 1.3
    while K <= upper:
        delta = bs_delta(S, K, T, sigma, option_type)
        diff = abs(abs(delta) - target_delta)
        if diff < best_diff:
            best_diff, best_strike = diff, K
        K += step
    return round(best_strike, 2)


@dataclass
class RealizedVolEstimator:
    """Trailing annualized realized volatility from daily closes, used as
    the sigma input to Black-Scholes. Only ever looks backward from the
    bar being evaluated -- no lookahead into future price data."""

    window_days: int = 20

    def estimate(self, daily_closes: list[float]) -> float:
        if len(daily_closes) < 3:
            return 0.20  # fallback: a generic equity-index vol assumption
        window = daily_closes[-(self.window_days + 1):]
        log_returns = [math.log(window[i] / window[i - 1]) for i in range(1, len(window)) if window[i - 1] > 0]
        if len(log_returns) < 2:
            return 0.20
        mean = sum(log_returns) / len(log_returns)
        variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
        realized = math.sqrt(variance) * math.sqrt(252)
        return max(0.05, realized * IV_MARKUP_OVER_REALIZED)
