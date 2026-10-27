# MarketPilot AI + AlphaScout AI

A stock market research and **paper trading** platform, built incrementally.

- **MarketPilot AI**: market data, technical analysis, rule-based strategies, simulated (paper) execution, risk controls, backtesting.
- **AlphaScout AI**: evidence-based stock research from primary sources (SEC EDGAR), with bear/base/bull scenarios, red-team review and versioned reports. *Vertical slice built.*

> **Research, education and paper trading only.** There is no live order execution and no connection to real-money trading. The app refuses to start unless `TRADING_MODE=paper`. Nothing here is investment advice.

## Status: MarketPilot phases 1–9 complete; AlphaScout vertical slice

| Implemented | Not yet (later phases) |
|---|---|
| Alpaca historical bars, idempotent storage, audit log | Parameter optimization / walk-forward analysis |
| Trend-following strategy v1.0.0 with versioned signals | Momentum, VWAP and mean-reversion strategies |
| PAPER broker: orders, fills, ledger, risk gate, reconciliation | Intraday strategies |
| Backtests sharing the PAPER fill/cost/risk/accounting code | Point-in-time universes (survivorship-bias-free data) |
| Scheduled daily forward cycles that reproduce the backtest | Market calendar |
| Restart-safe, idempotent cycles; pause; emergency shutdown | Push notifications |
| Read-only Streamlit dashboard; computed operational alerts | Corporate-action adjustment of PAPER positions (detected, not applied) |
| PAPER long calls/puts: option quotes, quote-driven fills, expiry settlement | Option writing, spreads, early exercise, option backtests |
| Grounded AI explanations of orders, signals, backtests (validated, audited) | AlphaScout: transcripts, company IR pages, company-guided dates |
| AlphaScout: SEC fundamentals with citations, screening, scenarios, red team, versioned reports, outcome tracking | AlphaScout: filing-text analysis (going concern, Form 4), alerts |
| AlphaScout: news verification (dedupe, flags, SEC corroboration) and dated catalysts | |

> **Signals are not trade recommendations.** No strategy here has been shown to be profitable. That requires the backtesting phase, and even then past results don't guarantee future ones. Signals exist to be paper-traded and evaluated.

## Project layout

```text
market-intelligence/
├── apps/dashboard/              # read-only Streamlit dashboard (GET-only API client)
│   ├── app.py, api_client.py, transforms.py
│   └── views/                   # one module per page
├── apps/marketpilot/            # MarketPilot API application
│   ├── main.py                  # app factory + startup/shutdown (lifespan)
│   ├── config.py                # typed settings from env / .env
│   ├── market_data.py           # builds the configured provider
│   ├── services/ingestion.py    # fetch -> validate -> store, with audit trail
│   ├── services/signals.py      # stored bars -> strategy -> recorded signals
│   ├── services/paper_trading.py  # PAPER accounts, orders, simulation, reconciliation
│   ├── services/paper_common.py   # helpers shared by the share and option services
│   ├── services/option_trading.py # option chain ingestion, option orders, expiry
│   ├── services/backtests.py    # load stored bars, run, report, persist
│   ├── services/forward_trading.py  # deployments + the daily trading cycle
│   ├── services/alerts.py       # computed operational alerts
│   ├── cli.py                   # scheduler entry points (run-cycle, run-due)
│   ├── backtesting/             # pure: event-driven engine, metrics
│   ├── execution/               # pure: costs, fill rules, accounting, option rules
│   ├── risk/                    # pure: risk limits gate, position sizing
│   ├── strategies/
│   │   ├── base.py              # interface, data-quality gate, result models
│   │   ├── indicators.py        # SMA, ATR, highs/lows in exact Decimal
│   │   └── trend_following.py   # strategy v1.0.0 (rules documented in the file)
│   └── api/
│       ├── dependencies.py, errors.py, schemas.py, security.py
│       └── routes/{health,market,options,signals,paper,deployments,backtests,alerts}.py
├── apps/alphascout/             # AlphaScout research API (separate service, port 8001)
│   ├── main.py, api/routes.py
│   ├── analysis/                # pure: fundamentals, screening, valuation, red_team, report
│   └── services/research.py     # SEC + stored prices -> report -> database
├── packages/
│   ├── sec/                     # SEC EDGAR client (rate-limited) and JSON parsers
│   ├── ai/                      # explanation model interface, Anthropic adapter, validator
│   ├── market_data/             # provider-agnostic models, validation, Alpaca adapter,
│   │                            # options.py (OCC symbols, quotes, chains)
│   ├── database/
│   │   ├── models.py            # ORM tables (bars, ingestion runs, strategy configs, signals)
│   │   ├── engine.py            # async engine, sessions, health check
│   │   ├── market_bars.py       # idempotent upsert + range queries
│   │   ├── ingestion_runs.py    # audit log repository
│   │   ├── signals.py           # strategy configs + append-only signals
│   │   └── option_quotes.py     # append-only option quote time series
│   └── shared/                  # time helpers, logging, base error
├── migrations/                  # Alembic migrations (schema history)
├── infra/postgres/init/         # creates the separate *_test database
├── tests/
│   ├── unit/                    # offline, no credentials or database needed
│   ├── db/                      # need a real PostgreSQL test database
│   ├── integration/             # live Alpaca tests (opt-in)
│   └── fixtures/, fakes.py, strategy_data.py   # SYNTHETIC data only
├── docker-compose.yml           # local PostgreSQL 16
├── alembic.ini
├── pyproject.toml               # dependencies, pytest + ruff config
├── requirements-lock.txt        # exact pinned versions (reproducible installs)
└── .env.example                 # configuration template (no real secrets)
```

## Setup

Requires **Python 3.12+** (tested on 3.13) and **Docker Desktop** (for PostgreSQL).

### 1. Python environment

Windows (PowerShell):

```powershell
cd market-intelligence
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements-lock.txt
pip install -e . --no-deps
Copy-Item .env.example .env
```

macOS / Linux:

```bash
cd market-intelligence
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements-lock.txt
pip install -e . --no-deps
cp .env.example .env
```

### 2. Edit `.env`

- Set `POSTGRES_PASSWORD`, and use the **same** password in `DATABASE_URL` and `TEST_DATABASE_URL`. The placeholder works, but the database is only reachable from your machine.
- Add your Alpaca keys (`ALPACA_API_KEY`, `ALPACA_SECRET_KEY`).
- To allow ingestion, set `API_WRITE_TOKEN`. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

### 3. Start PostgreSQL and create the schema

```bash
docker compose up -d --wait     # starts PostgreSQL and waits until healthy
alembic upgrade head            # creates the tables
```

The first `docker compose up` also creates a separate `<POSTGRES_DB>_test` database for the automated tests. If you change `POSTGRES_PASSWORD` after the first start, the existing data volume keeps the old password. Reset it with `docker compose down -v`, which **deletes all stored data**.

## Run the API

```bash
uvicorn apps.marketpilot.main:create_app --factory --reload
```

