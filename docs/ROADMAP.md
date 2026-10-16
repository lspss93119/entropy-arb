# entropy-arb 開發計畫與決策基準（ROADMAP v1.1）

> 更新日期：2026-10-16（Asia/Taipei）  
> **這份檔案是專案方向與核准範圍的主要依據；GitHub Issues 保存工作細節與尚未採納的想法，Git Commit / CI 是實際完成證據。**  
> 這份文件的維護不代表某個技術修改已獲准實作或部署。

## 1. 目標與範圍

從 [your-quantguy/entropy-arb](https://github.com/your-quantguy/entropy-arb) 乾淨 Fork 出發，先確保基礎執行可靠，再沿著作者提出的四個方向優化：

1. 動態進出場價差門檻。
2. 動態滑點控制。
3. 程式及網路延遲優化。
4. 多交易所同時套利。

先保持作者原版 Fixed Midline / Upper-Lower Band、雙向交易、Inventory Ladder、兩腿執行方式作為 baseline。**不引入舊 Fork 的 Rolling Z-score、Lot Ledger、Range Inventory、虛擬多空庫存，亦不實作此前討論的「方案 B」。** 未核准前不得自行擴大策略範圍。

## 2. 現在的真實狀態

- Repository：[`lspss93119/entropy-arb`](https://github.com/lspss93119/entropy-arb)
- 上游原始交易程式基準 Commit：`aa0391471f6bf72f78c45801fb8117b7bf7e8c89`（2026-08-26）。
- `main` 保留原版**交易程式**；此 ROADMAP 是文件型變更，不代表合併 Phase 0 程式修改。
- Phase 0 工作分支：[`audit/phase0-baseline`](https://github.com/lspss93119/entropy-arb/tree/audit/phase0-baseline)，已實作 P0-01，HEAD `61a1787f209ed7b29798c6d8e0774f8a0aeca6da`。
- [原版基準 CI：29 passed](https://github.com/lspss93119/entropy-arb/actions/runs/38836752414)；[P0-01 最新 CI：31 passed](https://github.com/lspss93119/entropy-arb/actions/runs/38837123890)。
- **無已授權的合併或實盤部署。** 舊 Bot 已停止。GitHub 的測試通過不等於獲准實盤。

## 3. 開發順序（僅批准研究方向，不代表提前批准實作）

| ID / Phase | 範圍 | 目前狀態 | 追蹤 |
|---|---|---|---|
| P0-01 | 雙腿預估名義金額限制 | 已實作、審查與 CI PASS；尚待合併決策 | [#1](https://github.com/lspss93119/entropy-arb/issues/1) |
| P0-02 | 訂單簿時間戳、連線存活與行情有效性 | 獨立分支已實作，40 tests PASS；**Draft PR #4 待 RH 唯讀驗證，未合併部署** | [#2](https://github.com/lspss93119/entropy-arb/issues/2) · [PR #4](https://github.com/lspss93119/entropy-arb/pull/4) |
| P0-03 | 不確定成交、部分成交與雙腿恢復 | 尚未研究 | 待安排 |
| P0-04 | 訊號、下單與實際成交滑點／延遲歸因 | 尚未研究 | 待安排 |
| Phase 1 | 動態進出場門檻（以原版交易語義為基準） | 尚未研究／實作 | 待安排 |
| Phase 2 | 動態滑點管理 | 尚未研究／實作 | 待安排 |
| Phase 3 | 程式與網路延遲改善 | 尚未研究／實作 | 待安排 |
| Phase 4 | 多交易所套利擴展 | 尚未研究／實作 | 待安排 |

只有當前已選定的工作才進入實作；Phase 1–4 不預先搭建框架。高風險修改先針對性驗證，必要時在離線、唯讀或模擬環境測試。

## 4. 每項修改採用四步輕量流程

1. **研究及事前計畫**：查看目前程式碼、相關官方文件、已知問題或群組實例。提出「問題／影響檔案／最小修改／驗證方式／不做的事」，**使用者核可後**才動交易程式。
2. **最小修改**：獨立分支，一項工作只解決一項問題。不順手改策略或擴充架構。
3. **測試與審查**：原有測試、針對性回歸測試、完整 Diff 審查；行情或執行邏輯影響重大時追加必要整合與異常情境驗證。禁止僅調整測試答案來掩蓋意外行為。
4. **完成報告**：說明改動、理由、Commit、CI、相容性與殘餘風險，給出 PASS／NEEDS WORK／FAIL，決定是否可以提交合併審查。

**合併與部署分開核准。** 完成適用測試及審查後可提合併；涉及下單、倉位或成交恢復的版本，部署前須另做對應的整合驗證及必要的最小規模 Canary。部署有明確授權才執行。

## 5. 額外想法怎麼記錄

- **ROADMAP**：只放已確定的方向、優先順序與有證據的完成狀態。
- **GitHub Issues**：每項想法或問題一張 Issue，記錄來源、假設、預期價值、待研究事項及目前狀態。
- **Backlog 想法不能自動升級為任務**。需要用戶批准後，才能排進 ROADMAP 或實作。
- 研究出現其他 Bug／優化機會時：記入 Issue，**不打斷當前任務**。
- 已有範例：[延遲分位數自適應拒單研究 #3](https://github.com/lspss93119/entropy-arb/issues/3)（僅 Backlog）。

Issue 文內使用簡單狀態詞：`Backlog`、`Research`、`Approved`、`In progress`、`Review`、`Done`、`Deferred`；不必另建多套看板系統。

## 6. 開新對話時先確認這三項

1. 先讀這份 `docs/ROADMAP.md`，確定範圍及核准狀態。
2. 再看與當前工作相關的 GitHub Issue、當前分支／Commit、測試結果。
3. 根據**GitHub 實際狀態**開始，不依賴 ChatGPT 記憶認定「已完成、已部署、已同意」。

計畫變更需更新本文，並清楚記錄變更原因。實作報告優先放在對應 Issue / PR，避免文件無限制膨脹。
