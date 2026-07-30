import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from google import genai
from google.genai import types

from usage_logger import log_usage

MODEL_NAME = "gemini-3.5-flash-lite"
USAGE_LOG_PATH = Path("react_agent_usage.jsonl")
INPUT_PRICE_PER_MILLION_USD = 0.30
OUTPUT_PRICE_PER_MILLION_USD = 2.50
MAX_AGENT_STEPS = 5
QUESTION = "請針對台積電近期財報進行分析並彙整30字總結說明"


def stock_price() -> str:
    """取得台積電目前股價。需要分析台積電股價時使用。"""
    return "台積電股價1000元"


def get_information() -> str:
    """取得台積電近期營運與財報相關資訊。需要分析營收展望時使用。"""
    return "預計26年營收持續加倍成長"


TOOLS = [stock_price, get_information]
TOOL_FUNCTIONS = {
    "stock_price": stock_price,
    "get_information": get_information,
}
REQUIRED_TOOLS = set(TOOL_FUNCTIONS)


def generate_agent_response(
    client: genai.Client,
    history: list[types.Content],
    *,
    step: int,
    available_tools: list,
) -> object:
    """請 Gemini 規劃下一步，並由 Python 接管工具呼叫。"""
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=history,
        config=types.GenerateContentConfig(
            system_instruction=(
                "你是 ReAct 財報分析 agent。僅使用 observation 中已取得的資訊，"
                "不可捏造真實財報數字。需要資料時，先簡短輸出『公開規劃：』說明要查什麼，"
                "再呼叫對應 function tool。當仍有未取得資料的相關可用工具時，"
                "必須在同一個回應中一次呼叫所有這些工具，不要一次只呼叫一個。"
                "分析此問題必須至少使用 stock_price 和 get_information 各一次。"
                "兩個 observation 都具備後，停止呼叫工具，"
                "並以繁體中文輸出不超過 30 字的最終結論，且只輸出結論本身。"
            ),
            tools=available_tools,
            tool_config=types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(
                    mode=types.FunctionCallingConfigMode.AUTO,
                )
            ),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        ),
    )
    log_usage(
        f"agent_step_{step}",
        response,
        model_name=MODEL_NAME,
        usage_log_path=USAGE_LOG_PATH,
        input_price_per_million=INPUT_PRICE_PER_MILLION_USD,
        output_price_per_million=OUTPUT_PRICE_PER_MILLION_USD,
    )
    return response


def get_model_content(response: object) -> types.Content:
    """取得回應中的模型內容，供後續 function response 建立對話歷史。"""
    candidates = getattr(response, "candidates", None) or []
    if not candidates or candidates[0].content is None:
        raise RuntimeError("Gemini 沒有回傳可用的模型內容。")
    return candidates[0].content


def get_public_plan(response: object) -> str:
    """顯示模型公開輸出的簡短規劃，不要求或揭露內部思考。"""
    model_content = get_model_content(response)
    text = "".join(
        part.text or ""
        for part in model_content.parts
        if getattr(part, "text", None)
    ).strip()
    return text or "模型未提供文字規劃，以下依 function call 執行。"