- Interactive docs: http://127.0.0.1:8000/docs
- Health: http://127.0.0.1:8000/health. Reports market data configuration, database reachability, and the applied migration (`schema_revision`).

### Ingest bars, then read them back

bash:

```bash
TOKEN=...   # your API_WRITE_TOKEN
curl -X POST http://127.0.0.1:8000/market/bars/ingest \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"symbol": "AAPL", "timeframe": "1Day", "start": "2024-01-02", "end": "2024-01-31"}'

curl "http://127.0.0.1:8000/market/bars/stored?symbol=AAPL&start=2024-01-02&end=2024-01-31"
curl "http://127.0.0.1:8000/market/ingestion-runs?symbol=AAPL"
```

PowerShell:

```powershell
$headers = @{ Authorization = "Bearer <your API_WRITE_TOKEN>" }
$body = '{"symbol": "AAPL", "timeframe": "1Day", "start": "2024-01-02", "end": "2024-01-31"}'
Invoke-RestMethod -Method Post http://127.0.0.1:8000/market/bars/ingest -Headers $headers -ContentType "application/json" -Body $body
Invoke-RestMethod "http://127.0.0.1:8000/market/bars/stored?symbol=AAPL&start=2024-01-02&end=2024-01-31"
```

Running the same ingest again is safe. The second response reports every bar as `unchanged`.

### Generate signals

The strategy runs on **stored, split-adjusted** daily bars. It needs at least 200 completed bars, so ingest about 10 months of history first. bash:

```bash
curl -X POST http://127.0.0.1:8000/market/bars/ingest \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"symbol": "AAPL", "timeframe": "1Day", "adjustment": "split", "start": "2023-01-01", "end": "2024-12-31"}'

curl -X POST http://127.0.0.1:8000/signals/generate \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"symbols": ["AAPL", "MSFT"]}'

curl "http://127.0.0.1:8000/signals?symbol=AAPL"
```

PowerShell:

```powershell
$body = '{"symbols": ["AAPL", "MSFT"]}'
Invoke-RestMethod -Method Post http://127.0.0.1:8000/signals/generate -Headers $headers -ContentType "application/json" -Body $body
```

The response explains **every** symbol, rule by rule, with the numbers used. Possible outcomes:

| Outcome | Meaning |
|---|---|
| `signal` | All entry rules passed; the signal is recorded. |
| `no_signal` | At least one entry rule failed. |
| `insufficient_data` | Too few stored completed bars. Not stored. |
| `stale_data` | The newest completed bar is too old. Not stored. |
| `data_gap` | Bars are missing inside the window. Not stored. |

Optional fields:
- `"as_of": "2024-06-03T21:00:00Z"` replays the strategy as of a past moment, using only bars that had completed by then.
- `"parameters": {...}` overrides strategy parameters. Every distinct parameter set is stored as its own versioned config.

### Endpoints

| Endpoint | What it does |
|---|---|
| `GET /health` | Service, provider, and database status. Never calls the provider. |
| `GET /market/bars` | Fetches bars **live from the provider**. Nothing is stored. |
| `POST /market/bars/ingest` | Fetches bars and **stores** them idempotently. Requires `Authorization: Bearer <API_WRITE_TOKEN>`. |
| `GET /market/bars/stored` | Reads **stored** bars from the database. No provider call. |
| `GET /market/ingestion-runs` | Lists recent ingestion attempts, newest first, including failures and their reasons. |
| `POST /signals/generate` | Runs the strategy on stored bars and records any signals. Requires the bearer token. |
| `GET /signals` | Lists stored signals (newest signal bar first), with their strategy version and rule checks. |
| `POST /paper/accounts` | Creates a PAPER account with simulated cash, risk limits, and cost model. |
| `GET /paper/accounts[/{id}]` | Lists accounts, or shows one: cash, equity, positions marked at the last completed close, P&L, drawdown. |
| `POST /paper/accounts/{id}/orders` | Submits a manual PAPER order. It is risk-gated, and rejections are recorded with reasons. |
| `POST /paper/accounts/{id}/orders/from-signal` | Turns a stored signal into a sized entry order. One per signal. |
| `GET /paper/accounts/{id}/orders` | Lists orders, optionally filtered by `?status=working`. |
| `POST /paper/accounts/{id}/orders/{order_id}/cancel` | Cancels a working order. |
| `POST /paper/accounts/{id}/process` | Advances the simulation through newly completed sessions. |
| `GET /paper/accounts/{id}/fills`, `/ledger` | Lists simulated fills and the append-only cash ledger. |
| `GET /paper/accounts/{id}/equity` | Equity, cash and market value at each processed session close. |
| `GET /paper/accounts/{id}/reconcile` | Rebuilds cash and positions from the ledger and checks they match. |
| `POST /paper/accounts/{id}/halt`, `/resume` | Emergency stop and resume. |
| `POST /paper/deployments` | Binds a strategy config and symbols to a PAPER account for forward trading. |
| `GET /paper/deployments[/{id}]`, `/{id}/cycles` | Lists deployments, shows one, or lists its daily cycles with step-by-step summaries. |
| `POST /paper/deployments/{id}/cycle` | Runs one daily cycle now. Normally the scheduler does this. |
| `POST /paper/deployments/{id}/pause`, `/resume` | Stops or restarts *new* entries. Exits keep being managed either way. |
| `POST /paper/deployments/{id}/shutdown` | Emergency stop: halts the account and stops the deployment. With `{"liquidate": true}` it also sells everything. |
| `POST /backtests` | Runs a backtest on stored adjusted bars and saves the report. Requires the bearer token. |
| `GET /backtests`, `GET /backtests/{id}` | Lists saved backtests, or returns one full report. |
| `GET /market/coverage` | Stored series: first and last bar, bar count, last retrieval time (data freshness). |
| `GET /alerts` | Current operational alerts (see below). |
| `POST /market/options/chain/ingest` | Fetches the latest quote per option contract for an underlying and **stores** it. Requires the bearer token. |
| `GET /market/options/chain?underlying=` | Latest **stored** quote per contract (optionally `expiration=`, `as_of=`). No provider call. |
| `GET /market/options/contracts/{symbol}/quotes` | Every stored quote for one contract, oldest first. |
| `POST /explanations/{order\|signal\|backtest}/{id}` | Explains a stored record: AI-written if configured and it passes validation, otherwise deterministic. Requires the bearer token (AI calls cost money). |
| `GET /explanations/{kind}/{id}/attempts` | Audit trail of explanation attempts, including rejected and failed ones. |
| `POST /paper/accounts/{id}/options/orders` | Submits a PAPER long option order (buy to open / sell to close). See "PAPER options". |

All PAPER endpoints except the GETs require the bearer token. Every PAPER response carries `"mode": "PAPER"`.

Bar request parameters (query string for the GETs, JSON body for ingest):

