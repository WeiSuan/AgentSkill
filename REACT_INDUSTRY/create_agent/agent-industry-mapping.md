# 台經院產業景氣對應代理規格

## 1. 目的

將台經院景氣動向 JSON 中出現的產業名稱，對應至主計處四碼產業代碼主表。對應以主表為唯一合法代碼來源，允許一對多與多對多關係，並保存 Gemini AI 判定依據與執行版本。景氣狀態不屬於本程式職責，應由後續程式依名稱對照結果另行處理。

正式輸入：

- 主表：`115年主計處產業代碼(四碼).xlsx`
- 主表欄位：`head4_industry_code`、`head4_industry_name`
- 來源輸入：`台經院景氣動向/Result/台經院_景氣動向_YYYYMM.json`；由 `analysis.industry_outlook[*].industries[*]` 解析產業名稱
- 對照輸出：`台經院景氣動向/IndustryMapping/台經院_產業名稱對照.json`

## 2. 對應原則

1. 產業代碼只能使用主表中存在的四碼代碼；程式必須在寫入前驗證。
2. 主表每一個唯一的 `(head4_industry_code, head4_industry_name)` 組合都必須出現在輸出；一個主表產業可對應零到多個台經院產業，一個台經院產業也可對應多個主表產業。
3. 僅在具產業關聯時建立對應；不相關時不得強制配對。
4. Gemini 結果自動採用，但每筆結果均須保留信心度與中文理由(最多50字)，供後續抽查。
5. 無法對應的主表產業不得刪除；該主表項目仍須保留，使用空的 `matches`、`match_status: "unmatched"` 與未匹配原因(最多50字)註記。
6. 對照的主鍵為 `(head4_industry_code, head4_industry_name)`；每個 `matches` 內的唯一鍵為 `(head4_industry_code, head4_industry_name, tier_industry_name)`。同一名稱跨月份出現時，應重用既有有效對照，不因景氣狀態重複呼叫 Gemini。

## 3. 來源解析與產業彙整

程式應讀取每個輸入 JSON 的 `analysis.industry_outlook` 陣列，逐項取出 `industries` 陣列中的名稱。例如：

```json
{
  "ct_status": "壞",
  "ft_in6mon": "持平",
  "industries": ["塑橡膠原料", "化學製品", "塑膠製品", "金屬工具機業"]
}
```

應將所有月份、所有 `industry_outlook` 項目的 `industries` 攤平、去除空白值與重複名稱，建立一張唯一產業名稱清單。`ct_status` 與 `ft_in6mon` 僅用於原始景氣資料，既不寫入本對照表，也不作為 Gemini 對照請求的輸入。

接著以主表的每個 `head4_industry_code`、`head4_industry_name` 為輸出基準，與彙整後的 `tier_industry_name` 清單比對。完全同名可直接建立對照；其餘主表項目交由 Gemini 判定零到多個台經院名稱。即使 Gemini 判定零個名稱，該主表項目仍須保留並註記未匹配原因。

## 4. 對照表輸出規格

對照表 JSON 應包含下列結構。`mappings` 必須保留主表全部唯一的代碼＋名稱組合；目前主表為 764 筆、747 個唯一名稱。無法對應時仍保留主表項目，`matches` 為空陣列：

```json
{
  "source_jsons": ["台經院景氣動向/Result/台經院_景氣動向_202607.json"],
  "generated_at": "2026-08-05T00:00:00+00:00",
  "mapping_model": "models/gemini-3.5-flash-lite",
  "catalog_sha256": "...",
  "run_id": "uuid",
  "mappings": [
    {
      "head4_industry_code": "2209",
      "head4_industry_name": "其他塑膠製品製造業",
      "match_status": "matched",
      "matches": [{
        "tier_industry_name": "塑膠製品",
        "match_type": "涵蓋",
        "confidence": 0.91,
        "reason": "台經院的塑膠製品分類涵蓋此四碼產業。"
      }],
      "unmatched_reason": ""
    },
    {
      "head4_industry_code": "0111",
      "head4_industry_name": "稻作栽培業",
      "match_status": "unmatched",
      "matches": [],
      "unmatched_reason": "沒有足夠關聯的台經院產業名稱。"
    }
  ],
  "summary": {
    "source_industry_count": 67,
    "master_industry_count": 764,
    "master_industry_name_count": 747,
    "mapped_master_count": 0,
    "unmatched_master_count": 0,
    "mapping_count": 0,
    "low_confidence_count": 0
  }
}
```

`confidence` 範圍為 $0$ 至 $1$。建議將低於 $0.70$ 的資料標記為低信心，仍保存與同步資料庫，但納入人工抽查清單。

## 5. Gemini 分類設計

新程式建議命名為 `tier_MapTierIndustries.py`，重用既有 `tier_AnalyzeTIERForecast.py` 的 Gemini 結構化 JSON 輸出與 `usage_logger.log_usage()` 用量記錄模式。程式只將解析與彙整後的唯一產業名稱納入規劃，排除已在對照表中且主表雜湊未變的名稱。

Gemini 請求內容應包含：

- 固定且版本化的對應規則。
- 一批台經院產業名稱。
- 官方四碼產業代碼與名稱清單。
- 要求每個主表項目回傳零到多個合法台經院名稱。
- 每筆配對的 `match_type`、`confidence` 和簡短中文 `reason`。
- 明確要求不得杜撰名稱；找不到適當產業時回傳空名稱陣列及未匹配理由。

程式端必須驗證 Gemini 回傳的每個主表代碼＋名稱皆存在於請求批次、每個台經院名稱皆存在於來源清單，並驗證批次項目全部回傳。刪除重複的 `(head4_industry_code, head4_industry_name, tier_industry_name)` 組合。模型回應不合法或遺漏主表項目時，該批次須明確失敗並記錄事件，不得寫入不完整正式對照檔案。

