# options-bot

An automated options trading agent: you feed it strategies, it scans the
market, sizes positions, and places orders through Tradier, with a
built-in "news agent" that halves position size on days it judges risky.

The engine (broker plumbing, risk
limits, position sizing, the caution gate) is real and functional. The
included `example_momentum` strategy is a *reference implementation*, not a
proven edge - it exists so the bot runs out of the box. Whether this makes
money depends entirely on the strategy you put into it, same as any
algo-trading system.

## How it fits together

```
main.py
  -> loads config/settings.yaml + .env
  -> TradierClient          (src/broker/)      quotes, chains, orders, positions
  -> strategies              (src/strategies/)  your entry/exit rules — pluggable
  -> CautionGate              (src/news_agent/)  VIX + econ calendar + LLM headline read
  -> RiskManager + sizer     (src/risk/)        daily loss limit, per-trade cap, cautious-day half-size
  -> Scheduler                (src/engine/)      the poll loop that wires it all together
```

Each poll (default every 60s during market hours):
1. Re-checks the day's caution assessment every N minutes (default 30).
2. Builds a market snapshot (recent bars + option chain) for each watched symbol.
3. Checks open positions for stop-loss / take-profit / strategy-defined exits.
4. Runs each active strategy's `scan()`. If it returns a `TradeIntent`, the
   position sizer applies the caution-day multiplier and an account-balance
   risk cap, then the order executor places the order.

## Setup

1. `pip install -r requirements.txt`
2. Create a free Tradier developer account: https://developer.tradier.com
   — this gives you a **sandbox** API key and account ID for paper trading
   with no funding required. That's what you should use first.
3. `cp .env.example .env` and fill in `TRADIER_API_KEY` / `TRADIER_ACCOUNT_ID`.
   Leave `TRADIER_MODE=paper`.
4. Optional but recommended for the full news agent:
   - `FINNHUB_API_KEY` (free tier) — powers the economic-calendar check and
     the headlines the LLM sentiment read uses.
   - `ANTHROPIC_API_KEY` — powers the LLM headline sentiment read. Without
     it, the news agent still works, just on VIX + calendar alone.
5. Edit `config/settings.yaml`:
   - `account.balance` — your account size (or leave `use_live_balance: true`
     to pull it live from Tradier).
   - `strategies.underlyings` — symbols to trade (defaults to `[SPY]`).
   - `strategies.active` — which strategy plugins to run.
6. `python main.py`

The bot refuses to place live orders unless **both** `.env`'s
`TRADIER_MODE=live` **and** `config/settings.yaml`'s `broker.mode: live`
say so — a deliberate double-check before real money moves.

## Writing your own strategy

Strategies live in `src/strategies/`. Copy `example_momentum.py` and edit
`scan()` (when to enter, and what contract to target via `target_delta` /
`dte_min`/`dte_max`) and `should_exit()` (any exit logic beyond the
stop-loss/take-profit percentages, which the engine enforces automatically).
Add the filename (no `.py`) to `strategies.active` in `config/settings.yaml`.

See `src/strategies/base.py` for the full interface — `MarketSnapshot` gives
you OHLCV bars and the live option chain; return a `TradeIntent` from
`scan()` to enter a trade, or `None` to pass.

You can run several strategies at once; each is capped to one open position
at a time by default (see `Scheduler._run_one_poll` in
`src/engine/scheduler.py` if you want to change that).

## The news/caution agent

`src/news_agent/caution_gate.py` combines three signals into a NORMAL or
CAUTIOUS call for the day:

- **VIX level/spike** (`vix_calendar.py`) — cautious if VIX is above
  `news_agent.vix_caution_level`, or has jumped more than
  `vix_caution_change_pct` vs. the prior close.
- **Economic calendar** (`vix_calendar.py`, via Finnhub) — cautious if a
  high-impact event (FOMC, CPI, NFP, etc. — see `high_impact_events` in
  config) is scheduled today.
