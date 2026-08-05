"""
model_registrar.py — restore LiteLLM's context-window validation for local models.

THE BUG (upstream, reproduced against ghcr.io/berriai/litellm:main-stable)
--------------------------------------------------------------------------
Router.__init__ registers each deployment's model_info, but register_model
normalizes the provider prefix OFF the key, while the router's pre-call check
builds the key WITH the prefix. They never match:

    Router.__init__()
        -> litellm.register_model({"ollama_chat/qwen3-coder:30b": {...}})
        -> utils.py:2713-2716  model_cost_key = builtin["key"]   # prefix stripped
        -> litellm.model_cost["qwen3-coder:30b"]                 # stored HERE
    ...
    Router._pre_call_checks()                       router.py:9888
        -> get_router_model_info()
        -> router.py:8545  model_info_name = "{provider}/{model}"
        -> router.py:8549  litellm.get_model_info("ollama_chat/qwen3-coder:30b")
        -> raises "This model isn't mapped yet"     # looked up THERE

The raise is swallowed by the broad `except Exception` at router.py:9911, so the
request still succeeds with a 200 — but the entire validation block at
router.py:9889-9910 is skipped. That block is what compares input_tokens against
max_input_tokens. With it skipped, an oversized prompt is forwarded to the
backend unvalidated and silently truncated instead of being rejected.

Note the config's own `model_info:` block cannot fix this: the router merges it
at router.py:8555, six lines AFTER the call that throws at 8549.

THE WORKAROUND
--------------
Populate litellm.model_cost under the EXACT key the lookup builds, at import
time. This uses only the public litellm namespace — no fork, no vendored-file
patch, and no wholesale replacement of the cost map (LITELLM_MODEL_COST_MAP_URL
would do the latter, since __init__.py:508 assigns rather than merges).

PROVIDER-AGNOSTIC BY DESIGN
---------------------------
The key is whatever string appears in `litellm_params.model` — verbatim. Nothing
here knows the word "ollama", so migrating a capability to vllm/, mlx/, openai/
or any custom provider keeps working with no edit here. Deployments LiteLLM
already maps (real cloud models) are left untouched, so this never overrides
genuine upstream pricing.

Remove this module once the upstream registration/lookup inconsistency is fixed;
the startup self-check below will say so — every model reports "already mapped".
"""

import logging
import os

import litellm
from litellm.integrations.custom_logger import CustomLogger

log = logging.getLogger("model_registrar")

# The GENERATED config — sync-models.py writes it to $AILOCAL_STATE and Compose
# mounts it at /app/generated. It is NOT in the authored /app/config mount.
CONFIG_PATH = os.environ.get("AILOCAL_CONFIG_PATH", "/app/generated/config.yaml")


