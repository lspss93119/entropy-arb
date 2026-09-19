# entropy-arb

**[中文文档 / Chinese documentation → README.zh-CN.md](README.zh-CN.md)**

Open-source two-venue perp arbitrage bot. One leg is always **Entropy**
(the `io` builder dex on Hyperliquid); the other leg — the hedge — is one of:

| `--hedge` | venue | quote | taker fee | protocol |
|---|---|---|---|---|
| `lighter` | Lighter mainnet | USDC | 0 bps | zkLighter ws (diff books, async settle) |
| `lighter-rh` | Lighter Robinhood chain | **USDG** | 0 bps | zkLighter ws |
| `tradexyz` | Hyperliquid trade.xyz dex | USDC | ~1 bps | HL l2Book, sync IOC settle |

> **Referral links** — signing up through these supports this project:
> - Entropy — Tier 4 referral, 100% rebates: <https://entropy.io/?r=yourquantguy>
> - Lighter Robinhood chain: <https://robinhoodchain.lighter.xyz/?referral=QUANT>
> - trade.xyz (Hyperliquid): <https://app.hyperliquid.xyz/join/QUANTGUY>

When the same symbol trades rich on one venue and cheap on the other, the bot
simultaneously sells the rich book and buys the cheap book with taker orders,
carrying a delta-neutral position until the premium reverts and the opposite
crossing unwinds it. Every price it acts on is the **actual order book of the
exchange that will fill the order** — Hyperliquid books come from the official
websocket (`wss://api.hyperliquid.xyz/ws`), Lighter books from Lighter's
official websocket.

While it runs — even with no credentials and no strategy — it records both
books to **1-minute CSV bars**, and the bundled analyzer turns that data into
the three numbers that define the whole strategy.

## The signal

The band is three numbers in `config.yaml`, derived by you from recorded
data:

```
premium_bps = (Entropy price / hedge price − 1) × 10 000

                          ┌──────────────  SELL entropy + BUY hedge
midline + upper  ───────────────────────────────────────────────────
                                       ▲
midline          ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┼ ─ ─   the premium's usual level
                                       ▼
midline − lower  ───────────────────────────────────────────────────
                          └──────────────  BUY entropy + SELL hedge
```

- `midline_bps` — where the premium normally sits. Cross-venue premiums are
  rarely centered at zero (different oracles, different quote assets, listing
  premia), so a zero-centered band would fire one direction only, cap out and
  never unwind. Measure where the premium actually sits and type it in.
- `upper_bps` / `lower_bps` — the entry bands on each side of the midline.

Both hurdles are applied to **executable** prices (entropy bid vs hedge ask,
and vice versa) and are **net of both venues' taker fees** — the engine adds
fees on top before a slice qualifies. A full round trip therefore nets
**≥ upper + lower bps after fees by construction**.

One consequence worth understanding: with `midline_bps: 5`, the buy-entropy
hurdle is `lower − midline`, which can be **negative**. That is intentional —
if entropy is persistently 5 bps rich, buying it at a 0 bps premium is 5 bps
cheap versus its own equilibrium, and that trade is the profitable unwind of
an earlier sell at `midline + upper`. It also means a **wrong midline loses
money**: if you type `midline_bps: 5` while the true premium sits at 0, the
bot happily buys entropy at fair value all day. Measure first, then trade —
that is what the recorder and analyzer are for.

### Optional rolling-window strategy

The fixed-threshold strategy remains the default. To explicitly select the
walk-forward strategy, set `strategy.mode: rolling` in `config.yaml` and tune
the `rolling:` block in [config.example.yaml](config.example.yaml). It uses
completed recorder minute closes from the preceding window, recalculates its
snapshot every `update_minutes`, and requires both a z-score excursion and a
minimum bps distance from the rolling mean. Current top-of-book spreads and
the normal fee-aware executable plan are still required before an entry.

Rolling mode allows only one open spread position. It exits when the z-score
returns inside `exit_z`, or after `timeout_hours`; exits are reduce-only and
may cross a temporarily negative edge to remove risk. An invalid or
insufficiently covered window blocks new entries rather than falling back to
the fixed strategy. At live startup, both reconciled venue positions must be
flat, because the direction of an inherited spread cannot be inferred safely.

