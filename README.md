# kalshi-demo-bot

Paper-trading bot for Kalshi **demo** 15-minute crypto Up/Down markets
(`KXBTC15M`, `KXETH15M`, and other `KX*15M` series when they are open).

It is a short-window **liquidity provider / statistical scalper**, not a
directional crypto book. Default mode is **maker-first market making** with
optional **YES+NO pair arb** when combined cost still leaves edge after fees.

This repository talks to the Kalshi **demo/paper** Trade API only
(`external-api.demo.kalshi.co`). Production hosts are refused unless you set
both `KALSHI_ENV=production` and `KALSHI_ALLOW_PRODUCTION=1`.

## What it does

1. Discovers open windows via `GET /markets?series_ticker=KXBTC15M&status=open`
   (and ETH). Rolls over on `open_time` / `close_time`.
2. Rebuilds the Kalshi **bids-only** book. Implied ask = `1 − opposite bid`.
3. Evaluates maker quotes and maker–maker pair arb.
4. Pushes every intent through **Risk Desk v1** before any order is logged or sent.
5. **Dry-run by default.** Demo order submit is opt-in (`--demo-submit`).

Settlement oracle (for context, not a trading signal): CF Benchmarks **BRTI**
(BTC) / **ETHUSDRTI** (ETH) — 60s open average vs 60s close average; ties
resolve Yes. The last 60 seconds of each window are treated as toxic.

## Risk Desk v1 (locked, $1000 paper bankroll)

| Limit | At $1000 | Rescale |
| --- | --- | --- |
| Per fill / clip | $10–$30 (default $20) | absolute band |
| Max open notional | $50 | 5% of bankroll |
| Max concurrent 15m windows | 2 | config |
| Daily loss kill (realized + fees + unsettled MTM) | $20 | 2% of bankroll |
| Max incomplete / one-sided inventory | $30 | 3% of bankroll |
| Last 60s before `close_time` | no new risk; cancel / flatten only | config |
| Kill switch | cancel resting, block entries until process restart | — |

Change `KALSHI_BANKROLL` and the percentage limits move with it.

## Strategy notes

**Maker-first (default).** Resting limit bids, `post_only` when the V2 events
API accepts it, STP `taker_at_cross`. Quote **one side** unless
`KALSHI_QUOTE_MODE=two_sided`. Completing an incomplete pair is always
preferred over opening a new one-sided clip.

Maker fee on these series is believed **$0** (`fee_type=quadratic`). The bot
still computes and logs fee drag, and will use the series
`fee_type` / `fee_multiplier` from `GET /series/{ticker}` if Kalshi reports
maker fees.

**Pair arb.** Buy YES and buy NO only when `Py + Pn + fees + min_edge < 1`.

- **Maker–maker** is the realistic path (`Py + Pn < 1` after $0 maker fees).
- **Taker–taker** is off by default. Quadratic taker fees peak near
  `0.07 × 0.5 × 0.5 = $0.0175` per contract (~3.5% of $1 notional for both
  legs). You need `Py + Pn ≲ 0.965` after fees — usually not available,
  because taking both implied asks costs `2 − (yes_bid + no_bid)`.

**Capital velocity.** Tiny clips, recycle after settlement, keep open interest
low. Unpaired inventory above the one-sided cap is aborted (flatten / do not
add). Last 60s: cancel quotes; do not complete pairs; flatten unpaired if a
bid exists.

## Architecture

```
CLI (kalshi-pbot) ── runner.PaperBot
                       ├── MarketUniverse + OrderBookStore   (market_data)
                       ├── KalshiRestClient / WebSocket      (kalshi_client)
                       ├── PairArbStrategy + MakerStrategy   (strategy)
                       ├── RiskEngine                        (risk_engine)
                       ├── ExecutionEngine                   (execution)
                       └── Portfolio + metrics
```

