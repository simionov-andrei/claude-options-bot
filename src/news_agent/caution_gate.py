"""Combines the quantitative gate (VIX + economic calendar) and the optional
LLM headline read into a single DayAssessment that the engine uses to decide
whether today is a normal-size or half-size ("cautious") trading day.
"""
from __future__ import annotations

import logging
import os

from src.broker.tradier_client import TradierClient
from src.news_agent.models import DayAssessment
from src.news_agent.sentiment_llm import assess_headlines, fetch_headlines
from src.news_agent.vix_calendar import check_economic_calendar, check_vix

log = logging.getLogger(__name__)


class CautionGate:
    def __init__(self, tradier: TradierClient, config: dict):
        self.tradier = tradier
        self.config = config
        self.finnhub_key = os.getenv("FINNHUB_API_KEY")
        self.anthropic_key = os.getenv("ANTHROPIC_API_KEY")

    def assess(self) -> DayAssessment:
        cfg = self.config
        reasons: list[str] = []
        is_cautious = False

        if not cfg.get("enabled", True):
            return DayAssessment(mode="normal", contract_multiplier=1.0, reasons=["news agent disabled"])

        vix_cautious, vix_reasons = check_vix(
            self.tradier,
            cfg.get("vix_symbol", "VIX"),
            cfg.get("vix_caution_level", 25),
            cfg.get("vix_caution_change_pct", 8),
        )
        is_cautious = is_cautious or vix_cautious
        reasons += vix_reasons

        if cfg.get("econ_calendar_enabled", True):
            calendar_cautious, calendar_reasons = check_economic_calendar(
                self.finnhub_key, cfg.get("high_impact_events", [])
            )
            is_cautious = is_cautious or calendar_cautious
            reasons += calendar_reasons

        if cfg.get("llm_sentiment_enabled", True):
            headlines = fetch_headlines(self.finnhub_key)
            llm_cautious, llm_reasons = assess_headlines(self.anthropic_key, headlines)
            # LLM read can only escalate, never downgrade the quant gate.
            is_cautious = is_cautious or llm_cautious
            reasons += llm_reasons

        if is_cautious:
            multiplier = cfg.get("cautious_contract_multiplier", 0.5)
            log.info("Day assessment: CAUTIOUS (x%.2f contracts) - %s", multiplier, "; ".join(reasons))
            return DayAssessment(mode="cautious", contract_multiplier=multiplier, reasons=reasons)

        log.info("Day assessment: NORMAL")
        return DayAssessment(mode="normal", contract_multiplier=1.0, reasons=["no caution signals triggered"])
