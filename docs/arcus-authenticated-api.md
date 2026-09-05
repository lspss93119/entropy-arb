# Arcus authenticated/testnet API notes

Date checked: 2026-09-05 (Asia/Taipei)

This document records the Phase 4A API contract used by the adapter. Arcus
documentation is the authority for request construction; live observations
below are limited to safe `GET` requests. No credential, private key, API-key
registration, order, cancel, or other write request is recorded here.

## Verified endpoint contract

Official documentation:

- [Authentication](https://docs.arcus.xyz/api-reference/authentication.md)
- [REST trading/testnet guide](https://docs.arcus.xyz/guides/rest-trading.md)
- [Get account](https://docs.arcus.xyz/api-reference/public/get-account.md)
- [Get positions](https://docs.arcus.xyz/api-reference/public/get-positions.md)
- [Get open orders](https://docs.arcus.xyz/api-reference/public/get-open-orders.md)
- [Get fills](https://docs.arcus.xyz/api-reference/public/get-fills.md)
- [Get account stats](https://docs.arcus.xyz/api-reference/public/get-account-stats.md)
- [Get current rate-limit usage](https://docs.arcus.xyz/api-reference/public/get-current-rate-limit-usage.md)
- [Get fee tier table](https://docs.arcus.xyz/api-reference/public/get-fee-tier-table.md)

| Endpoint | Method/parameters | Contract status |
| --- | --- | --- |
| `/v1/account` | `GET`, `address`, optional `accountIndex` | Verified in official spec; safe public GET observed |
| `/v1/positions` | `GET`, `address`, optional `accountIndex` and `market` | Verified in official spec; safe public GET observed |
| `/v1/openOrders` | `GET`, `address`, optional `accountIndex`, `market`, `status`, `limit`, `from`, `to` | Verified in official spec |
| `/v1/fills` | `GET`, `address`, optional `accountIndex`, `market`, `role`, `side`, `limit`, `from`, `to` | Verified in official spec |
| `/v1/account/stats` | `GET`, `address`, `include=feeTier` | Verified in official spec; safe public GET observed |
| `/v1/rateLimit` | `GET`, `address`, optional `accountIndex` | Verified in official spec; safe public GET observed |
| `/v1/feetiers` | `GET` | Verified exchange-wide table endpoint; not used as the account-specific fee source |

Account-scoped reads are documented as not requiring a signature. When Arcus
credentials are configured, the adapter sends `X-API-Key` on these reads but
does not invent a GET signing scheme. Mutating requests use the three official
headers: `X-API-Key`, `X-Timestamp`, and `X-Signature`.

The testnet REST host is `https://api.testnet.arcus.xyz`. The production host
is `https://api.arcus.xyz`; authenticated writes in this project are code-level
blocked unless the selected environment is explicitly `testnet`.

## Signing contract (implementation source)

- API keys are Ed25519 public keys encoded as 64 lowercase hexadecimal
  characters. The private API signing key is loaded from the environment and
  is never placed in YAML, fixtures, logs, or source control.
- `X-Timestamp` is Unix nanoseconds and must be within the server drift window.
- Scheme 1 signs the compact, lexicographically key-sorted JSON ordersign
  payload directly. It is used by `placeOrder`, `cancelOrder`, and
  `modifyOrder`; `ct` equals `X-Timestamp` and the `ad` field is lowercased.
- Scheme 2 signs `timestamp + action + canonical_json(body)` with no
  delimiters. It is used by `cancelAllOrders`, `setLeverage`, and WebSocket
  `authenticate`. The two schemes are kept separate in
  `entropy_arb/arcus_signing.py`.

For orders, the official contract uses integer ticks/quantums in the signed
payload. `goodTilTime` is a user-facing epoch timestamp in microseconds, while
the signed `g` value is nanoseconds (`goodTilTime * 1000`). The current official
testnet changelog requires `goodTilTime` on every order, including IOC, and at
least one month in the future. Phase 4B must re-check the validator before any
testnet write.

## Observed response shapes

The safe testnet probes used the all-zero EVM address, which contains no real
account credential or user data:

- `GET /v1/account?address=0x0000000000000000000000000000000000000000` returned
  HTTP `404` with `{"error":"this account has no activity yet"}`.
- `GET /v1/positions?...` returned `{"positions":{}}`.
- `GET /v1/openOrders?...` returned `{"orders":[]}`.
- `GET /v1/fills?...&limit=1` returned `{"fills":[]}`.
- `GET /v1/account/stats?...&include=feeTier` returned
  `{"address":"0x000...000","tradingFeeTier":{"level":0,"makerFeePpm":150,"takerFeePpm":450}}`.
  The observed testnet account-specific values are 1.5 bps maker and 4.5 bps
  taker (`ppm / 100`). They are not copied into the YAML default: the adapter
  uses account API fees only when the Arcus config explicitly selects
  `fee_source: account_api`; otherwise the configured `taker_fee_bps` remains
  authoritative.
- `GET /v1/rateLimit?...` returned
  `{"address":"0x000...000","accountIndex":0,"order":{"used":0,"cap":20000,"nextAvailableMs":0},"cancel":{"used":0,"cap":40000,"nextAvailableMs":0}}`.
  The observed pool fields are `used`, `cap`, and `nextAvailableMs`; no reset
  timestamp or remaining-token field was returned.

The documented account object contains `accountIndex`, `address`,
`netQuoteBalance`, `equity`, `freeCollateral`, `netDeposits`,
`pendingDeposits`, `pendingWithdrawals`, `positions`, and `sequenceNumber`.
The adapter maps `equity` and `freeCollateral` to the existing `(equity,
free)` engine contract. It does not substitute `netQuoteBalance` or a USD
notional for equity/base position.

The documented positions response is an object under `positions`, keyed by
stringified `marketId`. Each position contains `marketId`,
`marketDisplayName`, `side` (`LONG`/`SHORT`), and signed base-asset `size`.
The adapter normalizes `LONG` to positive absolute base size, `SHORT` to
negative absolute base size, and an explicit flat/zero position to zero.

Open orders are returned under `orders`; relevant documented fields include
`orderId`, optional `clientId`, `marketId`, `marketDisplayName`, `side`,
`status`, `price`, `originalSize`, `filledSize`, `remainingSize`, optional
`avgFillPrice`, `timeInForce`, `goodTilTime`, and optional `reduceOnly`.
Fills are returned under `fills`; relevant fields include `tradeId`, `orderId`,
`marketId`, `marketDisplayName`, `clientId` when present, `side`, `size`,
`price`, `fee`, `role`, and `createdAt`.

All user-facing order/fill time fields and the `from`/`to` history bounds are
epoch microseconds. The request timestamp used by authentication and ordersign
is epoch nanoseconds.

## Verification limits and Phase 4A status

The local `.env` was inspected by variable name only and currently contains no
`ARCUS_API_KEY`, `ARCUS_API_PRIVATE_KEY`, `ARCUS_ACCOUNT_ADDRESS`, or
`ARCUS_ACCOUNT_INDEX`. Therefore no authenticated testnet account, position,
open-order, fill, fee, or rate-limit read can honestly be claimed yet. The
fixture tests are sanitized/offline contract tests; the authenticated smoke
gate remains blocked until the user supplies an already-registered testnet API
key/private-key pair and account address/index through the local environment.

