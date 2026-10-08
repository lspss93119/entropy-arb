# Range Inventory performance report design

Status: approved for implementation on `feature/range-inventory-live`; implementation is in scope, but deployment and live-process actions are not.

## Boundaries

Add a standalone, read-only report generator at `tools/range_performance_report.py` and deterministic tests at `tests/test_range_performance_report.py` on `feature/range-inventory-live`. Do not modify strategy, execution, persisted state, configuration, HALT, credentials, or any live process. The report may write only its three requested report artifacts under `logs/performance/`. It never submits, cancels, or modifies venue orders.

The live Mac production checkout is a data source only. Source changes and tests stay in this isolated worktree; the live bot is not restarted, halted, resumed, or deployed.

## User-facing command and outputs

```bash
python tools/range_performance_report.py --symbol ANTH --hedge lighter-rh
python tools/range_performance_report.py --symbol ANTH --hedge lighter-rh --offline
```

`--root` defaults to the current repository root and supports reading a
separate production-artifact directory without installing/copying the tool
into that checkout. `--config` and `--env-file` identify existing read-only
runtime inputs in default mode; the tool reads only public market identity,
freshness settings, and `HL_ACCOUNT_ADDRESS`, never `HL_PRIVATE_KEY`. Path
overrides and an explicit maximum quote age support deterministic fixtures.
An explicit `--recorder` path takes precedence over runtime resolution. Without
that override, resolve the recorder path using the same production config and
runtime path-namespacing rules as `main.py`; never assume a hardcoded
`logs/record/minutes-<symbol>-<hedge>.csv` path. If the effective recorder path
cannot be resolved uniquely, fail closed. Offline mode does not open the env
file or config; therefore `--offline` requires an explicit `--recorder` path
and must not attempt a config-based recorder fallback.

Default artifact paths are pair-scoped and follow the production naming rules:

- `logs/state/range-ANTH-lighter-rh.json`
- `logs/state/halt-ANTH-lighter-rh.json`
- `logs/trades/trades-ANTH-lighter-rh.csv`
- recorder path resolved from the effective production config/runtime, unless
  explicitly overridden by `--recorder`
- the matching engine run/settlement logs when present

Allow explicit root/path overrides for reproducible tests and archived fixtures. Report outputs are:

- `logs/performance/range-performance-ANTH-lighter-rh.html`
- `logs/performance/range-performance-ANTH-lighter-rh.json`
- `logs/performance/range-cycles-ANTH-lighter-rh.csv`

Writes should be staged via temporary files and replaced atomically. A source-data parse error must fail clearly rather than emit a plausible but partial financial report.

### Consistent snapshot protocol

Each report attempt follows one bounded consistent-snapshot protocol:

1. Read and validate Range state A.
2. Capture identity and EOF/size boundaries for each append-only execution,
   settlement/trade, and recorder artifact. Read each artifact only through
   its captured boundary; detect replacement, truncation, or in-place changes
   while reading.
3. Obtain the fresh public BBO snapshot used for current executable valuation.
4. Read and validate Range state B, then recheck the append-only source
   identities and boundaries.
5. Accept the snapshot only if state A and B agree on inventory, pending
   intent, and consumed minute, and the captured source boundaries remained
   compatible throughout the attempt. Otherwise retry the whole attempt up to
   a small fixed bound. Never merge state from one attempt with artifacts or
   BBO from another.

If bounded retries cannot obtain a consistent snapshot, preserve any
independently computable historical gross diagnostics, but mark all
current-position-dependent valuation N/A with reason code
`inconsistent_snapshot`. Do not publish current unrealized PnL or a total PnL
from an unstable mixture of state, fills, recorder rows, or quotes. Report the
attempt count and sanitized inconsistency reason.

## Read-only data and mode semantics

### Local artifacts

Read the Range state and HALT JSON, pair-filtered execution/trade records, settlement/fallback details, and completed-minute recorder CSV. The persisted Range state is the current strategy-state source for signed inventory, direction, target, latest signal metadata, pending intent, and cap. Do not query or infer live account positions in this report; display the state-derived Entropy/RH quantities as such, not as independently authoritative exchange positions.