| Param | Required | Example | Notes |
|---|---|---|---|
| `symbol` | yes | `AAPL`, `brk.b` | Case-insensitive; validated. |
| `start`, `end` | yes | `2024-01-02`, `2024-01-02T14:30:00Z` | Both inclusive. A date-only value is a UTC day (`start` = 00:00:00Z, `end` = 23:59:59Z). Datetimes **must** carry an offset; in URLs write `+` as `%2B` or use `Z`. |
| `timeframe` | no (`1Day`) | `1Min`, `15Min`, `1Hour`, `1Week`, `1Month` | Alpaca supports 1–59Min, 1–23Hour, 1Day, 1Week, 1/2/3/6/12Month. |
| `adjustment` | no (`raw`) | `split`, `dividend`, `all` | Raw is the default, so no transformation is hidden. |
| `feed` | no (config) | `iex`, `sip`, `delayed_sip` | |
| `max_bars` | no (`10000`) | up to `100000` | If more bars exist, the response sets `truncated: true`. |

Prices are **decimal strings** (for example `"185.64"`) to preserve exact precision. Every response states its provider, feed, adjustment, and timestamps.

Errors use one shape: `{"error": {"type", "message", "provider"}}`.

| Status | Meaning |
|---|---|
| 422 | Invalid input |
| 401 `unauthorized` | Missing or wrong bearer token |
| 403 `writes_disabled` | `API_WRITE_TOKEN` is not set, so writes are off |
| 503 `provider_not_configured` | Missing Alpaca keys |
| 503 `database_not_configured` | `DATABASE_URL` is not set |
| 503 `database_unavailable` | Database not running or unreachable |
| 502 `provider_access_denied` | Keys rejected, or the plan doesn't cover the request |
| 429 | Provider rate limit (includes a `Retry-After` header) |
| 503 `provider_unavailable` | Timeout, network failure, or provider outage |
| 502 `invalid_provider_response` / `data_validation_failed` | Provider sent data we can't trust |

## How storage works

**Table `market_bars`.** One row per bar, unique on `(symbol, timeframe, feed, adjustment, timestamp)`:

- Prices and volume are PostgreSQL `NUMERIC` without a fixed scale, so values round-trip exactly.
- Timestamps are `timestamptz` in UTC.
- Check constraints reject impossible bars (non-positive prices, high below low, negative volume) even if application validation were bypassed.

**Idempotent upsert** (`INSERT ... ON CONFLICT`):

| Incoming bar | Result |
|---|---|
| New key | Inserted, `revision = 1` |
| Same key, identical values | Untouched; counted as `unchanged` |
| Same key, different values (provider correction) | Updated; `revision + 1`; `retrieved_at` and `updated_at` refreshed |

**Only completed bars are stored.** If the last bar's interval hadn't ended at retrieval time, it is skipped and reported as `bars_skipped_incomplete`.

**Table `ingestion_runs`.** One row per ingest attempt:

- Status is `running`, then `succeeded` or `failed`.
- Records counts, the truncation flag, and the error type and message.
- Bars and the success record are written in one transaction, so they can't disagree.
- A failure is recorded in its own transaction.

**Adjusted data changes over time.** `split`/`all` bars are adjusted as of retrieval time; a later split changes every earlier price. Re-ingesting picks this up as revisions. Prefer `raw` as the system of record.

## Strategy: `trend_following` v1.0.0

Long-only, daily bars. The authoritative rules live in `apps/marketpilot/strategies/trend_following.py`. Everything is evaluated on the most recent **completed** bar `t`.

**Entry: all four rules must pass.**

| Rule | Condition (defaults) |
|---|---|
| `trend_alignment` | SMA(50) > SMA(200) |
| `above_slow_ma` | close[t] > SMA(200) |
| `breakout` | close[t] > highest high of the 20 bars **before** t |
| `valid_stop` | stop = close[t] − 2 × ATR(14) is > 0 |

**Order.** Buy at the **next** session's open. The order is valid for that session only. Cancel it if that open is:
- below the stop (the signal is already invalidated), or
- more than 1 ATR above close[t] (the risk would be bigger than planned).

**Exits: whichever comes first.**
- The initial stop is hit.
- The price closes below the lowest low of the previous 10 bars.
- 60 bars have passed (time stop).

**No profit target, so no reward:risk ratio.** Trend following tries to let winners run, so any edge would have to show up in backtests. It isn't assumed.

**Score components** (`trend_spread_pct`, `breakout_size_atr`, `relative_volume`) are descriptive measurements, **not probabilities**. With the IEX feed, relative volume reflects IEX volume only.

**ATR.** A simple mean of true range, so a value depends only on its visible window.

### How signals stay honest

- **No look-ahead.** A bar is used only if its interval has ended by `as_of`. Daily bars count as complete 24 hours after their timestamp, which is conservative. Breakout and volume windows exclude the signal bar itself. Tests prove that adding or changing *future* bars never changes a result.
- **Reproducible.** The same stored bars, `as_of` and parameters always give identical output, computed in exact Decimal with a fixed precision. Each signal references its strategy config: name, version, parameters, and a SHA-256 `parameters_hash`.
- **Data-quality gate.** The strategy refuses to evaluate on too little history, stale data (newest bar ended more than 4 days before `as_of`), or gaps of more than 6 calendar days.
- **Adjusted prices only.** Raw prices are rejected for signals, because a 4:1 split would look like a 75% crash.
- **Append-only history.** `signals` is unique per strategy config, series and bar. Regenerating returns the existing signal with `already_existed: true`. If stored bars have since been revised, the original signal is kept unchanged and a notice says so.
- **Version pinning.** A test pins the strategy's name, version and default parameters. Changing the rules must come with a version bump.

## PAPER trading

**Everything here is simulated.** No code path can reach a real broker. Every account, order, fill and response is labelled `PAPER`, and the database rejects any fill whose mode isn't `PAPER`.

### Workflow

1. **Ingest the bars.** Store **raw** daily bars for every symbol you'll trade, since fills and marks use raw prices (what you'd actually pay). Also store **split**-adjusted bars for signals.
2. **Create an account:** `POST /paper/accounts {"name": "main", "starting_cash": "100000"}`. You can optionally pass `risk_limits` and `cost_model`. Limits are fixed at creation and are never loosened automatically.
3. **Submit orders,** in either of two ways:
   - From a signal: `POST /paper/accounts/{id}/orders/from-signal {"signal_id": 1}`. The order is a market buy at the next open, valid for that session only. It uses the signal's cancel-on-open bounds and protective stop.
   - Manually: `POST /paper/accounts/{id}/orders`. Buys **must** include `protective_stop_price`. Omit `quantity` to size it automatically.
4. **After each session completes,** ingest the new raw bars, then `POST /paper/accounts/{id}/process`. Buys that fill get a protective GTC stop order automatically.
5. **Check:** `GET /paper/accounts/{id}` for the portfolio, and `GET /paper/accounts/{id}/reconcile` for the books.

### Fill rules (daily bars, deliberately conservative)

| Order | Fills when… | Base price |
|---|---|---|
| Market | the session's open | open |
| Limit buy | the open is strictly below the limit, or the low is strictly below it. **Touching is not a fill.** | the open if it's below the limit, otherwise the limit |
| Stop sell | the low reaches the stop | the stop, or the **open** if it gapped below the stop (gap risk) |
| Limit sell / stop buy | mirror images of the above | |