def execute_tool(tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
    """執行一個零參數工具並回傳可寫入 observation 的資料。"""
    if arguments:
        return {"error": f"{tool_name} 不接受參數：{arguments}"}
    return {"result": TOOL_FUNCTIONS[tool_name]()}


def parse_arguments() -> argparse.Namespace:
    """讀取用於模擬工具缺失的測試參數。"""
    parser = argparse.ArgumentParser(description="Gemini ReAct agent 範例")
    parser.add_argument(
        "--remove_function",
        choices=sorted(TOOL_FUNCTIONS),
        help="測試時不提供指定工具給 Gemini。",
    )
    return parser.parse_args()


def main() -> None:
    arguments = parse_arguments()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("請設定 GEMINI_API_KEY 環境變數。")

    client = genai.Client(api_key=api_key)
    history = [types.Content(role="user", parts=[types.Part.from_text(text=QUESTION)])]
    executed_tools: set[str] = set()
    final_answer = ""
    withheld_tool = arguments.remove_function

    print(f"問題：{QUESTION}")
    if withheld_tool:
        print(f"測試模式：不提供 {withheld_tool} 給 Gemini。")
    print("=" * 60)

    for step in range(1, MAX_AGENT_STEPS + 1):
        missing_tool_names = REQUIRED_TOOLS - executed_tools
        if missing_tool_names and all(
            tool_name == withheld_tool or tool_name not in TOOL_FUNCTIONS
            for tool_name in missing_tool_names
        ):
            missing_tools = "、".join(sorted(missing_tool_names))
            print(f"\n資料不足：未取得工具結果：{missing_tools}。")
            final_answer = "資料不足：找不到相關即時新聞，無法完成完整財報分析。"
            break

        available_tools = [
            tool
            for tool in TOOLS
            if tool.__name__ != withheld_tool and tool.__name__ not in executed_tools
        ]
        available_tool_names = {tool.__name__ for tool in available_tools}
        print(f"\n[Loop {step}] Thought")
        response = generate_agent_response(
            client,
            history,
            step=step,
            available_tools=available_tools,
        )
        function_calls = response.function_calls or []
        print(get_public_plan(response))

        if not function_calls:
            if REQUIRED_TOOLS.issubset(executed_tools):
                final_answer = (response.text or "").strip()
                break

            missing_tools = "、".join(sorted(REQUIRED_TOOLS - executed_tools))
            print(f"[Loop {step}] Observation: 尚缺少工具結果：{missing_tools}")
            history.extend(
                [
                    get_model_content(response),
                    types.Content(
                        role="user",
                        parts=[
                            types.Part.from_text(
                                text=f"請繼續呼叫尚未使用的工具：{missing_tools}。"
                            )
                        ],
                    ),
                ]
            )
            continue

        print(f"[Loop {step}] Action")
        observations: list[types.Part] = []
        pending_calls = {}
        for function_call in function_calls:
            tool_name = function_call.name
            arguments = dict(function_call.args)
            if tool_name not in available_tool_names:
                observation = {
                    "error": f"{tool_name} 目前不可用，請改用已提供的工具。"
                }
            elif tool_name not in TOOL_FUNCTIONS:
                observation = {"error": f"不支援的工具：{tool_name}"}
            elif tool_name in pending_calls:
                observation = {
                    "skipped": True,
                    "reason": f"{tool_name} 已在本輪呼叫，避免重複執行。",
                }
            else:
                pending_calls[tool_name] = arguments
                continue

            print(f"- {tool_name}({arguments})")
            print(f"[Loop {step}] Observation: {json.dumps(observation, ensure_ascii=False)}")
            observations.append(
                types.Part.from_function_response(name=tool_name, response=observation)
            )

        if pending_calls:
            with ThreadPoolExecutor(max_workers=len(pending_calls)) as executor:
                futures = {
                    tool_name: executor.submit(execute_tool, tool_name, arguments)
                    for tool_name, arguments in pending_calls.items()
                }
                for tool_name, arguments in pending_calls.items():
                    observation = futures[tool_name].result()
                    if "result" in observation:
                        executed_tools.add(tool_name)

                    print(f"- {tool_name}({arguments})")
                    print(
                        f"[Loop {step}] Observation: "
                        f"{json.dumps(observation, ensure_ascii=False)}"
                    )
                    observations.append(
                        types.Part.from_function_response(
                            name=tool_name,
                            response=observation,
                        )
                    )

        history.extend(
            [
                get_model_content(response),
                types.Content(role="user", parts=observations),
            ]
        )
    else:
        missing_tools = "、".join(sorted(REQUIRED_TOOLS - executed_tools))
        print(f"\n資料不足：未取得工具結果：{missing_tools}。")
        final_answer = "資料不足：找不到相關即時新聞，無法完成完整財報分析。"

    if not final_answer:
        raise RuntimeError("Agent 未產生最終結論。")

    print("\n" + "=" * 60)
    print("最終結論：")
    print(final_answer)


if __name__ == "__main__":
    main()