The existing `thresholds:` block is still required so the same configuration
file can be switched back to `fixed`. Record-only mode remains unchanged and
never sends orders.

## Quick start

```bash
git clone https://github.com/your-quantguy/entropy-arb.git && cd entropy-arb
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # data collection needs only this

cp config.example.yaml config.yaml       # the strategy (thresholds, sizing, risk)
cp .env.example .env                     # credentials — required to trade
```

The markets are **not** in the config file — you state them explicitly on
every start: `--symbol` (traded on both venues) and `--hedge` (one of
`lighter`, `lighter-rh`, `tradexyz`; Entropy is always the
other leg). Known native aliases are accepted case-insensitively — for
example, `--symbol OAI` and `--symbol OPENAI` both normalize to canonical
`OAI`; the hedge adapter then uses its native market name.

There is **no paper mode** — the bot either collects data (`--record-only`)
or trades live. Validate with recorded data and tiny position caps, not with
simulated fills.

**1. Collect data first** (no credentials needed):

```bash
python3 main.py --record-only --symbol SNDK --hedge lighter-rh
```

Let it run for at least a few hours (a day is better — premiums have
intraday regimes). It writes a pair-specific file under `logs/record/`, for
example `logs/record/minutes-SNDK-lighter-rh.csv`.

For basic multi-market collection, repeat the flags (or use comma-separated
values). This is record-only and starts one independent recorder per
`symbol x hedge` pair:

```bash
python3 main.py --record-only --no-dashboard \
  --symbol SNDK --symbol BTC \
  --hedge lighter --hedge lighter-rh
```

The files are namespaced automatically, for example
`logs/record/minutes-SNDK-lighter.csv` and
`logs/record/minutes-BTC-lighter-rh.csv`.
Market discovery is still explicit; this first multi-market mode does not
scan for symbols automatically.

**2. Analyze and set your thresholds:**

```bash
python3 tools/analyze.py
# multi-market example:
python3 tools/analyze.py --csv logs/record/minutes-SNDK-lighter.csv
```

It prints the premium distribution, how often each candidate band would have
fired, and a ready-to-paste `thresholds:` block for `config.yaml`.

Trade execution results have a separate analyzer. It reads one namespaced
trade file at a time and reports fill quality, BBO slippage, hedge outcomes,
realized matched-edge bps, weighted slippage, and hedge fill context by
signal-distance bucket:

```bash
python3 tools/analyze_trades.py \
  --csv logs/trades/trades-SNDK-lighter-rh.csv
```

If more than one pair-specific trade file exists, pass `--csv` explicitly so
different markets and hedge venues are not mixed into one statistic.

**3. Go live** — fill in `.env`, install the signing SDKs, and start with
the smallest position caps that clear the venue minimums:

```bash
pip install -r requirements-live.txt
python3 main.py --symbol SNDK --hedge lighter-rh
```

Running without `--record-only` sends real orders immediately once both
feeds are fresh and the band is crossed.

**Dashboard.** On a terminal the bot shows a live Rich dashboard: both
books with age/spread, positions and caps, equity and session PnL, the
executable premium of each direction against its full hurdle (fees and
inventory surcharge included, ● = armed), recorder progress, the last
executions, and a tail of the log (the full log goes to `logging.file`,
default `logs/engine/engine-SNDK-lighter-rh.log`). It works in `--record-only` too. Add `--cn` to
display the dashboard in Chinese. Use `--no-dashboard` for plain console
logs (nohup/systemd — off-terminal runs fall back automatically), or set
`logging.dashboard: false`.

## Data collection & analysis

The recorder runs automatically in every mode (`recorder.enabled: true`).
Once per second it samples both live books; once per minute it writes a row:

