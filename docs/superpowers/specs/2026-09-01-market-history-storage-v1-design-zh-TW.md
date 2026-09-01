# Market History Storage v1（市場歷史資料儲存）設計

日期：2026-09-01
狀態：已由使用者批准，進入 implementation plan
目標分支：`feature/p2-persistence-research`
前置狀態：Subproject A — Strategy Library + Engine Boundary 已完成於 `dfd055ad020faa6371576c1977e2ecaa6e9cac6b`

## 1. 背景與目的

原先規劃的 Market Analyzer 已取消。市場判讀、策略比較與參數研究改由人工／ChatGPT 進行；本地程式只需要把市場歷史資料**完整、穩定、可查詢、可單檔分享**地保存。

目前 recorder 會約 1 Hz 寫 `samples-v2-*.csv`，並寫分鐘摘要 `minutes-*.csv`；reference collector 另外寫 Entropy oracle/mark 與 hedge index/mark CSV。隨著 symbol、hedge、日期與多個 bot process 增加，CSV 會逐漸散落、難以去重、難以做跨期間查詢，也不利於「一次上傳少量檔案給 ChatGPT 分析」的工作流。

Storage v1 的目標是把 market-history data 收斂到一個本地 SQLite database：

```text
data/market-history.sqlite
```

並支援：

1. 多個獨立 entropy-arb processes 同時寫入不同市場。
2. 永久保存約 1 Hz 的 raw samples，不把舊資料降級成 minute-only。
3. 保存現有 minute aggregates。
4. 保存現有 Entropy / hedge reference data。
5. 將既有 CSV 非破壞式 migration 進同一 database。
6. 需要研究時，使用 SQLite backup API 產生一個一致性 snapshot；使用者只需上傳該單一 `.sqlite` 檔案。

Storage v1 **不是 Market Analyzer、不是 data warehouse、不是 execution backtester**。

---

## 2. 設計原則

1. **單一正式資料庫**：`data/market-history.sqlite` 是未來 market-history 的 source of truth。
2. **SQLite WAL**：允許多個 bot process 各自持有 connection 並以小批次 transaction 寫入。
3. **Raw samples 永久保留**：約 1 Hz BBO observation 是最重要的研究資產。
4. **不補資料**：gap 保留為 gap；不 interpolation、不 forward-fill。
5. **不改市場資料語義**：premium / executable edge 仍由既有 `calculate_premiums()` 計算。
6. **Idempotent**：live restart 與 migration 重跑不得製造 duplicate rows。
7. **非破壞式 migration**：舊 CSV 在 migration 完成後仍原地保留；Storage v1 不自動刪除或 archive。
8. **單檔分享**：snapshot 必須是一個 standalone SQLite file，不要求同時上傳 `-wal` / `-shm`。
9. **不保存研究結論**：strategy recommendation、regime、selected center、分析結果不得寫進 market-history database。
10. **簡單優先**：不引入 server、storage daemon、DuckDB live writer、Parquet partitions、manifest、compaction pipeline 或 cloud sync。

---

## 3. 高階架構

```text
                 process A: SNDK / lighter-rh
                 process B: ANTH / lighter-rh
                 process C: ...
                           |
                           v
                 MarketHistoryStore
                 one connection/process
                           |
                    SQLite WAL mode
                           |
                           v
               data/market-history.sqlite
                 |       |        |
                 |       |        +-- reference tables
                 |       +----------- minutes
                 +------------------- samples

legacy CSV ---------------- migration tool ----------------^

market-history.sqlite
        |
        | sqlite3 backup API
        v
exports/market-history-snapshot-<UTC>.sqlite
        |
        v
single-file upload for analysis
```

每個 process 只建立一個 `MarketHistoryStore` / SQLite connection，供該 process 的 recorder 與 reference collector 共用。不得為同一 process 的不同 writer 再開多個獨立 SQLite writer connections，避免不必要的本機 writer contention。

---

## 4. SQLite runtime policy

