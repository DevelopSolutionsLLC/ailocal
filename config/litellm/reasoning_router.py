"""reasoning_router.py — translate `reasoning_effort` into Ollama's native `think`.

THE GAP (measured on LiteLLM 1.93.0 + Ollama 0.32.5, this machine)
------------------------------------------------------------------
Two request-level reasoning controls exist and they do NOT behave the same.

`reasoning_effort` reaches the proxy and is then dropped without being mapped.
Through /v1/chat/completions to ailocal-review (gpt-oss:20b), reasoning output
in characters, two reps each:

    none  112, 61        low  120, 102        high  122, 70

`high` overlaps `none` entirely. There is no monotonic relationship: the control
does nothing. This is BerriAI/litellm#15059, still open on 1.93.0, and it is why
`registry.yaml` already warns that per-request effort levels are UNRELIABLE.

Ollama's native `think` works, and LiteLLM forwards it verbatim. Native, three
reps each, non-overlapping:

    low  [37, 20, 37]    medium  [111, 111, 122]    high  [202, 345, 298]

Through the proxy as a request-level field: low 18, high 104. Differentiated.

So the smallest correct fix is a translation, not a fork: accept the parameter
clients actually send, emit the one the backend actually honours.

WHAT THIS DOES NOT DO
---------------------
It does not decide how hard to think. It only translates an explicit request.
An automatic task->effort classifier belongs in registry.yaml alongside the
existing `task_classes`, and is deliberately not implemented here.

`none` is aspirational on reasoning-native models. MEASURED: gpt-oss:20b with
`think: false` still emitted 253 characters of reasoning. `low` is the real
floor. The mapping is honest about this rather than promising silence.
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
