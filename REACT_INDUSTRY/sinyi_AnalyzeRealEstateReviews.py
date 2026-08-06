"""Analyze Sinyi real-estate review PDFs with Gemini structured output."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types
import psycopg

from industry_repository import database_connection_string, load_dotenv
from usage_logger import log_usage


BASE_DIR = Path(__file__).resolve().parent
ORIGINAL_DIR = BASE_DIR / "信義不動產評論" / "Original"
RESULT_DIR = BASE_DIR / "信義不動產評論" / "Result"
METADATA_PATH = ORIGINAL_DIR / ".metadata.json"
USAGE_LOG_PATH = BASE_DIR / "logs" / "gemini-usage.jsonl"
DEFAULT_MODEL = "models/gemini-3.5-flash-lite"
PROMPT_VERSION = "sinyi-real-estate-review-v1"

ANALYSIS_SCHEMA = {
    "type": "object",
    "required": ["source", "report_date", "industry", "current_market_status", "future_market_status", "key_summary"],
    "properties": {
        "source": {"type": "string"},
        "report_date": {"type": "string"},
        "industry": {"type": "string"},
        "current_market_status": {"type": "string", "enum": ["好", "壞", "持平"]},
        "future_market_status": {"type": "string", "enum": ["好", "壞", "持平"]},
        "key_summary": {"type": "string", "maxLength": 500},
    },
}

ANALYSIS_PROMPT = """請根據附件中的信義不動產評論 PDF，整理不動產產業景氣。
只能使用附件內容，不得使用外部知識或自行推測。請輸出指定 JSON：source 固定為「信義不動產評論」；
report_date 使用報告發布日期 YYYY-MM-DD；industry 固定為「不動產」；current_market_status 是目前市場狀態；
future_market_status 是報告對未來市場的判斷，兩者只能是好、壞或持平；key_summary 為 500 字以內繁體中文摘要。
不得輸出 Markdown 或其他文字。"""


def model_name_from_environment() -> str:
    value = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    return value if value.startswith("models/") else f"models/{value}"


def price_from_environment(name: str) -> float:
    return float(os.environ.get(name, "0"))


def select_pdf(pdf_argument: str | None, source_dir: Path = ORIGINAL_DIR) -> Path:
    candidate = Path(pdf_argument) if pdf_argument else sorted(source_dir.glob("*.pdf"))[-1]
    if not candidate.is_absolute():
        candidate = BASE_DIR / candidate
    if candidate.suffix.lower() != ".pdf" or not candidate.is_file():
        raise FileNotFoundError(f"找不到有效 PDF：{candidate}")
    if candidate.read_bytes()[:5] != b"%PDF-":
        raise ValueError(f"來源檔案不是有效 PDF：{candidate}")
    return candidate


def parse_response_json(response: Any) -> dict[str, Any]:
    text = getattr(response, "text", None)
    if not text:
        raise ValueError("Gemini 回應沒有文字內容")
    parsed = json.loads(text)
    required = set(ANALYSIS_SCHEMA["required"])
    if not isinstance(parsed, dict) or not required.issubset(parsed) or len(parsed["key_summary"]) > 500:
        raise ValueError("Gemini 回應不符合預期 JSON 結構")
    parsed["report_date"] = normalize_report_date(str(parsed["report_date"]))
    return parsed


def normalize_report_date(value: str) -> str:
    """Normalize ISO dates and quarter labels to a PostgreSQL-compatible date."""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        pass
    match = re.fullmatch(r"(\d{4})-Q([1-4])", value.strip(), re.IGNORECASE)
    if not match:
        raise ValueError(f"報告日期不是 YYYY-MM-DD 或 YYYY-Qn：{value}")
    year = int(match.group(1))
    quarter = int(match.group(2))
    month = quarter * 3
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    return (next_month - timedelta(days=1)).isoformat()


def report_id_for(pdf_path: Path, metadata_path: Path = METADATA_PATH) -> str:
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
    item = metadata.get(pdf_path.stem, {})
    source = str(item.get("sha256") or pdf_path.name)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"sinyi:{source}"))


def analyze_pdf(pdf_path: Path, *, client: Any, model_name: str, result_dir: Path = RESULT_DIR, usage_log_path: Path = USAGE_LOG_PATH) -> Path:
    with tempfile.TemporaryDirectory(prefix="sinyi_pdf_") as temporary_dir:
        ascii_pdf_path = Path(temporary_dir) / "source.pdf"
        shutil.copyfile(pdf_path, ascii_pdf_path)
        uploaded_file = client.files.upload(file=str(ascii_pdf_path), config={"mime_type": "application/pdf"})
    response = client.models.generate_content(
        model=model_name,
        contents=[types.Content(role="user", parts=[types.Part.from_uri(file_uri=uploaded_file.uri, mime_type=uploaded_file.mime_type or "application/pdf"), types.Part.from_text(text=ANALYSIS_PROMPT)])],
        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=ANALYSIS_SCHEMA, max_output_tokens=2000, thinking_config=types.ThinkingConfig(thinking_level="MINIMAL")),
    )
    usage_log_path.parent.mkdir(parents=True, exist_ok=True)
    log_usage("sinyi_pdf_analysis", response, model_name=model_name, usage_log_path=usage_log_path, input_price_per_million=price_from_environment("GEMINI_INPUT_PRICE_PER_MILLION"), output_price_per_million=price_from_environment("GEMINI_OUTPUT_PRICE_PER_MILLION"))
    analysis = parse_response_json(response)
    result = {"report_id": report_id_for(pdf_path), "source_pdf": pdf_path.name, "prompt_version": PROMPT_VERSION, "model": model_name, "generated_at": datetime.now(timezone.utc).isoformat(), "analysis": analysis}
    result_dir.mkdir(parents=True, exist_ok=True)
    output_path = result_dir / f"{pdf_path.stem}.json"
    temporary_path = output_path.with_suffix(".json.tmp")
    temporary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_path.replace(output_path)
    return output_path


def load_result(result_path: Path) -> dict[str, Any]:
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["analysis"]["report_date"] = normalize_report_date(str(result["analysis"]["report_date"]))
    return result


def write_result(result_path: Path, result: dict[str, Any]) -> None:
    temporary_path = result_path.with_suffix(".json.tmp")
    temporary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_path.replace(result_path)


def sync_to_postgres(connection_string: str, result_path: Path) -> None:
    result = json.loads(result_path.read_text(encoding="utf-8"))
    analysis = result["analysis"]
    with psycopg.connect(connection_string) as connection:
        with connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sinyi_real_estate_review_runs (
                    report_id UUID PRIMARY KEY, source_pdf TEXT NOT NULL, pdf_sha256 TEXT,
                    report_date DATE, model_name TEXT NOT NULL, prompt_version TEXT NOT NULL,
                    generated_at TIMESTAMPTZ NOT NULL, status TEXT NOT NULL, output_path TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sinyi_real_estate_review_analyses (
                    report_id UUID NOT NULL REFERENCES sinyi_real_estate_review_runs(report_id),
                    source TEXT NOT NULL, report_date DATE NOT NULL, industry TEXT NOT NULL, current_market_status TEXT NOT NULL,
                    future_market_status TEXT NOT NULL, key_summary TEXT NOT NULL,
                    analysis_json JSONB NOT NULL, PRIMARY KEY (report_id, source)
                )
            """)
            cursor.execute("ALTER TABLE sinyi_real_estate_review_analyses ADD COLUMN IF NOT EXISTS report_date DATE")
            cursor.execute("ALTER TABLE sinyi_real_estate_review_analyses ADD COLUMN IF NOT EXISTS analysis_json JSONB")
            cursor.execute("""
                INSERT INTO sinyi_real_estate_review_runs
                    (report_id, source_pdf, report_date, model_name, prompt_version, generated_at, status, output_path)
                VALUES (%s, %s, %s, %s, %s, %s, 'success', %s)
                ON CONFLICT (report_id) DO UPDATE SET
                    model_name = EXCLUDED.model_name, prompt_version = EXCLUDED.prompt_version,
                    generated_at = EXCLUDED.generated_at, status = EXCLUDED.status, output_path = EXCLUDED.output_path
            """, (result["report_id"], result["source_pdf"], analysis["report_date"], result["model"], result["prompt_version"], result["generated_at"], str(result_path)))
            cursor.execute("""
                INSERT INTO sinyi_real_estate_review_analyses
                    (report_id, source, report_date, industry, current_market_status, future_market_status, key_summary, analysis_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (report_id, source) DO UPDATE SET
                    report_date = EXCLUDED.report_date, industry = EXCLUDED.industry,
                    current_market_status = EXCLUDED.current_market_status,
                    future_market_status = EXCLUDED.future_market_status, key_summary = EXCLUDED.key_summary,
                    analysis_json = EXCLUDED.analysis_json
            """, (result["report_id"], analysis["source"], analysis["report_date"], analysis["industry"], analysis["current_market_status"], analysis["future_market_status"], analysis["key_summary"], json.dumps(analysis, ensure_ascii=False)))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--pdf")
    group.add_argument("--all", action="store_true")
    parser.add_argument("--force", action="store_true", help="忽略既有 JSON 並重新呼叫 Gemini")
    return parser.parse_args()


def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError(".env 缺少 GEMINI_API_KEY。")
    args = parse_args()
    paths = sorted(ORIGINAL_DIR.glob("*.pdf")) if args.all else [select_pdf(args.pdf)]
    client = genai.Client(api_key=api_key)
    for path in paths:
        result_path = RESULT_DIR / f"{path.stem}.json"
        if result_path.is_file() and not args.force:
            result = load_result(result_path)
            write_result(result_path, result)
        else:
            result_path = analyze_pdf(path, client=client, model_name=model_name_from_environment())
        if all(os.environ.get(key) for key in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")):
            sync_to_postgres(database_connection_string(), result_path)
        print(f"完成：{result_path}")


if __name__ == "__main__":
    main()