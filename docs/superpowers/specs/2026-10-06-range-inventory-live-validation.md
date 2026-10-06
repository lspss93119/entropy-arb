# Range Inventory Live：本地驗證報告

本工作只做 isolated branch 的 implementation / offline tests。沒有部署、啟動交易、解除 HALT、修復 production mismatch、修改 production config 或 `.env`。

## 版本與架構

- Branch：`feature/range-inventory-live`
- Starting SHA：`a81eb822baa1d27ce609c4a5b95fae0a3b383a59`
- Base：`feature/range-inventory-shadow`；不是 checkout/修改 production。
- Pure strategy：`entropy_arb/range_inventory.py`，只產生 causal target 與 signal metadata。
- Shadow：仍用原來的 T+1 simulated executor/accounting。
- Live：`entropy_arb/range_inventory_live.py`，用 completed minute 的 signal、當下 fresh BBO、現有 `plan_arb` 及 paired execution / residual hedge / nonce / HALT pipeline。
- Range 減倉不經 rolling dynamic threshold 或 lot BE gate。
- Independent state：`logs/state/range-<symbol>-<hedge>.json`。記錄 profile、target、direction、actual paired quantities、signal、consumed minute、pending intent 與 realized-capture telemetry。
- Unknown actual fill price 不用 plan limit 代替；paired quantities 仍根據 actual fills 更新，PnL telemetry 標記 unavailable。

Frozen canary profile：long/short/hard cap 皆 $1,500；每 completed minute 最多一次 paired adjustment、上限 $53；paired visible BBO depth 最多 75%；signal age <=15 秒，從 `minute_ts + 60` 起算。Venue / strategy minima、step、headroom 及既有 execution guards 仍生效。Range gate 只限制增加 exposure，不能擋 release/cover；反方向需實際 flat 後的下一個 completed-minute signal。

## Historical / target parity

使用正在寫入的 EC2 ANTH recorder 下載副本，不改 originals。與 pinned shadow SHA 的 original implementation 及原 EC2 historical output 比較，而不是只比較總收益。

- Selected interval：`2026-09-12T07:50:00Z` 至 `2026-10-06T14:18:00Z`，inclusive cutoff。
- Completed minutes：29,570；每個 profile 的 baseline/range_gate 合計 59,140 variant rows。
- Selected-input SHA256：`eca7e463af63a0fe65ba1f306b8319ff401ede78eed8e488527fb31fcd685fe4`
- Downloaded source SHA256：`b5bb4afcc578e47e4cc6211818af46f7626c1126cf42c649505a3d91326b20bb`
- Original-shadow oracle：`a81eb822baa1d27ce609c4a5b95fae0a3b383a59`
- Historical comparison：每分鐘所有 output fields，absolute rounding tolerance `1e-8`；**0 divergence**。
- Live adapter signal path：每分鐘 target / action，並與 independent pinned T+1 executor 比較 executed signal timestamp / action / notional / inventory；shadow profile 與 $1,500/$53 canary profile 各 29,570 分鐘，**0 divergence**。

| Profile / variant | Equity proxy USD | Turnover USD | Final inventory USD | Closed long / short cycles |
|---|---:|---:|---:|---:|
| Original shadow baseline | 455.693626 | 1,138,111.396195 | -1,963.788345 | 21 / 20 |
| Original shadow range_gate | 409.422079 | 1,097,617.910505 | -1,963.788345 | 25 / 24 |
| Canary baseline | 27.612646 | 252,471.411030 | -38.284895 | 22 / 20 |
| Canary range_gate | 30.434531 | 232,063.586587 | -42.663167 | 21 / 20 |

這些是 **minute-BBO T+1 execution proxy**，funding excluded；equity 包含當時 liquidation proxy，不是全已實現收益。Live 用即時 fresh BBO 且有實際 step/minimum/slippage/partial fills，不能將這些數字當作 live PnL 預測。先前約 $455.07/$399.65 不是本次 pinned cutoff 的 exact oracle；本次相同輸入、相同 cutoff 與原 EC2 CSV 逐欄相等，未調參。

Replay artifacts 放在 worktree 的 ignored `logs/research/range-live-parity/`，不 commit 市場資料：`source-recorder.csv`、`reference-shadow.csv`、`reference-summary.json`、`parity-final.json`。原始 `parity.json` 保留，不覆寫。

另外全量重跑 downloaded recorder，至 `2026-10-06T15:51:00Z`，共 29,663 分鐘 / 每 profile 59,326 variant rows，historical 與 target/action **仍零 divergence**。`parity-latest-final.json` 保存此 extended 結果；它不與較早 cutoff 的 frozen CSV 混比。Extended original profile equity proxy：baseline $455.013608，range_gate $408.742060。

