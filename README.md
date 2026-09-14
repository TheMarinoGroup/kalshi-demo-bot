# kalshi-demo-bot

Paper-trading **sampler** for Kalshi crypto Up/Down markets at
**15 minutes or greater only** (paper-v2 Option B default `KXBTC15M`;
`KXETH15M` and later 1H/4H clips are allowed). It does **not** trade Polymarket-style
5-minute markets. It is a short-window **liquidity provider /
statistical scalper**, not a directional crypto book and **not a live trader**.

Research Dig #2 ships a **paper data plane**: public prod REST for events and
books, an authenticated WebSocket (demo or read-only prod), a conservative
local matcher, and a JSONL tape. Default mode is **dry-run + paper-tape**.
`POST /portfolio/events/orders` is off unless you pass `--demo-submit`.

This is still demo/paper only. Production *order* hosts are refused unless you
set both `KALSHI_ENV=production` and `KALSHI_ALLOW_PRODUCTION=1`. Public prod
REST (no key) is the **data** default.

HTTP **429** is a rate limit: the bot retries public REST with backoff (honors
`Retry-After`). Wait a minute if you just hammered `/events` before restarting
`kalshi-pbot hud`. Transient WebSocket drops (keepalive ping timeout, send on a
closed socket) reconnect and resubscribe — the HUD stays up through 15m
universe rollover.

## What it does

1. **Events-first rollover** via `GET /events?status=open` and
   `GET /events?status=unopened` (not `/markets?status=unopened`) for
   configured series (default `KXBTC15M`). The live 15m clip is usually a nested
   `active` market under `status=unopened`; `status=open` is often the
   window that just determined. Windows persist to `data/windows.json`.
2. Rebuilds the Kalshi **bids-only** book. Implied ask = `1 − opposite bid`.
   Emits a **100 ms top-of-book heartbeat**.
3. Auth WS (demo or read-only prod): `orderbook_delta` + `trade` + `ticker` +
   `market_lifecycle_v2` + `cfbenchmarks_value` (optional 5 Hz). CFB indices:
   BTC `BRTI`, ETH `ETHUSD_RTI`.
4. Evaluates maker quotes. Pair arb runs only when `quote_mode=two_sided`.
5. Pushes every intent through **Risk Desk v1** before it is registered.
6. **Local paper matcher** — join the **back** of the book; fills only from
   public trades at or through our price; ambiguous size wipes = **no fill**.
   Latency buckets `L ∈ {50, 150, 500}` ms (default 150).
7. **Zero order POSTs** in the research/paper-tape phase. `--demo-submit` is
   explicit and off by default.
8. **Bloomberg-style HUD** (`kalshi-pbot hud`) — dark desk: risk meters,
   live TOB, window countdown (last-60s amber), inventory, paper tape,
   session PnL. Works on mock or public prod data; never needs live
   trading.

Settlement oracle (context, not a trading signal): CF Benchmarks **BRTI**
(BTC) / **ETHUSD_RTI** (ETH) — 60s open average vs 60s close average; ties
resolve Yes. The last 60 seconds of each window are treated as toxic.

**Actual settlement is fast.** On public REST, N=8000 finalized
`KXBTC15M`+`KXETH15M` windows, `close_time` → `settlement_ts` is
p50≈7s, p90≈12s, p99≈59s (~99% within 60s). The market field
`expected_expiration` (~close+300s) is **not** settlement latency — do
not use it as a settle-lock. Paper capital recycles at
`close + settle_recycle_seconds` (default **75s**, band 60–90s).
`KALSHI_SETTLE_RARE_TAIL=true` waits 300s instead if you want the
conservative tail.

Architecture supports later **7×24h tape / expectancy** runs. v1 ships the
modules (`tape`, `expectancy`) plus the dry-run / paper-tape path.

## Risk Desk v1 (locked fractions; paper-v2 Option B at $500)

| Limit | At $500 (Option B default) | Rescale |
| --- | --- | --- |
| Per fill / clip | $10–$30 (default $10) | absolute band |
| Max open notional | $25 | 5% of bankroll |
| Max concurrent 15m windows | 1 | config |
| Daily loss kill (realized + fees + unsettled MTM) | $10 | 2% of bankroll |
| Max incomplete / one-sided inventory | $15 | 3% of bankroll |
| Last 60s before `close_time` | no new risk floor; cancel / flatten only | config (paper-v2 uses 120s) |
| Paper recycle after close | 75s default (60–90s band; p99≈59s) | `KALSHI_SETTLE_RECYCLE_SECONDS` |
| Rare-tail settle lock | off; 300s if enabled | `KALSHI_SETTLE_RARE_TAIL` — **not** `expected_expiration` |
| Kill switch | daily loss latches (persists across HUD restart). Open overshoot is **refused before register/fill** and does not trip kill in paper-tape | — |

