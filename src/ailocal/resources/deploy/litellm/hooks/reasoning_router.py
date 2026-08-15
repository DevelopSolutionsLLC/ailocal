"""reasoning_router.py — translate `reasoning_effort` into Ollama's native `think`.

RETIREMENT CANDIDATE — the reason this was written is no longer true.

It said LiteLLM accepts `reasoning_effort` and drops it unmapped
(BerriAI/litellm#15059, "still open on 1.93.0"). That issue was fixed by
BerriAI/litellm#15465, merged October 2025, and the mapping IS present in the
pinned 1.93.0 image, in `llms/ollama/chat/transformation.py` — for a
non-`gpt-oss` model it does:

    optional_params["think"] = value in {"low", "medium", "high"}

Upstream therefore already turns thinking ON. What it does not do is preserve
the LEVEL: low, medium and high all collapse to boolean True. This hook maps
them to Ollama's graded string form instead, and maps `minimal` to "low" where
upstream maps it to False.

[APPROX] That remaining difference has no measured effect on the model we run.
Against gemma4:26b-mlx directly, `think: true`, `think: "high"` and
`think: "low"` were all accepted without error and produced thinking blocks of
61, 71 and 79 characters — no ordering, no signal. n=1 per level on one trivial
prompt: too thin to delete a hook on, which is why this is a candidate rather
than a removal.

DELETE THIS FILE when an A/B on a reasoning-heavy prompt shows graded and
boolean `think` are indistinguishable on the active model — or immediately if
Ollama is confirmed to coerce a non-boolean `think` to truthy for models without
graded support, since that makes the two provably identical.

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
