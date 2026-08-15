"""reasoning_router.py — translate `reasoning_effort` into Ollama's native `think`.

KEPT ON MEASUREMENT, not on history. The issue that originally justified this
(BerriAI/litellm#15059, reasoning_effort dropped unmapped) was fixed by #15465
in October 2025 and the mapping IS present in the pinned 1.93.0:

    optional_params["think"] = value in {"low", "medium", "high"}

So the original reason is gone. A/B against the running stack found a DIFFERENT
one that is still real. Same prompt, four effort levels, router removed from the
callback list and the proxy restarted:

    effort     reasoning_content chars
               with hook      without hook
    none            0             1828
    low          1506             1460
    medium       1517             1650
    high         1613             1491

`none` is the whole justification. Without this hook it does not suppress
thinking — the model emitted 1,828 characters of reasoning for a request that
asked for none. With it, zero, reproducibly. Upstream's expression evaluates to
`False` for "none", so the intent is right somewhere upstream and does not reach
the backend on this path; that is the gap being covered.

The GRADED mapping is inert and is kept only because it costs nothing. low,
medium and high are indistinguishable in both columns (1506/1517/1613 against
1460/1650/1491 — the spread is larger between runs of the same setting than
between settings). Do not cite graded effort as a working control.

RETIREMENT CONDITION: delete when `reasoning_effort: "none"` yields zero
reasoning_content with this hook removed. That is one A/B, and the table above
is the format for it.

It does not decide how hard to think, only translate an explicit request. An
automatic task->effort classifier belongs in registry.yaml beside the existing
`task_classes`.

`none` maps to `think: false` and, measured above, actually yields zero
reasoning_content on gemma4:26b-mlx. An earlier note here called `none`
"aspirational" and named `low` the real floor; the A/B contradicts that, and the
measurement wins.
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