- **Costs.** The base price is moved against you by `half_spread_bps + slippage_bps` (default 2 + 5) and rounded to a legal tick against you (cents, or $0.0001 under $1). A limit fill is never worse than the limit. Commission is `max(commission_per_share × qty, min_commission)`, rounded up to the cent (default 0).
- **Latency.** An order only fills in sessions that open **after** it was submitted. The session open is approximated as the bar timestamp + 9h30m, so bars must be stamped at midnight U.S. Eastern; any other timestamp is refused.
- **Same-session stop.** A protective stop is live for the rest of the entry session, so a bar that opens above the stop and trades below it fills the entry *and* the stop.
- **One session for signal orders.** A signal order can only fill in the first session after its signal bar. If that session had already opened at submission, the order is rejected or expires.
- **No partial fills (v1).** Order size is capped at 1% of average daily volume instead. With the IEX feed, volume is IEX-only, so this cap is stricter than with consolidated volume.

### Risk gate

The gate runs **when an order is submitted, and again at the moment it would fill**, using the actual fill price and the account's state at that time. Every rule is reported, with numbers.

| Rule | Default |
|---|---|
| Account not halted | — |
| Buy has a stop below its worst possible entry | required |
| Per-trade risk (worst entry → stop, after costs and fees) | ≤ 0.5% of equity |
| Position cost | ≤ 10% of equity |
| Gross exposure, including working buys | ≤ 100% (no leverage) |
| Cash, net of cash reserved by working buys | no margin |
| Open positions plus pending entries | ≤ 10; one per symbol |
| Order size vs. average daily volume (20 bars) | ≤ 1% |
| Price | ≥ $5 |
| Last session's change | > −2% (otherwise new entries are blocked) |
| Drawdown from peak equity | < 10% (otherwise new entries are blocked) |

- **Sizing.** Position sizing picks the largest whole-share quantity that satisfies every limit, at the **worst admissible entry**. That's the cancel-above bound for signal orders, the limit price for limit orders, and the last close for market orders.
- **Market buys can be cancelled at fill.** A manual market buy that gaps up past its risk budget is cancelled when it would fill. Use a limit order to bound the entry price.
- **Sells that only close positions are always allowed,** even when halted or past limits. Short selling is never allowed.
- **Halt** cancels working buys and blocks new entries. Protective stops stay active.
- **Rejected orders are stored too,** with every rule check, so they can be reviewed.

### Accounting

- **The ledger is the source of truth.** `paper_ledger` is append-only, with one row per deposit, buy, sell and fee. Positions track a **total cost basis**, not a rounded average, so this identity holds exactly, to the last decimal:

  `equity − deposits = realized P&L + unrealized P&L − fees`

- **Reconciliation.** `GET .../reconcile` rebuilds the portfolio from the ledger and verifies:
  - the stored `cash` and `paper_positions` match it;
  - the fills match the ledger's trade lines and cash flows;
  - the identity holds;
  - cash is never negative.
- **Long only, whole shares, no margin.** A fill that would overdraw cash is cancelled.
- **Daily-loss and drawdown history.** Equity is snapshotted at every processed session close, and those snapshots drive the daily-loss and drawdown limits.

## PAPER options (long calls and puts)

**Simulated only, long only.** You can buy calls and puts to open and sell them to close. There is no option writing, no spreads, no exercise decisions and no margin. Options share the account's cash, ledger, risk limits and halt switch with shares.

Only **standard contracts** are supported: 100 shares per contract, with an OCC root equal to the ticker. Adjusted contracts (e.g. root `AAPL1` after a split), index options and renamed roots (e.g. `BRKB` for BRK.B) are skipped at ingestion and listed as `skipped`, because settling them as 100-share contracts would be wrong.

### Workflow

1. **Ingest quotes:** `POST /market/options/chain/ingest {"underlying": "AAPL", "expiration_date_lte": "2024-07-19", "strike_price_gte": "180", "strike_price_lte": "200"}`. This stores Alpaca's latest quote per contract (`GET /v1beta1/options/snapshots/{underlying}`). Repeat it whenever you want fresher quotes; unchanged quotes are not duplicated.
2. **Submit an order:** `POST /paper/accounts/{id}/options/orders {"symbol": "AAPL240719C00190000", "side": "buy", "quantity": 1, "limit_price": "2.50"}`. The response's `sizing` shows the **premium at risk** (the maximum loss) and the **breakeven at expiry**.
3. **Ingest quotes again, then process:** `POST /paper/accounts/{id}/process`. Option orders fill against quotes stored since submission.
4. **At expiry,** ingest the underlying's raw daily bar for the expiration date and process. The position is settled (see below).

### Fill rules (quote-driven, deliberately conservative)

- **Fills use only stored option quotes, never the underlying's price and never a model price.**
- **Buys fill at the ask, sells at the bid.** You always pay the full spread; there is no midpoint optimism.
- **Limit orders** fill only when the quote reaches the limit (ask ≤ limit for buys, bid ≥ limit for sells). Market orders take the next quote.
- **Displayed size must cover the whole order.** There are no partial fills, and no filling more contracts than were shown.
- **Only quotes stamped after submission count.** Among several working orders, quotes are processed in strict time order.
- **Lifetime.** A DAY order can only fill on quotes before 16:00 Eastern on the day it was submitted, then expires. Submitting a DAY order after that close is rejected (use `gtc`). GTC orders work until the contract expires.
- **Commission** is $0.65 per contract by default (`option_settings.commission_per_contract`), rounded up to the cent.
- **Marks.** Option positions are valued at the **bid** of the latest stored quote: what they could be sold for, and 0 if nobody is bidding.

### Options risk gate

The gate runs at submission and again at fill, using the actual fill quote. Defaults are in `option_settings`, set per account at creation:

| Rule | Default |
|---|---|
| Account not halted; one position per contract; ≤ 10 open positions | shared with shares |
| **Premium at risk** = premium × contracts × 100 + entry commission | ≤ 0.5% of equity (the per-trade risk budget) |
| Position cost, gross exposure, cash net of reservations | same limits as shares; no margin |
| Daily loss and drawdown | same limits as shares |
| Newest quote age at submission | ≤ 15 minutes |
| Quote is two-sided (bid > 0 and ask > 0) | required |
| Spread `(ask − bid) / mid` | ≤ 20% |
| Days to expiry (Eastern date) | ≥ 1: never buys on expiration day |

A market buy reserves cash at the ask seen at submission. If the ask is higher when it would fill, the gate runs again and cancels the order if the premium now breaks a limit.

### Expiry (a disclosed simplification)

A position still held after the expiration-day close is **cash-settled at intrinsic value**, using the underlying's **raw daily close** on the expiration date from stored bars:

- call: `max(close − strike, 0)` per share; put: `max(strike − close, 0)` per share;
- the ledger gets an `expire` line (no fill, no commission), and working orders on the contract are cancelled.

