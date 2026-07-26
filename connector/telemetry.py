"""Pure normalization of optional Hermes completion telemetry."""

def reported_number(source: dict, names: tuple[str, ...], integer: bool = False):
    for name in names:
        value = source.get(name)
        if isinstance(value, bool):
            continue
        try:
            number = int(value) if integer else float(value)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            return number
    return None


def normalize_usage(usage: dict) -> dict:
    prompt = reported_number(usage, ("prompt_tokens", "input_tokens", "promptTokens", "inputTokens"), True)
    completion = reported_number(usage, ("completion_tokens", "output_tokens", "completionTokens", "outputTokens"), True)
    total = reported_number(usage, ("total_tokens", "totalTokens"), True)
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    model_calls = reported_number(usage, ("model_calls", "modelCalls"), True)
    credits = reported_number(usage, ("credits", "compute_credits", "computeCredits"))
    cost = reported_number(usage, ("cost", "cost_usd", "costUsd"))
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total, "telemetry": {"tokens_reported": any(value is not None for value in (prompt, completion, total)), "input_output_reported": prompt is not None and completion is not None, "model_calls": model_calls, "credits": credits, "cost": cost}}