Reproduce（不讀 `.env`、不連 API、不送單）：

```bash
python3 tools/check_range_inventory_parity.py \
  --csv logs/research/range-live-parity/source-recorder.csv \
  --cutoff 2026-10-06T14:18:00Z \
  --reference-csv logs/research/range-live-parity/reference-shadow.csv \
  --output logs/research/range-live-parity/parity-new.json
```

Runner 不覆寫既有報告，target/action/accounting 任一 divergence 均 fail。

## Test evidence

目前 full suite：`255 passed, 2 warnings`。Warnings 是既有 Lighter SDK websockets deprecation，不是本次程式失敗。

在 original 187 tests 以外新增 68 個 collected regression cases（core 6、live adapter 29、engine integration 27、parity 5、RH nonce await guard 1）。

新增測試覆蓋：pure core causal formula / shadow parity；completed minute 即時 wakeup；stale/incomplete/duplicate signal；range gate add-only；$53 / 75% depth / position headroom / venue minima；flatten-before-reversal；atomic intent / restart / corrupted state；actual partial/residual accounting；missing fill price；strict reconciliation / persisted HALT / explicit resume；record-only 無 state/execution side effects；target/action/accounting drift detection。

已通過：full pytest、`compileall entropy_arb tools`、ruff、`git diff --check`，以及整個 base..HEAD 的 whitespace 檢查。

## Independent review 與 RED/GREEN 修正

同一個 independent reviewer 對完整 branch 提出三個 Important findings（無已確認 Critical），已在 `dce86d72fbb4677a953484e60d8559ade309675b` 修正：

1. Fsync / `asyncio.gather` / nonce await 後可能仍送 stale 或不符合 depth/cap 的 order。新增 final live plan validation；optional `submit_guard` 延伸至 HL POST 前與 RH authoritative nonce await 後。Default `None` 保留 fixed/rolling/residual execution；沒有重寫 nonce、signing、order 或 hedge pipeline。只有 proven zero submissions 才 abandon intent；已送一腿仍交給既有 residual hedge 與 actual-fill settlement。
2. 原 BBO notional $10 可能在 10bps protection 後變為 sell-limit $9.99。現在兩邊 final rounded protective limits 都檢查 venue / strategy minima，不放寬、不 enlarge。
3. Unknown-price primary + residual roundtrip 的 paired delta/cash subtotal 都可能為零。Positive fills 任一 required actual price 缺失，realized/cumulative capture 都持久化為 `null`，不假報零收益；known quantities 正常 settle。

三項先由 regression tests 重現失敗，再修正並通過；另補 changed-minute arming regression 與 long/short closed-gate price-drift reductions。Frozen core / target formula / parameters 未修改。EOF whitespace 已清除。

同一 reviewer 針對 `996183d..dce86d7` bounded follow-up 確認：R1 / R2 / R3 / EOF 全部 closed，未發現該 delta 的具體新 regression；獨立執行 affected modules 得到 `63 passed, 2 warnings`。Full suite 與兩份 final parity 由 main executor 在最終 code 上再次執行並驗證。

### Reviewer set-aside 項目的 executor rulings

- Live fill/PnL 等同 shadow：不要求，T+1 對即時 execution 差異已明確揭露；只 assert 同 inventory inputs 的 causal target/action。
- Actual venue minimum / reduce-only exceptions：未做真實 API 驗證，採 conservative local minima，不利用未確認的 exceptions。
- Legacy engine cash/volume limit-price fallbacks：保留舊 execution metrics，非本次 Range realized-capture authority。Range state/log 的 actual spread capture 才是本策略 telemetry；unknown 明確 unavailable，不能把 generic engine cash 當作已驗證 Range PnL。
- DEX / resolved market IDs 等超出 canonical symbol/hedge 的 identity binding：未宣稱已做完整跨帳號/市場綁定。後續 deployment 必須核對 account、DEX、asset/market IDs 與 Range state ownership；不能任意搬 state。
- 全組合 malformed-but-consistent metadata / same-process disk corruption：已測現有 schema/identity/parameters/signal metadata corrupt cases 與 startup fail-closed，不宣稱 exhaustive adversarial fuzz coverage；operator explicit resume 的正常 startup 必須重新讀 state 並嚴格 reconcile。
- 真實 power-loss durability：已檢查 atomic replace/fsync/read-back、write failure 與 pending-intent restart rejection；未做硬體斷電注入。
- Reconciliation tolerance 以下 dust / execution-drain timeout：保留舊 pipeline，Range quantities mismatch / unresolved intent fail closed；不新增 enlargement/force-flatten/shutdown framework。
- Production position ownership / live readiness：目前只核對不變性，不處理現存 incident；需另行授權 preflight。

