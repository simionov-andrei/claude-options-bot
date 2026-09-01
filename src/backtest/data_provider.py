"""Free, no-API-key historical data for backtesting, via Yahoo Finance's
public chart endpoint.

This is a substitute for Tradier, which only exposes *current* option chains
and quotes -- it has no historical options data at all, and its own
intraday history is short-retention. Yahoo's intraday retention is limited
too (about 60 days for 5-minute bars, ~7 days for 1-minute), which caps how
long a backtest window can be; see README's backtesting section.
"""
from __future__ import annotations

import logging

import pandas as pd
import pytz
import requests

log = logging.getLogger(__name__)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
ET = pytz.timezone("America/New_York")


def _fetch_chart(symbol: str, interval: str, range_: str) -> pd.DataFrame:
    resp = requests.get(
        CHART_URL.format(symbol=symbol),
        params={"range": range_, "interval": interval, "includePrePost": "false"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()
    result = payload.get("chart", {}).get("result")
    if not result:
        raise ValueError(f"No chart data returned for {symbol} ({interval}, {range_})")
    result = result[0]

    timestamps = result.get("timestamp")
    if not timestamps:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    quote = result["indicators"]["quote"][0]
    df = pd.DataFrame(
        {
            "open": quote["open"],
            "high": quote["high"],
            "low": quote["low"],
            "close": quote["close"],
            "volume": quote["volume"],
        },
        index=pd.to_datetime(timestamps, unit="s", utc=True),
    )
    df = df.dropna(subset=["open", "high", "low", "close"])
    df.index = df.index.tz_convert(ET)
    return df


def get_intraday_bars(symbol: str, interval: str = "5m", range_: str = "60d") -> pd.DataFrame:
    """Regular-trading-hours-only intraday OHLCV bars, ascending, tz-aware (ET)."""
    df = _fetch_chart(symbol, interval, range_)
    if df.empty:
        return df
    # Regular session is 09:30-16:00 ET; keep weekdays only as a safety net.
    df = df.between_time("09:30", "16:00")
    df = df[df.index.dayofweek < 5]
    return df.sort_index()


def get_daily_bars(symbol: str, range_: str = "1y") -> pd.DataFrame:
    df = _fetch_chart(symbol, interval="1d", range_=range_)
    return df.sort_index()
