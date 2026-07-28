import os
import json
from datetime import datetime, timezone
from pathlib import Path

from urllib.parse import urlencode
from urllib.request import urlopen

from google import genai
from google.genai import types

API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    raise RuntimeError("請設定 GEMINI_API_KEY 環境變數。")

MODEL_NAME = "gemini-3.5-flash-lite"
client = genai.Client(api_key=API_KEY)
USAGE_LOG_PATH = Path("gemini_usage.jsonl")
INPUT_PRICE_PER_MILLION_USD = 1.50
OUTPUT_PRICE_PER_MILLION_USD = 9.00


def createModel(prompt: str):
    """使用 Gemini 3.5 Flash 產生內容。"""
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=TOOLS,
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.AUTO,
                    # 強制使用 add_numbers 時，改回以下兩行：
                    # mode=types.FunctionCallingConfigMode.ANY,
                    # allowed_function_names=["add_numbers"],
                )
            ),
        ),
    )
    log_usage(response)
    return response


def log_usage(response) -> None:
    """將 Gemini 回應的 token 用量與估算費用追加至本機 JSONL 紀錄。"""
    usage = response.usage_metadata
    if usage is None:
        return

    input_tokens = usage.prompt_token_count or 0
    output_tokens = usage.candidates_token_count or 0
    estimated_cost_usd = (
        input_tokens * INPUT_PRICE_PER_MILLION_USD
        + output_tokens * OUTPUT_PRICE_PER_MILLION_USD
    ) / 1_000_000
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": MODEL_NAME,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thinking_tokens": usage.thoughts_token_count or 0,
        "total_tokens": usage.total_token_count or 0,
        "estimated_cost_usd": round(estimated_cost_usd, 10),
    }

    with USAGE_LOG_PATH.open("a", encoding="utf-8") as usage_log:
        usage_log.write(json.dumps(record) + "\n")
    print(
        "Token usage: "
        f"input={input_tokens}, output={output_tokens}, "
        f"estimated=${estimated_cost_usd:.8f}"
    )


def add_numbers(a: float, b: float) -> float:
    # """
    # 將兩個數字相加並回傳結果

    # Args:
    #     a: 第一個數字。
    #     b: 第二個數字。

    # Returns:
    #     float: 兩個數字的和。
    # """
    print(f"[系統端] 正在執行工具：計算 {a} + {b}")
    return a + b


def get_weather(city: str) -> dict[str, str | float]:
    """
    查詢指定城市的目前天氣。

    使用 Open-Meteo 的公開 API，不需要 API 金鑰。

    Args:
        city: 要查詢的城市名稱，例如 "Taipei"。

    Returns:
        包含城市名稱、溫度、體感溫度與天氣代碼的字典。

    Raises:
        ValueError: 找不到指定城市時拋出。
    """
    print(f"[系統端] 正在執行工具：查詢 {city} 的目前天氣")

    geocoding_query = urlencode({"name": city, "count": 1, "language": "zh"})
    with urlopen(
        f"https://geocoding-api.open-meteo.com/v1/search?{geocoding_query}",
        timeout=10,
    ) as response:
        location_data = json.load(response)

    results = location_data.get("results", [])
    if not results:
        raise ValueError(f"找不到城市：{city}")

    location = results[0]
    weather_query = urlencode(
        {
            "latitude": location["latitude"],
            "longitude": location["longitude"],
            "current": "temperature_2m,apparent_temperature,weather_code",
            "timezone": "auto",
        }
    )
    with urlopen(
        f"https://api.open-meteo.com/v1/forecast?{weather_query}", timeout=10
    ) as response:
        weather_data = json.load(response)

    current = weather_data["current"]
    return {
        "city": location["name"],
        "temperature_celsius": current["temperature_2m"],
        "apparent_temperature_celsius": current["apparent_temperature"],
        "weather_code": current["weather_code"],
    }


TOOLS = [add_numbers, get_weather]


if __name__ == "__main__":
    response = createModel("計算15+5是多少")
    print("Gemini 回應結果 (Function  Calls):")
    print(response.function_calls)

    print("Gemini 回應結果 (Text):")
    print(response.text)