| column | meaning |
|---|---|
| `symbol`, `hedge` | market pair identity |
| `minute_ts`, `time_utc` | minute start (epoch seconds, ISO UTC) |
| `entropy_bid/ask`, `hedge_bid/ask` | last fresh top-of-book of the minute |
| `entropy_bid/ask_qty`, `hedge_bid/ask_qty` | last fresh top-of-book sizes |
| `premium_open/high/low/close/mean/std_bps` | mid-to-mid premium of Entropy over the hedge |
| `sell_edge_mean/max_bps` | executable premium for SELL entropy (entropy bid / hedge ask − 1) |
| `buy_edge_mean/max_bps` | executable premium for BUY entropy (hedge bid / entropy ask − 1) |
| `entropy_update_count`, `hedge_update_count` | number of book updates observed during the minute |
| `*_gap_p50/p95_ms` | p50/p95 inter-update gap for each feed |
| `entropy_age_p95_ms` | p95 local receipt age versus Hyperliquid's book server timestamp |
| `samples` | how many of the ~60 seconds both books were fresh |

Recorded edges are pre-fee; the analyzer subtracts `--fees-bps` (pass the
**sum** of both venues' taker fees — default 0.0 for the zero-fee venues,
~1.0 with a `tradexyz` hedge) before counting firings, so its table and
suggestions translate directly into config values. `--hours 24` restricts to
recent data; premiums drift, so re-run it regularly and update
`config.yaml`.

## Configuration

Strategy lives in `config.yaml` (validated — unknown keys are startup
errors), credentials in `.env`, and the markets on the command line
(`--symbol`, `--hedge`). Full commented reference:
[config.example.yaml](config.example.yaml). The essentials:

| key | meaning | default |
|---|---|---|
| `strategy.mode` | signal mode: `fixed` or `rolling` | `fixed` |
| `thresholds.midline_bps` | premium center (measure it!) | — |
| `thresholds.upper_bps` / `lower_bps` | entry bands (> 0) | — |
| `rolling.*` | walk-forward window, z-score, spread, coverage and timeout gates | see file |
| `entropy.dex` | Entropy's dex name on Hyperliquid | `io` |
| `*.taker_fee_bps` | per-venue taker fee | 0.0 (tradexyz hedge: 1.0) |
| `*.max_position_usd` | per-venue position cap | 1000 |
| `*.max_orders_per_min` | per-venue send budget (sliding 60 s) | 120; lighter hedges 30 |
| `sizing.take_fraction` | fraction of crossable depth taken | 0.5 |
| `sizing.max_order_notional_usd` | per-slice cap | 500 |
| `inventory.scale_bps` / `floor_frac` | inventory ladder (extra bps past `floor_frac` of the cap) | 10 / 0.5 |
| `execution.premium_persist_sec` | edge must persist before firing | 0.3 |
| `execution.*` | slippage bounds, timeouts, reconcile cadence… | see file |
| `recorder.*` | minute-data recorder | on, `logs/record/minutes-SYMBOL-HEDGE.csv` |
| `logging.trades_csv` | per-pair execution summary | `logs/trades/trades-SYMBOL-HEDGE.csv` |
| `logging.dashboard` / `logging.file` | Rich dashboard on a tty; log file while it runs | on, `logs/engine/engine-SYMBOL-HEDGE.log` |

Each process start also appends the effective strategy parameters to
`logs/engine/runs-SYMBOL-HEDGE.csv`. The trade CSV includes the matching
`run_id`, so executions can be compared across parameter changes without
duplicating the full configuration on every trade row. Live trade rows also
include each leg's settle duration, the first leg to settle, quote/feed age at
the signal, and venue-native cancellation or rejection reasons. Run rows record
the host region, feed/order transport mode, and `code_version`; set
`ENTROPY_ARB_CODE_VERSION` when deploying a revision outside a Git checkout.

## Credentials (`.env`, live only)

- **Entropy / tradexyz (Hyperliquid)** — create an API ("agent") wallet at
  <https://app.hyperliquid.xyz/API>. `HL_PRIVATE_KEY` is the **agent** key,
  `HL_ACCOUNT_ADDRESS` your main account address. With `--hedge tradexyz`
  both legs share this account by default (one nonce sequence is handled
  internally); set `HL_PRIVATE_KEY_XYZ` / `HL_ACCOUNT_ADDRESS_XYZ`
  to split them. Fund the dex-specific clearinghouses you trade.