In reality, in-the-money contracts are normally **exercised into 100 shares each**, which then carry overnight gap risk and possible fees; out-of-the-money contracts expire worthless. Until the expiration-day bar is stored *and complete*, the position is reported as **awaiting settlement**. It is never settled with a guessed price.

Reconciliation includes these settlements, so the accounting identity stays exact.

### Option feeds

`ALPACA_OPTIONS_FEED` (default `indicative`) chooses the quotes used for fills and marks. It can be overridden per account with `option_settings.quote_feed`.

- `indicative` is Alpaca's free options feed. Alpaca describes these quotes as **indicative**: they may be delayed or derived rather than firm exchange quotes. PAPER fills against them are rough approximations.
- `opra` is the consolidated feed from all U.S. options exchanges and needs an Alpaca subscription; otherwise expect `provider_access_denied`.

## AlphaScout (research support)

> **Research support only.** AlphaScout never places orders and never says a stock will double. Scenarios are hypothetical arithmetic on stated assumptions. Scores are unvalidated heuristics, **not probabilities**.

AlphaScout runs as a separate API that shares the database and `.env` with MarketPilot:

```bash
# .env: SEC_USER_AGENT="Your Name you@example.com"  (required by SEC's fair-access policy)
uvicorn apps.alphascout.main:create_app --factory --port 8001
```

### Vertical slice

