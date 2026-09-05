# Arcus public API discovery

Verified 2026-09-05 against the Arcus production origin from a credential-less
client. This is a discovery record, not a trading integration contract. No
credential, wallet address, or signing material was used.

Official references:

- [Arcus API docs](https://docs.arcus.xyz/)
- [Arcus documentation index](https://docs.arcus.xyz/llms.txt)
- [Get markets](https://docs.arcus.xyz/api-reference/public/get-markets)
- [Get BBO](https://docs.arcus.xyz/api-reference/public/get-best-bid-offer-bbo)
- [Get L2 orderbook](https://docs.arcus.xyz/api-reference/public/get-l2-orderbook-snapshot)
- [Get market funding rates](https://docs.arcus.xyz/api-reference/public/get-market-funding-rates)
- [WebSocket](https://docs.arcus.xyz/api-reference/websocket)
- [BBO WebSocket channel](https://docs.arcus.xyz/api-reference/market-data/bbo)
- [Rate limits](https://docs.arcus.xyz/api-reference/rate-limits)

## Confirmed production surface

Base URL: `https://api.arcus.xyz`

| Surface | Method and request | Observed result | Status |
| --- | --- | --- | --- |
| Root | `GET /` | `{"docs":"https://docs.arcus.xyz","status":"running","version":"production-v0.2.0"}` | verified |
| Health | `GET /health` | `{"status":"ok"}` | verified |
| Markets | `GET /v1/markets` | `{"markets":[...]}`, 64 entries at discovery time | verified |
| Single market | `GET /v1/markets?market=SNDK-USD` | One-entry `markets` response | verified |
| BBO | `GET /v1/bbo/SNDK-USD` | Real `bestBid`, `bestAsk`, sequence IDs, and timestamp | verified |
| L2 | `GET /v1/l2OrderBook/SNDK-USD?nLevels=2` | Real two-level-per-side snapshot | verified |
| Funding | `GET /v1/fundingRates?market=SNDK-USD&limit=3` | Three newest hourly funding rows | verified |
| Fee table | `GET /v1/feeTiers` | Exchange-wide fee tier table | verified |
| Server time | `GET /v1/time` | `timeNs` Unix nanoseconds | verified |
| Rate-limit usage | `GET /v1/rateLimit?address=...` | Missing address gives 400; an unlisted address gives 403 | partially verified |

The candidate `GET /v1/l2OrderbookSnapshot?market=SNDK-USD` returned HTTP 404
`{"error":"Not found"}`. It is not the production L2 path.

## Markets response

The top-level response is an object with one key, `markets`. At discovery time
all 64 returned rows had `type: "PERPETUAL"`; 58 were `ONLINE` and 6 were
`OFFLINE`. The observed union of market-row keys was:

```text
addedTimestamp, assetResolution, baseAsset, category,
currentSettlementPrice, fullAssetName, fundingRate, high24h,
initialMarginFraction, isLowerInExpansionZone, isOutsideRth,
isUpperInExpansionZone, lastTradePrice, low24h, lowerExpectedExpansionAt,
lowerTradingBound, lowerZoneEnteredAt, maintenanceMarginFraction, markPrice,
marketDisplayName, marketId, maxOrderSize, minOrderNotional, minOrderSize,
nextFundingAt, nextFundingRate, nextLowerTradingBound, nextUpperTradingBound,
offHoursInitialMarginFraction, openInterest, openInterestCapNotional,
oraclePrice, priceChange24h, pythId, quoteAsset, regularTradingHours, status,
stepSize, tickSize, tickTiers, trades24h, type, upperExpectedExpansionAt,
upperTradingBound, upperZoneEnteredAt, volume24h, volume24hNotional
```

The following is an actual sanitized `SNDK-USD` row observed from
`GET /v1/markets?market=SNDK-USD`:

```json
{
  "marketDisplayName": "SNDK-USD",
  "fullAssetName": "SanDisk",
  "marketId": 33,
  "status": "ONLINE",
  "baseAsset": "SNDK",
  "quoteAsset": "USD",
  "tickSize": "0.01",
  "stepSize": "0.0000001",
  "tickTiers": [
    {"upToPrice": "5000", "tick": "0.01"},
    {"upToPrice": "10000", "tick": "0.02"},
    {"upToPrice": "20000", "tick": "0.05"},
    {"upToPrice": "50000", "tick": "0.1"},
    {"upToPrice": "100000", "tick": "0.2"},
    {"tick": "0.5"}
  ],
  "minOrderNotional": "5",
  "minOrderSize": "0.01",
  "maxOrderSize": "100000",
  "oraclePrice": "1761.37",
  "markPrice": "1762.33",
  "lastTradePrice": "1762.16",
  "fundingRate": "0.000004814814814814",
  "nextFundingRate": "0.00000474537037037",
  "nextFundingAt": 1788609600,
  "priceChange24h": "0.1041",
  "volume24h": "1153.1",
  "volume24hNotional": "1948592.07",
  "high24h": "1781.19",
  "low24h": "1562.69",
  "trades24h": 4694,
  "openInterest": "162.2616429",
  "openInterestCapNotional": "1000000",
  "initialMarginFraction": "0.1",
  "maintenanceMarginFraction": "0.066667",
  "offHoursInitialMarginFraction": "0.15",
  "regularTradingHours": {
    "startSecondsOfDay": 14400,
    "endSecondsOfDay": 72000,
    "timezone": "America/New_York",
    "isOvernight": false
  },
  "isOutsideRth": true,
  "currentSettlementPrice": "1732.9",
  "upperTradingBound": "1819.54",
  "lowerTradingBound": "1646.26",
  "nextUpperTradingBound": "1906.19",
  "nextLowerTradingBound": "1559.61",
  "isUpperInExpansionZone": false,
  "isLowerInExpansionZone": false,
  "upperZoneEnteredAt": null,
  "upperExpectedExpansionAt": null,
  "lowerZoneEnteredAt": null,
  "lowerExpectedExpansionAt": null,
  "type": "PERPETUAL",
  "category": "EQUITIES",
  "addedTimestamp": 1778786860,
  "assetResolution": "10000000000",
  "pythId": "2858"
}
```

The API returns price, size, and funding values as decimal strings. Numeric
market IDs and counters are integers. Nullable trading-bound and session fields
were observed on both online and offline rows.

For the adapter, canonical `SNDK` is mapped by filtering the live response to
`baseAsset == "SNDK"`, `type == "PERPETUAL"`, and `status == "ONLINE"`, then
retaining the selected `marketDisplayName` and `marketId`. The adapter must
reject zero or multiple matches; it must not synthesize a `-USD` identifier.

## BBO

The verified REST request is:

```text
GET https://api.arcus.xyz/v1/bbo/SNDK-USD
```

An actual response was:

```json
{
  "bestBid": {"price": "1762.16", "size": "0.2340218"},
  "bestAsk": {"price": "1762.29", "size": "0.2822"},
  "lastSequenceId": 93593887,
  "globalSequenceId": 1856151886,
  "timestamp": 1788607484331573
}
```

`bestBid` and `bestAsk` are nullable according to the official schema. A real
unknown market request returned HTTP 404 with
`{"error":"Unknown market: NO_SUCH-USD"}`. The REST response does not repeat a
market identifier; the path is the identifier.

## Verified BBO WebSocket

The exact production connection and subscription tested were:

```text
wss://api.arcus.xyz/v1/ws
```

```json
{"type":"subscribe","channel":"bbo","id":"SNDK-USD"}
```

The connection emitted a `connected` frame, then an actual initial snapshot:

```json
{
  "channel": "bbo",
  "id": "SNDK-USD",
  "type": "subscribed",
  "contents": {
    "bestBid": {"price": "1762.92", "size": "0.285"},
    "bestAsk": {"price": "1763.37", "size": "0.2821"},
    "lastSequenceId": 93594319,
    "globalSequenceId": 1856155925,
    "timestamp": 1788607522677299
  }
}
```

It then emitted actual `channel_data` frames with the same `contents` shape;
for example, one observed frame contained `lastSequenceId: 93594320`,
`globalSequenceId: 1856155977`, and timestamp `1788607522980011`. The observed
subscription returned changing real bid/ask values rather than a synthetic
quote.

The matching unsubscribe request was:

```json
{"type":"unsubscribe","channel":"bbo","id":"SNDK-USD"}
```

and the observed response was:

```json
{"channel":"bbo","id":"SNDK-USD","type":"unsubscribed"}
```

The official documentation defines `lastSequenceId` as the per-market orderbook
sequence and `globalSequenceId` as the cross-market sequence. Both were present
in the observed BBO frames. BBO/L2 timestamps are Unix microseconds, and are
inside `contents`; there is no top-level publish timestamp.

The official WebSocket docs do not define an Arcus application-level JSON
heartbeat, and none was observed during the short BBO session. No guessed
heartbeat message will be sent. A client may use the WebSocket library's
standard control-frame keepalive; a disconnect or malformed/stale feed must
leave the book not ready until a fresh real snapshot arrives.

## L2

The verified production REST endpoint is:

```text
GET /v1/l2OrderBook/{market}?nLevels=1..100
```

An actual `SNDK-USD?nLevels=2` response was:

```json
{
  "bids": [["1762.16", "0.2340218"], ["1761.93", "0.2852"]],
  "asks": [["1762.29", "0.2822"], ["1762.53", "0.4254873"]],
  "lastSequenceId": 93593887,
  "globalSequenceId": 1856151886,
  "timestamp": 1788607484331573
}
```

L2 is verified, but the Phase 3 adapter consumes the smaller verified BBO
channel and writes only the real top-of-book into the existing `OrderBook`.
It does not invent additional depth.

## Funding discovery

The verified request is:

```text
GET /v1/fundingRates?market=SNDK-USD&limit=3
```

An actual response was:

```json
{
  "fundingRates": [
    {"marketId": 33, "marketDisplayName": "SNDK-USD", "fundingRate": "0.000004814814814814", "time": 1788606000000000},
    {"marketId": 33, "marketDisplayName": "SNDK-USD", "fundingRate": "0.000004814814814814", "time": 1788602400000000},
    {"marketId": 33, "marketDisplayName": "SNDK-USD", "fundingRate": "0.000004814814814814", "time": 1788598800000000}
  ]
}
```

Confirmed behavior:

- `market` is required and accepts the display name; the no-market request
  returned HTTP 400 `{"error":"invalid or missing market"}`.
- `limit` is supported. The official maximum is 1000; one production request
  with `limit=1000` returned exactly 1000 rows.
- Rows are newest first and expose no cursor or pagination field. The observed
  1000-row sample covered `1785009600000000` through `1788606000000000`.
- `from` and `to` are supported as inclusive Unix microsecond bounds. A
  three-hour window returned three rows.
- `time` is Unix microseconds, with observed one-hour spacing
  (`3600000000` microseconds). The current market row's `nextFundingAt` is Unix
  seconds.
- Funding was not connected to the strategy in Phase 3.

## Rate limits and fees

Normal production responses exposed `content-type`, `server: cloudflare`, CORS
headers, and dynamic Cloudflare/date headers. No `X-RateLimit-*` header was
observed on the successful markets, BBO, L2, funding, or health calls. The
official rate-limit documentation publishes the limits and weights instead:
the per-IP bucket is 1,500 weight per minute, BBO is weight 2, markets/funding
are weight 20, and L2 is weighted by requested depth. No deliberate 429 test was
performed, as requested.

`GET /v1/rateLimit` is a public endpoint but requires an `address` query
parameter. Credential-less checks produced HTTP 400 `{"error":"missing
address"}` without it, and HTTP 403 `{"error":"address not on access
whitelist"}` for both a zero address and the documentation example address.
Therefore an actual rate-pool payload was not captured without a whitelisted
account address.

`GET /v1/feeTiers` returned the exchange-wide table. The observed Base tier was
`maker_fee_ppm: 0`, `taker_fee_ppm: 225`; 225 ppm is 2.25 bps by unit
conversion. This is not asserted to be every account's effective fee, so the
adapter/config must not silently treat it as a universal account fee.

## Verification boundary

Verified: production REST markets, market filtering, BBO, L2, funding history,
health, server time, fee-tier table, BBO WebSocket connection/subscription/
initial snapshot/updates/unsubscribe, timestamp units, and sequence fields.

Not verified: a production account's effective fee tier, the rate-limit usage
payload for a whitelisted account, and any Arcus order-placement capability.
Arcus Phase 3 remains public market-data only; no signer, account mutation,
order, cancel, or leverage call belongs in this phase.