## Production read-only preservation evidence

在 `2026-10-06T16:05:30Z` 與修正後 `2026-10-06T16:40:16Z` 只讀核對 EC2，兩次與開始 baseline 一致：

- `/home/ec2-user/entropy-arb-live`
- Branch：`feature/dynamic-midline-be-exit`
- SHA：`07723bd1e2da44a1268b7d54db323520faa9b43d`，source clean。
- tmux：`anth-live`；PID `1035989`，process start ticks `198051689` 與本工作開始一致；沒有 stop/restart。
- Config SHA256：`fa496ba59b8a5137b11be960bbbc3fe9b4ad9af9ba4ae9b31a52c8d69787aaaa`，未變。
- `.env` SHA256：`b180ae392e6bd50ed58a8b6773ac028b78731f8e1799d358580a65808b033892`，未變；沒有顯示 secret。
- HALT 仍 `true`，同一 production reconciliation-mismatch reason。Running production 自己更新 halt timestamp，因此不聲稱整個 HALT JSON byte hash 固定；本工作沒有解除/編輯 HALT。
- Local production worktree 同 SHA、clean；original local checkout 的既有 untracked files 保留，沒有 stash/move/delete/commit。

## $1,500 live canary blockers

1. Production 現存 HALT / positions-vs-ledger mismatch 尚未處理。本工作刻意不修復，也不 resume。任何後續操作需要獨立授權及 authoritative preflight。
2. Range 首次启动需要兩邊 flat 或自己的已存在、合法且嚴格一致的 Range state；不能把 rolling lot ledger 猜測搬成 Range inventory。相同 symbol/hedge 只能有一個 live order owner。
3. Mock/offline tests 不等於 exchange live validation。需要另行批准的部署、HALTED startup、strict reconciliation、fresh signal/feed/runtime profile / signer / account stream 檢查，之後才可明確批准 resume。
4. Min-base/min-notional 以下的 remainder 仍 fail closed；沒有 force flatten / enlargement / rolling BE terminal bypass。本次依要求不擴策略，需要 operator 接受並規劃小額 canary 的 venue-minimum remainder 風險。
5. Replay 使用 minute-BBO proxy，未涵蓋即時 depth、slippage、fees/funding 的完整實盤行為；funding 刻意不計。

未 merge、push、deploy；沒有任何真實 order API call。

## Commits 與精確修改檔案

- `509604f231664ede375782d526c1da24b8775507` — `docs: specify range inventory live adapter`
- `04b29895577080a5f3956afdc4e0b5c12a259d3b` — `refactor: share pure range inventory strategy core`
- `28f00f2b0a1e7971a4d7c0b81406b9ebd2f5e10c` — `feat: add persisted range inventory live adapter`
- `e69b471788d0f2f680393a9010133627a30fc0b9` — `feat: integrate range inventory with safe paired execution`
- `996183d76b8fe220111821581a745577b4628672` — `test: verify range inventory historical and live target parity`
- `dce86d72fbb4677a953484e60d8559ade309675b` — `fix: guard range submissions at transport boundaries`
- 收尾 docs commit 保存本報告與 completed plan，ending SHA 以最後 `git rev-parse HEAD` 為準。

精確 18 檔（相對 isolated worktree root）：

```text
config.range-inventory.example.yaml
docs/superpowers/plans/2026-10-06-range-inventory-live.md
docs/superpowers/specs/2026-10-06-range-inventory-live-design.md
docs/superpowers/specs/2026-10-06-range-inventory-live-validation.md
entropy_arb/config.py
entropy_arb/engine.py
entropy_arb/range_inventory.py
entropy_arb/range_inventory_live.py
entropy_arb/range_inventory_shadow.py
entropy_arb/venue_hl.py
entropy_arb/venue_lighter.py
tests/test_lighter_nonce.py
tests/test_range_inventory.py
tests/test_range_inventory_engine.py
tests/test_range_inventory_live.py
tests/test_range_inventory_parity.py
tests/test_range_inventory_shadow.py
tools/check_range_inventory_parity.py
```

`tests/test_range_inventory_shadow.py` 只移除 unused import 以通過 Ruff；其原始 tests/behavior 沒有改寫。Spec/plan 在 implementation 前已 commit。