## 6. 執行與重跑策略

建議提供以下 CLI：

```powershell
python tier_MapTierIndustries.py --month 202607
python tier_MapTierIndustries.py --all
python tier_MapTierIndustries.py --input-json "台經院景氣動向/Result/台經院_景氣動向_202607.json" --dry-run
python tier_MapTierIndustries.py --all --force
```

參數說明：

- `--month YYYYMM`：從指定月份輸入檔擷取產業名稱並更新對照表。
- `--all`：從所有月度輸入 JSON 擷取產業名稱並更新對照表。
- `--input-json PATH`：從指定 JSON 擷取產業名稱並更新對照表。
- `--dry-run`：完成載入、驗證與請求規劃，但不呼叫 Gemini、不寫檔、不寫入資料庫。
- `--force`：忽略既有名稱對照快取，重新呼叫 Gemini 並建立新的 run。
- `--model`：覆寫預設 Gemini 模型。

預設重跑時，若產業名稱已存在於成功對照表，且主表雜湊與提示詞版本一致，應重用既有結果，避免重複 API 成本。主表或提示詞版本變更時才重新評估既有名稱。正式檔案應採原子寫入；先寫暫存檔、完成完整性驗證後再取代對照檔。

## 7. PostgreSQL 留存規格

資料庫應保留歷史執行版本，不只保存當月最新結果。建議建立兩張表：

### `tier_industry_mapping_runs`

- `run_id`：主鍵，UUID。
- `source_jsons`：本次擷取名稱的來源 JSON 路徑清單。
- `catalog_sha256`。
- `model_name`、`prompt_version`。
- `generated_at`、`status`、`mapping_count`。
- `output_path`：對照表 JSON 路徑。

### `tier_industry_mapping_details`

- `run_id`：關聯至執行表。
- `head4_industry_code`、`head4_industry_name`。
- `tier_industry_name`：從台經院 JSON 的 `industries` 陣列解析出的產業名稱，位於 `matches` 內。
- `match_type`、`confidence`、`reason`。
- `is_unmatched_master`：主表產業無匹配時為真。

以 `run_id`、主表代碼、主表名稱與台經院名稱建立唯一鍵；未匹配主表項目的台經院名稱為 NULL，並以 `is_unmatched_master` 註記。採用 transaction 與 upsert。檔案完成驗證後才開始資料庫交易；交易失敗時應讓該次 run 標記為失敗，避免檔案與資料庫狀態被誤認為一致。

查詢預設可使用最新成功 run，但不得刪除舊 run，確保模型、提示詞或主表更新後可以回溯差異。

## 8. 文件自動更新

`tier_MapTierIndustries.py` 應提供 `update_mapping_documentation()`，每次成功或失敗執行後只更新本文件的「最近執行摘要」區塊，不覆蓋其餘規格與人工補充內容。

摘要至少列出：處理的來源檔數、run ID、執行結果、模型、輸出檔、主表代碼＋名稱筆數、主表唯一名稱數、已對應主表筆數、未匹配主表筆數、對應關係筆數、低信心筆數，以及資料庫同步結果。

## 9. 測試與驗證

新增 `tests/test_tier_industry_mapping.py`，至少涵蓋：

- 主表四碼代碼正規化與白名單驗證。
- `analysis.industry_outlook[*].industries[*]` 的跨月份攤平、去空值與去重。
- 一對多、多對多與零對應情境。
- 相同台經院名稱出現在不同月份或景氣狀態時僅產生一組對照的行為。
- Gemini 回傳不存在的主表項目或台經院名稱時拒絕寫入。
- 快取命中、`--force` 重跑與原子檔案寫入。
- PostgreSQL upsert 冪等與交易失敗處理。
- 輸出完整保留每一個主表代碼＋名稱組合，未匹配項目以空 `matches` 與原因註記。

建議驗證順序：

```powershell
pytest -xvs tests/test_tier_industry_mapping.py
python tier_MapTierIndustries.py --input-json "台經院景氣動向/Result/台經院_景氣動向_202607.json" --dry-run
python tier_MapTierIndustries.py --month 202607
pytest -xvs
```

## 10. 注意事項

- 台經院產業名稱每月可能改變，禁止建立僅靠固定字串字典的唯一對應；字典可作為輔助，但輸入 JSON 必須重新解析以發現新名稱。
- 產業名稱存在上位、下位與跨分類情形，因此應保存 `match_type` 與理由，不可只存單一代碼。
- API 金鑰只透過 `.env` 讀取，不寫入輸出、日誌或文件。
- Gemini 的模型版本、提示詞版本與主表雜湊是結果可重現與比對的必要 metadata。
- 景氣狀態應由獨立程式讀取月度 JSON 後，依 `tier_industry_name` 查詢本對照表並展開至 `head4_industry_code`；該流程不應呼叫 Gemini。
- 正式使用前應對低信心與高影響產業進行人工抽樣覆核，並以新 run 保存修正後結果，不直接覆蓋歷史資料。

<!-- RECENT_RUN_SUMMARY_START -->
## 最近執行摘要

- 執行結果：成功
- Run ID：b60e4e58-7d91-4baf-a235-e7cee8d0ca3d
- 模型：models/gemini-3.5-flash-lite
- 輸出檔：台經院景氣動向/IndustryMapping/台經院_產業名稱對照.json
- 來源檔數：12
- 主表代碼＋名稱筆數：764
- 主表唯一名稱數：747
- 已對應主表筆數：522
- 未匹配主表筆數：242
- 對應關係筆數：628
- 低信心筆數：68
- 資料庫同步：成功
<!-- RECENT_RUN_SUMMARY_END -->