- **LLM headline sentiment** (`sentiment_llm.py`, via Finnhub headlines +
  Claude) — cautious if today's top financial headlines suggest shock/panic
  conditions.

The VIX and calendar checks are deterministic and always run if enabled
(they need no LLM key). The LLM read is a secondary signal layered on top —
it can only *escalate* a normal day to cautious, never downgrade a cautious
call from the other two. On a cautious day, every strategy's requested
contract count is multiplied by `news_agent.cautious_contract_multiplier`
(default `0.5` — half size, same ratio described in the reference video).

## Risk controls

Configured in `config/settings.yaml` under `account:`:
- `max_risk_pct_per_trade` — hard cap on dollars risked per trade, as a
  fraction of account balance (position sizer also respects this on top of
  whatever the strategy asks for).
- `daily_loss_limit_pct` — halts new entries for the rest of the day once
  realized losses hit this fraction of account balance.
- `max_concurrent_positions`, `max_trades_per_day` — caps on activity.

## Data

Every trade is logged automatically as it happens -- no separate process to
run, it's built into `OrderExecutor`/`TradeLogger`.

**Per-trade detail**: `data/trades.db` (SQLite), one row per trade, updated
on open and again on close. Beyond the basics (strategy, symbol, direction,
contracts, entry/exit premium, pnl, status), each row also records:
`exit_reason` (stop loss / take profit / strategy exit signal), `notes`
(the strategy's own description of what triggered the entry, e.g. "support
bounce @ 500.00 stop_u=498.00 target_u=506.00"), `strike`, `expiration`,
`delta_at_entry`, `entry_underlying_price` / `exit_underlying_price` (the
stock's price, not the option's, at each end of the trade), `day_mode`
(normal/cautious), `account_balance_at_entry`, and `duration_minutes`.
Query it directly for analysis (`sqlite3 data/trades.db` or pandas'
`read_sql`).

**Per-day summary**: `data/daily_summary.json`, rewritten fresh after every
trade close (always current, no drift risk since it's a full recompute
each time, not an incremental patch). One entry per date: trade count,
wins, losses, win rate, total win/loss, net, average hold time, and which
strategies/symbols/day-modes were active that day.

At the end of each trading day (or on startup after a day off), the bot
also prints a summary table to the console in the same shape as the
reference screenshot:

```
DATE        WINS    LOSSES       TOTAL WIN    TOTAL LOSS           NET
------------------------------------------------------------------------
Aug 03      3       1              $5,408.00     -$300.00      $5,108.00
```

## Backtesting

```
python backtest.py --strategy no_wick_candles --symbol SPY
python backtest.py --strategy no_wick_candles --symbol QQQ --interval 60m --range 1y --account-balance 1000
python backtest.py --strategy example_momentum --symbol SPY --range 60d --account-balance 25000
```

Pulls real historical price bars from Yahoo Finance's public chart endpoint
(free, no API key -- Tradier has no historical *options* data at all, only
current chains, so this is the only free source that works here). Yahoo's
own intraday retention caps how far back each bar interval goes: ~60 days
for 5-minute/15-minute bars, ~7 days for 1-minute, but a full ~2 years for
60-minute bars. So a 6-12 month backtest is only possible on hourly bars
(`--interval 60m`) — see the gamma/granularity caveat below before trusting
the dollar totals from one of those runs. Runs the strategy through the exact same
`scan()`/`should_exit()` interface it uses live, applies the cautious-day
contract-halving (via a VIX-only historical gate -- no economic calendar or
LLM headline history is available for backtesting), and enforces the same
daily-loss-limit / max-trades-per-day guardrails as live.

**The one piece that can't be real: option premiums.** There's no free
historical options-chain data anywhere, so fills are simulated with
Black-Scholes, using trailing realized volatility (with a fixed markup) as
an IV proxy — see `src/backtest/options_pricing.py`. That's a legitimate,
commonly used approximation for backtesting, but it doesn't capture
volatility skew/smile, bid-ask spread, or liquidity effects the way a real
historical chain would. Treat backtest output as a read on whether the
entry/exit *logic* has an edge, not as a precise forecast of live P&L.

Results land in `data/backtest_<strategy>_<symbol>.db` (same schema as live
trade logs) and print the same daily win/loss/net table, plus win rate,
profit factor, and max drawdown.

**A note on bar interval and 0-3 DTE options.** Exits (stop-loss/take-profit)
are only checked once per bar. `no_wick_candles` trades very short-dated
options (0-3 DTE), which have extreme gamma — on a coarse bar, price can gap
straight past the intended exit level before the backtest ever checks it,
turning what should have been an ~80%-of-premium win into a simulated 10x
one. This is real and it happens at 60-minute bars (needed for a 6-12 month
window; see below). `backtest.py` now warns whenever the interval is 30
minutes or coarser, and every run reports a "granularity-corrected PnL"
alongside the raw total — each trade re-capped at its own stop-loss/
take-profit bounds, i.e. what a live bot polling every ~60s would actually
have captured. Treat the corrected number, not the raw one, as the
realistic read; win rate is unaffected either way since capping only
changes trade *size*, not outcome.

**What actual runs showed**, `no_wick_candles`, $1,000 simulated account,
cautious-day gate on:

| Symbol | Bars | Window | Trades | Win rate | Profit factor | Raw PnL | Corrected PnL |
|---|---|---|---|---|---|---|---|
| SPY | 5m | 60d (2026-06-08 to 2026-09-01) | 183 | 33.9% | 1.12 | $623.94 | $332.45 |
| QQQ | 60m | 1y (2025-09-02 to 2026-09-01) | 89 | 38.2% | 1.86 | $3,920.92 | $296.68 |
| AAPL | 60m | 1y (2025-09-02 to 2026-09-01) | 101 | 36.6% | 2.35 | $3,783.16 | $84.21 |

Three symbols, two very different bar intervals, one consistent story: win
rate clusters in the **mid-to-high 30s%**, nowhere near the 88-90% claimed
in the reference video. All three still land net positive once corrected —
average wins ran larger than average losses across the board, which is a
real property of the trend-flip exit plus the 2:1 take-profit/stop-loss
ratio, not an artifact — but the *size* of that edge on the hourly-bar runs
(QQQ, AAPL) is much smaller than their raw totals suggest, and even the
5-minute SPY run overstates it somewhat (the corrected total is about half
the raw one). $1,000 in starting capital did not block trades outright —
short-dated slightly-OTM premiums were often cheap enough for the 5%-of-
balance risk cap to still afford 1 contract — but it does mean each trade
risks only tens of dollars, so don't read the dollar totals as anything
more than directional.

For comparison, `example_momentum` over the same SPY window and account
size came out net *negative* (140 trades, 30% win rate, profit factor
0.81) — confirming the backtester discriminates between strategies rather
than flattering whatever you point it at.

Small samples, few symbols, approximated fills, no lookahead by design but
still an approximation of history — this is a starting read on the
entry/exit logic, not a verdict on whether to trade this live.

## Why forward-test results can diverge from backtests

Not just execution latency — several things a backtest can't capture:

- **Bid-ask spread** — market orders fill at ask (buy) / bid (sell), not the
  mid-price backtests assume. A real round-trip test showed this alone
  costs money even with zero price movement.
- **Data source mismatch** — live signals use Tradier's bars; backtests
  used Yahoo's. Same symbol, slightly different bars, so a signal can fire
  a bit differently live than it did historically.
- **Simulated vs real option pricing** — backtests price fills with
  Black-Scholes, not real quoted premiums (no skew/smile/liquidity).
- **Regime change** — market behavior going forward isn't guaranteed to
  resemble the backtested window at all.
- **API/order latency** — real, but just one factor among these, not the
  dominant one.

## Disclaimer

This is trading software, not financial advice, and options trading can
lose money quickly, especially at short expirations. 