Change `KALSHI_BANKROLL` and the percentage limits move with it. Paper only.

**Daily kill $10 = 1× clip**, so the soft layer is mandatory: abort unpaired
above $10 or after 45s, complete the other side after any touch, and refuse
a new one-sided clip while unpaired exists. Disabling soft abort
(`soft_onesided=0` or `max_unpaired_age_seconds=0`) is refused when daily
kill ≤ clip.

## Strategy notes

**Maker-first (default).** Resting limit bids, `post_only` when the V2 events
API accepts it, STP `taker_at_cross`. Quote **one side**
(`KALSHI_QUOTE_MODE=one_sided`; do not switch to `two_sided` unless Risk
later OK). After any fill, completing the other side is the only quote
until the pair is done or unpaired is aborted.

Maker fee on these series is believed **$0** (`fee_type=quadratic`). The bot
logs **fee drag** on every paper fill. Quadratic maker = $0 is **pending
demo-fill confirmation** (`pending_demo_confirm=true` in logs).

**Pair arb.** Buy YES and buy NO only when `Py + Pn + fees + min_edge < 1`.

- **Maker–maker** is the realistic path (`Py + Pn < 1` after $0 maker fees).
- **Taker–taker** is off by default. Quadratic taker fees peak near
  `0.07 × 0.5 × 0.5 = $0.0175` per contract (~3.5% of $1 notional for both
  legs). You need `Py + Pn ≲ 0.965` after fees — usually not available,
  because taking both implied asks costs `2 − (yes_bid + no_bid)`.

**Capital velocity.** Tiny clips, recycle **~60–90s after close** (default
75s) once a result is known — not 5–6 minutes and not
`expected_expiration`. Unsettled MTM still counts toward the daily kill
until recycle. Completing an incomplete pair is always preferred over a
new one-sided clip. Unpaired inventory is aborted on age
(`KALSHI_MAX_UNPAIRED_AGE_SECONDS`, default 45s), when unpaired notional
is **above** the soft $10 preference (`KALSHI_SOFT_ONESIDED`), or when
the completing side cannot be quoted — not only at the hard onesided
kill. A $10 clip at the soft cap is still completed; growth past $10 is
flattened. A new one-sided clip is refused while unpaired inventory
exists on any other window/ticker. Last 120s (config; Risk Desk floor
60s): cancel quotes; do not complete pairs; flatten unpaired if a bid
exists.

### Overnight soak lesson + paper-v2 Option B (paper)

A paper overnight soak tripped **onesided ≥ $30** with `day_pnl_net` ≈
−$24.7 (unrealized ≈ −$25.5, realized +$0.76, fees ≈ $0). The loss was
**unpaired one-sided MTM**, not taker fees. Do not warehouse leftover
YES or NO across windows; finish or flatten the open clip first.

**Paper-v2 Option B** is an explicit state machine (`FLAT` → `COMPLETE` →
`SOFT_ABORT` / `BLOCK_NEW` / `LAST_SECONDS` / `HARD_KILL`). It stays
`QUOTE_MODE=one_sided` (not `two_sided`). Soft layer (mandatory at $500
because daily kill = 1× clip): onesided abort above $10, unpaired age
45s, complete-other-side after any touch, no new onesided while unpaired
exists, `MIN_EDGE=0.04` (`bid_sum ≤ 0.96`), `improve_ticks=0`,
`taker_pair_arb=false`.

These are now the code defaults (still dry-run / paper-tape; no
production orders):

```bash
KALSHI_DRY_RUN=true
KALSHI_PAPER_TAPE=true
KALSHI_BANKROLL=500
KALSHI_SERIES=KXBTC15M
KALSHI_MAX_WINDOWS=1
KALSHI_CLIP_DOLLARS=10
KALSHI_QUOTE_MODE=one_sided
KALSHI_IMPROVE_TICKS=0
KALSHI_TAKER_PAIR_ARB=false
KALSHI_MIN_EDGE=0.04
KALSHI_LAST_SECONDS=120
KALSHI_MAX_UNPAIRED_AGE_SECONDS=45
KALSHI_SOFT_ONESIDED=10
KALSHI_ONLY_QUOTE_UNDERROUND=true
```

Soft knobs fire before the Option B hard caps ($25 / $15 / $10). Do not
disable soft abort on this bankroll.

