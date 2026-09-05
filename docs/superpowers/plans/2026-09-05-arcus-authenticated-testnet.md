# Arcus Authenticated Testnet Adapter Implementation Plan

> For agentic workers: required reading — use the `superpowers:executing-plans` skill to execute this plan.

**Goal:** Add Arcus Ed25519 authentication, testnet account reads, and a testnet-only IOC execution adapter without enabling Arcus mainnet orders or cross-venue live arbitrage.

**Architecture:** Keep Arcus-specific signing, authenticated HTTP, response normalization, precision conversion, and bounded order reconciliation inside `ArcusVenue` plus small focused helpers. Extend the existing explicit venue mapping and configuration model; do not add a framework, registry, ABC, or changes to existing venue protocol implementations. Preserve the existing Arcus public WebSocket record-only path.

**Tech Stack:** Python 3, existing `aiohttp`/WebSocket clients, `cryptography` Ed25519, dataclasses, pytest, and the repository's current engine/OrderBook contracts.

**Spec:** Current user-provided Phase 4A/4B specification dated 2026-09-05.

## Global constraints

- Use only Arcus official documentation and reproducible testnet observations as API/signing authority.
- Never print, persist, fixture, or commit real credentials.
- Testnet is the only permitted Arcus authenticated write environment. Mainnet `send_taker()` must fail with `Arcus mainnet execution is not enabled`.
- Do not submit cross-venue live arbitrage orders from the engine.
- Do not add Arcus funding strategy, maker/MM logic, database/framework changes, or synthetic prices.
- Complete Phase 4A and its verification gate before any Phase 4B order is submitted.

## Phase 4A — authentication and account reads

### Task 1: Verify the official testnet contract and record it

**Files:** `docs/arcus-authenticated-api.md`, official Arcus documentation URLs.

1. Verify the testnet REST base URL, protected-request headers, timestamp unit, Scheme 1 and Scheme 2 signing payloads, account/position/open-order/fill endpoints, fee/rate-limit endpoints, and account response fields.
2. Make only credential-less or already-authorized read requests needed for verification; do not call key-registration or order endpoints.
3. Record exact observed schemas, field-to-engine mappings, timestamp units, fee source, and any docs-versus-validator differences in the documentation artifact.

### Task 2: Define deterministic signer behavior with RED tests

**Files:** `tests/test_arcus_signing.py`, `tests/test_arcus_credentials.py`.

1. Add tests first for compact sorted canonical JSON, lower-cased account address, omitted empty client id, nanosecond request timestamp, Scheme 1 order/cancel payload bytes, Scheme 2 `timestamp + action + canonical_json(body)` bytes, exact Ed25519 signatures, exact authentication headers, raw-seed/PEM parsing, and secret-safe representations/errors.
2. Run the focused tests and capture the expected RED failure because the signer helper does not yet exist.

### Task 3: Define testnet credentials and lifecycle contract with RED tests

**Files:** `tests/test_config_arcus_auth.py`, `tests/test_arcus_account.py`, `tests/fixtures/arcus/*.json`.

1. Add sanitized fixtures for account, positions, open orders, fills, account stats/fee data, and rate-limit data using official field shapes only.
2. Add tests for complete/incomplete credentials, testnet endpoint selection, account/position/equity normalization, long/short/flat signed base quantity, market precision parsing, and read failures that remain explicit.
3. Run the focused tests and capture RED evidence before production implementation.

### Task 4: Implement isolated authentication and account reads

**Files:** `entropy_arb/arcus_signing.py`, `entropy_arb/config.py`, `entropy_arb/venue_arcus.py`, `.env.example`, `docs/arcus-authenticated-api.md`.

1. Implement the isolated Ed25519 signer and credential loader with exact official canonicalization and separate Scheme 1/Scheme 2 methods.
2. Add environment-only credential loading for API key, private key, account address, account index, and explicit Arcus environment; keep `.env.example` placeholders only.
3. Add Arcus authenticated/public read helpers for account, positions, open orders, fills, account-specific fee data, and one normal rate-limit query. Normalize equity to the documented account value and positions to signed base units, with no USD-notional substitution.
4. Keep the existing public BBO WebSocket behavior and make `warm_http()`/`close()` safe for read-only operation.
5. Run focused tests to GREEN, then inspect the diff for accidental protocol or strategy changes.

### Task 5: Close the Phase 4A gate and commit

**Files:** all Phase 4A files above.

1. If testnet credentials are present in the local environment, perform bounded authenticated account, positions, open-orders, fills, fee, and rate-limit reads without any write endpoint.
2. Verify that no order endpoint was called and that no credential-bearing value appears in output, logs, fixtures, or git diff.
3. Run focused tests, static checks, and the full 160-column suite. Do not “fix” the known 80-column Rich test.
4. Commit exactly the Phase 4A work as `feat: add arcus authentication and account reads` after the gate is satisfied. If credentials are unavailable or the gate fails, stop before Phase 4B and report the blocker without submitting orders.

## Phase 4B — testnet execution

### Task 6: Define order construction and outcome RED tests

**Files:** `tests/test_arcus_execution.py`, `tests/test_arcus_precision.py`.

1. Add tests first for top-level and tiered tick safety rounding, non-increasing size quantization, minimum quantity/notional validation, Scheme 1 LIMIT+IOC order payloads, `reduceOnly`, unique client ids, goodTilTime microsecond body/nanosecond signed units, and the one-month validator horizon.
2. Add mocked response tests for rejected, accepted zero-fill, partial, full, cancelled remainder, timeout, ambiguous, order-status reconciliation, fill reconciliation, and unresolved outcomes.
3. Add tests proving the mainnet hard block, credential redaction, and that no Arcus order method is reachable through record-only startup.
4. Run the focused tests and capture RED evidence before adding execution code.

### Task 7: Implement testnet-only `send_taker()` and bounded reconciliation

**Files:** `entropy_arb/venue_arcus.py`, `entropy_arb/arcus_signing.py`, `entropy_arb/config.py`, `tests/test_arcus_execution.py`.

1. Build only LIMIT+IOC protective orders using verified market precision and official Scheme 1 signing.
2. Enforce `ARCUS_ENV=testnet` in code before every write; reject mainnet with the exact hard-block error.
3. Return the existing unified execution result contract: `status`, `filled_base`, `avg_px`, `err`, and `unresolved`.
4. On timeout or ambiguous HTTP results, reconcile by client id/order status/fills/position within a bounded read budget; preserve `unresolved=True` when authoritative state remains unknown.
5. Keep cross-venue engine live execution rejected for this phase.

### Task 8: Run the explicitly authorized one-shot testnet ladder

**Files:** `docs/arcus-authenticated-api.md`, test output records kept outside the repository.

1. Confirm Phase 4A is green and testnet identity/environment before any write.
2. Run exactly one small place/cancel test, then one IOC zero-fill test. Do not retry automatically.
3. Run small fill and reduce-only flatten only when testnet liquidity and minimums make the operation safe; stop if liquidity is insufficient rather than chasing.
4. Record every order id/client id, response, fill status, and final position without recording secrets. Do not connect the pair engine or submit cross-venue arbitrage.

### Task 9: Final regression, commit, and delivery

**Files:** final Phase 4B implementation/tests/docs.

1. Run all focused tests and `COLUMNS=160 LINES=60 python3 -m pytest tests/ -q`, plus relevant lint/compile/diff checks.
2. Confirm Arcus record-only, existing venues, recorder/analyzer, and generic pair math remain green.
3. Commit focused Phase 4B changes, push `rebuild/generic-pair` to `origin`, verify remote and clean worktree, and stop without starting Phase 5.
