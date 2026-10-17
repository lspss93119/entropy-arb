# P0-02 真實 RH SNDK 唯讀量測

本工具不使用 API Key、無私有帳戶資料、不下單。直接使用 RH 官方公開 market metadata 解析精確 symbol=SNDK 的 market_id（不假設等於 Lighter Core）。

從 repo 根目錄且 Python 虛擬環境啟用後執行：

```bash
git switch research/p0-live-readonly-validation
git pull --ff-only
python tools/probe_rh_sndk.py --seconds 600
```

輸出包含來源時間戳 event_age_sec、每次更新的 receive_gap_sec、每秒 sampled_quote_age_sec，以及在 0 / 0.5 / 1 / 2 / 5 / 10 / 30 / 60 秒假設門檻下被拒絕的比例。0 表示停用來源報價年齡限制，不代表行情必定可交易；其他門檻使用 P0-02 的 -2 秒時鐘容忍與本地更新年齡條件。

**重要限制：** 每秒抽樣不是交易機會出現時的條件概率；不含 Entropy 對手腿、不含兩腿同時可交易性、交易所 session gate／實際下單回應；不保證目前實盤會觸發/不觸發。應區分美股正式交易時段、盤前盤後及休市，保留本機時間、時區及輸出，用不同時段同一工具比較。
