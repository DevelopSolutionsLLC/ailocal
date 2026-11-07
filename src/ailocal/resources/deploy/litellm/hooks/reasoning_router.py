"""Translate `reasoning_effort` into Ollama's native `think`.

Kept on measurement: upstream does not suppress thinking for `none` on this
path, and LiteLLM 1.106 maps `xhigh` to False for non-gpt-oss models. Graded
low/medium/high showed no measurable difference; do not cite it as a control.
Retire when `none` yields zero reasoning_content with this hook removed.
"""
from __future__ import annotations

from typing import Any

from litellm.integrations.custom_logger import CustomLogger

# `minimal` → lowest real level: False does not reliably disable thinking.
EFFORT_TO_THINK = {
    "none": False,
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
}


class ReasoningRouter(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict: Any, cache: Any, data: object,
                                  call_type: str) -> object:
        if not isinstance(data, dict):
            return data

        # An explicit `think` outranks reasoning_effort and the model default.
        if "think" in data:
            return data

        effort = data.get("reasoning_effort")
        if effort is None and isinstance(data.get("reasoning"), dict):
            effort = data["reasoning"].get("effort")
        if effort is None:
            return data  # fall through to the role's configured default

        mapped = EFFORT_TO_THINK.get(str(effort).lower())
        if mapped is None:
            return data  # unknown value: leave the role default alone, do not guess

        data["think"] = mapped
        return data


proxy_handler_instance = ReasoningRouter()