- **Lighter** — credentials are namespaced by deployment:
  - `--hedge lighter` reads `LIGHTER_MAINNET_ACCOUNT_INDEX`,
    `LIGHTER_MAINNET_API_KEY_INDEX`, and `LIGHTER_MAINNET_API_PRIVATE_KEY`.
  - `--hedge lighter-rh` reads `LIGHTER_RH_ACCOUNT_INDEX`,
    `LIGHTER_RH_API_KEY_INDEX`, and `LIGHTER_RH_API_PRIVATE_KEY`.
  Both blocks may be filled in the same `.env`, but each account index, API-key
  index, and private key must belong to the matching deployment. See
  [lighter-python](https://github.com/elliottech/lighter-python).

## How execution works

- Both legs are **taker** orders sent concurrently: Lighter market orders
  with average-price protection settling on the authenticated account
  websocket; Hyperliquid IOC limits settling synchronously (with
  orderStatus polling for unknown outcomes).
- A **persistence gate** (`premium_persist_sec`) arms each direction and only
  fires if the edge survives — one-tick phantoms are filtered.
- **Rolling mode** (when explicitly selected): uses a strict pre-block
  rolling snapshot, permits one spread position, and closes on mean reversion
  or timeout with reduce-only primary legs.
- **Inventory ladder**: past `floor_frac` of a venue's cap, adding to the
  position requires linearly more edge, up to `scale_bps` extra at the cap.
- **Net-delta hedge**: if legs fill unevenly, the imbalance is immediately
  reduced (reduce-only, price-protected), and positions are reconciled
  against the chain every `reconcile_sec`.
- **Failure containment**: a rate-limited venue pauses briefly; an
  unreachable venue (e.g. exchange maintenance) pauses trading and is probed
  every `venue_probe_sec` until it recovers; `max_consecutive_errors`
  execution pathologies halt the engine entirely.
- **Live-only**: there is no simulated-fill mode. `--record-only` is the
  risk-free way to run it; anything else trades real money.

## Layout

```
main.py                  entry point (--record-only, or live by default)
entropy_arb/config.py    YAML + .env contract, validation
entropy_arb/book.py      order books + fee-aware crossing/sizing math
entropy_arb/feeds.py     official HL ws + zkLighter ws book feeds
entropy_arb/venue_hl.py  Hyperliquid dex adapter (Entropy, tradexyz)
entropy_arb/venue_lighter.py  zkLighter adapter (mainnet, Robinhood chain)
entropy_arb/engine.py    the two-venue strategy loop
entropy_arb/dashboard.py Rich terminal dashboard
entropy_arb/recorder.py  1-minute orderbook bars
tools/analyze.py         logs/record/minutes-SYMBOL-HEDGE.csv -> suggested thresholds
tools/analyze_trades.py  logs/trades/trades-SYMBOL-HEDGE.csv -> execution quality
tests/                   python3 -m pytest tests/
```

## Known risks

- **A wrong midline is a losing strategy.** The premium center drifts;
  re-measure regularly and keep `config.yaml` current.
- **USDG basis** (`lighter-rh`): the hedge quotes in USDG. Part of any
  persistent premium is the stablecoin itself; your midline absorbs the
  level, but a USDG *move* is real PnL.
- **Funding**: two venues, two independent funding rates; carry is not
  modeled. Position caps bound it — keep them modest.
- **Thin books**: Entropy depth can be tiny; `take_fraction` and notional
  caps keep clips small, but slippage on the hedge leg after a partial fill
  is real.
- **Market hours**: for equity perps (e.g. SNDK), off-hours oracle regimes
  differ per venue; consider wider bands or not trading them.
- **One-leg risk**: a leg can fail after the other filled. The bot hedges
  and reconciles automatically, but you should still watch it.

Use at your own risk. This is trading software operating with real money;
nothing here is investment advice. Start with tiny position caps.

## License

[MIT](LICENSE)
