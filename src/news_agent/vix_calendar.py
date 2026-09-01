"""Quantitative half of the news/caution agent: VIX level + spike, and
high-impact economic calendar events (FOMC, CPI, NFP, etc).

This is deliberately deterministic and doesn't depend on any LLM call, so it
still works (and is what actually gates trading) even if no LLM key is
configured. sentiment_llm.py can only ever escalate normal -> cautious on
top of this, never downgrade it.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

import requests

from src.broker.tradier_client import TradierClient

log = logging.getLogger(__name__)

FINNHUB_CALENDAR_URL = "https://finnhub.io/api/v1/calendar/economic"


def check_vix(
    tradier: TradierClient, symbol: str, caution_level: float, caution_change_pct: float
) -> tuple[bool, list[str]]:
    """Returns (is_cautious, reasons)."""
    reasons: list[str] = []
    quote = tradier.get_quote(symbol)
    if quote is None:
        log.warning("Could not fetch VIX quote for %s; skipping VIX check", symbol)
        return False, reasons

    if quote.last >= caution_level:
        reasons.append(f"VIX at {quote.last:.1f} (>= caution level {caution_level})")

    if quote.prev_close:
        change_pct = (quote.last - quote.prev_close) / quote.prev_close * 100
        if change_pct >= caution_change_pct:
            reasons.append(f"VIX up {change_pct:.1f}% vs prior close (>= {caution_change_pct}%)")

    return len(reasons) > 0, reasons


def check_economic_calendar(
    finnhub_api_key: Optional[str], high_impact_events: list[str], today: Optional[date] = None
) -> tuple[bool, list[str]]:
    """Returns (is_cautious, reasons). Skips gracefully if no API key set."""
    if not finnhub_api_key:
        log.info("No FINNHUB_API_KEY set; skipping economic calendar check")
        return False, []

    today = today or datetime.now().date()
    reasons: list[str] = []
    try:
        resp = requests.get(
            FINNHUB_CALENDAR_URL,
            params={"from": today.isoformat(), "to": today.isoformat(), "token": finnhub_api_key},
            timeout=10,
        )
        resp.raise_for_status()
        events = resp.json().get("economicCalendar", []) or []
    except requests.RequestException as e:
        log.warning("Economic calendar fetch failed: %s; skipping", e)
        return False, []

    keywords = [k.lower() for k in high_impact_events]
    for event in events:
        if event.get("country") != "US":
            continue
        name = (event.get("event") or "").lower()
        impact = (event.get("impact") or "").lower()
        if impact == "high" or any(k in name for k in keywords):
            reasons.append(f"Economic event today: {event.get('event')} ({event.get('time', 'time n/a')})")

    return len(reasons) > 0, reasons
