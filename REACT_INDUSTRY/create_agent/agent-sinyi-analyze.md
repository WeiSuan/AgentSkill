# 信義不動產評論下載與分析代理規格

## 1. 目的

定期從信義房屋「信義不動產評論」頁面取得上傳日期晚於 2025-01-01 的 PDF 報告，保存原始檔案並以 Gemini AI 產生單份報告的結構化不動產市況摘要。每份解析結果都要留下來源、模型、token、估算費用與 PostgreSQL 紀錄，確保後續可重跑、查詢與稽核。

來源頁面：

`https://www.sinyinews.com.tw/real_estate_review`

## 2. 檔案與目錄

新增兩支 Python 程式：

- `sinyi_DownloadRealEstateReviews.py`：解析網頁並增量下載 PDF。
- `sinyi_AnalyzeRealEstateReviews.py`：以 Gemini 解析 PDF、輸出 JSON 並同步 PostgreSQL。

資料夾結構：

```text
信義不動產評論/
  Original/
    <期數名稱>.pdf
    .metadata.json
  Result/
    <期數名稱>.json
```

註記：`Original/.metadata.json` 是下載狀態與來源紀錄檔，保存期數、上傳日期、來源 URL、檔案雜湊與下載時間。下載器用它判斷哪些 PDF 可跳過或需重新下載；分析器用它帶入報告來源與日期至 JSON、PostgreSQL。它不存放 PDF 內容。

範例檔名：

```text
信義不動產評論/Original/2026年第一季 - 信義不動產評論.pdf
信義不動產評論/Result/2026年第一季 - 信義不動產評論.json
```

## 3. 下載規格

1. 從頁面初始 HTML 表格擷取上傳日期、期數名稱、檔案大小與 PDF 連結。
2. 僅處理上傳日期嚴格晚於 `2025-01-01` 的資料；以實際上傳日為準，不以報告期數推論。
3. 下載網址必須取自頁面實際 `href`，不得自行組合下載用的 hash 或 UUID。
4. PDF 下載後須驗證 HTTP 成功、檔案大小大於零與 `%PDF-` 檔頭。
5. 先下載到 `.part` 暫存檔，驗證成功後再原子取代正式 PDF。
6. `.metadata.json` 至少記錄期數、上傳日期、來源 URL、來源檔案 UUID、SHA256、檔案大小與下載時間。
7. 重跑時，若 metadata、來源 URL、SHA256、實體檔案與 PDF 檔頭皆有效，應跳過；若檔案缺少、損壞或 URL 變更，才重新下載。
8. 建議 CLI：

```powershell
python sinyi_DownloadRealEstateReviews.py --all
python sinyi_DownloadRealEstateReviews.py --period "2026年第一季 - 信義不動產評論"
python sinyi_DownloadRealEstateReviews.py --all --dry-run
python sinyi_DownloadRealEstateReviews.py --all --force
```

## 4. Gemini 解析規格

模型預設為 `models/gemini-3.5-flash-lite`，從 `.env` 載入 `GEMINI_API_KEY`，並沿用既有 `usage_logger.log_usage()` 寫入 `logs/gemini-usage.jsonl`。

每份 PDF 直接以 Gemini Files API 作為附件上傳。因 Windows 路徑和來源檔名可能包含中文，程式需先以 ASCII 暫存檔名複製後上傳，避免 multipart HTTP header 編碼錯誤。

模型只能根據附件 PDF 內容輸出 JSON。建議結構：

```json
{
  "source": "信義不動產評論",
  "report_date": "2026-05-11",
  "industry": "不動產",
  "current_market_status": "持平",
  "future_market_status": "好",
  "key_summary": "不超過 500 個中文字元的報告重點，以及支持當前與未來市況結論的說明。"
}
```

規則：

1. `source` 固定為 `信義不動產評論`。
2. `industry` 固定為 `不動產`。
3. `current_market_status` 與 `future_market_status` 僅可為 `好`、`壞`、`持平`。
4. `report_date` 優先採 PDF 或頁面上清楚標示的報告資料日；資料不足時不得自行推測。
5. `key_summary` 最多 500 個中文字元，需摘要報告重點及市況判斷的證據。
6. 程式需在寫入前驗證 JSON、必填欄位、三態列舉值與摘要長度。

分析 JSON 另應補入可稽核 metadata：來源 PDF、PDF SHA256、來源 URL、上傳日期、模型、prompt 版本與產生時間。

建議 CLI：

```powershell
python sinyi_AnalyzeRealEstateReviews.py --pdf "信義不動產評論/Original/2026年第一季 - 信義不動產評論.pdf"
python sinyi_AnalyzeRealEstateReviews.py --all
python sinyi_AnalyzeRealEstateReviews.py --all --dry-run
python sinyi_AnalyzeRealEstateReviews.py --all --force
```

