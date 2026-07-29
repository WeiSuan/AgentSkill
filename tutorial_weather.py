import os
import json
from pathlib import Path

from urllib.parse import urlencode
from urllib.request import urlopen

from google import genai
from google.genai import types

from usage_logger import log_usage

API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    raise RuntimeError("請設定 GEMINI_API_KEY 環境變數。")

MODEL_NAME = "gemini-3.5-flash-lite"
client = genai.Client(api_key=API_KEY)
USAGE_LOG_PATH = Path("gemini_usage.jsonl")
INPUT_PRICE_PER_MILLION_USD = 0.30
OUTPUT_PRICE_PER_MILLION_USD = 2.50


def createModel(prompt: str):
    """讓 Gemini 自動選擇並執行可用工具。"""
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=TOOLS,
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.AUTO,
                )
            ),
        ),
    )
    log_usage(
        "generate_content",
        response,
        model_name=MODEL_NAME,
        usage_log_path=USAGE_LOG_PATH,
        input_price_per_million=INPUT_PRICE_PER_MILLION_USD,
        output_price_per_million=OUTPUT_PRICE_PER_MILLION_USD,
    )
    return response


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


TAIWAN_CITIES = {
    "基隆市": (25.1283, 121.7419),
    "臺北市": (25.0330, 121.5654),
    "新北市": (25.0173, 121.4628),
    "桃園市": (24.9937, 121.3010),
    "新竹市": (24.8138, 120.9675),
    "新竹縣": (24.8387, 121.0177),
    "苗栗縣": (24.5602, 120.8214),
    "臺中市": (24.1477, 120.6736),
    "彰化縣": (24.0756, 120.5440),
    "南投縣": (23.9609, 120.9719),
    "雲林縣": (23.7092, 120.4313),
    "嘉義市": (23.4801, 120.4491),
    "嘉義縣": (23.4518, 120.2555),
    "臺南市": (22.9997, 120.2270),
    "高雄市": (22.6273, 120.3014),
    "屏東縣": (22.5519, 120.5488),
    "宜蘭縣": (24.7021, 121.7378),
    "花蓮縣": (23.9911, 121.6112),
    "臺東縣": (22.7554, 121.1500),
    "澎湖縣": (23.5712, 119.5793),
    "金門縣": (24.4327, 118.3171),
    "連江縣": (26.1605, 119.9499),
}


WEATHER_DESCRIPTIONS = {
    0: "晴朗",
    1: "大致晴朗",
    2: "局部多雲",
    3: "陰天",
    45: "有霧",
    48: "霧淞",
    51: "毛毛雨",
    53: "毛毛雨",
    55: "強毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    80: "陣雨",
    81: "中度陣雨",
    82: "強陣雨",
    95: "雷雨",
}


def get_weather(city: str) -> dict[str, str | float]:
    """取得指定台灣縣市當日目前天氣與最高、最低溫。"""
    normalized_city = city.strip().replace("台", "臺")
    coordinates = TAIWAN_CITIES.get(normalized_city)
    if coordinates is None:
        available_cities = "、".join(TAIWAN_CITIES)
        raise ValueError(f"找不到城市：{city}。可查詢：{available_cities}")

    print(f"[系統端] 正在執行工具：查詢 {normalized_city} 天氣")
    weather_query = urlencode(
        {
            "latitude": coordinates[0],
            "longitude": coordinates[1],
            "current": "temperature_2m,weather_code",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min",
            "timezone": "Asia/Taipei",
        }
    )

    with urlopen(
        f"https://api.open-meteo.com/v1/forecast?{weather_query}", timeout=20
    ) as response:
        weather_data = json.load(response)

    return {
        "city": normalized_city,
        "date": weather_data["daily"]["time"][0],
        "current_temperature_celsius": weather_data["current"]["temperature_2m"],
        "minimum_temperature_celsius": weather_data["daily"]["temperature_2m_min"][0],
        "maximum_temperature_celsius": weather_data["daily"]["temperature_2m_max"][0],
        "weather": WEATHER_DESCRIPTIONS.get(
            weather_data["current"]["weather_code"], "未知"
        ),
    }


TOOLS = [add_numbers, get_weather]


if __name__ == "__main__":
    test_prompts = [
        "請計算 15.5 加 20.2。",
        "請查詢臺北市今天的天氣。",
    ]
    for prompt in test_prompts:
        print(f"\n使用者問題：{prompt}")
        response = createModel(prompt)
        print("Gemini 回應結果：")
        print(response.text)
        print("工具呼叫歷程：")
        print(response.automatic_function_calling_history)