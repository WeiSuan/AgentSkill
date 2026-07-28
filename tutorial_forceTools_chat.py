import json
import os
from datetime import datetime, timezone
from pathlib import Path

from google import genai
from google.genai import types

API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    raise RuntimeError("請設定 GEMINI_API_KEY 環境變數。")

MODEL_NAME = "gemini-3.5-flash-lite"
USAGE_LOG_PATH = Path("gemini_chat_usage.jsonl")
INPUT_PRICE_PER_MILLION_USD = 0.30 * 32.5   # TWD
OUTPUT_PRICE_PER_MILLION_USD = 2.50 * 32.5  # TWD
client = genai.Client(api_key=API_KEY)


def add_numbers(a: float, b: float) -> float:
    """將兩個數字相加並回傳結果。"""
    print(f"[工具執行] add_numbers(a={a}, b={b})")
    return a + b


def log_usage(step: str, response) -> None:
    """記錄單次 Gemini 回應公開提供的 token 用量與估算費用。"""
    usage = response.usage_metadata
    if usage is None:
        print(f"[{step}] API 未回傳 token usage metadata。")
        return

    input_tokens = usage.prompt_token_count or 0
    output_tokens = usage.candidates_token_count or 0
    estimated_cost_usd = (
        input_tokens * INPUT_PRICE_PER_MILLION_USD
        + output_tokens * OUTPUT_PRICE_PER_MILLION_USD
    ) / 1_000_000
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "step": step,
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
        f"[{step}] token usage: input={input_tokens}, output={output_tokens}, "
        f"total={record['total_tokens']}, estimated=${estimated_cost_usd:.8f}"
    )


def main() -> None:
    original_question = "計算 15 + 5 是多少？"
    function_config = types.GenerateContentConfig(
        tools=[add_numbers],
        tool_config=types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(
                mode=types.FunctionCallingConfigMode.ANY,
                allowed_function_names=["add_numbers"],
            )
        ),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    print("\n[步驟 1] 提出原始問題，並強制模型呼叫 add_numbers。")
    first_response = client.models.generate_content(
        model=MODEL_NAME,
        contents=original_question,
        config=function_config,
    )
    log_usage("step_1_forced_function_call", first_response)

    function_calls = first_response.function_calls or []
    print("[步驟 1] 模型回傳的 function calls:")
    print(function_calls)
    if len(function_calls) != 1 or function_calls[0].name != "add_numbers":
        raise RuntimeError("模型沒有依要求回傳唯一的 add_numbers function call。")

    function_call = function_calls[0]
    print(f"[步驟 2] 執行工具：{function_call.name}，參數：{dict(function_call.args)}")
    function_result = add_numbers(**dict(function_call.args))
    print(f"[步驟 2] 工具結果：{function_result}")

    first_model_content = first_response.candidates[0].content
    tool_response_content = types.Content(
        role="user",
        parts=[
            types.Part.from_function_response(
                name=function_call.name,
                response={"result": function_result},
            )
        ],
    )
    chat_history = [
        types.Content(role="user", parts=[types.Part.from_text(text=original_question)]),
        first_model_content,
        tool_response_content,
    ]

    print("[步驟 3] 建立 chat，放入原始問題、function call 與工具結果。")
    print("[步驟 3] 可公開的 chat history:")
    print(chat_history)
    chat = client.chats.create(model=MODEL_NAME, history=chat_history)

    follow_up = (
        f"原始問題：{original_question}\n"
        f"add_numbers 的結果：{function_result}\n"
        "請根據上述工具結果，直接以繁體中文回答原始問題。"
    )
    print("[步驟 4] 將原始問題與工具結果合併後送入 chat。")
    print(f"[步驟 4] 送出內容：\n{follow_up}")
    second_response = chat.send_message(follow_up)
    log_usage("step_4_chat_answer", second_response)

    print("[步驟 5] 第二次模型輸出：")
    print(second_response.text)
    print("[步驟 5] Chat history:")
    print(chat.get_history())


if __name__ == "__main__":
    main()
