"""Optional LLM half of the news/caution agent: reads today's top financial
headlines and gives a risk read. This can only escalate a normal day to
cautious -- it never downgrades a cautious call from the quantitative gate
(vix_calendar.py), since VIX/calendar are the reliable signal and headline
sentiment is a softer, secondary one.

Skips gracefully if ANTHROPIC_API_KEY or FINNHUB_API_KEY isn't set.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)

FINNHUB_NEWS_URL = "https://finnhub.io/api/v1/news"

SENTIMENT_MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = (
    "You are a market-risk classifier for a same-day options trading bot. "
    "Given a list of today's top financial news headlines, decide whether "
    "conditions look NORMAL or CAUTIOUS for opening new short-dated options "
    "positions today. Flag CAUTIOUS for things like: signs of a market shock, "
    "geopolitical crisis, major surprise Fed/policy news, a large unexpected "
    "single-stock or index move, liquidity/credit stress, or broad panic "
    "language. Do NOT flag CAUTIOUS for routine/expected news. "
    'Respond with strict JSON only: {"risk_level": "normal"|"cautious", "reasoning": "<one sentence>"}'
)


def fetch_headlines(finnhub_api_key: Optional[str], limit: int = 15) -> list[str]:
    if not finnhub_api_key:
        return []
    try:
        resp = requests.get(
            FINNHUB_NEWS_URL,
            params={"category": "general", "token": finnhub_api_key},
            timeout=10,
        )
        resp.raise_for_status()
        articles = resp.json() or []
    except requests.RequestException as e:
        log.warning("Headline fetch failed: %s; skipping LLM sentiment check", e)
        return []
    return [a["headline"] for a in articles[:limit] if a.get("headline")]


def assess_headlines(anthropic_api_key: Optional[str], headlines: list[str]) -> tuple[bool, list[str]]:
    """Returns (is_cautious, reasons)."""
    if not anthropic_api_key or not headlines:
        return False, []

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=anthropic_api_key)
        message = client.messages.create(
            model=SENTIMENT_MODEL,
            max_tokens=200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "\n".join(f"- {h}" for h in headlines)}],
        )
        text = "".join(block.text for block in message.content if hasattr(block, "text")).strip()
        result = json.loads(text)
        if result.get("risk_level") == "cautious":
            return True, [f"LLM headline read: {result.get('reasoning', 'flagged cautious')}"]
        return False, []
    except Exception as e:  # noqa: BLE001 - this check is best-effort, never fatal
        log.warning("LLM sentiment check failed (%s); ignoring and continuing", e)
        return False, []