## 5. PostgreSQL 留存規格

新增下列資料表：

### `sinyi_real_estate_review_runs`

每份 PDF 的分析執行紀錄。至少包含：

- `report_id`：UUID 主鍵。
- `report_period`、`report_date`、`pdf_path`、`pdf_sha256`。
- `source_url`、`source_file_uuid`。
- `model_name`、`prompt_version`。
- `input_tokens`、`output_tokens`、`thinking_tokens`、`total_tokens`、`estimated_cost`。
- `status`、`error_message`、`analyzed_at`。

以 `(pdf_sha256, prompt_version)` 建立唯一鍵，讓同一份檔案與規格重跑時可重用成功結果。

### `sinyi_real_estate_review_analyses`

保存解析後的市場結論。至少包含：

- `report_id`：關聯執行表。
- `source`、`report_date`、`industry`。
- `current_market_status`、`future_market_status`。
- `key_summary`。
- `analysis_json`：原始結構化 JSON。

PDF 分析成功後才進行同一個資料庫 transaction 的 UPSERT。JSON 檔已成功寫出但資料庫同步失敗時，必須保留 JSON 並在執行摘要標示同步失敗。

## 6. 重跑與錯誤策略

1. 下載與分析都必須冪等，可定期安全重跑。
2. 下載器只補缺少、損壞或來源異動的 PDF。
3. 分析器預設只處理尚無成功 JSON 或資料庫成功 run 的 PDF；`--force` 才重新解析。
4. 模型回應不合法、附件上傳失敗或資料庫失敗時，不得留下半成品 JSON，也不得將 run 標記為成功。
5. API key、密碼、完整 token 與連線字串不得寫入 JSON、Markdown 或日誌。
6. 費用由 `GEMINI_INPUT_PRICE_PER_MILLION` 與 `GEMINI_OUTPUT_PRICE_PER_MILLION` 計算；未設定時仍記錄 token，但費用為 0。

## 7. Playwright 與網站取得方式

目前資料頁的上傳日期、期數與 PDF href 已存在初始 HTML，可用 Python 標準庫 `urllib.request` 與 `HTMLParser` 取得，不必仰賴 JavaScript 或瀏覽器點擊。

目前聊天工作階段未提供可用的 Playwright MCP 工具，因此尚未驗證 Playwright MCP 是否正常運作。若網站未來改為 JavaScript 動態載入，再以 Playwright 作為替代方案。

## 8. 文件自動更新

兩支程式每次執行完成或失敗後，都必須更新本文件的「最近執行摘要」區塊，不得修改其他規格內容或人工補充文字。

摘要至少包含：

- 執行時間、腳本名稱與命令模式。
- 發現、下載、跳過與失敗筆數。
- 分析、JSON 寫入與 PostgreSQL 同步狀態。
- 已處理期數。
- Gemini 模型、token 與估算費用。
- 最近錯誤或待辦。

## 9. 測試與驗證

新增 focused pytest，至少涵蓋：

- HTML 表格解析、日期篩選與 PDF URL 正規化。
- 中文期數的合法檔名轉換、metadata、SHA256 與增量 skip。
- PDF 損壞後僅該檔重抓。
- Gemini JSON schema、三態市況、500 字限制與 ASCII 暫存附件上傳。
- JSON 原子寫入、Gemini token 記錄與 PostgreSQL UPSERT 冪等性。
- Agent 文件機器摘要更新且不覆蓋人工內容。

建議實作驗證順序：

```powershell
python sinyi_DownloadRealEstateReviews.py --all --dry-run
python sinyi_DownloadRealEstateReviews.py --period "2026年第一季 - 信義不動產評論"
python sinyi_AnalyzeRealEstateReviews.py --pdf "信義不動產評論/Original/2026年第一季 - 信義不動產評論.pdf"
pytest -q
```

<!-- RECENT_RUN_SUMMARY_START -->
## 最近執行摘要

- 執行結果：成功
- 下載狀態：發現 6、下載 6、跳過 0、失敗 0 筆。
- 分析狀態：6 份 JSON 已完成；季度日期已正規化為 PostgreSQL DATE。
- 資料庫同步：6 份分析結果已完成 UPSERT。
- Gemini 模型：models/gemini-3.5-flash-lite；費用估算因價格環境變數未設定為 0。
- 待辦：後續重跑可使用 --all，已存在 JSON 會重用；需要重新分析時使用 --force。
<!-- RECENT_RUN_SUMMARY_END -->
