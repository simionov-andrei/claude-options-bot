"""Turns a strategy's requested contract count into a final, risk-capped
contract count, after applying the news agent's cautious-day multiplier and
an account-balance-based cap.
"""
from __future__ import annotations

import math


def compute_contracts(
    base_contracts: int,
    day_multiplier: float,
    account_balance: float,
    max_risk_pct_per_trade: float,
    option_price_per_contract: float,
) -> int:
    """
    base_contracts: strategy's requested size before any adjustment.
    day_multiplier: 1.0 on normal days, e.g. 0.5 on cautious days.
    option_price_per_contract: the option's premium (not x100'd yet).
    """
    if option_price_per_contract <= 0:
        return 0

    sized = max(1, math.floor(base_contracts * day_multiplier)) if base_contracts * day_multiplier >= 1 else 0
    if sized == 0:
        return 0

    max_dollars_at_risk = account_balance * max_risk_pct_per_trade
    cost_per_contract = option_price_per_contract * 100
    max_by_balance = math.floor(max_dollars_at_risk / cost_per_contract) if cost_per_contract > 0 else 0

    return max(0, min(sized, max_by_balance))
