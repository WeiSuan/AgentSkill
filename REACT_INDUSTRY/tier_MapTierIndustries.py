"""Map every official four-digit industry to zero or more TIER names."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import psycopg
from google import genai
from google.genai import types

from industry_repository import IndustryCatalogItem, IndustryRepository, database_connection_string, load_dotenv
from tier_AnalyzeTIERForecast import DEFAULT_MODEL, model_name_from_environment, price_from_environment
from usage_logger import log_usage

BASE_DIR = Path(__file__).resolve().parent
RESULT_DIR = BASE_DIR / "台經院景氣動向" / "Result"
OUTPUT_PATH = BASE_DIR / "台經院景氣動向" / "IndustryMapping" / "台經院_產業名稱對照.json"
CATALOG_PATH = BASE_DIR / "115年主計處產業代碼(四碼).xlsx"
USAGE_LOG_PATH = BASE_DIR / "logs" / "gemini-usage.jsonl"
DOCUMENT_PATH = BASE_DIR / "create_agent" / "agent-industry-mapping.md"
PROMPT_VERSION = "tier-industry-mapping-master-centric-v2"
DEFAULT_BATCH_SIZE = 25

MAPPING_SCHEMA = {
    "type": "object",
    "required": ["results"],
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["head4_industry_code", "head4_industry_name", "matches", "unmatched_reason"],
                "properties": {
                    "head4_industry_code": {"type": "string"},
                    "head4_industry_name": {"type": "string"},
                    "matches": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["tier_industry_name", "match_type", "confidence", "reason"],
                            "properties": {
                                "tier_industry_name": {"type": "string"},
                                "match_type": {"type": "string"},
                                "confidence": {"type": "number"},
                                "reason": {"type": "string"},
                            },
                        },
                    },
                    "unmatched_reason": {"type": "string"},
                },
            },
        }
    },
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_tier_industry_names(payload: dict[str, Any]) -> set[str]:
    try:
        groups = payload["analysis"]["industry_outlook"]
    except (KeyError, TypeError) as error:
        raise ValueError("來源 JSON 缺少 analysis.industry_outlook") from error
    if not isinstance(groups, list):
        raise ValueError("analysis.industry_outlook 必須是陣列")
    names: set[str] = set()
    for group in groups:
        industries = group.get("industries") if isinstance(group, dict) else None
        if not isinstance(industries, list):
            raise ValueError("industry_outlook 項目缺少 industries 陣列")
        for value in industries:
            if not isinstance(value, str):
                raise ValueError("industries 僅能包含字串")
            if name := value.strip():
                names.add(name)
    return names


def load_tier_names(paths: Iterable[Path]) -> tuple[set[str], list[str]]:
    names: set[str] = set()
    source_jsons: list[str] = []
    for path in sorted(set(paths)):
        if not path.is_file():
            raise FileNotFoundError(f"找不到來源 JSON：{path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError(f"來源 JSON 格式無效：{path}") from error
        if not isinstance(payload, dict):
            raise ValueError(f"來源 JSON 根物件必須是物件：{path}")
        names.update(extract_tier_industry_names(payload))
        try:
            source_jsons.append(path.relative_to(BASE_DIR).as_posix())
        except ValueError:
            source_jsons.append(str(path))
    return names, source_jsons


def catalog_entries(items: Iterable[IndustryCatalogItem]) -> list[dict[str, str]]:
    return [
        {"head4_industry_code": item.code, "head4_industry_name": label}
        for item in items
        for label in item.labels
    ]


def catalog_by_code_name(entries: Iterable[dict[str, str]]) -> dict[tuple[str, str], dict[str, str]]:
    return {(entry["head4_industry_code"], entry["head4_industry_name"]): entry for entry in entries}


def split_batches(entries: list[dict[str, str]], batch_size: int = DEFAULT_BATCH_SIZE) -> list[list[dict[str, str]]]:
    if batch_size < 1:
        raise ValueError("batch_size 必須大於 0")
    ordered = sorted(entries, key=lambda item: (item["head4_industry_code"], item["head4_industry_name"]))
    return [ordered[index:index + batch_size] for index in range(0, len(ordered), batch_size)]


def exact_mappings(entries: Iterable[dict[str, str]], tier_names: set[str]) -> list[dict[str, Any]]:
    results = []
    for entry in entries:
        name = entry["head4_industry_name"]
        matches = []
        if name in tier_names:
            matches.append({
                "tier_industry_name": name,
                "match_type": "同名",
                "confidence": 1.0,
                "reason": "台經院與主表產業名稱完全相同。",
            })
        results.append({
            **entry,
            "matches": matches,
            "match_status": "matched" if matches else "unmatched",
            "unmatched_reason": "" if matches else "尚待產業語意對照。",
        })
    return results


def build_mapping_prompt(batch: list[dict[str, str]], tier_names: list[str]) -> str:
    return f"""你是台灣產業分類對照專家。現在以主計處四碼產業主表為主，為每個主表項目尋找零到多個台經院產業名稱。
