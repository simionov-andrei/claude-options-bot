# options-bot

An automated options trading agent: you feed it strategies, it scans the
market, sizes positions, and places orders through Tradier — with a
built-in "news agent" that halves position size on days it judges risky.

**Read this before running it live.** The engine (broker plumbing, risk
limits, position sizing, the caution gate) is real and functional. The
included `example_momentum` strategy is a *reference implementation*, not a
proven edge — it exists so the bot runs out of the box. Whether this makes
money depends entirely on the strategy you put into it, same as any
algo-trading system. Start in paper mode and watch it for a while before
ever flipping `broker.mode: live`.

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

Trades are logged to a local SQLite database at `data/trades.db`. At the end
of each trading day (or on startup after a day off), the bot prints a
summary table in the same shape as the reference screenshot:

```
DATE        WINS    LOSSES       TOTAL WIN    TOTAL LOSS           NET
------------------------------------------------------------------------
Aug 03      3       1              $5,408.00     -$300.00      $5,108.00
```

## Running unattended (24/7 without your computer on)

Not set up yet by design — get it working and validated in paper mode on
your own machine first. Once you're ready, this is a good candidate for a
small always-on VPS running the bot in a Docker container (systemd/cron for
restarts, log shipping, etc.) — ask and that can be built out next.

## Disclaimer

This is trading software, not financial advice, and options trading can
lose money quickly, especially at short expirations. The daily win/loss
table format mirrored here is a UI convenience, not a claim about what
returns you should expect — the account performance described in the
reference video is a third party's unverified screenshot, not a benchmark
this code is expected to replicate. Test extensively in paper mode, don't
risk money you can't afford to lose, and understand every strategy you run.
