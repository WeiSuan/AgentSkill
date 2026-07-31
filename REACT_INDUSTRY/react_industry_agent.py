from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

from industry_repository import (
    IndustryAssetResult,
    IndustryCatalogItem,
    IndustryRepository,
    database_connection_string,
    load_dotenv,
)
from usage_logger import log_usage


BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
USAGE_LOG_PATH = LOG_DIR / "gemini-usage.jsonl"
EVENT_LOG_PATH = LOG_DIR / "agent-events.jsonl"
DEFAULT_MODEL = "models/gemini-3.5-flash-lite"
QUERY_SYNONYMS = {
    "小吃": ("小吃", "餐食", "餐館", "早餐", "便當", "外燴", "飲料"),
    "營造": ("營造", "營建", "建築", "土木", "工程"),
    "不動產": ("不動產", "租售", "經紀", "開發", "代銷", "營造", "建築"),
    "水產": ("水產", "漁業", "養殖", "魚", "批發", "零售"),
}


def append_event(event: str, **fields: Any) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    with EVENT_LOG_PATH.open("a", encoding="utf-8") as event_log:
        event_log.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def normalize_model_name(model_name: str) -> str:
    model_name = model_name.strip()
    if not model_name:
        return DEFAULT_MODEL
    if model_name.startswith("models/"):
        return model_name
    return f"models/{model_name.lower()}"


def display_label(item: IndustryCatalogItem) -> str:
    return item.labels[0] if item.labels else f"產業代碼 {item.code}"


def catalog_prompt(catalog: list[IndustryCatalogItem]) -> str:
    lines = []
    for item in catalog:
        labels = "、".join(item.labels[:8]) or "無中文標籤"
        lines.append(f"- {item.code}: {labels}")
    return "\n".join(lines)


def relevant_catalog(
    catalog: list[IndustryCatalogItem], prompt: str
) -> list[IndustryCatalogItem]:
    terms = [term for key, values in QUERY_SYNONYMS.items() if key in prompt for term in values]
    if not terms:
        return catalog

    scored_items = []
    for item in catalog:
        labels = " ".join(item.labels)
        score = sum(labels.count(term) for term in terms)
        if score:
            scored_items.append((score, item.code, item))

    if not scored_items:
        return catalog
    return [item for _, _, item in sorted(scored_items, key=lambda value: (-value[0], value[1]))[:16]]


def extract_function_call(response: Any) -> list[str] | None:
    function_calls = getattr(response, "function_calls", None) or []
    for function_call in function_calls:
        if function_call.name != "analyze_industries":
            continue
        values = function_call.args.get("head4_industry_codes", [])
        if not isinstance(values, list):
            return None
        return [str(value).zfill(4) for value in values]
    return None


def select_industry_codes(
    client: genai.Client,
    model_name: str,
    prompt: str,
    catalog: list[IndustryCatalogItem],
) -> list[str]:
    catalog_codes = {item.code for item in catalog}
    prompt_catalog = relevant_catalog(catalog, prompt)
    tool = types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="analyze_industries",
                description="選出使用者問題中所有對應的固定四碼產業代碼。",
                parameters_json_schema={
                    "type": "object",
                    "properties": {
                        "head4_industry_codes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                        }
                    },
                    "required": ["head4_industry_codes"],
                },
            )
        ]
    )
    instruction = """
你是產業分類助手。只能使用下列固定產業目錄選出使用者提到的所有產業。
不得自行發明代碼、不得回答一般文字，也不得跳過函式呼叫。只呼叫 analyze_industries。

固定產業目錄：
{catalog}

使用者問題：{prompt}
""".format(catalog=catalog_prompt(prompt_catalog), prompt=prompt)

    for attempt in range(1, 3):
        response = client.models.generate_content(
            model=model_name,
            contents=instruction,
            config=types.GenerateContentConfig(tools=[tool], temperature=1),
        )
        log_usage(
            f"industry_selection_attempt_{attempt}",
            response,
            model_name=model_name,
            usage_log_path=USAGE_LOG_PATH,
            input_price_per_million=float(os.environ.get("GEMINI_INPUT_PRICE_PER_MILLION", "0")),
            output_price_per_million=float(os.environ.get("GEMINI_OUTPUT_PRICE_PER_MILLION", "0")),
        )
        selected_codes = extract_function_call(response)
        append_event(
            "model_response",
            attempt=attempt,
            function_called=selected_codes is not None,
            selected_codes=selected_codes or [],
        )
        if selected_codes is not None:
            invalid_codes = sorted(set(selected_codes) - catalog_codes)
            if invalid_codes:
                raise ValueError(f"模型選出不存在的產業代碼: {', '.join(invalid_codes)}")
            return list(dict.fromkeys(selected_codes))

        instruction += "\n再次提醒：本回合必須呼叫 analyze_industries，不能輸出文字。"

    raise RuntimeError("模型未呼叫必要的 analyze_industries 工具。")


def format_observation(
    results: list[IndustryAssetResult], catalog: list[IndustryCatalogItem]
) -> list[str]:
    labels = {item.code: display_label(item) for item in catalog}
    lines = []
    for result in results:
        label = labels.get(result.code, f"產業代碼 {result.code}")
        if not result.has_data or result.percentage is None:
            lines.append(f"{label}產業，擷取有問題")
            continue
        percentage = result.percentage.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        amount = result.asset_amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        lines.append(f"{label}產業，整體資產規模 {amount:,.0f}，占公司總資產 {percentage}%")
    return lines


def run_turn(
    client: genai.Client,
    model_name: str,
    repository: IndustryRepository,
    prompt: str,
) -> list[str]:
    catalog = repository.get_catalog()
    if not catalog:
        raise RuntimeError("產業目錄沒有可用的 head4_industry_code。")

    append_event("prompt", prompt=prompt)
    selected_codes = select_industry_codes(client, model_name, prompt, catalog)
    labels = {item.code: display_label(item) for item in catalog}
    thought = "、".join(f"{labels[code]} ({code})" for code in selected_codes)
    print(f"Thought: 已依固定產業標籤判定 {thought}")
    print(f"Action: analyze_industries(head4_industry_codes={selected_codes})")
    append_event("function_call", function="analyze_industries", head4_industry_codes=selected_codes)

    try:
        results = repository.analyze_industries(selected_codes)
    except Exception as exc:
        append_event("tool_error", error_type=type(exc).__name__)
        raise RuntimeError("PostgreSQL 產業資產工具執行失敗。") from exc

    observations = format_observation(results, catalog)
    append_event("tool_result", result_count=len(results), successful_codes=[result.code for result in results if result.has_data])
    print("Observation: 已完成 PostgreSQL 產業資產工具查詢")
    return observations


def main() -> None:
    load_dotenv(BASE_DIR / ".env")
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError(".env 缺少 GEMINI_API_KEY。")

    LOG_DIR.mkdir(exist_ok=True)
    model_name = normalize_model_name(os.environ.get("GEMINI_MODEL", DEFAULT_MODEL))
    client = genai.Client(api_key=api_key)
    repository = IndustryRepository(database_connection_string())
    print("產業資產 ReAct Agent 已啟動；輸入 exit 結束。")

    while True:
        try:
            prompt = input("\n問題> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if prompt.lower() in {"exit", "quit"}:
            break
        if not prompt:
            continue
        try:
            for observation in run_turn(client, model_name, repository, prompt):
                print(observation)
        except Exception as exc:
            append_event("turn_error", error_type=type(exc).__name__)
            print(f"處理失敗：{exc}")


if __name__ == "__main__":
    main()