每個 process 開啟 database 後設定：

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA busy_timeout = 10000;
PRAGMA foreign_keys = ON;
```

理由：

- `WAL`：支援多 process concurrent readers / writers；實際寫 transaction 仍由 SQLite 序列化。
- `FULL`：Storage v1 優先保護不可重建的歷史市場資料；目前約 1 Hz 且 batch write，fsync 成本可接受。
- `busy_timeout=10000`：另一個 process 正在 transaction 時等待最多 10 秒，而不是立即因 `database is locked` 失敗。

Storage v1 不自行實作 cross-process lock manager。

---

## 5. Database schema

Schema version：`1`。

### 5.1 `meta`

```sql
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

至少保存：

- `schema_version = 1`
- `created_at_utc`
- `last_migration_at_utc`（若 migration 曾成功執行）

`meta` 不保存分析結果。

### 5.2 `samples` — primary research dataset

```sql
CREATE TABLE samples (
    timestamp_ms            INTEGER NOT NULL,
    symbol                  TEXT NOT NULL,
    hedge                   TEXT NOT NULL,
    premium_bps             REAL NOT NULL,
    sell_edge_bps           REAL NOT NULL,
    buy_edge_bps            REAL NOT NULL,
    entropy_bid             REAL NOT NULL,
    entropy_ask             REAL NOT NULL,
    hedge_bid               REAL NOT NULL,
    hedge_ask               REAL NOT NULL,
    entropy_book_update_ms  INTEGER NOT NULL,
    hedge_book_update_ms    INTEGER NOT NULL,
    PRIMARY KEY (symbol, hedge, timestamp_ms)
);
```

語義完全沿用現有 `samples-v2`：

```text
premium_bps   = mid-to-mid Entropy / hedge premium
sell_edge_bps = executable SELL Entropy / BUY hedge edge
buy_edge_bps  = executable BUY Entropy / SELL hedge edge
```

Live recorder 必須繼續透過 shared `calculate_premiums()` 產生這三個 derived values。

唯一 identity：

```text
(symbol, hedge, timestamp_ms)
```

查詢不依賴 physical row order；研究時使用 `ORDER BY timestamp_ms`。

### 5.3 `minutes` — derived convenience dataset

```sql
CREATE TABLE minutes (
    minute_ts             INTEGER NOT NULL,
    symbol                TEXT NOT NULL,
    hedge                 TEXT NOT NULL,
    entropy_bid           REAL NOT NULL,
    entropy_ask           REAL NOT NULL,
    hedge_bid             REAL NOT NULL,
    hedge_ask             REAL NOT NULL,
    premium_open_bps      REAL NOT NULL,
    premium_high_bps      REAL NOT NULL,
    premium_low_bps       REAL NOT NULL,
    premium_close_bps     REAL NOT NULL,
    premium_mean_bps      REAL NOT NULL,
    premium_std_bps       REAL NOT NULL,
    sell_edge_mean_bps    REAL NOT NULL,
    sell_edge_max_bps     REAL NOT NULL,
    buy_edge_mean_bps     REAL NOT NULL,
    buy_edge_max_bps      REAL NOT NULL,
    samples               INTEGER NOT NULL,
    PRIMARY KEY (symbol, hedge, minute_ts)
);
```

欄位與現有 minute CSV 語義保持一致。`minutes` 是 derived convenience data；raw `samples` 才是研究 source of truth。

### 5.4 `entropy_reference`

目前 Entropy reference CSV schema 為：`recv_ms, oracle_px, mark_px`。SQLite 額外加入 `symbol` 與 `hedge`，保留是哪一組 market-pair process 收到該 observation 的 provenance。

```sql
CREATE TABLE entropy_reference (
    symbol      TEXT NOT NULL,
    hedge       TEXT NOT NULL,
    recv_ms     INTEGER NOT NULL,
    oracle_px   REAL NOT NULL,
    mark_px     REAL NOT NULL,
    PRIMARY KEY (symbol, hedge, recv_ms, oracle_px, mark_px)
);
```

Reference data 不參與任何自動 strategy logic。

### 5.5 `hedge_reference`

目前 Lighter hedge reference CSV schema 為：`recv_ms, server_ms, index_px, mark_px`。

```sql
CREATE TABLE hedge_reference (
    symbol      TEXT NOT NULL,
    hedge       TEXT NOT NULL,
    recv_ms     INTEGER NOT NULL,
    server_ms   INTEGER NOT NULL,
    index_px    REAL NOT NULL,
    mark_px     REAL NOT NULL,
    PRIMARY KEY (symbol, hedge, recv_ms, server_ms, index_px, mark_px)
);
```