Replay the settled actual paired fills locally and compare the resulting expected inventory to the persisted Range quantities. A mismatch, malformed state, or non-null pending intent is an anomaly: show it explicitly and do not silently repair or overwrite either source. Any dependent current-position valuation is N/A until the discrepancy is resolved by the operator.

In `--offline` mode, perform no network access and do not load credentials. Compute gross cashflow PnL, reconstruct lots/cycles, and expose fallback cashflows from local actual-fill records. Do not estimate fees from configured rates. If any required actual fee is unavailable, show `fees = N/A`, `fee_net_realized_pnl = N/A`, and `Trading PnL before funding = N/A`. No stale recorder quote is promoted to a current mark; without a fresh live quote, current unrealized and total are N/A.

### Default read-only venue enrichment

Use only public/read-only market data plus the public HL account address required to query the configured production account. Do not read or display a private key. Never include the HL account address, API credential, authorization header, token, or unredacted request/response in generated reports or errors.

1. Query the official Hyperliquid Info API `userFillsByTime` for the configured production account and the report’s required time range, using the configured Entropy HIP-3 DEX/coin identity. Raw fills must retain `fee`, `feeToken`, `time`, `coin`, `side`, `sz`, `px`, `oid`, `hash`, and `tid`; do not use configured fee rates or aggregate away identity. The official fill `fee` is the authority for that fill and is counted once; do not add configured or separately inferred builder fees. The official Info API fill example documents `fee` as the total fee inclusive of `builderFee`; count the reported `fee` once and never add the separate `builderFee` field again. Only fee tokens that are directly denominated in the report’s USD accounting currency are usable without a separate authoritative conversion source.
2. Respect the official per-response and historical-availability limits. Partition dense ranges deterministically and deduplicate by venue fill identity. If any requested interval cannot be proven completely covered (including a still-capped minimum interval or history-limit ambiguity), fee status is incomplete.
3. Match each local Entropy actual-fill leg independently. Local primary `buy_fill`/`sell_fill` and any actual fallback fill form separate expected legs, identified by venue, side, size, actual average price, and the local signal-to-settlement time window. Match against raw official fills using coin, side, size, price, and timestamp, with exchange order/transaction identity when available. Group partial official fills only by their common order identity; never combine unrelated fills to force a match. Use deterministic precision tolerances derived from the local CSV’s documented numeric formatting and a fixed, documented clock-skew bound.
4. Require one global, one-to-one unique assignment between every expected Entropy fill and official fill group. Missing candidates, multiple valid assignments, identity collisions, conflicting duplicate venue fills, unsupported fee currency, malformed fee, or incomplete venue coverage make the whole fee result incomplete. Incomplete results carry explicit reason codes and do not publish a partial `fees_usd` or fee-net PnL.
5. Lighter-RH fees may be recorded as zero only when current official RH market metadata explicitly states `taker_fee = 0` and the fee mechanism is inactive for the relevant market/account context. The local configured `taker_fee_bps` alone is not actual-fee evidence. Otherwise RH fee coverage is incomplete, `fees_usd` and fee-net PnL are null/N/A, and the report must explain why. Funding remains excluded in all modes.

For current marks, open public WebSocket book feeds for the configured Hyperliquid Entropy coin and configured RH market. Resolve the current market identifiers from current venue metadata; do not hardcode a previously observed market id or mainnet identity for RH. Record source, local snapshot UTC time, quote age, and exchange timestamp/age when provided. If a source does not provide an exchange timestamp, explicitly label quote age as local receipt age; do not imply it is exchange timestamp age. Both books must have valid bid and ask and meet a freshness bound sourced from an explicit CLI override or the existing execution staleness setting (with its exact value reported). If either leg is missing, stale, crossed/invalid, or cannot be resolved, current executable unrealized PnL and total PnL are N/A; never fall back to a mid, a last trade, or a stale recorder row.

Executable liquidation marks:

- Long Range inventory: close Entropy long at Entropy bid and RH short at RH ask.
- Short Range inventory: close Entropy short at Entropy ask and RH long at RH bid.

The report uses no order endpoint and no signed/authenticated API request. The only account-specific query is the official public HL fills query, which uses the already configured public account address.

## Accounting definitions

### Fills, lots, and gross PnL