| Module | Role |
| --- | --- |
| `kalshi_pbot/config.py` | Env + Risk Desk limits; demo URLs |
| `kalshi_pbot/kalshi_client.py` | REST V2 + WS auth (RSA-PSS); mock source |
| `kalshi_pbot/market_data.py` | Series discovery, rollover, book snapshot/delta |
| `kalshi_pbot/strategy/maker.py` | One-sided / controlled two-sided post-only quotes |
| `kalshi_pbot/strategy/pair_arb.py` | Maker–maker and optional taker–taker pairing |
| `kalshi_pbot/fees.py` | Quadratic taker / maker fee + pair edge |
| `kalshi_pbot/risk_engine.py` | Hard gates (last-60s, daily kill, caps) |
| `kalshi_pbot/execution.py` | Dry-run ledger or `POST /portfolio/events/orders` |
| `kalshi_pbot/portfolio.py` | Fills, paired PnL, one-sided notional |
| `kalshi_pbot/runner.py` | Discover → decide → risk → execute loop |
| `kalshi_pbot/cli.py` | `run`, `discover`, `status`, `flatten` |

Orders use the current **V2 portfolio events API**
(`POST /portfolio/events/orders`) with `client_order_id` idempotency,
`post_only`, and `self_trade_prevention_type`. Prices are YES-leg dollar
strings (`bid` = buy YES, `ask` = sell YES ≡ buy NO at `1 − price`).

WebSocket channels: `orderbook_delta`, `ticker`, `market_lifecycle_v2`,
`fill`, `market_positions`, `user_orders`. CF Benchmarks `cfbenchmarks_value`
is left as a later optional feed.

## Setup

Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

### Demo credentials

1. Create a **demo** account at [https://demo.kalshi.co/](https://demo.kalshi.co/).
   Demo keys do not work on production and the reverse is also true.
2. Account & security → API Keys → Create Key.
3. Store the key id and the downloaded `.key` file **outside git**.

```bash
# .env
KALSHI_API_KEY_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
KALSHI_PRIVATE_KEY_PATH=/absolute/path/to/kalshi-demo.key
KALSHI_ENV=demo
KALSHI_BANKROLL=1000
KALSHI_DRY_RUN=true
KALSHI_SERIES=KXBTC15M,KXETH15M
```

Never commit `.env`, `*.key`, or `*.pem`.

## Commands

Dry-run against **live demo market data** (needs credentials for WS; REST
public market data works without them — without keys the bot uses a mock
book):

```bash
kalshi-pbot status
kalshi-pbot discover
kalshi-pbot run --dry-run
```

Fully offline mock (synthetic `KXBTC15M` / `KXETH15M` windows):

```bash
kalshi-pbot discover --mock
kalshi-pbot run --mock
```

Place and cancel **demo** orders (explicit flag, still demo hosts only):

```bash
kalshi-pbot run --demo-submit
kalshi-pbot flatten --demo-submit
```

`--demo-submit` will not send to production.

## Tests

```bash
pytest
```

Coverage is focused on Risk Desk v1 (last-60s gate, daily kill, one-sided cap,
window / notional / clip limits) and quote / pair-arb decision logic.

## Docker

```bash
docker compose up --build
```

Compose defaults to `kalshi-pbot run --dry-run`. Mount a `./secrets` directory
for the demo private key. To submit demo orders:

```bash
docker compose run --rm pbot kalshi-pbot run --demo-submit
```

## Out of scope (v1)

Live/production trading, ML price prediction, and a full UI dashboard.

## Docs used

- [API environments](https://docs.kalshi.com/getting_started/api_environments)
- [Demo environment](https://docs.kalshi.com/getting_started/demo_env)
- [Authenticated requests](https://docs.kalshi.com/getting_started/quick_start_authenticated_requests)
- [Create order](https://docs.kalshi.com/getting_started/quick_start_create_order)
- [Create Order V2](https://docs.kalshi.com/api-reference/orders/create-order-v2)
- [Orderbook responses](https://docs.kalshi.com/getting_started/orderbook_responses)
- [WebSockets](https://docs.kalshi.com/getting_started/quick_start_websockets)
- [Get markets](https://docs.kalshi.com/api-reference/market/get-markets) (`series_ticker`)
- [Get series](https://docs.kalshi.com/api-reference/market/get-series) (`fee_type`)
