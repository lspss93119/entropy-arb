# Venue overlap snapshot

Verified 2026-09-05 from credential-less/public market metadata. This is a
time-specific snapshot; rerun the three public metadata queries before choosing
markets for a future recorder.

Canonical matching is exact uppercase symbol matching after venue-specific
perpetual/active filters:

- Entropy: Hyperliquid `POST /info` with `{"type":"meta","dex":"io"}`;
  identifiers are the returned `io:<symbol>` names.
- Lighter RH: `GET https://api.rh.lighter.xyz/api/v1/orderBooks`, keeping
  `market_type == "perp"` and `status == "active"`; identifiers are the
  returned symbols and `market_id` values.
- Arcus: `GET https://api.arcus.xyz/v1/markets`, keeping
  `type == "PERPETUAL"` and `status == "ONLINE"`; identifiers are the
  returned `marketDisplayName` and `marketId` values.

Observed market counts after those filters were Entropy 8, Lighter RH 57, and
Arcus 58. A Lighter RH spot row such as `SNDK/USDG` was excluded; the Lighter
RH perpetual `SNDK` row remained.

## Pair and triple overlap

| Venue combination | Canonical symbols |
| --- | --- |
| Entropy + Lighter RH | `SNDK` |
| Entropy + Arcus | `NBIS`, `SNDK` |
| Lighter RH + Arcus | `AAPL`, `AMD`, `AMZN`, `BABA`, `BE`, `BTC`, `CASHCAT`, `COIN`, `CRCL`, `CRWV`, `ETH`, `GOOGL`, `HYPE`, `INTC`, `LIT`, `META`, `MSFT`, `MU`, `NEAR`, `NVDA`, `ORCL`, `PLTR`, `QQQ`, `SKHY`, `SLV`, `SNDK`, `SOL`, `SPCX`, `SPY`, `SUI`, `TSLA`, `USAR`, `USO`, `XRP`, `ZEC` |
| All three venues | `SNDK` |

## Identifiers for all symbols shared by at least two venues

`—` means that the symbol was not listed by that venue after the filters above.

| Canonical | Entropy | Lighter RH | Arcus |
| --- | --- | --- | --- |
| AAPL | — | `AAPL` (10) | `AAPL-USD` (30) |
| AMD | — | `AMD` (29) | `AMD-USD` (9) |
| AMZN | — | `AMZN` (11) | `AMZN-USD` (31) |
| BABA | — | `BABA` (19) | `BABA-USD` (17) |
| BE | — | `BE` (20) | `BE-USD` (39) |
| BTC | — | `BTC` (1) | `BTC-USD` (1) |
| CASHCAT | — | `CASHCAT` (36) | `CASHCAT-USD` (45) |
| COIN | — | `COIN` (23) | `COIN-USD` (43) |
| CRCL | — | `CRCL` (24) | `CRCL-USD` (19) |
| CRWV | — | `CRWV` (33) | `CRWV-USD` (36) |
| ETH | — | `ETH` (0) | `ETH-USD` (2) |
| GOOGL | — | `GOOGL` (12) | `GOOGL-USD` (14) |
| HYPE | — | `HYPE` (2) | `HYPE-USD` (6) |
| INTC | — | `INTC` (30) | `INTC-USD` (10) |
| LIT | — | `LIT` (5) | `LIT-USD` (41) |
| META | — | `META` (13) | `META-USD` (15) |
| MSFT | — | `MSFT` (14) | `MSFT-USD` (32) |
| MU | — | `MU` (31) | `MU-USD` (16) |
| NBIS | `io:NBIS` | — | `NBIS-USD` (46) |
| NEAR | — | `NEAR` (7) | `NEAR-USD` (56) |
| NVDA | — | `NVDA` (15) | `NVDA-USD` (28) |
| ORCL | — | `ORCL` (17) | `ORCL-USD` (37) |
| PLTR | — | `PLTR` (34) | `PLTR-USD` (35) |
| QQQ | — | `QQQ` (25) | `QQQ-USD` (24) |
| SKHY | — | `SKHY` (37) | `SKHY-USD` (44) |
| SLV | — | `SLV` (28) | `SLV-USD` (21) |
| SNDK | `io:SNDK` | `SNDK` (32) | `SNDK-USD` (33) |
| SOL | — | `SOL` (3) | `SOL-USD` (3) |
| SPCX | — | `SPCX` (18) | `SPCX-USD` (38) |
| SPY | — | `SPY` (26) | `SPY-USD` (23) |
| SUI | — | `SUI` (9) | `SUI-USD` (59) |
| TSLA | — | `TSLA` (16) | `TSLA-USD` (29) |
| USAR | — | `USAR` (21) | `USAR-USD` (40) |
| USO | — | `USO` (22) | `USO-USD` (22) |
| XRP | — | `XRP` (6) | `XRP-USD` (42) |
| ZEC | — | `ZEC` (4) | `ZEC-USD` (8) |
