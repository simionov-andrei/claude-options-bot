"""Persists every trade to SQLite (data/trades.db by default) for later
analysis, maintains a per-day summary JSON file alongside it, and prints
the daily wins/losses/net table (same shape as the reference screenshot).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    strategy TEXT NOT NULL,
    underlying TEXT NOT NULL,
    option_symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    contracts INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL,
    pnl REAL,
    status TEXT NOT NULL DEFAULT 'open',
    day_mode TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    exit_reason TEXT,
    entry_underlying_price REAL,
    exit_underlying_price REAL,
    notes TEXT,
    strike REAL,
    expiration TEXT,
    delta_at_entry REAL,
    account_balance_at_entry REAL,
    duration_minutes REAL
);
"""


class TradeLogger:
    def __init__(self, db_path: str = "data/trades.db", daily_summary_path: str = "data/daily_summary.json"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(SCHEMA)
        self._conn.commit()
        self.daily_summary_path = daily_summary_path

    def log_open(
        self,
        strategy: str,
        underlying: str,
        option_symbol: str,
        direction: str,
        contracts: int,
        entry_price: float,
        day_mode: str,
        opened_at: datetime | None = None,
        notes: str = "",
        strike: float | None = None,
        expiration: str | None = None,
        delta_at_entry: float | None = None,
        entry_underlying_price: float | None = None,
        account_balance_at_entry: float | None = None,
    ) -> int:
        # opened_at lets the backtester stamp real historical timestamps;
        # live callers omit it and get the current time.
        now = opened_at or datetime.now()
        cur = self._conn.execute(
            "INSERT INTO trades (trade_date, strategy, underlying, option_symbol, direction, "
            "contracts, entry_price, status, day_mode, opened_at, notes, strike, expiration, "
            "delta_at_entry, entry_underlying_price, account_balance_at_entry) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                now.date().isoformat(),
                strategy,
                underlying,
                option_symbol,
                direction,
                contracts,
                entry_price,
                "open",
                day_mode,
                now.isoformat(),
                notes,
                strike,
                expiration,
                delta_at_entry,
                entry_underlying_price,
                account_balance_at_entry,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def log_close(
        self,
        trade_id: int,
        exit_price: float,
        pnl: float,
        closed_at: datetime | None = None,
        exit_reason: str = "",
        exit_underlying_price: float | None = None,
    ) -> None:
        closed_at = closed_at or datetime.now()
        opened_at_row = self._conn.execute("SELECT opened_at FROM trades WHERE id=?", (trade_id,)).fetchone()
        duration_minutes = None
        if opened_at_row and opened_at_row[0]:
            opened_at = datetime.fromisoformat(opened_at_row[0])
            duration_minutes = (closed_at - opened_at).total_seconds() / 60

        self._conn.execute(
            "UPDATE trades SET exit_price=?, pnl=?, status='closed', closed_at=?, exit_reason=?, "
            "exit_underlying_price=?, duration_minutes=? WHERE id=?",
            (exit_price, pnl, closed_at.isoformat(), exit_reason, exit_underlying_price, duration_minutes, trade_id),
        )
        self._conn.commit()
        self.write_daily_summary_json()

    def daily_summary(self, for_date: date | None = None) -> dict:
        for_date = for_date or date.today()
        rows = self._conn.execute(
            "SELECT pnl FROM trades WHERE trade_date=? AND status='closed'", (for_date.isoformat(),)
        ).fetchall()
        wins = sum(1 for (pnl,) in rows if pnl and pnl > 0)
        losses = sum(1 for (pnl,) in rows if pnl is not None and pnl <= 0)
        total_win = sum(pnl for (pnl,) in rows if pnl and pnl > 0)
        total_loss = sum(pnl for (pnl,) in rows if pnl and pnl < 0)
        return {
            "date": for_date.isoformat(),
            "wins": wins,
            "losses": losses,
            "total_win": total_win,
            "total_loss": total_loss,
            "net": total_win + total_loss,
        }

    def write_daily_summary_json(self) -> None:
        """Recomputes the full per-day summary from the trades table and
        writes it to daily_summary_path -- one row per date, oldest first.
        Called automatically after every trade close, so it's always
        current; safe/cheap to call repeatedly since it's a fresh
        recompute, not an incremental patch (no drift risk)."""
        rows = self._conn.execute(
            "SELECT trade_date, "
            "COUNT(*) as trades, "
            "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
            "SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses, "
            "SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) as total_win, "
            "SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END) as total_loss, "
            "AVG(duration_minutes) as avg_duration_minutes, "
            "GROUP_CONCAT(DISTINCT strategy) as strategies, "
            "GROUP_CONCAT(DISTINCT underlying) as symbols, "
            "GROUP_CONCAT(DISTINCT day_mode) as day_modes "
            "FROM trades WHERE status='closed' GROUP BY trade_date ORDER BY trade_date ASC"
        ).fetchall()

        summary = []
        for (
            trade_date, trades, wins, losses, total_win, total_loss,
            avg_duration_minutes, strategies, symbols, day_modes,
        ) in rows:
            total_win, total_loss = total_win or 0.0, total_loss or 0.0
            summary.append({
                "date": trade_date,
                "trades": trades,
                "wins": wins or 0,
                "losses": losses or 0,
                "win_rate_pct": round((wins or 0) / trades * 100, 1) if trades else 0.0,
                "total_win": round(total_win, 2),
                "total_loss": round(total_loss, 2),
                "net": round(total_win + total_loss, 2),
                "avg_duration_minutes": round(avg_duration_minutes, 1) if avg_duration_minutes is not None else None,
                "strategies_traded": (strategies or "").split(",") if strategies else [],
                "symbols_traded": (symbols or "").split(",") if symbols else [],
                "day_modes_seen": (day_modes or "").split(",") if day_modes else [],
            })

        Path(self.daily_summary_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self.daily_summary_path, "w") as f:
            json.dump(summary, f, indent=2)

    def print_summary_table(self, days: int = 5) -> None:
        rows = self._conn.execute(
            "SELECT trade_date, "
            "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
            "SUM(CASE WHEN pnl <= 0 AND status='closed' THEN 1 ELSE 0 END) as losses, "
            "SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) as total_win, "
            "SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END) as total_loss "
            "FROM trades WHERE status='closed' GROUP BY trade_date ORDER BY trade_date DESC LIMIT ?",
            (days,),
        ).fetchall()
        rows = list(reversed(rows))

        header = f"{'DATE':<12}{'WINS':<8}{'LOSSES':<10}{'TOTAL WIN':>14}{'TOTAL LOSS':>14}{'NET':>14}"
        print(header)
        print("-" * len(header))
        t_wins = t_losses = t_win = t_loss = 0
        for trade_date, wins, losses, total_win, total_loss in rows:
            wins, losses = wins or 0, losses or 0
            total_win, total_loss = total_win or 0.0, total_loss or 0.0
            net = total_win + total_loss
            print(
                f"{trade_date:<12}{wins:<8}{losses:<10}{'$' + format(total_win, ',.2f'):>14}"
                f"{'$' + format(total_loss, ',.2f'):>14}{'$' + format(net, ',.2f'):>14}"
            )
            t_wins += wins
            t_losses += losses
            t_win += total_win
            t_loss += total_loss
        print("-" * len(header))
        net_total = t_win + t_loss
        print(
            f"{'TOTAL':<12}{t_wins:<8}{t_losses:<10}{'$' + format(t_win, ',.2f'):>14}"
            f"{'$' + format(t_loss, ',.2f'):>14}{'$' + format(net_total, ',.2f'):>14}"
        )