def _load_model_list():
    """The generated model_list from the mounted config. sync-models.py owns this
    file, so new capabilities are picked up with no change here."""
    try:
        import yaml

        with open(CONFIG_PATH, encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("model_list") or []
    except Exception as exc:  # noqa: BLE001 - never block proxy startup
        log.error("model_registrar: could not read %s: %s", CONFIG_PATH, exc)
        return []


def _has_exact_key(model_key):
    """True if the EXACT key is present in litellm.model_cost.

    Deliberately not `litellm.get_model_info(model_key)`: that helper falls back
    to the prefix-stripped key, which register_model already created, so it
    reports success for models the router still cannot resolve — gating on it
    makes this module a no-op. The router needs the prefixed key itself.
    """
    return model_key in litellm.model_cost


def _router_can_resolve(model_key):
    """Replicate the router's own lookup (router.py:8549) as closely as possible
    from outside, for the post-registration self-check."""
    try:
        litellm.get_model_info(model=model_key)
        return model_key in litellm.model_cost
    except Exception:  # noqa: BLE001 - a raise here IS the "not mapped" signal
        return False


def _cost_entry(model_key, model_info):
    """Build a cost-map entry from the deployment's own model_info block.

    litellm_provider is derived from the key's prefix so the entry survives
    LiteLLM's provider-match filtering (_check_provider_match drops entries whose
    provider disagrees with the resolved one)."""
    provider = model_key.split("/", 1)[0] if "/" in model_key else None
    entry = {
        # Local backends are free; explicit zeros keep cost tracking coherent
        # rather than leaving the fields absent.
        "input_cost_per_token": model_info.get("input_cost_per_token", 0),
        "output_cost_per_token": model_info.get("output_cost_per_token", 0),
        "mode": model_info.get("mode", "chat"),
    }
    if provider:
        entry["litellm_provider"] = provider
    # These two are the whole point: they are what router.py:9895 compares the
    # counted input tokens against.
    for field in ("max_input_tokens", "max_output_tokens", "max_tokens"):
        if model_info.get(field) is not None:
            entry[field] = model_info[field]
    return entry


def register_local_models():
    """Inject every configured deployment that LiteLLM cannot already map, then
    verify. Returns (registered, already_mapped, failed)."""
    registered, already, failed = [], [], []

    for deployment in _load_model_list():
        params = deployment.get("litellm_params") or {}
        model_key = params.get("model")
        if not model_key:
            continue

        if _has_exact_key(model_key):
            already.append(model_key)
            continue

        entry = _cost_entry(model_key, deployment.get("model_info") or {})
        # Assign directly rather than via litellm.register_model(): register_model
        # is precisely what rewrites the key (utils.py:2713-2716), which is the bug.
        litellm.model_cost[model_key] = entry
        registered.append(model_key)

    # Invalidate LiteLLM's case-insensitive lookup cache so the new keys are seen.
    try:
        from litellm.utils import _invalidate_model_cost_lowercase_map

        _invalidate_model_cost_lowercase_map()
    except Exception:  # noqa: BLE001 - private helper; absence is not fatal
        pass

    # get_model_info is LRU-cached (_cached_get_model_info, utils.py:5654). Clear
    # it or a lookup performed before this injection keeps returning the stale
    # prefix-stripped answer.
    try:
        from litellm.utils import _cached_get_model_info

        _cached_get_model_info.cache_clear()
    except Exception:  # noqa: BLE001 - private helper; absence is not fatal
        pass

    # ── Self-check ─────────────────────────────────────────────────────────
    # Fail LOUDLY rather than silently losing context-window validation again if
    # LiteLLM's internals shift in a future image. This re-runs the real lookup
    # (litellm.get_model_info) — not a dict membership test — because that is
    # exactly what router.py:8549 calls.
    for model_key in registered:
        if not _router_can_resolve(model_key):
            failed.append(model_key)

    # print(), not log.info(): the proxy's logging config filters third-party
    # loggers, and this summary must be visible in `docker logs` to be useful.
    print("model_registrar: config=%s models=%d" % (CONFIG_PATH, len(registered)+len(already)), flush=True)
    if registered:
        print("model_registrar: REGISTERED %s" % ", ".join(registered), flush=True)
    if already:
        print("model_registrar: already mapped %s" % ", ".join(already), flush=True)
    if failed:
        print("model_registrar: FAILED %s" % ", ".join(failed), flush=True)
    if registered:
        log.info(
            "model_registrar: registered %d local model(s) for context-window "
            "validation: %s",
            len(registered),
            ", ".join(registered),
        )
    if already:
        log.info(
            "model_registrar: %d model(s) already mapped by LiteLLM, left "
            "untouched: %s",
            len(already),
            ", ".join(already),
        )
    if failed:
        log.error(
            "model_registrar: %d model(s) STILL unmapped after registration: %s. "
            "Context-window validation is DISABLED for these — router._pre_call_checks "
            "will skip max_input_tokens enforcement. LiteLLM internals may have "
            "changed; re-check router.py get_router_model_info().",
            len(failed),
            ", ".join(failed),
        )

    return registered, already, failed


class ModelRegistrar(CustomLogger):
    """No-op logger. Registration happens at import; this exists so the module can
    be listed in litellm_settings.callbacks, which is what triggers the import."""


# Run at import — before the proxy serves its first request. _pre_call_checks
# consults litellm.model_cost per request, so import-time injection is early enough.
REGISTERED, ALREADY_MAPPED, FAILED = register_local_models()

proxy_handler_instance = ModelRegistrar()