Reconstruct actual cashflows from settled execution quantities and average prices; include each actual fallback hedge fill/cashflow. Long build is buy Entropy / sell RH; long release is sell Entropy / buy RH. Short build/release are the inverse. A cycle begins only on a verified flat-to-nonflat paired inventory transition and is complete only when actual settled paired inventory returns to flat with no unresolved/pending execution affecting that cycle. An unfinished or unresolved cycle remains open and is never emitted as a completed cycle row.

Gross PnL is reconstructed as actual execution cashflows plus executable liquidation cashflows for the remaining positions. Gross realized PnL is attributed to closed lots and settled fallback-repair cashflows; gross unrealized PnL is the remaining lots’ original execution cashflows plus current executable liquidation cashflows. This decomposition must sum back to gross total PnL. Do not use `cumulative_realized_capture_usd` or `fill_edge_usd` as authority. Historical curve points use each valid recorder minute’s own executable BBO and the inventory known at that completed-minute boundary; missing/invalid minute data creates a gap, never forward-fill. The current mark is separate and must come from fresh public BBO in default mode.

The HTML exposes Gross Trading PnL separately from fee-net Trading PnL before funding. `Realized` and `Unrealized` must distinguish gross from fee-net values so an unavailable fee cannot be mistaken for zero. `Trading PnL before funding` is fee-net realized plus executable fee-net unrealized, only when all required actual fees and a fresh executable mark are available. If fee coverage is incomplete, this headline is N/A even if gross values are available. If the mark is stale, unrealized and total are N/A even if realized fee data is complete.

Actual fees include all uniquely recovered Entropy fee fills and any separately authoritative RH fee amount. Do not apply strategy-config fee rates. Fee-net realized and unrealized are calculated by allocating each actual fill’s fee to its closed-lot or remaining-lot quantity; the sum reconciles to gross total less all actual fees. Fallback hedge impact is derived from its actual settled cashflows, including the actual residual primary fill being repaired; do not treat the hedge leg in isolation as PnL. `fallback_hedge_pnl_usd` is a diagnostic attribution subtotal already included in realized/total PnL and must never be added a second time. The `fees_usd`/fee-net fields are null when any relevant actual fee is unknown. Funding is always excluded and explicitly labeled.

### Canonical fill ledger and inventory cost basis

Build one canonical actual-fill ledger from settled execution artifacts before
computing lots, cycles, turnover, fees, or PnL. Each actual primary or fallback
venue fill must appear exactly once; deduplicate only when stable execution
identity proves two records describe the same fill. If identity or quantities
are ambiguous, mark dependent metrics incomplete rather than double-counting
or dropping a fill.

Reconstruct inventory with the same weighted-average-cost and
`mean_cost_per_base` semantics used by `RangeInventoryLive`: each settled
paired quantity updates the signed base inventory and mean cost; reductions
realize PnL against that carried mean cost; only an actual transition to flat
clears the open cost basis. Do not substitute FIFO lots or a different
spread-capture formula. Any fee allocation and cycle realized PnL must be
derived from this canonical ledger and this strategy-compatible cost basis.

### Current inventory and open cycle

Display current direction, state-derived signed Entropy/RH inventory, pending intent, target, and the current open cycle. Current inventory USD is calculated from state quantities times the average of the two current venue mid-quotes, only as a sizing/reference metric (never as PnL), and is labeled state-derived; if no suitable current BBO exists, show N/A. Cap comes from the persisted Range parameter fingerprint/state, not a separately edited config.

Peak inventory USD for each cycle uses the paired base inventory after each completed event multiplied by the arithmetic mean of the actual average Entropy and RH execution prices for that event. If either average price is missing, the USD peak is unavailable rather than guessed. Turnover is the sum of absolute actual notional for all fills, including fallback fills. Return on peak inventory is fee-net cycle PnL divided by peak inventory, and is null when fees or denominator are unavailable. Current cycle return uses the same fee-net PnL and is N/A unless fee coverage and current executable marks are complete.

### Cycle CSV

One row per completed flat→position→flat cycle, with exactly these columns:

```text
cycle_id,direction,start_utc,end_utc,duration_sec,build_count,release_count,peak_base_inventory,peak_inventory_usd,turnover_usd,gross_spread_capture_usd,fees_usd,fallback_hedge_pnl_usd,realized_pnl_before_funding_usd,return_on_peak_inventory_pct,unresolved_count,final_net_delta
```