1. **Store prices first.** Ingest raw daily bars for the tickers through MarketPilot (`POST /market/bars/ingest`). AlphaScout uses **stored** prices only, so a missing or stale price is reported, never filled in.
2. **Scan a small universe:** `POST /discovery/scan {"tickers": ["ABCD", "EFGH"]}` (max 25). Each ticker gets the screen result with reasons: price, market cap from SEC shares outstanding, average dollar volume, and exchange. OTC/unlisted and microcap names are flagged. Filters are configurable experiments, not safety criteria.
3. **Research one candidate:** `POST /research/ABCD {"horizon_months": 24}`. This:
   - fetches SEC **submissions** and **XBRL company facts** (primary sources). Every fetch, including failures, is recorded in `research_documents` with its SHA-256, and identical content is stored once;
   - extracts **point-in-time** fundamentals: only facts *filed* on or before the report date are used, so there is no look-ahead. Annual periods are found by their dates (a 10-K's `fp=FY` tag also covers its 3-month Q4 figures). If a period was restated, the latest filing known at the time wins;
   - **cites every number**: each figure links to its filing index on sec.gov with the form, accession number, filing date and XBRL concept. Derived numbers (growth, free cash flow, runway, dilution) are marked `computed` and list their inputs;
   - values **bear / base / bull** scenarios (EV/Sales): future revenue, enterprise value, equity value after **net debt**, **diluted** shares, implied price and return, plus a sensitivity grid. It also reports the revenue CAGR that 2× would *require*. The default assumptions are mechanical, derived from the company's own history and labelled as such; you can pass your own. A doubled enterprise value does not double the share price, and the tests check exactly that;
   - runs the **red team**:

     | Severity | Findings |
     |---|---|
     | **hard** (reject) | runway < 12 months · dilution > 20%/yr · not exchange-listed · late filing (NT 10-K/10-Q) · restatement (8-K item 4.02) · implausible bull case |
     | **warning** | auditor change (8-K 4.01) · shelf/ATM/offering filings · negative FCF · revenue decline · debt not reported |

   - decides the verdict **in a fixed order**: any hard finding → `rejected` (nothing can override it, not even the score or a bull case that doubles) · missing core data → `insufficient_evidence` · no scenario reaches 2× → `no_2x_path` · otherwise `watchlist`, which means "keep researching", not "buy";
   - saves a **new version**. Reports are append-only.
4. **Read it:** `GET /research/ABCD` (latest), `GET /reports`, `GET /reports/{id}`, `GET /evidence/ABCD`, `GET /catalysts` (upcoming, soonest first, from each symbol's latest report), `GET /news/ABCD`. The dashboard has a read-only **Research** page.
5. **Track outcomes:** `GET /reports/{id}/evaluation` reports returns after 1/5/20/60 trading days and 6/12/24 months, plus maximum favourable and adverse excursion, using **only stored bars after the report's reference bar**. Reports are never rewritten.

`POST /explanations/report/{id}` (MarketPilot API) gives a validated AI explanation of a report. Company names and filing fields are untrusted text; a test shows that an injected instruction in a company name cannot get an invented price target past the validator.

### News verification

With Alpaca keys configured, research also pulls the last 90 days of articles from the **Alpaca News API** (same keys; content is Benzinga's, subject to the provider's licence) and stores them in `news_items`, one row per provider id. News is treated as a **lead, not proof**, and its text is untrusted data:

- **Deduplication uses more than the headline:** the canonical URL (tracking parameters and `www`/`http` variants removed), a fingerprint of headline plus body, and an identical headline within 48 hours.
- **Flags:**
  - promotional language;
  - anonymous or unconfirmed sourcing;
  - analyst opinion presented as news;
  - instruction-like text (kept as data);
  - figures in the headline that the body doesn't contain;
  - headline-only items;
  - articles updated long after publication.
- **Verification status:**

  | Status | Meaning |
  |---|---|
  | `corroborated_by_filing` | A matching SEC filing (e.g. earnings → 8-K item 2.02 / 10-Q) was filed within 3 days before to 1 day after the article |
  | `re_report_of_older_filing` | The matching filing is more than 7 days older: an old event, **not a new catalyst** |
  | `uncorroborated` | No matching primary source |
  | `opinion_not_fact`, `promotional_downgraded` | Never treated as facts |

- **Point-in-time:** articles published, and filings filed, after the report date are invisible to it.
- **Red-team warnings:** offering/dilution news corroborated by filings, and coverage that is mostly promotional.

### Catalysts and date status

| Status | Source | Example |
|---|---|---|
| `occurred` | 8-K items (past) | 2.02 results, 1.01 agreement, 3.02 equity sale, 4.02 non-reliance |
| `confirmed` | a primary filing states the date | annual meeting date in the definitive proxy (DEF 14A), with the excerpt and link |
| `estimated` | **our** window from the company's own 10-Q/10-K cadence (±10 days) | next periodic results. Not a company date; omitted if the cadence is broken |
| `reported` / `rumored` | a date in a news headline; `rumored` when sourcing is anonymous | never confirmed by news alone |

Catalyst credibility (out of 20 points) = relevance (high 20 / medium 12 / low 6) × status weight (confirmed 1.0, company-guided 0.7, estimated 0.4, reported 0.2, **rumored 0**), taking the best event inside the horizon. These are heuristic weights, not probabilities. An upcoming event never guarantees a price move.

### Not done yet (and stated in every report)

- **Filing text is only read for proxy meeting dates.** Going-concern language, auditor opinions, Form 4 insider details, customer concentration and litigation are not analysed; they are listed under `not_checked`.
- **No company-guided dates yet:** earnings-call announcements and investor-relations calendars are not ingested, and transcripts aren't either.
- **Narrow news corroboration:** it matches event *categories* to filing types and items within a time window, so a contract article corroborated by an unrelated 8-K item 1.01 is possible. Treat `corroborated_by_filing` as "a matching filing exists; read it".
- **Not run against live SEC or Alpaca News** in this environment (both blocked by the network policy). See "Live verification" below.

## AI explanations

The AI layer **explains**; it never calculates, decides, sizes, or trades. Accounting, indicators, sizing, valuation math and risk stay in deterministic code.

How an explanation is made (`POST /explanations/{kind}/{id}`):

1. **A fact sheet is built in code** from the stored record: for example, an order's every risk check with its numbers, a signal's rule checks and levels, or a backtest's metrics and warnings.
2. **The model sees only that fact sheet.** It is sent as JSON inside a `<facts>` block, and the system prompt says the facts are data, not instructions.
3. **The model must answer in a fixed JSON shape** (structured outputs), with every point citing the ids of the facts it uses.
4. **The answer is validated, and rejected if:**
   - it cites a fact id that doesn't exist, or a point cites nothing;
   - it contains **any number that is not in the facts**, so it cannot compute, round, convert or invent a price;
   - it uses certainty or advice language ("guaranteed", "risk-free", "you should buy", "price target", …).
5. **A rejected or failed answer is never shown.** The response falls back to a deterministic explanation (the facts, plainly listed) and says why.
6. **Every attempt is stored** in `ai_explanations`: the facts and their hash, the model and request id, the raw answer, the validation issues and any error. Rejected and failed attempts are stored too.

Prompt injection: fact values can contain untrusted text (and will, once AlphaScout feeds documents in). Escaping the text and the system prompt help, but the **validator is the real control**: an injected instruction that changes the answer still has to pass the checks above.

Configuration:

- Set `ANTHROPIC_API_KEY` to enable AI explanations. Without it, explanations are deterministic and `GET /health` shows `ai_explanations_enabled: false`.
- The model is `AI_MODEL` (default `claude-opus-5-5`). Requests use low effort, JSON-schema output, the SDK's bounded retries (`AI_MAX_RETRIES`, default 2), and a timeout (`AI_TIMEOUT_SECONDS`).
- Requests also opt into Anthropic's server-side **refusal fallback** (`fallbacks: "default"`). If the model declines, the API reruns the request on a fallback model it chooses; a final refusal is recorded as `ai_refused`.
- The key is passed to the SDK explicitly, so the app never picks up another credential from the machine. It is redacted from logs like every other secret.

Tests use a scripted fake model (`tests/fakes_ai.py`) and the real SDK against a mocked HTTP transport. **No test calls the real API, and AI explanations have not been run against the live Anthropic API in this environment.**

## Forward paper trading

A **deployment** runs the strategy forward on a PAPER account, one daily cycle at a time, using exactly the code the backtester uses. A test runs the daily cycle over historical data and requires the **same trades, prices and final equity** as the backtest.

```bash
# 1. a PAPER account and a deployment
curl -X POST http://127.0.0.1:8000/paper/accounts -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"name": "main", "starting_cash": "100000"}'
curl -X POST http://127.0.0.1:8000/paper/deployments -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"account_id": 1, "name": "trend-v1", "symbols": ["AAPL", "MSFT", "SPY"]}'

# 2. one cycle by hand, or let the scheduler run it daily (below)
python -m apps.marketpilot.cli run-cycle --deployment 1
```

### What a daily cycle does

1. **Ingest.** Fetches new **raw** and **split**-adjusted daily bars for each symbol. The first run fetches about 10 months of warm-up; later runs fetch from the last stored bar, re-reading 5 days to catch provider revisions. Failures are recorded and the cycle continues on stored data; the strategy's stale-data gate refuses to act on old data. The cycle status becomes `partial`.
2. **Process.** Runs the PAPER account: fills, protective stops, expiries.
3. **Exits.** For positions this deployment opened, it applies the strategy's exit rules: a close below the 10-bar low, or the 60-bar time stop. These are the same `exit_reason` function the backtester uses. Exits are placed as market sells for the next open.
4. **Entries** (active deployments only). Generates signals, then submits risk-gated signal orders for the next open. Symbols already held or pending are skipped.
5. **Record.** Every cycle is stored with a step-by-step summary: `GET /paper/deployments/{id}/cycles`.

### When to run it

Once per trading day, **after midnight U.S. Eastern** (when the previous daily bar counts as complete) and **before the 09:30 open**. Then entries and exits fill at that morning's open, exactly as in the backtest. Suggested: **06:30 America/New_York**. Running more than once is harmless.

Linux/macOS (crontab; the server's clock should be set to New York time, or adjust the hour):

```cron
30 6 * * 1-5  cd /path/to/market-intelligence && mkdir -p logs && .venv/bin/python -m apps.marketpilot.cli run-due >> logs/cycles.log 2>&1
```

Windows (Task Scheduler, from PowerShell):

```powershell
$action = New-ScheduledTaskAction -Execute "$PWD\.venv\Scripts\python.exe" `
  -Argument "-m apps.marketpilot.cli run-due" -WorkingDirectory "$PWD"
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 6:30am
Register-ScheduledTask -TaskName "MarketPilot daily cycle" -Action $action -Trigger $trigger
```

- **Output.** `run-due` runs every deployment and prints one JSON line per cycle.
- **Exit code.** Non-zero if any cycle failed or couldn't start. A `partial` cycle (for example, ingestion unavailable) exits 0.

### Restart safety

- **Every step is idempotent.** Bar upserts, the account's `processed_through`, unique signals, one order per signal, and exit orders keyed `d<deployment>-<reason>-<symbol>-<bar date>` (plus a "no second exit while one is working" check). Re-running a crashed, retried or duplicated cycle never creates duplicates. Tests crash a cycle mid-way, re-run it, and get the same result as an uninterrupted run.
- **Interrupted cycles are marked.** A cycle left `running` by a crash is marked `interrupted` on the next run.
- **No overlapping cycles.** A **PostgreSQL advisory lock** prevents two cycles for one deployment running at once. The second is refused, and the lock is released automatically if a process dies.

### Pause and emergency shutdown

| Status | Ingest + process fills | Strategy exits | New entries |
|---|---|---|---|
| `active` | yes | yes | yes |
| `paused` (`/pause`) | yes | yes | **no** |
| `stopped` (`/shutdown`) | yes, so liquidation and stop orders complete | no | no |

`shutdown` halts the **whole PAPER account** (cancelling working buys and blocking new entries), and with `liquidate: true` it submits market sells for every open position. It is final: create a new deployment to start again.

## Dashboard

```bash
pip install -e ".[dashboard]"     # already included if you installed requirements-lock.txt
uvicorn apps.marketpilot.main:create_app --factory          # terminal 1: the API
streamlit run apps/dashboard/app.py                         # terminal 2: http://localhost:8501
```

Set `DASHBOARD_API_URL` if the API isn't at `http://127.0.0.1:8000`. `.streamlit/config.toml` makes the dashboard listen on **localhost only**, because it has no login, and turns off Streamlit's usage statistics. Start it from the project root so that file is read.

| Page | Shows |
|---|---|
| Overview | Alerts, PAPER accounts (equity, return, drawdown), deployments and their last cycle |
| PAPER account | Equity and cash curve, drawdown, positions with mark date and age, working orders, fills, reconciliation, limits |
| Deployments | Status, symbols, parameters hash, every cycle with a step-by-step summary |
| Signals | Every rule check with its numbers, entry and exit plan, score components (labelled *not probabilities*) |
| Backtests | Warnings first, then equity and drawdown, metrics with conventions, in/out-of-sample, benchmark, trades, reproducibility data |
| Market data | Stored coverage and freshness per series (feed, adjustment), recent ingestion runs and failures |
| System health | Trading mode, live trading **disabled**, database and schema, provider configuration |

**Read-only by design.** The dashboard talks only to the API, never the database, and its client can only send **GET** requests, without the write token. Halting, pausing and shutting down stay with the token-protected API and CLI. A test renders every page on real data and asserts that every request was a GET.

**Labelling.** Simulated activity is marked **PAPER**, backtests **BACKTEST**, and stored provider data **MARKET DATA**, with its feed, adjustment and age.

**When things are missing,** for example the API is down or the database isn't configured, every page still renders and says what is wrong. Tests cover both cases.

## Alerts

`GET /alerts` (also on the dashboard's Overview) computes alerts from current state. Nothing is stored, so alerts are never duplicated: each has a stable `key` and disappears when its condition clears.

| Severity | Conditions |
|---|---|
| critical | last cycle **failed** · books **do not reconcile** · **drawdown limit** reached (entries blocked) · account cannot be valued · **possible split in a held stock** (raw vs split-adjusted close ratio changed) |
| warning | last cycle **partial** or **interrupted** · **no cycle for more than 3 days** · **stale data** (latest bar ended more than 4 days ago) · drawdown at 80% or more of its limit · **daily-loss limit** hit · account **halted** · failed ingestions in the last 24 h · expired **option positions awaiting settlement** |
| info | deployment never run · market data provider not configured |

Stopped deployments are not checked. Thresholds are calendar-day approximations (no exchange calendar), chosen to stay quiet over a weekend plus one holiday.

**Getting notified.** `python -m apps.marketpilot.cli alerts` prints the alerts as JSON and **exits with code 2 if anything is critical**. Schedule it shortly after the daily cycle. cron emails a job's output when `MAILTO` is set:

```cron
MAILTO=you@example.com
45 6 * * 1-5  cd /path/to/market-intelligence && .venv/bin/python -m apps.marketpilot.cli alerts > /dev/null || .venv/bin/python -m apps.marketpilot.cli alerts
```

The second command runs only on a non-zero exit, so you get an email only when something is critical. On Windows, have Task Scheduler run the same command and act on the exit code.

## Backtesting

```bash
# Ingest fully adjusted daily history for the symbols and the benchmark (SPY by default)
curl -X POST http://127.0.0.1:8000/market/bars/ingest -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"symbol": "SPY", "adjustment": "all", "start": "2015-01-01", "end": "2024-12-31"}'
# (repeat for each symbol)

curl -X POST http://127.0.0.1:8000/backtests -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"symbols": ["AAPL", "MSFT"], "start": "2016-01-01", "end": "2024-12-31",
       "out_of_sample_start": "2022-01-01"}'
```

Optional request fields: `starting_cash`, `parameters` (strategy), `risk_limits`, `cost_model`, `benchmark`, `risk_free_rate_pct`, `feed` and `adjustment` (`all` or `split`).

**The engine** (`apps/marketpilot/backtesting/engine.py`) walks daily sessions in time order. In each session it:

1. **fills** orders decided at earlier closes: market orders at the open first, then stops and limits. Entries are re-checked by the risk gate at the actual fill price.
2. **marks** positions at the close;
3. **decides** exits and entries *at the close*, for the next session only.

It runs the strategy's **full** rules:
- entry at the next open, within the signal's cancel bounds;
- the protective stop;
- an exit on a close below the 10-bar low;
- the 60-bar time stop.

It uses **the same functions as PAPER trading** for fills, costs, sizing, the risk gate and accounting. A parity test proves both give identical fills for the same signal.

**No look-ahead** is verified two ways:
- **Truncation invariance:** cutting the data at any session leaves every earlier result unchanged.
- **Future changes:** altering future prices never changes the past.

Bars before `start` serve only as indicator warm-up.

**The report** includes:
- **Returns:** total return, CAGR (only for periods of at least a year), max drawdown and its duration, annualized volatility.
- **Ratios:** Sharpe and Sortino, using √252 annualization and the given risk-free rate.
- **Exposure:** % of sessions invested, and average gross exposure.
- **Trades:** count, win rate, average win and loss, profit factor, expectancy in $ and in R-multiples (P&L divided by the trade's initial risk), average holding period.
- **Costs:** fees and estimated spread plus slippage cost.
- **Benchmark:** buy-and-hold total return, its drawdown, and the strategy's excess return.
- **Optional split:** separate in-sample and out-of-sample metrics.
- **Detail:** every trade and the full equity curve.
- **Signal statistics:** how many evaluations, and why entries were rejected.

**Reproducibility.** Each saved report records:
- the full request;
- the strategy version and `parameters_hash`;
- a **SHA-256 fingerprint of every bar used**, including warm-up and benchmark.

The same fingerprint plus the same request always gives the same result. If stored data is revised, the fingerprint changes.

**Prices.** Backtests use **adjusted** bars (default `all`, meaning splits and dividends) for both signals and fills. This keeps positions correct across splits and makes the benchmark comparison a total-return comparison. The trade-off is that fills before a later split or dividend are at adjusted prices, not the prices that actually traded.

**Every report carries warnings, and they matter:**
- **Survivorship bias** always applies: symbols chosen today exclude companies that died during the period, so results are likely optimistic.
- **Costs are assumptions.**
- **Fewer than 30 trades** gets an explicit "too few to conclude" warning.
- **Under a year** of data is not annualized.
- **The IEX feed's** volume is IEX-only, so the liquidity cap is stricter and positions may be smaller.
- **Out-of-sample** results only count if the parameters were chosen without looking at that period.

**Performance.** About 12 s for 10 symbols × 10 years on the build machine. Runs execute in a worker thread and are limited to 20 symbols and 15 years.

> A backtest is a hypothesis test with many ways to fool yourself. It is not evidence that a strategy will make money. Results here have not been validated on real data (see limitations).

### Database migrations

```bash
alembic upgrade head        # apply all migrations
alembic current             # show the applied revision
alembic check               # fail if ORM models and migrations have drifted
alembic downgrade -1        # undo the latest migration
```

The schema is changed **only** through migrations in `migrations/versions/`.

**Never edit a migration that has already been applied** anywhere that matters; add a new one instead. `alembic check` compares tables, columns, indexes, and constraints by name, but **not** the text of CHECK constraints. An edited constraint can therefore go unnoticed on a database that ran the old version. This happened during development of 0003 and was caught by a smoke test.

## Testing

```bash
pytest                                  # unit tests + database tests
ruff check . && ruff format --check .
```

- **Unit tests** (`tests/unit`) are offline and need no credentials or database. Provider tests use `httpx.MockTransport` with **synthetic** payloads.
- **Database tests** (`tests/db`) need PostgreSQL running and `TEST_DATABASE_URL`, read from your shell or `.env`. Without it they are skipped, not failed.
  - Each test session rebuilds the schema through the real migrations, and tests wipe the tables between runs.
  - As a safety guard, they refuse to run unless the database name ends in `_test`.
- **Live tests** call the real Alpaca API. They need keys and network access, and the second also needs the test database. They verify a real fetch, then a real ingest that is idempotent on repeat:

```bash
# bash
ALPACA_LIVE_TESTS=1 pytest -m live -s
# PowerShell
$env:ALPACA_LIVE_TESTS="1"; pytest -m live -s
```

### Live verification

Everything above has been verified on synthetic data only. When the network allows `data.alpaca.markets`, `data.sec.gov` and `www.sec.gov`, and the credentials are set as environment variables (never in code or chat), one command checks the real providers:

```bash
ALPACA_LIVE_TESTS=1 SEC_LIVE_TESTS=1 pytest -m live -s
```

| Test file | What it checks |
|---|---|
| `tests/integration/test_alpaca_live.py` | real bars; idempotent ingest into the test database; news; the option chain on `ALPACA_OPTIONS_FEED` |
| `tests/integration/test_sec_live.py` | ticker map, submissions and company facts for CIK 320193; fundamentals cited to sec.gov; filing text |
| `tests/integration/test_alphascout_live.py` | a full research run on real data into the test database |

The tests print what they received (`-s`), so the shapes and values can be inspected. A blocked network makes them **fail** with the provider error; they never pass on fake data.

## Design decisions

- **App factory** (`create_app`): importing modules has no side effects, and tests build apps with their own settings and a fake provider.
- **Provider interface**: routes and services depend on `MarketDataProvider`, not Alpaca.
- **Decimal end to end**: JSON is parsed with `parse_float=Decimal` and stored as `NUMERIC`, so prices never pass through binary floats.
- **UTC only**: naive datetimes are rejected instead of guessed.
- **No silent repair**: bad, conflicting, or out-of-range bars raise errors. Exact duplicates are dropped and counted. Empty results stay empty. Nothing is invented.
- **No transaction during network calls**: ingestion fetches first, then writes in one short transaction.
- **Bounded retries**: only timeouts, network errors, 429, and 500/502/503/504 are retried. Backoff is exponential, capped, and honours `Retry-After`.
- **Async SQLAlchemy with psycopg 3**: the same driver serves the async app and Alembic's synchronous migrations.
- **Secure by default**: write endpoints are disabled until `API_WRITE_TOKEN` is set. The token is compared in constant time.
- **Secrets**: API keys, the database password, and the write token are `SecretStr`. They are redacted from logs and never echoed in errors, including `DATABASE_URL` validation errors.

## Limitations and assumptions

- **The Alpaca adapter is not yet verified against the live API.** The build environment had no Alpaca credentials, and its network policy blocked `data.alpaca.markets`. The adapter follows the contract in Alpaca's official `alpaca-py` SDK source (`GET /v2/stocks/bars`, `APCA-API-KEY-ID`/`APCA-API-SECRET-KEY` headers, `next_page_token` pagination, max 10,000 bars per page). Run the live tests above to confirm it.
- **IEX feed (default)** covers IEX-exchange trades only, not the consolidated tape. Volumes and some prices differ from SIP data.
- **SIP feed:** recent consolidated data may need a paid Alpaca subscription. Otherwise expect `provider_access_denied`. `delayed_sip` is delayed by 15 minutes.
- **Bar timestamps** are stored exactly as Alpaca reports them. Alpaca is generally understood to stamp bars at interval start (daily bars at midnight U.S. Eastern). Confirm with live data.
- **No market calendar yet.** Date-only bounds are UTC days, and stored series are not checked for missing trading days.
- `last_bar_may_be_incomplete` is a heuristic based on approximate interval length.
- **No market calendar yet.** Staleness and gap limits are calendar-day approximations, chosen to tolerate weekends plus one holiday. A long market closure would show up as `data_gap`.
- **Strategy exits are automated only for deployment positions.** Positions opened by a deployment get every strategy exit. Positions from manual or one-off signal orders get only their protective stop.
- **Alerts are pull-based.** There is no built-in email/Slack delivery. Use the CLI exit code with cron's `MAILTO`, or Task Scheduler. The dashboard has no authentication: keep it on localhost.
- **Scheduling is external.** Use cron or Task Scheduler: there is no in-process scheduler, deliberately, so a crashed web server cannot silently stop trading cycles. A missed day is caught up on the next run, but a missed *entry* session cannot be (the signal expires).
- **Corporate actions are not applied to PAPER positions.** If a held stock splits, its raw price drops and the position quantity is not adjusted, so P&L is misstated until this is handled. A critical alert flags it when the stored raw and split-adjusted closes change ratio. That needs both series ingested after the event, because providers adjust history retroactively.
- **Simulation timing is approximate.** Sessions are inferred from midnight-Eastern bar timestamps, with no exchange calendar (half days and holidays are not modelled). Bars stored *after* the simulation has processed their session are not replayed.
- **Options are a narrow simulation.**
  - Long calls and puts only, standard 100-share contracts only.
  - Fills need a stored quote, so they happen only as often as you ingest quotes. A quote is a snapshot, not the full quote history: prices between snapshots are never seen.
  - Expiry is cash-settled at intrinsic value instead of exercising into shares. Early exercise, assignment, dividends and corporate actions on contracts are not modelled. There are no option backtests.
  - Within one processing run, a session's share fills are applied before that day's option fills. Across runs, events are applied in the order they're processed, which can differ from the true intraday order.
  - The option chain adapter follows the `alpaca-py` SDK source (`GET /v1beta1/options/snapshots/{underlying}`, `latestQuote` fields `t/bp/bs/ap/as`) and is **not verified against the live API** (no credentials or network access in the build environment).
- **Fill realism is limited by daily bars.** The intraday path is unknown, so the rules assume the worse outcome. No partial fills, queue modelling or market impact beyond the fixed bps.
- **No performance claims.** No backtest has been run on real market data yet: the build environment couldn't reach Alpaca. All verification used synthetic data. Neither paper results nor backtests are evidence of future profitability.
- **No parameter optimization or walk-forward analysis yet.** The out-of-sample split only reports fixed-parameter results for two periods.
- The `xmax = 0` technique for counting inserted vs. updated rows is PostgreSQL-specific. The project targets PostgreSQL only.
- Market data licensing: data obtained through Alpaca is subject to Alpaca's and the exchanges' terms. Do not redistribute it.
