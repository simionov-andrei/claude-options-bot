"""Backtest counterpart to src/news_agent/caution_gate.py.

Only reproduces the VIX half of the live gate: economic-calendar and LLM
headline history aren't available for free over an arbitrary lookback
window, so backtests judge each day using VIX level/spike only. This still
exercises the same "cautious day -> half-size contracts" mechanic requested
for the live workflow, just on a narrower signal.

Each trading day's mode is decided using the *prior* day's closing VIX level
and the change into it -- never that day's own close -- since intraday VIX
isn't fetched here and using a day's own close to gate trades placed during
that same day would leak same-day information the live bot couldn't have
had in real time. This is a deliberately conservative stand-in for the
live gate's real-time VIX read.
"""
from __future__ import annotations

import pandas as pd


def build_daily_mode_map(vix_daily: pd.DataFrame, vix_caution_level: float, vix_caution_change_pct: float, cautious_multiplier: float) -> dict:
    """Returns {date: (mode, multiplier)}, one entry per date in vix_daily
    except the first (which has no prior close to judge from)."""
    modes = {}
    closes = vix_daily["close"]
    for i in range(1, len(vix_daily)):
        day = vix_daily.index[i].date()
        level = closes.iloc[i - 1]  # yesterday's close, known before today's session
        prev_close = closes.iloc[i - 2] if i > 1 else level
        change_pct = (level - prev_close) / prev_close * 100 if prev_close else 0.0

        is_cautious = level >= vix_caution_level or change_pct >= vix_caution_change_pct
        modes[day] = ("cautious", cautious_multiplier) if is_cautious else ("normal", 1.0)
    return modes
