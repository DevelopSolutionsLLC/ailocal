"""reasoning_router.py — translate `reasoning_effort` into Ollama's native `think`.

LiteLLM accepts `reasoning_effort` and drops it without mapping it onto a backend
control, so the parameter has no measurable effect on output — BerriAI/litellm#15059,
still open on 1.93.0, and why registry.yaml warns that per-request effort levels
are UNRELIABLE. Ollama's native `think` is honoured, and LiteLLM forwards it
verbatim. This translates the parameter clients send into the one the backend
honours.

It does not decide how hard to think, only translate an explicit request. An
automatic task->effort classifier belongs in registry.yaml beside the existing
`task_classes`.

`none` is aspirational on reasoning-native models, which still emit some
reasoning under `think: false`. `low` is the real floor.
"""
from litellm.integrations.custom_logger import CustomLogger

# reasoning_effort (OpenAI vocabulary) -> Ollama `think`.
# `minimal` is OpenAI's tier below `low`; Ollama has no equivalent, so it maps to
# the lowest real level rather than to False, which does not reliably disable
# thinking on a reasoning-native model.
EFFORT_TO_THINK = {
    "none": False,
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
}


class ReasoningRouter(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if not isinstance(data, dict):
            return data

        # An explicit `think` is the caller being specific about the backend
        # control. It outranks reasoning_effort, and outranks the per-model
        # default in config.yaml, because a manual request must always win.
        if "think" in data:
            return data

        effort = data.get("reasoning_effort")
        if effort is None:
            return data  # fall through to the role's configured default

        mapped = EFFORT_TO_THINK.get(str(effort).lower())
        if mapped is None:
            return data  # unknown value: leave the role default alone, do not guess

        data["think"] = mapped
        return data


proxy_handler_instance = ReasoningRouter()