Reference tables 使用完整 observation payload 作 exact-duplicate identity，避免把同一 millisecond 內可能存在的不同 reference payload 靜默合併。

---

## 6. Live write semantics

### 6.1 一個 process、一個 store

Engine 啟動時建立一個 `MarketHistoryStore`，該 process 的：

- `MinuteRecorder`
- Entropy reference recorder
- Hedge reference recorder

都把 rows 提交給同一 store。

Storage v1 不改變 recorder/reference 的既有 lifecycle decision；只替換 persistence backend。

### 6.2 Samples

既有 recorder 約每秒：

1. 檢查兩邊 books fresh。
2. 讀 BBO。
3. 使用 `calculate_premiums()`。
4. 建立一筆 sample。
5. 更新既有 minute aggregator。

上述資料語義完全不改。

Sample row 先進 process-local memory buffer，預設約每 10 秒 flush 一次；一次 transaction 批次寫入多筆 rows。

Flush interval 在 Storage v1 固定為 implementation constant，不加入 config，避免把 storage tuning 變成策略參數。

### 6.3 Minutes

沿用現有 `_MinuteAgg` semantics。Minute rollover / shutdown 時產生 minute row，交給同一 store 寫入 `minutes`。

不得為 SQLite 重寫另一套 minute aggregation formula。

### 6.4 Reference rows

現有 reference parser、market filtering 與 timestamp semantics 保持不變；只把 `ReferenceCsvWriter` persistence 換成 store append API。

Reference writer 是否啟動、何時停止等 lifecycle 由既有 Engine / reference code 決定，Storage v1 不新增 reference strategy 或 analysis behavior。

### 6.5 Transactions and conflicts

每次 flush：

```text
BEGIN
batch INSERT samples
batch INSERT minutes
batch INSERT entropy_reference
batch INSERT hedge_reference
COMMIT
```

若 transaction 失敗，整批 rollback。

Live duplicate 使用 idempotent insert；已存在完全相同 identity 時 no-op，不得 overwrite 既有 row。

對 `samples` / `minutes`，若同一唯一 key 已存在但 payload 不同，live path 不得用 `REPLACE` 覆寫歷史。至少記錄 ERROR / conflict count，保留 database 內既有 row。

---

## 7. Storage failure behavior

Market-history persistence failure不得直接改變交易策略、下單邏輯或風控行為。

短暫 `SQLITE_BUSY`：由 `busy_timeout` 等待；仍失敗則 rollback，保留尚未成功寫入的 process-local buffer，下次 flush 重試。

其他 storage errors（例如 disk full / I/O error）：

- 不得標記為成功。
- 不得 silent drop。
- 必須 ERROR log。
- 未寫入 rows 暫留 buffer 並重試。

為避免永久 storage failure 導致無限記憶體成長，implementation 必須有一個保守 hard buffer cap。達 cap 時不得靜默丟資料：必須 CRITICAL log 明確列出 dropped row count / dataset type。該 cap 是 operational safety constant，不加入 strategy config。

正常 shutdown 必須嘗試 final flush；但不能讓 recorder shutdown 永久卡住。

---

## 8. Config migration

目前 config：

```yaml
recorder:
  enabled: true
  csv: logs/minutes.csv
```

Storage v1 改為：

```yaml
recorder:
  enabled: true
  database: data/market-history.sqlite
```

`recorder.enabled` semantics 保留：live mode 依現有規則決定是否啟動 market recorder；`--record-only` 仍依既有行為強制收集市場資料。

`recorder.database` 同時作為該 process 的 market-history store path，供 recorder 與既有 reference collector 共用。

Legacy `recorder.csv` 不得 silent ignore；startup 應提供清楚 migration error，例如：

```text
legacy 'recorder.csv' is no longer supported; use
recorder.database: data/market-history.sqlite
```

不保留 dual-write CSV compatibility mode。

`logging.trades_csv` 與一般 engine log 不屬於 market-history Storage v1，維持現況。

---

## 9. Legacy CSV migration