規則版本：{PROMPT_VERSION}。
你必須逐一回傳輸入的每個 head4_industry_code；不得遺漏、合併或刪除任何主表項目。
只能從提供的台經院產業名稱清單選取 tier_industry_name，不得創造新名稱。
有產業關聯才建立 matches；沒有足夠關聯時 matches 為空陣列，並提供不超過 50 字的 unmatched_reason。
每筆配對須提供 match_type、0 到 1 的 confidence，以及不超過 50 字的中文 reason。

主表項目：{json.dumps(batch, ensure_ascii=False)}
台經院產業名稱清單：{json.dumps(tier_names, ensure_ascii=False)}
"""


def normalize_model_results(response_text: str, requested_entries: list[dict[str, str]], tier_names: set[str]) -> list[dict[str, Any]]:
    try:
        results = json.loads(response_text)["results"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError("Gemini 回應不符合預期 JSON 結構") from error
    if not isinstance(results, list):
        raise ValueError("Gemini 回應 results 必須是陣列")

    requested = catalog_by_code_name(requested_entries)
    returned: set[tuple[str, str]] = set()
    normalized: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, dict):
            raise ValueError("Gemini results 項目必須是物件")
        code = str(result.get("head4_industry_code", "")).zfill(4)
        name = result.get("head4_industry_name")
        key = (code, name) if isinstance(name, str) else (code, "")
        if key not in requested or key in returned:
            raise ValueError("Gemini 回應包含未要求或重複的主表項目")
        matches = result.get("matches")
        if not isinstance(matches, list):
            raise ValueError("Gemini matches 必須是陣列")
        returned.add(key)
        normalized_matches: list[dict[str, Any]] = []
        seen_tier_names: set[str] = set()
        for match in matches:
            if not isinstance(match, dict):
                raise ValueError("Gemini matches 項目必須是物件")
            tier_name = match.get("tier_industry_name")
            confidence = match.get("confidence")
            match_type = match.get("match_type")
            reason = match.get("reason")
            if not isinstance(tier_name, str) or tier_name not in tier_names:
                raise ValueError(f"Gemini 回傳不存在的台經院產業名稱：{tier_name}")
            if tier_name in seen_tier_names:
                continue
            if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
                raise ValueError("Gemini confidence 必須介於 0 與 1")
            if not isinstance(match_type, str) or not match_type.strip():
                raise ValueError("Gemini match_type 不可空白")
            if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 50:
                raise ValueError("Gemini reason 必須為 1 到 50 字")
            seen_tier_names.add(tier_name)
            normalized_matches.append({
                "tier_industry_name": tier_name,
                "match_type": match_type.strip(),
                "confidence": float(confidence),
                "reason": reason.strip(),
            })
        unmatched_reason = result.get("unmatched_reason", "")
        if not normalized_matches:
            if not isinstance(unmatched_reason, str) or not unmatched_reason.strip() or len(unmatched_reason.strip()) > 50:
                raise ValueError("Gemini 未匹配原因必須為 1 到 50 字")
            unmatched_reason = unmatched_reason.strip()
        else:
            unmatched_reason = ""
        normalized.append({
            **requested[key],
            "matches": normalized_matches,
            "match_status": "matched" if normalized_matches else "unmatched",
            "unmatched_reason": unmatched_reason,
        })
    if returned != set(requested):
        missing = ", ".join(f"{code}/{name}" for code, name in sorted(set(requested) - returned))
        raise ValueError(f"Gemini 回應遺漏主表項目：{missing}")
    return normalized


def request_model_mappings(entries: list[dict[str, str]], tier_names: set[str], *, client: Any, model_name: str, usage_log_path: Path) -> list[dict[str, Any]]:
    all_results: list[dict[str, Any]] = []
    batch_size = int(os.environ.get("TIER_MAPPING_BATCH_SIZE", DEFAULT_BATCH_SIZE))
    for batch in split_batches(entries, batch_size):
        prompt = build_mapping_prompt(batch, sorted(tier_names))
        last_error: ValueError | None = None
        for attempt in range(2):
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=MAPPING_SCHEMA,
                    thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
                ),
            )
            if not getattr(response, "text", None):
                raise ValueError("Gemini 回應沒有文字內容")
            usage_log_path.parent.mkdir(parents=True, exist_ok=True)
            log_usage(
                "tier_industry_mapping", response, model_name=model_name, usage_log_path=usage_log_path,
                input_price_per_million=price_from_environment("GEMINI_INPUT_PRICE_PER_MILLION"),
                output_price_per_million=price_from_environment("GEMINI_OUTPUT_PRICE_PER_MILLION"),
            )
            try:
                all_results.extend(normalize_model_results(response.text, batch, tier_names))
                break
            except ValueError as error:
                last_error = error
                if attempt == 0:
                    prompt += f"\n上一回應驗證失敗：{error}。請重新輸出同一批完整結果；只能逐字使用清單中的名稱。"
        else:
            raise last_error or ValueError("Gemini 回應驗證失敗")
    return all_results


def load_cache(path: Path, *, catalog_hash: str, force: bool) -> tuple[list[dict[str, Any]], list[str]]:
    if force or not path.is_file():
        return [], []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"既有對照檔格式無效：{path}") from error
    if not isinstance(payload, dict) or payload.get("catalog_sha256") != catalog_hash or payload.get("prompt_version") != PROMPT_VERSION:
        return [], []
    mappings = payload.get("mappings")
    source_jsons = payload.get("source_jsons", [])
    if not isinstance(mappings, list) or not isinstance(source_jsons, list):
        raise ValueError(f"既有對照檔缺少必要內容：{path}")
    return mappings, [value for value in source_jsons if isinstance(value, str)]


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        json.dump(payload, temporary, ensure_ascii=False, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def build_output(source_names: set[str], source_jsons: list[str], catalog_hash: str, model_name: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    by_key = {(entry["head4_industry_code"], entry["head4_industry_name"]): entry for entry in entries}
    mappings = [by_key[key] for key in sorted(by_key)]
    low_confidence_count = sum(match["confidence"] < 0.70 for entry in mappings for match in entry["matches"])
    unmatched_count = sum(entry["match_status"] == "unmatched" for entry in mappings)
    return {
        "source_jsons": sorted(set(source_jsons)),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mapping_model": model_name,
        "prompt_version": PROMPT_VERSION,
        "catalog_sha256": catalog_hash,
        "run_id": str(uuid.uuid4()),
        "mappings": mappings,
        "summary": {
            "source_industry_count": len(source_names),
            "master_industry_count": len(mappings),
            "master_industry_name_count": len({entry["head4_industry_name"] for entry in mappings}),
            "mapped_master_count": len(mappings) - unmatched_count,
            "unmatched_master_count": unmatched_count,
            "mapping_count": sum(len(entry["matches"]) for entry in mappings),
            "low_confidence_count": low_confidence_count,
        },
    }


def sync_to_postgres(connection_string: str, output: dict[str, Any], output_path: Path) -> None:
    with psycopg.connect(connection_string) as connection:
        with connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tier_industry_mapping_runs (
                    run_id UUID PRIMARY KEY, source_jsons JSONB NOT NULL, catalog_sha256 TEXT NOT NULL,
                    model_name TEXT NOT NULL, prompt_version TEXT NOT NULL, generated_at TIMESTAMPTZ NOT NULL,
                    status TEXT NOT NULL, mapping_count INTEGER NOT NULL, output_path TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tier_industry_mapping_details (
                    run_id UUID NOT NULL REFERENCES tier_industry_mapping_runs(run_id),
                    head4_industry_code TEXT NOT NULL, head4_industry_name TEXT NOT NULL,
                    tier_industry_name TEXT, match_type TEXT, confidence DOUBLE PRECISION,
                    reason TEXT NOT NULL, is_unmatched_master BOOLEAN NOT NULL DEFAULT FALSE,
                    UNIQUE (run_id, head4_industry_code, head4_industry_name, tier_industry_name)
                )
            """)
            cursor.execute("""
                ALTER TABLE tier_industry_mapping_details
                ADD COLUMN IF NOT EXISTS is_unmatched_master BOOLEAN NOT NULL DEFAULT FALSE
            """)
            cursor.execute("""
                ALTER TABLE tier_industry_mapping_details
                ALTER COLUMN tier_industry_name DROP NOT NULL
            """)
            cursor.execute("""
                ALTER TABLE tier_industry_mapping_details
                DROP CONSTRAINT IF EXISTS tier_industry_mapping_details_run_id_head4_industry_code_ti_key
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS tier_industry_mapping_details_master_tier_key
                ON tier_industry_mapping_details
                (run_id, head4_industry_code, head4_industry_name, tier_industry_name)
            """)
            cursor.execute(
                """INSERT INTO tier_industry_mapping_runs
                   (run_id, source_jsons, catalog_sha256, model_name, prompt_version, generated_at, status, mapping_count, output_path)
                   VALUES (%s, %s::jsonb, %s, %s, %s, %s, 'success', %s, %s)""",
                (output["run_id"], json.dumps(output["source_jsons"]), output["catalog_sha256"], output["mapping_model"], PROMPT_VERSION, output["generated_at"], output["summary"]["mapping_count"], str(output_path)),
            )
            for entry in output["mappings"]:
                if not entry["matches"]:
                    cursor.execute(
                        """INSERT INTO tier_industry_mapping_details
                           (run_id, head4_industry_code, head4_industry_name, reason, is_unmatched_master)
                           VALUES (%s, %s, %s, %s, TRUE)""",
                        (output["run_id"], entry["head4_industry_code"], entry["head4_industry_name"], entry["unmatched_reason"]),
                    )
                for match in entry["matches"]:
                    tier_industry_name = match["tier_industry_name"].strip()
                    cursor.execute(
                        """INSERT INTO tier_industry_mapping_details
                           (run_id, head4_industry_code, head4_industry_name, tier_industry_name, match_type, confidence, reason)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                        (output["run_id"], entry["head4_industry_code"], entry["head4_industry_name"], tier_industry_name, match["match_type"], match["confidence"], match["reason"]),
                    )
            cursor.execute(
                """SELECT head4_industry_code, head4_industry_name, tier_industry_name,
                          confidence, is_unmatched_master
                   FROM tier_industry_mapping_details
                   WHERE run_id = %s""",
                (output["run_id"],),
            )
            stored_rows = {
                (code, name, tier_name, confidence, is_unmatched)
                for code, name, tier_name, confidence, is_unmatched in cursor.fetchall()
            }
            expected_rows = {
                (entry["head4_industry_code"], entry["head4_industry_name"], None, None, True)
                for entry in output["mappings"]
                if not entry["matches"]
            }
            expected_rows.update(
                (entry["head4_industry_code"], entry["head4_industry_name"], match["tier_industry_name"].strip(), match["confidence"], False)
                for entry in output["mappings"]
                for match in entry["matches"]
            )
            if stored_rows != expected_rows:
                missing = expected_rows - stored_rows
                extra = stored_rows - expected_rows
                raise RuntimeError(f"PostgreSQL 對照留存不完整：missing={len(missing)}, extra={len(extra)}")


def update_mapping_documentation(*, output: dict[str, Any] | None, status: str, database_status: str, error: str | None = None, document_path: Path = DOCUMENT_PATH) -> None:
    if not document_path.is_file():
        return
    if output:
        summary = output["summary"]
        body = (
            "## 最近執行摘要\n\n"
            f"- 執行結果：{status}\n- Run ID：{output['run_id']}\n- 模型：{output['mapping_model']}\n"
            f"- 輸出檔：{OUTPUT_PATH.relative_to(BASE_DIR).as_posix()}\n- 來源檔數：{len(output['source_jsons'])}\n"
            f"- 主表代碼＋名稱筆數：{summary['master_industry_count']}\n- 主表唯一名稱數：{summary['master_industry_name_count']}\n"
            f"- 已對應主表筆數：{summary['mapped_master_count']}\n- 未匹配主表筆數：{summary['unmatched_master_count']}\n"
            f"- 對應關係筆數：{summary['mapping_count']}\n- 低信心筆數：{summary['low_confidence_count']}\n"
            f"- 資料庫同步：{database_status}\n"
        )
    else:
        body = f"## 最近執行摘要\n\n- 執行結果：{status}\n- 原因：{error or '未知錯誤'}\n- 資料庫同步：未執行\n"
    content = document_path.read_text(encoding="utf-8")
    updated, count = re.subn(r"## 最近執行摘要\n.*?(?=<!-- RECENT_RUN_SUMMARY_END -->)", body, content, count=1, flags=re.DOTALL)
    if count:
        document_path.write_text(updated, encoding="utf-8")


def select_source_paths(args: argparse.Namespace, result_dir: Path = RESULT_DIR) -> list[Path]:
    if args.input_json:
        return [Path(args.input_json)]
    if args.month:
        if not re.fullmatch(r"\d{6}", args.month):
            raise ValueError("--month 必須為 YYYYMM")
        return [result_dir / f"台經院_景氣動向_{args.month}.json"]
    paths = sorted(result_dir.glob("台經院_景氣動向_*.json"))
    if not paths:
        raise FileNotFoundError(f"找不到來源 JSON：{result_dir}")
    return paths


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--month", help="處理指定 YYYYMM 月份")
    inputs.add_argument("--all", action="store_true", help="處理所有月度 JSON")
    inputs.add_argument("--input-json", help="處理指定 JSON")
    parser.add_argument("--dry-run", action="store_true", help="僅載入、驗證與規劃，不呼叫 Gemini、不寫入")
    parser.add_argument("--force", action="store_true", help="忽略既有對照快取")
    parser.add_argument("--model", help="覆寫預設 Gemini 模型")
    return parser.parse_args()


def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    output: dict[str, Any] | None = None
    try:
        args = parse_args()
        source_names, source_jsons = load_tier_names(select_source_paths(args))
        catalog_items = IndustryRepository("unused-for-catalog", CATALOG_PATH).get_catalog()
        catalog = catalog_entries(catalog_items)
        catalog_keys = {(entry["head4_industry_code"], entry["head4_industry_name"]) for entry in catalog}
        catalog_hash = sha256_file(CATALOG_PATH)
        cached, cached_sources = load_cache(OUTPUT_PATH, catalog_hash=catalog_hash, force=args.force)
        cached_by_key = {
            (entry.get("head4_industry_code"), entry.get("head4_industry_name")): entry
            for entry in cached
            if isinstance(entry, dict) and (entry.get("head4_industry_code"), entry.get("head4_industry_name")) in catalog_keys
        }
        pending_entries = [entry for entry in catalog if (entry["head4_industry_code"], entry["head4_industry_name"]) not in cached_by_key]
        exact_entries = exact_mappings(pending_entries, source_names)
        exact_keys = {(entry["head4_industry_code"], entry["head4_industry_name"]) for entry in exact_entries if entry["matches"]}
        model_entries = [entry for entry in pending_entries if (entry["head4_industry_code"], entry["head4_industry_name"]) not in exact_keys]
        print(f"解析到 {len(source_names)} 個台經院產業名稱；主表 {len(catalog)} 筆；快取 {len(cached_by_key)} 筆；同名 {len(exact_keys)} 筆；待 Gemini 判定 {len(model_entries)} 筆。")
        if args.dry_run:
            return
        model_name = args.model or model_name_from_environment() or DEFAULT_MODEL
        generated: list[dict[str, Any]] = []
        if model_entries:
            api_key = os.environ.get("GEMINI_API_KEY")
            if not api_key:
                raise ValueError(".env 缺少 GEMINI_API_KEY。")
            generated = request_model_mappings(model_entries, source_names, client=genai.Client(api_key=api_key), model_name=model_name, usage_log_path=USAGE_LOG_PATH)
        output = build_output(source_names, [*cached_sources, *source_jsons], catalog_hash, model_name, [*cached_by_key.values(), *exact_entries, *generated])
        atomic_write_json(OUTPUT_PATH, output)
        database_status = "未設定，略過"
        if all(os.environ.get(key) for key in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")):
            sync_to_postgres(database_connection_string(), output, OUTPUT_PATH)
            database_status = "成功"
        update_mapping_documentation(output=output, status="成功", database_status=database_status)
        print(f"完成：{OUTPUT_PATH}")
    except Exception as error:
        update_mapping_documentation(output=output, status="失敗", database_status="失敗", error=str(error))
        raise


if __name__ == "__main__":
    main()
