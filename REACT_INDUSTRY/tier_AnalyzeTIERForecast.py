"""Analyze one TIER forecast PDF with Gemini structured output."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

from industry_repository import load_dotenv
from usage_logger import log_usage


BASE_DIR = Path(__file__).resolve().parent
ORIGINAL_DIR = BASE_DIR / "台經院景氣動向" / "Original"
RESULT_DIR = BASE_DIR / "台經院景氣動向" / "Result"
USAGE_LOG_PATH = BASE_DIR / "logs" / "gemini-usage.jsonl"
DEFAULT_MODEL = "models/gemini-3.5-flash-lite"

ANALYSIS_SCHEMA = {
    "type": "object",
    "required": ["industry_outlook"],
    "properties": {
        "industry_outlook": {
            "type": "array",
            "description": "依當月景氣與未來半年預期分組的產業前景",
            "items": {
                "type": "object",
                "required": [
                    "ct_status",
                    "ft_in6mon",
                    "industries",
                ],
                "properties": {
                    "ct_status": {"type": "string", "enum": ["好", "壞", "持平"]},
                    "ft_in6mon": {"type": "string", "enum": ["好", "壞", "持平"]},
                    "industries": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        }
    },
}

ANALYSIS_PROMPT = """根據附件解析台經院「綜合分析判斷」後對各個產業未來半年預期。

只能根據附件 PDF 作答，不得使用外部知識或自行推測。請依「當月景氣」及「未來半年預期」
組合分組：相同看法的所有產業必須合併至同一筆的 industries 陣列；每個組合只能出現一次。
ct_status 與 ft_in6mon 僅可為好、壞或持平。請嚴格依照指定 JSON schema 輸出，不要輸出
Markdown 或其他文字。
"""


def select_pdf(pdf_argument: str | None, source_dir: Path = ORIGINAL_DIR) -> Path:
    if pdf_argument:
        candidate = Path(pdf_argument)
        if not candidate.is_absolute():
            candidate = BASE_DIR / candidate
    else:
        candidates = sorted(source_dir.glob("*.pdf"))
        if not candidates:
            raise FileNotFoundError(f"找不到 PDF：{source_dir}")
        candidate = candidates[-1]

    if candidate.suffix.lower() != ".pdf":
        raise ValueError(f"來源檔案必須是 PDF：{candidate}")
    if not candidate.is_file():
        raise FileNotFoundError(f"找不到 PDF：{candidate}")
    with candidate.open("rb") as pdf_file:
        header = pdf_file.read(5)
    if header != b"%PDF-":
        raise ValueError(f"來源檔案不是有效 PDF：{candidate}")
    return candidate


def select_pdfs(
    pdf_argument: str | None,
    all_pdfs: bool,
    source_dir: Path = ORIGINAL_DIR,
) -> list[Path]:
    if not all_pdfs:
        return [select_pdf(pdf_argument, source_dir)]

    candidates = sorted(source_dir.glob("*.pdf"))
    if not candidates:
        raise FileNotFoundError(f"找不到 PDF：{source_dir}")
    return [select_pdf(str(candidate)) for candidate in candidates]


def model_name_from_environment() -> str:
    model_name = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL).strip()
    if not model_name:
        return DEFAULT_MODEL
    return model_name if model_name.startswith("models/") else f"models/{model_name}"


def price_from_environment(name: str) -> float:
    return float(os.environ.get(name, "0"))


def parse_response_json(response: Any) -> dict[str, Any]:
    text = getattr(response, "text", None)
    if not text:
        raise ValueError("Gemini 回應沒有文字內容")
    parsed = json.loads(text)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("industry_outlook"), list):
        raise ValueError("Gemini 回應不符合預期 JSON 結構")
    return parsed


def analyze_pdf(
    pdf_path: Path,
    *,
    client: Any,
    model_name: str,
    result_dir: Path = RESULT_DIR,
    usage_log_path: Path = USAGE_LOG_PATH,
) -> Path:
    with tempfile.TemporaryDirectory(prefix="tier_pdf_") as temporary_dir:
        ascii_pdf_path = Path(temporary_dir) / "source.pdf"
        shutil.copyfile(pdf_path, ascii_pdf_path)
        uploaded_file = client.files.upload(
            file=str(ascii_pdf_path),
            config={"mime_type": "application/pdf"},
        )
    response = client.models.generate_content(
        model=model_name,
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part.from_uri(
                        file_uri=uploaded_file.uri,
                        mime_type=uploaded_file.mime_type or "application/pdf",
                    ),
                    types.Part.from_text(text=ANALYSIS_PROMPT),
                ],
            )
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ANALYSIS_SCHEMA,
            max_output_tokens=10000,
            thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
        ),
    )
    usage_log_path.parent.mkdir(parents=True, exist_ok=True)
    log_usage(
        "tier_pdf_analysis",
        response,
        model_name=model_name,
        usage_log_path=usage_log_path,
        input_price_per_million=price_from_environment("GEMINI_INPUT_PRICE_PER_MILLION"),
        output_price_per_million=price_from_environment("GEMINI_OUTPUT_PRICE_PER_MILLION"),
    )
    analysis = parse_response_json(response)
    result = {
        "source_pdf": pdf_path.name,
        "model": model_name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "analysis": analysis,
    }
    result_dir.mkdir(parents=True, exist_ok=True)
    output_path = result_dir / f"{pdf_path.stem}.json"
    temporary_path = output_path.with_suffix(".json.tmp")
    temporary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_path.replace(output_path)
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument("--pdf", help="PDF 路徑；未指定時使用 Original 中最新檔案")
    input_group.add_argument("--all", action="store_true", help="解析 Original 中所有 PDF")
    return parser.parse_args()


def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError(".env 缺少 GEMINI_API_KEY。")

    args = parse_args()
    pdf_paths = select_pdfs(args.pdf, args.all)
    model_name = model_name_from_environment()
    USAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    client = genai.Client(api_key=api_key)
    for pdf_path in pdf_paths:
        output_path = analyze_pdf(pdf_path, client=client, model_name=model_name)
        print(f"完成：{output_path}")


if __name__ == "__main__":
    main()