提供一次性 migration tool，概念 CLI：

```text
python3 tools/migrate_market_history.py \
  --source logs \
  --database data/market-history.sqlite
```

### 9.1 支援資料種類

- `samples-v2*.csv`
- `minutes*.csv`
- `reference-*-entropy.csv`
- `reference-*.csv`

Migration 不匯入：

- trades CSV
- engine logs
- arbitrary unknown CSV

### 9.2 Schema-first discovery

Filename 只用於協助辨識 provenance；真正資料種類必須先由 header/schema 驗證。

Unknown / old incompatible schema 不得硬猜，標記 FAIL / unsupported，原檔不動。

### 9.3 Symbol / hedge provenance

- `minutes`：row 本身已有 `symbol` / `hedge`，以 row data 為 authoritative。
- reference：現有 filename 已包含 symbol / hedge，可解析後寫入。
- `samples-v2`：舊 schema 沒有 symbol / hedge，因此 migration 必須 fail-closed。

`samples-v2` provenance 解析順序：

1. 若 filename 可依已知 hedge suffix (`lighter`, `lighter-rh`, `tradexyz`) 無歧義解析出 symbol / hedge，使用 filename provenance。
2. 否則若存在對應 minute file，且可無歧義確認該 samples file 只屬於一個 `(symbol, hedge)` dataset，允許採用 companion provenance。
3. 若仍有歧義，不得猜測；報 `NEEDS_MAPPING`，要求使用者透過 migration CLI 顯式指定 mapping 後重跑。

### 9.4 Validation

每個 source file 回報：

```text
source rows
valid rows
invalid rows
already existing / exact duplicates
conflicting-key rows
inserted rows
min timestamp
max timestamp
status = PASS | PARTIAL | NEEDS_MAPPING | FAIL
```

Invalid numeric / timestamp row 可 skip，但必須計數與報告。

Gap 與 out-of-order row 不視為 migration failure：

- 不補 gap。
- SQLite 不依 physical order；研究 query 再 `ORDER BY`。

### 9.5 Duplicate / conflict policy

Migration 必須可安全重跑。

- exact duplicate：no-op，計入 duplicate/already-existing。
- same unique key + different payload：不得 `REPLACE`；保留 existing row，計入 conflict 並在報告警告。

任何 migration failure 都不得刪除、移動或覆寫原 CSV。

第一版 migration 完成後，legacy CSV 仍原地保留，直到使用者日後另行決定 archive/delete。

---

## 10. Snapshot / backup

提供：

```text
python3 tools/snapshot_data.py
```

預設輸出：

```text
exports/market-history-snapshot-YYYYMMDD-HHMMSSZ.sqlite
```

Snapshot **不得**用 filesystem copy 直接複製正在被多個 bots 寫入的 live database。

必須使用 Python `sqlite3.Connection.backup()` / SQLite backup API 建立一致性 snapshot。

完成後對 snapshot 執行：

```sql
PRAGMA quick_check;
```

只有 `quick_check` 成功才回報 snapshot PASS。

產物必須是 standalone single file；使用者不需要上傳 live DB 的 `-wal` / `-shm` sidecars。

同一 snapshot mechanism 也可人工輸出到：

```text
data/backups/
```

Storage v1 不做 scheduler、backup daemon、cloud upload 或 automatic retention policy。

---

## 11. Research / sharing workflow

日常：

```text
多個 bots -> data/market-history.sqlite
```

需要市場分析時：

```text
python3 tools/snapshot_data.py
```

然後上傳單一：

```text
market-history-snapshot-*.sqlite
```

分析者可以自行 SQL query，例如：

```sql
SELECT *
FROM samples
WHERE symbol = 'SNDK'
  AND hedge = 'lighter-rh'
ORDER BY timestamp_ms;
```

若未來 database 大到不方便整庫上傳，再另立獨立需求實作 date/symbol export；**不屬於 Storage v1**。

---

## 12. 與現有系統的邊界

Storage v1 只替換 market-history persistence backend。

必須保持不變：

- `calculate_premiums()` 公式與語義
- ~1 Hz fresh-BBO sampling semantics
- minute aggregation semantics
- Strategy Library semantics
- live execution / hedge / reconcile
- strategy observation lifecycle
- rate limits / outage handling
- trade CSV / engine logs
- reference parser / market filtering semantics

