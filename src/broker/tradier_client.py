"""Thin wrapper around the Tradier brokerage REST API.

Docs: https://documentation.tradier.com/brokerage-api

Defaults to the sandbox (paper trading) host. Live trading only happens if
mode="live" is passed explicitly (main.py wires this from config, which
defaults to "paper").
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Literal, Optional

import pandas as pd
import requests

from src.broker.models import (
    AccountBalance,
    OptionContract,
    OrderLeg,
    OrderResult,
    Position,
    Quote,
)

log = logging.getLogger(__name__)

SANDBOX_BASE = "https://sandbox.tradier.com/v1"
LIVE_BASE = "https://api.tradier.com/v1"


class TradierClient:
    def __init__(self, api_key: str, account_id: str, mode: Literal["paper", "live"] = "paper"):
        if not api_key or not account_id:
            raise ValueError("Tradier API key and account id are required")
        self.account_id = account_id
        self.mode = mode
        self.base_url = LIVE_BASE if mode == "live" else SANDBOX_BASE
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            }
        )
        log.info("TradierClient initialized in %s mode (%s)", mode, self.base_url)

    # ---- internal helpers -------------------------------------------------

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        resp = self._session.get(f"{self.base_url}{path}", params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, data: dict) -> dict:
        resp = self._session.post(f"{self.base_url}{path}", data=data, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ---- market data --------------------------------------------------

    def get_quote(self, symbol: str) -> Optional[Quote]:
        data = self._get("/markets/quotes", {"symbols": symbol, "greeks": "false"})
        quotes = data.get("quotes", {}).get("quote")
        if not quotes:
            return None
        if isinstance(quotes, list):
            quotes = quotes[0]
        return Quote(
            symbol=quotes["symbol"],
            last=quotes.get("last") or quotes.get("close") or 0.0,
            bid=quotes.get("bid", 0.0),
            ask=quotes.get("ask", 0.0),
            prev_close=quotes.get("prevclose", 0.0),
            timestamp=datetime.now(),
        )

    def get_expirations(self, symbol: str) -> list[str]:
        data = self._get("/markets/options/expirations", {"symbol": symbol, "includeAllRoots": "true"})
        exp = data.get("expirations")
        if not exp:
            return []
        dates = exp.get("date")
        if dates is None:
            return []
        return dates if isinstance(dates, list) else [dates]

    def get_option_chain(self, symbol: str, expiration: str) -> list[OptionContract]:
        data = self._get(
            "/markets/options/chains",
            {"symbol": symbol, "expiration": expiration, "greeks": "true"},
        )
        options = data.get("options")
        if not options:
            return []
        rows = options.get("option")
        if rows is None:
            return []
        if not isinstance(rows, list):
            rows = [rows]

        contracts = []
        for row in rows:
            greeks = row.get("greeks") or {}
            contracts.append(
                OptionContract(
                    symbol=row["symbol"],
                    underlying=row["underlying"],
                    strike=float(row["strike"]),
                    expiration=row["expiration_date"],
                    option_type="call" if row["option_type"] == "call" else "put",
                    bid=row.get("bid") or 0.0,
                    ask=row.get("ask") or 0.0,
                    last=row.get("last"),
                    volume=row.get("volume") or 0,
                    open_interest=row.get("open_interest") or 0,
                    delta=greeks.get("delta"),
                    gamma=greeks.get("gamma"),
                    theta=greeks.get("theta"),
                    vega=greeks.get("vega"),
                    iv=greeks.get("mid_iv") or greeks.get("smv_vol"),
                )
            )
        return contracts

    def get_intraday_bars(self, symbol: str, interval: str = "5min", lookback_days: int = 5) -> pd.DataFrame:
        """Returns an OHLCV DataFrame indexed by datetime, ascending, using
        Tradier's timesales endpoint. Empty DataFrame on failure."""
        end = datetime.now()
        start = end - timedelta(days=lookback_days)
        data = self._get(
            "/markets/timesales",
            {
                "symbol": symbol,
                "interval": interval,
                "start": start.strftime("%Y-%m-%d %H:%M"),
                "end": end.strftime("%Y-%m-%d %H:%M"),
                "session_filter": "open",
            },
        )
        series = data.get("series")
        if not series or series == "null":
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        rows = series.get("data")
        if rows is None:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        if not isinstance(rows, list):
            rows = [rows]
        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["time"])
        df = df.set_index("time").sort_index()
        return df[["open", "high", "low", "close", "volume"]]

    def get_market_clock(self) -> dict:
        data = self._get("/markets/clock")
        return data.get("clock", {})

    # ---- account --------------------------------------------------

    def get_balance(self) -> Optional[AccountBalance]:
        data = self._get(f"/accounts/{self.account_id}/balances")
        bal = data.get("balances")
        if not bal:
            return None
        return AccountBalance(
            total_equity=bal.get("total_equity", 0.0),
            cash=bal.get("cash", {}).get("cash_available", bal.get("total_cash", 0.0))
            if isinstance(bal.get("cash"), dict)
            else bal.get("total_cash", 0.0),
            option_buying_power=bal.get("margin", {}).get("option_buying_power", 0.0)
            if isinstance(bal.get("margin"), dict)
            else bal.get("cash", {}).get("cash_available", 0.0)
            if isinstance(bal.get("cash"), dict)
            else 0.0,
        )

    def get_positions(self) -> list[Position]:
        data = self._get(f"/accounts/{self.account_id}/positions")
        positions = data.get("positions")
        if not positions or positions == "null":
            return []
        rows = positions.get("position")
        if rows is None:
            return []
        if not isinstance(rows, list):
            rows = [rows]
        out = []
        for row in rows:
            qty = int(row.get("quantity", 0))
            cost_basis = float(row.get("cost_basis", 0.0))
            per_contract = abs(cost_basis) / (abs(qty) * 100) if qty else 0.0
            out.append(
                Position(
                    option_symbol=row["symbol"],
                    underlying=row.get("symbol", "")[:6].rstrip("0123456789"),
                    quantity=qty,
                    cost_basis=cost_basis,
                    entry_price=per_contract,
                )
            )
        return out

    # ---- orders --------------------------------------------------

    def place_single_leg_order(
        self,
        underlying: str,
        option_symbol: str,
        side: str,
        quantity: int,
        order_type: str = "market",
        limit_price: Optional[float] = None,
        duration: str = "day",
    ) -> OrderResult:
        payload = {
            "class": "option",
            "symbol": underlying,
            "option_symbol": option_symbol,
            "side": side,
            "quantity": quantity,
            "type": order_type,
            "duration": duration,
        }
        if order_type == "limit" and limit_price is not None:
            payload["price"] = round(limit_price, 2)

        try:
            data = self._post(f"/accounts/{self.account_id}/orders", payload)
            order = data.get("order", {})
            return OrderResult(order_id=str(order.get("id")), status=order.get("status", "ok"), raw=data)
        except requests.HTTPError as e:
            log.error("Order failed: %s", e.response.text if e.response is not None else e)
            return OrderResult(order_id=None, status="error", error=str(e))

    def place_multileg_order(
        self,
        underlying: str,
        legs: list[OrderLeg],
        order_type: str = "market",
        limit_price: Optional[float] = None,
        duration: str = "day",
    ) -> OrderResult:
        payload = {
            "class": "multileg",
            "symbol": underlying,
            "type": order_type,
            "duration": duration,
        }
        for i, leg in enumerate(legs):
            payload[f"option_symbol[{i}]"] = leg.option_symbol
            payload[f"side[{i}]"] = leg.side
            payload[f"quantity[{i}]"] = leg.quantity
        if order_type == "debit" or order_type == "credit":
            payload["price"] = round(limit_price, 2) if limit_price is not None else 0.0

        try:
            data = self._post(f"/accounts/{self.account_id}/orders", payload)
            order = data.get("order", {})
            return OrderResult(order_id=str(order.get("id")), status=order.get("status", "ok"), raw=data)
        except requests.HTTPError as e:
            log.error("Multileg order failed: %s", e.response.text if e.response is not None else e)
            return OrderResult(order_id=None, status="error", error=str(e))