Later soak pass bar (paper): **0 daily-loss kills**; `day_pnl_net > 0` over
**≥ 96 BTC windows**; incomplete-pair **< 10%**; peak onesided **≤ $10**;
**peak open ≤ $25** (gross cost + reserved, including a completing quote).

**Open-notional overshoot (Dig6).** A $10 clip can still print `open_notional
33.327 >= 25.00` if a cheap YES fill (many contracts) is completed with a
same-count rich NO bid: reserved + cost ≈ qty × $1 until the pair prints.
**Primary control:** refuse the register and the fill **before** it sticks if
projected open would exceed `max_open` (Option B $25 / v1 $50). Completing
quotes are sized to remaining capacity. Kill-after-breach is not the control.
Paper-tape does **not** trip kill on open overshoot and does **not** auto-reset
any kill. Only **daily loss** (and HUD manual) freeze the soak, and that latch
is persisted so a watchdog process restart cannot clear it.

## Paper matcher

Not the exchange. Conservative queue:

* Join the **back** of the book (`queue_ahead = size already at our price`).
* A public **trade** at our price eats the queue first, then us.
* A through-print (`yes_price < our bid` for a YES bid) sweeps remaining.
* A book-size drop **without** a matching trade is an **ambiguous wipe**:
  shrink queue ahead, **never fill**. Wipe-to-zero cancels without fill.
* Events apply at `ts + L` for `L ∈ {50, 150, 500}` ms.

Risk Desk v1 still gates which quotes are registered.

## Architecture

```
CLI (kalshi-pbot) ── runner.PaperBot
                       ├── MarketUniverse + WindowStore + OrderBookStore
                       ├── KalshiRestClient (prod data / demo orders) + WS
                       ├── PairArbStrategy + MakerStrategy
                       ├── RiskEngine
                       ├── PaperMatcher + JsonlTape + expectancy.replay_tape
                       ├── ExecutionEngine   (paper-tape default; POST gated)
                       ├── Reconciler        (startup exchange snapshot; ready_to_trade gate)
                       └── Portfolio + metrics
```

| Module | Role |
| --- | --- |
| `kalshi_pbot/config.py` | Dual plane, latency buckets, settle recycle (not expected_expiration) |
| `kalshi_pbot/kalshi_client.py` | Public `/events` + books; auth WS; order POST refused in paper-tape |
| `kalshi_pbot/market_data.py` | Events-first rollover, book snapshot/delta, implied asks |
| `kalshi_pbot/windows.py` | Persist / reload 15m windows |
| `kalshi_pbot/paper_matcher.py` | Conservative local matcher + latency |
| `kalshi_pbot/tape.py` | Append-only JSONL research tape (`v=1`) |
| `kalshi_pbot/expectancy.py` | Tape replay hook for later 7×24h runs |
| `kalshi_pbot/strategy/maker.py` | One-sided / controlled two-sided post-only quotes; unpaired complete / age abort |
| `kalshi_pbot/strategy/pair_arb.py` | Maker–maker and optional taker–taker pairing |
| `kalshi_pbot/fees.py` | Quadratic taker / maker fee + `fee_drag` |
| `kalshi_pbot/risk_engine.py` | Hard gates (last-60s, daily kill, caps) + soft unpaired-exists reject |
| `kalshi_pbot/execution.py` | Paper register or `POST /portfolio/events/orders` |
| `kalshi_pbot/reconcile.py` | Startup GET positions + orders; fail-closed ready gate |
| `kalshi_pbot/portfolio.py` | Fills, paired PnL, one-sided notional, post-close recycle |
| `kalshi_pbot/runner.py` | Discover → decide → risk → match / execute |
| `kalshi_pbot/cli.py` | `run`, `hud`, `discover`, `status`, `replay`, `flatten` |
| `kalshi_pbot/__main__.py` | `python -m kalshi_pbot` (same Typer `app` as `kalshi-pbot`) |
| `kalshi_pbot/series.py` | 15m+ series gate (rejects 5-minute markets) |
| `kalshi_pbot/hud_state.py` / `hud_server.py` | Snapshot + FastAPI/WebSocket desk feed |
| `hud/` | Vite + React Bloomberg HUD |

Orders (demo-submit only) use the current **V2 portfolio events API**
(`POST /portfolio/events/orders`) with `client_order_id` idempotency,
`post_only`, and `self_trade_prevention_type`. Prices are YES-leg dollar
strings (`bid` = buy YES, `ask` = sell YES ≡ buy NO at `1 − price`).

## Setup

Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

### Credentials

Public **prod** market data (events, books) needs **no key**.