Storage v1 不允許因為資料庫重構順便修改 trading behavior。

---

## 13. `tools/analyze.py`

原 `tools/analyze.py` 是 legacy CSV heuristic analyzer。Market Analyzer 專案已取消，因此 Storage v1 **不把它升級成新的 SQLite Market Analyzer**。

文件應停止把它描述成未來正式的策略選擇流程。可保留檔案作 legacy CSV ad-hoc tool，但它不再是 Storage v1 驗收條件，也不要求支援新 SQLite dataset。

---

## 14. 明確不做

Storage v1 不包含：

- Market Analyzer
- auto strategy selection / switching
- regime detection
- backtest / replay engine
- parameter optimization
- Parquet source-of-truth
- DuckDB live store
- daily/monthly partitions
- compaction
- manifest database
- storage daemon / IPC
- PostgreSQL / MySQL server
- cloud storage / sync
- automatic backup schedule
- retention policy
- old CSV auto delete/archive
- funding/reference-derived strategy
- L2 order-book history
- trade log migration

---

## 15. Testing requirements

### 15.1 Schema / store

- fresh DB 建立所有 tables / schema_version。
- second open idempotent。
- unique key / exact duplicate semantics。
- conflicting payload 不 overwrite。
- batch transaction rollback on failure。

### 15.2 Multi-process WAL

建立至少 2 個獨立 processes，同時向同一 temporary SQLite database 寫不同 `(symbol, hedge)` samples；驗證：

- 無 corruption。
- 所有 committed rows 存在。
- 無 unintended duplicates。
- `PRAGMA quick_check` = `ok`。

測試不得只用同 process 的兩個 connections 冒充 multi-process。

### 15.3 Recorder parity

固定 synthetic BBO stream，同一 inputs 下，SQLite rows 的：

- premium
- sell edge
- buy edge
- minute aggregate

必須與目前 recorder semantics 一致。

### 15.4 Reference parity

固定 reference frames，SQLite rows 必須與現有 parser / CSV row semantics 一致。

### 15.5 Migration

至少覆蓋：

- valid samples/minutes/reference import
- rerun idempotency
- duplicate rows
- same-key conflicting payload
- invalid row accounting
- unknown schema fail-closed
- ambiguous samples provenance -> `NEEDS_MAPPING`
- explicit mapping success
- original CSV unchanged

### 15.6 Snapshot

在 source DB 處於 WAL mode 且有 concurrent writer 時建立 snapshot；驗證：

- snapshot 可獨立開啟。
- 不需要 source `-wal` / `-shm`。
- `PRAGMA quick_check` = `ok`。
- snapshot 代表一致 transaction state。

### 15.7 Existing regression

整個既有 test suite 必須保持通過。Storage migration 不得改變 Strategy / Engine trading semantics。

---

## 16. Operational success criteria

Storage v1 完成的定義：

1. 多個 entropy-arb processes 可以同時將 market history 寫到一個 `data/market-history.sqlite`。
2. Live 新資料不再需要 market-history CSV files。
3. 約 1 Hz samples、minutes、Entropy reference、hedge reference 都存在同一 SQLite。
4. 過去合法 CSV 可非破壞式匯入；migration 重跑不重複資料。
5. Ambiguous legacy samples 不會被猜錯 provenance。
6. Database/snapshot `quick_check` 通過。
7. 使用者可在 bots 持續運作時產生一個 standalone snapshot，並只上傳該一個檔案供研究。
8. Strategy Library / Engine execution behavior 沒有因 Storage v1 改變。

---

## 17. Implementation sequencing constraint

後續 implementation plan 應至少按以下依賴順序拆分：

```text
schema + MarketHistoryStore
        ↓
store unit/concurrency tests
        ↓
recorder persistence migration
        ↓
reference persistence migration
        ↓
config migration
        ↓
legacy CSV migration tool
        ↓
snapshot tool
        ↓
docs + full regression + live record-only smoke
```

每一階段必須能獨立驗證，不應一次把 recorder、reference、migration、snapshot 全部改完才測試。