Open cycles are shown in HTML/JSON but not represented as completed-cycle CSV rows. When a cycle has an unresolved/unknown fee or fill, dependent financial columns are blank/null, not zero.

## Output contract

### HTML

Self-contained, directly openable offline: inline CSS, SVG, and minimal embedded JavaScript only; no CDN, third-party framework, web server, or external assets. Escape all artifact/API-derived text before embedding it.

The first screen shows:

```text
Gross Trading PnL
Actual Fees
Trading PnL before funding
Realized (gross and fee-net status)
Unrealized (gross executable and fee-net status)
Fee data status and explicit reason
Market mark freshness (snapshot UTC, age, source)
Current direction/state
Current inventory USD / cap
Peak inventory USD
Current cycle return
Build count
Release/cover count
long percentile / short percentile / 4h range / gate / target
execution / fallback / unresolved counts
current net delta (state-derived, not exchange-authoritative)
HALT
```

Include two inline charts: `Trading PnL / equity curve` and `Range inventory USD over time`. Every missing-data interval is a visible gap. When fees are incomplete, label the plotted curve gross; when complete, plot fee-net PnL and identify it as such. Include an explicit `Current open cycle` section.

### JSON

Store all HTML headline metrics in valid JSON (no NaN/Infinity), plus data status, source timestamps/ages, fee match counts and sanitized reasons. Preserve the requested fields and distinguish gross from fee-net values, e.g. `gross_realized_pnl_usd`, `fee_net_realized_pnl_usd`, `gross_unrealized_pnl_usd`, `unrealized_pnl_usd`, `total_pnl_before_funding_usd`, `fees_usd`, `fee_status`, `market_mark`, `current_inventory_usd`, `current_target_usd`, cycle counts, fallback/unresolved counts, and `funding_included: false`. `fees_usd`, fee-net values, and total are JSON null when their prerequisites are not complete.

## EC2 regression

Use the archived 28 execution rows and corresponding official Hyperliquid venue fills as a read-only regression fixture. Confirm from available non-secret provenance that the queried account is the account that produced the archive; otherwise return `NOT VERIFIABLE`. Do not hardcode or assume the expected PnL is a pass. Only report `PASS` when all 28 rows/fills uniquely match, fee coverage is complete, the cycle is complete and flat, and recomputed realized trading PnL before funding is within a documented floating-point rounding tolerance of +$0.261158. If fill/fee matching cannot be proven complete and unique, report `NOT VERIFIABLE`; this is not a test failure and no fee may be invented. Include the match counts and result in JSON/HTML diagnostics.

## Validation

Tests must be deterministic and never contact live venues. Cover:

- offline mode makes zero network calls, does not load credentials, produces gross cashflow/lots/cycles, and reports missing actual fees/fee-net as N/A;
- actual-fee matching: exact unique match, many raw partial fills under one order, missing, ambiguous/non-unique assignment, duplicate identity conflict, bad currency/fee, API errors, response cap/history-coverage uncertainty, and no builder-fee double count;
- fallback cashflow and fallback hedge PnL inclusion;
- long and short executable liquidation BBO formulas, exact snapshot/source metadata, stale/missing/crossed book => unrealized/total N/A, and no mid/stale-recorder fallback;
- open-cycle state is not emitted as a completed cycle; flat completion, cycle metrics, fee nullability, recorder gaps, and inventory curve gaps;
- HTML is self-contained/escaped and JSON is standards-valid with nulls rather than NaN;
- EC2 fixture status is PASS only on complete unique matching and tolerance success, otherwise NOT VERIFIABLE.

The final run on current Mac production artifacts is read-only except for the three requested report outputs. No live restart, HALT, resume, deployment, order, cancellation, state/config write, or credential rotation is allowed.

## Official API references

- Hyperliquid Info API `userFillsByTime`, including fill `fee` and response/history limits: https://hyperliquid.gitbook.io/Hyperliquid-docs/for-developers/api/info-endpoint
- Hyperliquid public L2 book WebSocket: https://hyperliquid.gitbook.io/Hyperliquid-docs/for-developers/api/websocket/subscriptions
- Lighter Python SDK official API reference for public order-book endpoints: https://github.com/elliottech/lighter-python/blob/main/docs/OrderApi.md