Authenticated WS (demo or read-only prod) and `--demo-submit` need a **demo**
account at [https://demo.kalshi.co/](https://demo.kalshi.co/):

1. Account & security → API Keys → Create Key.
2. Store the key id and the downloaded `.key` file **outside git**.
3. Read-only prod WS additionally requires `KALSHI_ALLOW_PROD_WS=1`.

```bash
# .env
KALSHI_API_KEY_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
KALSHI_PRIVATE_KEY_PATH=/absolute/path/to/kalshi-demo.key
KALSHI_ENV=demo
KALSHI_BANKROLL=500
KALSHI_DRY_RUN=true
KALSHI_PAPER_TAPE=true
KALSHI_LATENCY_MS=150
KALSHI_SERIES=KXBTC15M
```

Never commit `.env`, `*.key`, or `*.pem`.

## Commands

Discover open windows on **public prod REST** (no key):

```bash
kalshi-pbot status
kalshi-pbot discover
kalshi-pbot run --dry-run --latency 150
```

Fully offline mock (synthetic 15m crypto windows + paper matcher):

```bash
kalshi-pbot discover --mock
kalshi-pbot run --mock
```

Replay a paper tape (expectancy hook; no orders):

```bash
kalshi-pbot replay data/tape.jsonl
```

Place and cancel **demo** orders (explicit flag, still demo hosts only):

```bash
kalshi-pbot run --demo-submit
kalshi-pbot flatten --demo-submit
```

`--demo-submit` turns off paper-tape and is the only path that may POST
`/portfolio/events/orders`. It will not send to production.

## HUD (paper desk)

Bloomberg-terminal aesthetic over the paper sampler. No order POSTs.

```bash
# Live public 15m books (no API key) + paper matcher + HUD
kalshi-pbot hud

# Offline synthetic windows
kalshi-pbot hud --mock

# Same thing from the runner
kalshi-pbot run --dry-run --hud

# Windows / any env where the kalshi-pbot console script is not on PATH
python -m kalshi_pbot hud --mock
```

On Windows, `start-hud.bat` cds to the repo, opens the desk in a browser after ~4s, and runs `python -m kalshi_pbot hud` once.

`watch-hud.bat` is process survival only: every 15s it hits `/api/health` on `:8080`, and if the desk is not healthy it restarts `python -m kalshi_pbot hud`, appending each restart to `data/hud-watchdog.log`. It does **not** change Risk Desk caps. It does **not** auto-clear the kill latch — daily-loss / manual kills persist in `data/kill-latch.json` and are restored on startup.

## Restart safety / reconcile

A process restart (watchdog, crash, `Ctrl+C`) does **not** reconstruct in-memory paper state by itself. What survives on disk today:

* `data/windows.json` — discovered 15m windows
* `data/tape.jsonl` — append-only research tape
* `data/kill-latch.json` — same-day daily-loss / manual kill only

What used to live only in RAM (and still does, until reconcile runs): paper positions, unpaired inventory, resting quotes, session PnL. **Before this feature, a paper restart lost that book.** After this feature:

1. `ready_to_trade=false` until reconcile completes. The HUD shows **SYNCING** vs **READY**. A failed fetch is **NOT READY · HARD HOLD** (never a silent empty book). Flatten/cancel of known inventory is allowed; **all new risk is refused**.
2. With credentials, the bot fetches **GET `/portfolio/positions`** and **GET `/portfolio/orders?status=resting`** before any new quotes and rebuilds `PortfolioSnapshot` (open, onesided, windows, daily PnL inputs from current-market `realized_pnl` + `fees`). Option B / v1 caps and Dig6 unpaired-age / soft abort still apply on that rebuilt state.
3. Auth / network / partial responses **fail closed**: stay NOT READY / hard hold, log `reconcile_failed`, retry with backoff. Do not quote blind and do not present an unverified empty book as flat.
4. Paper-tape / mock without exchange inventory: best-effort rebuild from *today's* tape + windows, labeled **PAPER LOCAL**. An empty exchange snapshot plus that local restore may mark ready. Pre-reconcile paper restarts still lose any in-memory fills that never hit the tape.
5. If the exchange returns positions or resting orders, the label is **EXCHANGE SYNC** and the tape is not mixed in (would double-count).
6. Cancel-orphan policy is **off by default**. Unexpected resting orders on watched series are adopted into the local book and logged. `KALSHI_CANCEL_ORPHANS=true` may cancel them only on `--demo-submit` against demo hosts. Production never auto-cancels, even with the flag.
7. Reconcile does **not** auto-clear the persisted daily-loss / manual kill latch (`data/kill-latch.json`). Inventory trips (open/onesided) cannot replace that latch.

Defaults remain paper / dry-run. View-only / paper never POST production orders. This does **not** enable `KALSHI_ALLOW_PRODUCTION` or live submit. Risk Desk must re-check before any micro-live.

Open **http://127.0.0.1:8080**. Frontend is Vite + React, served by the
bot's FastAPI process (`/api/snapshot`, `/ws`). For UI hot-reload:

```bash
kalshi-pbot hud --mock          # API/WS on :8080
cd hud && npm install && npm run dev   # Vite on :5173, proxies to :8080
```

The desk is wired to live bot/paper state. Risk Desk MUST-SHOW panels:

Prioritized Dig #4 fields on each live 15m card: **TTC zone** GREEN/AMBER/RED
(>180 / 180–60 / ≤60), **NO NEW RISK** banner on `last60s_lock`, TOB with
sizes + `bid_sum`/`ask_sum`, UNDERROUND vs REGIME A/TAKER-ARB (never a green
ARB chip for underround), `util_open` $/$25, `util_onesided`
$/$15, `util_windows` n/1, `day_pnl_net` vs −$10 (incl. unsettled),
`settlement_ts` + `capital_free_at = max(settlement_ts, close+60–90s)`
(**not** `expected_expiration`), `floor_strike` + CFB avg60/qtr_avg with
**chart≠settle** (live spot is not the oracle), fee drag + paper tape,
unpaired-abort / kill strobe. CFB lag and maker fee show **—** until
measured.

1. Mode badge — **PAPER** only (LIVE without approval = hard-stop visual)
2. Bankroll — config-driven (`KALSHI_BANKROLL`, default $500)
3. Clip / last fill — $10–$30 band (default $10)
4. Open notional util — $ / $25 + % bar (≥80% amber; ≥$25 red/kill)
5. Windows in flight — n / 1
6. One-sided / incomplete pair — $ / $15 + leg/ticker (abort unpaired)
7. Daily PnL + kill — day PnL vs −$10, **including unsettled until `settlement_ts`**
8. Fee drag — fees today + maker/taker split (maker $0 pending confirm)
9. Last-60s gate — time-to-close per window + `new_risk_allowed` (violation = red)
10. Settle buffer / unlock — free on `settlement_ts`; plan 60–90s (**not** `expected_expiration` +5m)
11. Kill switch — ARMED / TRIPPED + reason (loss / open / one-sided / manual) + manual kill
12. Compact limit strip — fill · open · windows · one-sided · daily loss

Colors: green inside limits / amber ~80% / red breach or kill. There is
**no** max-daily-notional gauge. Also shown when space allows: drawdown
from day high, settled-directional %, maker-first compliance, BTC/ETH mix.

## Tests

```bash
pytest
ruff check kalshi_pbot tests
```

Coverage includes Risk Desk v1, quote / pair-arb logic, the paper matcher
(join-back, through-print, ambiguous wipe, latency), events-first persist,
startup reconcile (empty / positions / resting / fail-closed), and
“no POST in paper-tape”.

## Docker

```bash
docker compose up --build
```

Compose defaults to the **desk** (`kalshi-pbot hud`) on port **8080** —
paper-tape, public prod 15m books, no order POSTs. Mount `./secrets` for
a demo key and `./data` for windows + tape.

Headless bot (no HUD):

```bash
docker compose run --rm pbot kalshi-pbot run --dry-run
```

Demo-submit (explicit, still demo hosts only):

```bash
docker compose run --rm pbot kalshi-pbot run --demo-submit
```

## Out of scope (v1)

Live/production trading, a full 7×24h expectancy runner (module + replay
only), and ML price prediction. The HUD is in scope.

## Docs used

- [API environments](https://docs.kalshi.com/getting_started/api_environments)
- [Demo environment](https://docs.kalshi.com/getting_started/demo_env)
- [Get events](https://docs.kalshi.com/api-reference/events/get-events) (`status=unopened|open`)
- [Authenticated requests](https://docs.kalshi.com/getting_started/quick_start_authenticated_requests)
- [Create Order V2](https://docs.kalshi.com/api-reference/orders/create-order-v2)
- [Orderbook responses](https://docs.kalshi.com/getting_started/orderbook_responses)
- [WebSockets](https://docs.kalshi.com/getting_started/quick_start_websockets)
- [Public trades](https://docs.kalshi.com/websockets/public-trades)
- [CF Benchmarks value](https://docs.kalshi.com/websockets/cfbenchmarks-value)
- [Get Positions](https://docs.kalshi.com/api-reference/portfolio/get-positions)
- [Get Orders](https://docs.kalshi.com/api-reference/orders/get-orders)
- [Get series](https://docs.kalshi.com/api-reference/market/get-series) (`fee_type`)
