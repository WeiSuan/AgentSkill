import json
from datetime import datetime, timezone
from pathlib import Path


def log_usage(
    step: str,
    response,
    *,
    model_name: str,
    usage_log_path: Path,
    input_price_per_million: float,
    output_price_per_million: float,
) -> None:
    """記錄單次 Gemini 回應公開提供的 token 用量與估算費用。"""
    usage = response.usage_metadata
    if usage is None:
        print(f"[{step}] API 未回傳 token usage metadata。")
        return

    input_tokens = usage.prompt_token_count or 0
    output_tokens = usage.candidates_token_count or 0
    estimated_cost = (
        input_tokens * input_price_per_million
        + output_tokens * output_price_per_million
    ) / 1_000_000
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "step": step,
        "model": model_name,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thinking_tokens": usage.thoughts_token_count or 0,
        "total_tokens": usage.total_token_count or 0,
        "estimated_cost": round(estimated_cost, 10),
    }
    with usage_log_path.open("a", encoding="utf-8") as usage_log:
        usage_log.write(json.dumps(record) + "\n")

    print(
        f"[{step}] token usage: input={input_tokens}, output={output_tokens}, "
        f"total={record['total_tokens']}, estimated={estimated_cost:.8f}"
    )
