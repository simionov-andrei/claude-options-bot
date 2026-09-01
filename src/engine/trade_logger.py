"""Persists trades to SQLite and prints the daily wins/losses/net summary
table (same shape as the screenshot: DATE WINS LOSSES TOTAL WIN TOTAL LOSS NET).
"""
from __future__ import annotations

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
    closed_at TEXT
);
"""


class TradeLogger:
    def __init__(self, db_path: str = "data/trades.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(SCHEMA)
        self._conn.commit()

    def log_open(
        self,
        strategy: str,
        underlying: str,
        option_symbol: str,
        direction: str,
        contracts: int,
        entry_price: float,
        day_mode: str,
    ) -> int:
        now = datetime.now()
        cur = self._conn.execute(
            "INSERT INTO trades (trade_date, strategy, underlying, option_symbol, direction, "
            "contracts, entry_price, status, day_mode, opened_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
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
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def log_close(self, trade_id: int, exit_price: float, pnl: float) -> None:
        self._conn.execute(
            "UPDATE trades SET exit_price=?, pnl=?, status='closed', closed_at=? WHERE id=?",
            (exit_price, pnl, datetime.now().isoformat(), trade_id),
        )
        self._conn.commit()

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
