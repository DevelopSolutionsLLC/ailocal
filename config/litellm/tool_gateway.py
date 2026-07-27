"""
tool_gateway.py — LiteLLM pre-call hook that measures (and, when explicitly
enabled, filters) the tool payload every client ships on every turn.

WHY THIS EXISTS
---------------
Frontier agent clients declare their whole tool surface on turn 1, before they
know what the task needs. Claude Code sends its full builtin set plus every MCP
tool; Codex sends its own. Against a frontier model that cost is amortised. On a
local 30B on Apple Silicon it is paid in prompt-eval time on the very first
token of every session, and it competes for a context window that is 16K-64K,
not 200K. This hook is the translation layer between "frontier agent protocol"
and "what this local model can actually use".

MODES (env AILOCAL_TOOL_GATEWAY, default "off")
-----------------------------------------------
  off     — hook returns immediately. No measurement, no mutation. THE DEFAULT.
  report  — measure and emit metrics. Never mutates the request. Savings are
            reported as HYPOTHETICAL: what a filter *would* have removed.
  filter  — measure, then actually apply the allowlist to data["tools"].

The mode is read per-request, not cached at import, so it can be flipped by
restarting the container without editing code.

HONESTY OF THE NUMBERS
----------------------
  bytes   — EXACT. len() of the canonical JSON encoding of each tool schema,
            separators=(",",":"). Deterministic and independently checkable;
            this is the number the known-answer tests pin.
  tokens  — ESTIMATE. litellm's token_counter selects `openai_tokenizer`
            (cl100k) even for ollama_chat/qwen3-coder — verified, not assumed.
            Qwen's own tokenizer is not available in this container, so every
            token figure here is an OpenAI-tokenizer proxy for a Qwen model and
            is labelled `tokens_est` with `tokenizer: "cl100k-proxy"` in the
            metric.

            MEASURED, 2026-07-26, via scripts/calibrate-tokens.py against
            Ollama's real prompt_eval_count on qwen3-coder:30b-a3b-q4_K_M, using
            the actual captured payloads:

              claude-code /v1/messages   cl100k 23,937  real 24,448  ratio 1.021
              codex       /v1/responses  cl100k  7,953  real  8,026  ratio 1.009

            So cl100k UNDER-counts Qwen by 1-2% on tool-schema JSON. tokens_est
            is therefore usable as a working figure for this payload shape, and
            slightly conservative. It is still an estimate: re-run the
            calibration after a model change before trusting it again.

The measurement refuses to report savings it cannot ground: with no policy file
loaded, it emits the inventory only and sets `policy: "none"`. An absent policy
must never read as "nothing to save".

METRICS
-------
One JSON line per request on stdout, prefixed `tool_gateway_metric ` — the same
convention tool_repair.py uses, and for the same reason: LiteLLM filters
third-party loggers, so print() is what actually survives to `docker logs`.

Registered via litellm_settings.callbacks: tool_gateway.proxy_handler_instance
Ref: https://docs.litellm.ai/docs/proxy/call_hooks
"""

import json
import os
import time

from litellm.integrations.custom_logger import CustomLogger

# ── Configuration (read per-request; see module docstring) ───────────────────
MODE_ENV = "AILOCAL_TOOL_GATEWAY"
VALID_MODES = ("off", "report", "filter")
POLICY_PATH = os.environ.get(
    "AILOCAL_TOOL_POLICY", "/app/config/tool-policy.yaml")
CAPTURE_DIR = os.environ.get("AILOCAL_TOOL_GATEWAY_CAPTURE") or ""

# Canonical JSON encoding used for every byte measurement. Fixed here so the
# hook, the tests, and any offline analysis all count the same bytes.
def encode(obj):
    """The one canonical encoding. Byte counts are only comparable if every
    call site uses this — separators and ensure_ascii both change the length."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False,
                      sort_keys=True, default=str)


def mode():
    """Current mode, validated. An unrecognised value is NOT silently treated
    as 'off' — that would be exactly the kind of quiet fallback that hides a
    typo'd env var for weeks. It is reported, then treated as off."""
    raw = (os.environ.get(MODE_ENV) or "off").strip().lower()
    if raw not in VALID_MODES:
        emit({"event": "bad_mode", "value": raw, "using": "off"})
        return "off"
    return raw


def emit(record):
    """One metric line to stdout. Never raises — a measurement layer that can
    break the request path is worse than no measurement layer."""
    try:
        print("tool_gateway_metric " + json.dumps(record, default=str),
              flush=True)
    except Exception:
        pass


# ── Tool-shape normalisation ─────────────────────────────────────────────────
# The same logical tool arrives in three different envelopes depending on which
# API dialect the client speaks. All three put the array at data["tools"];
# they disagree on everything inside it.
#
#   /v1/messages        {"name":…, "description":…, "input_schema":{…}}
#   /v1/chat/completions {"type":"function","function":{"name":…,"parameters":{…}}}
#   /v1/responses       {"type":"function","name":…,"parameters":{…}}
#
# /v1/responses also carries non-function entries (type "namespace",
# "web_search", …) that have no name at all. Those are real payload weight and
# must be counted, so they get a bracketed pseudo-name rather than being
# dropped from the inventory.

def tool_name(tool):
    """Logical name of a tool entry, whatever envelope it arrived in."""
    if not isinstance(tool, dict):
        return "<malformed>"
    fn = tool.get("function")
    if isinstance(fn, dict) and fn.get("name"):
        return fn["name"]
    if tool.get("name"):
        return tool["name"]
    return "<%s>" % (tool.get("type") or "unknown")


def tool_bytes(tool):
    """Exact wire cost of one tool entry under the canonical encoding."""
    return len(encode(tool).encode("utf-8"))


# Tool types LiteLLM itself discards when translating /v1/responses into Chat
# Completions, because they have no Chat Completions equivalent. Verbatim from
# litellm/responses/litellm_completion_transformation/transformation.py:1309 on
# the 1.93.0 image we run — not inferred from the log message.
#
# This matters for honesty, not just completeness. Codex's largest declarations
# are `namespace` bundles (mcp__lsp, mcp__grepai, multi_agent_v1 — 27,168 B of a
# 38,388 B payload in the captured session). LiteLLM already drops all three, so
# they never reach Ollama and never cost a prompt-eval token. A gateway that
# counted them as "saved" would be claiming credit for work already done, and
# would report a ~71% Codex reduction that does not exist at the model.
#
# Consequence worth naming: through Codex, the mcp__lsp and mcp__grepai
# namespaces do not reach the local model at all. Codex being *configured* with
# them is not the same as the model being able to call them.
RESPONSES_DROPPED_TYPES = ("computer_use", "image_generation", "namespace",
                           "shell")


def reaches_backend(tool, route):
    """Whether this entry survives LiteLLM's own translation to the backend.
    Only /v1/responses does this filtering; the other two routes pass tools
    through, so everything counts there."""
    if route != "/v1/responses":
        return True
    if not isinstance(tool, dict):
        return True
    return tool.get("type") not in RESPONSES_DROPPED_TYPES


def inventory(tools):
    """[(name, bytes)] for every declared tool, in declaration order."""
    return [(tool_name(t), tool_bytes(t)) for t in (tools or [])]


# ── Client detection ─────────────────────────────────────────────────────────
# Best-effort, and it says so. LiteLLM stashes the raw inbound request under
# data["proxy_server_request"]; the headers there are the only honest signal
# about who is calling. When they are absent or unrecognised the client is
# "unknown" — never guessed from the model name, which is an alias any client
# can request.

def detect_client(data):
    req = (data or {}).get("proxy_server_request") or {}
    headers = {k.lower(): v for k, v in (req.get("headers") or {}).items()}
    ua = str(headers.get("user-agent") or "").lower()
    originator = str(headers.get("originator") or "").lower()

    if "codex" in originator or "codex" in ua:
        return "codex"
    if "claude-cli" in ua or headers.get("x-app") == "cli":
        return "claude-code"
    if "continue" in ua:
        return "continue"
    if "vscode" in ua or "copilot" in ua:
        return "copilot"
    return "unknown"


def detect_route(data, call_type):
    """Which API dialect this arrived on. call_type is authoritative when it
    distinguishes; the payload shape settles the OpenAI-family ambiguity."""
    if call_type == "anthropic_messages":
        return "/v1/messages"
    if "input" in (data or {}):
        return "/v1/responses"
    return "/v1/chat/completions"


# ── Token estimation ─────────────────────────────────────────────────────────

def tokens_est(text, model=None):
    """cl100k-proxy token estimate. Returns None rather than a fabricated
    number when the counter is unavailable — a missing measurement and a
    measurement of zero are not the same thing."""
    try:
        import litellm
        return litellm.token_counter(model=model or "gpt-4o", text=text)
    except Exception:
        return None


# ── Policy ───────────────────────────────────────────────────────────────────

class Policy:
    """Allowlist loaded from config/tool-policy.yaml. Absent file == no policy,
    which means the gateway reports inventory only and never claims savings."""

    def __init__(self, path=POLICY_PATH):
        self.path = path
        self.loaded = False
        # `state` distinguishes the three ways there can be no policy, because
        # they demand different responses: "absent" is the expected default,
        # "unavailable" is a broken image, "error" is a broken policy file.
        # Collapsing them into one flag is how a corrupt policy gets mistaken
        # for a deliberately empty one.
        self.state = "absent"
        self.error = None
        self.groups = {}     # group name -> [tool name or prefix]
        self.rules = []      # [{match:{client,capability}, allow:[group]}]
        self._load()

    def _load(self):
        try:
            import yaml
        except ImportError:
            self.state = "unavailable"
            self.error = "PyYAML not importable"
            emit({"event": "policy_load_failed", "path": self.path,
                  "state": self.state, "error": self.error})
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                doc = yaml.safe_load(f) or {}
        except FileNotFoundError:
            self.state = "absent"
            return
        except Exception as exc:
            self.state = "error"
            self.error = "%s: %s" % (type(exc).__name__, exc)
            emit({"event": "policy_load_failed", "path": self.path,
                  "state": self.state, "error": self.error})
            return
        self.groups = dict(doc.get("groups") or {})
        self.rules = list(doc.get("rules") or [])
        self.loaded = True
        self.state = "loaded"

    def allowed_names(self, client, capability):
        """Resolved set of allowed tool names/prefixes for this combination, or
        None when no rule matches — None means 'no opinion', which the caller
        must treat as allow-all, not deny-all."""
        if not self.loaded:
            return None
        for rule in self.rules:
            match = rule.get("match") or {}
            if match.get("client") not in (None, "*", client):
                continue
            if match.get("capability") not in (None, "*", capability):
                continue
            names = []
            for group in rule.get("allow") or []:
                names.extend(self.groups.get(group) or [])
            return set(names)
        return None

    def permits(self, name, allowed):
        """A tool is permitted if allowed is None (no opinion), if it is named
        exactly, or if a listed entry ends in '*' and prefixes it.

        Entries the gateway could not name — `<web_search>`, `<namespace>`,
        `<unknown>` — are ALWAYS permitted. A policy is written in terms of tool
        names, so it cannot have formed an intent about an entry that has none,
        and dropping one would be acting on an opinion nobody expressed.

        This is not hypothetical. Codex declares web search as a bare
        {"type":"web_search"} with no name; it normalises to `<web_search>`, and
        an earlier revision dropped it. That would have removed the tool
        LiteLLM's websearch_interception rewrites into a SearXNG call — web
        search would have failed silently, which is the worst possible failure
        mode for a change sold as a performance optimisation. Caught by
        replaying a real captured payload, not by a unit test."""
        if allowed is None:
            return True
        if name.startswith("<") and name.endswith(">"):
            return True
        if name in allowed:
            return True
        return any(a.endswith("*") and name.startswith(a[:-1]) for a in allowed)


# ── Capability resolution ────────────────────────────────────────────────────
# Reuses the same alias -> capability mapping persona_injector.py relies on, for
# the same reason: the requested model is a client-facing compat name, and the
# policy has to key off the capability behind it.

def load_alias_map(config_path=None):
    path = config_path or os.environ.get(
        "AILOCAL_CONFIG_PATH", "/app/config/config.yaml")
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return dict((cfg.get("router_settings") or {}).get(
            "model_group_alias") or {})
    except Exception:
        return {}


def capability_of(model, alias):
    name = alias.get(model, model) or ""
    return name[len("ailocal-"):] if name.startswith("ailocal-") else name


# ── The hook ─────────────────────────────────────────────────────────────────

class ToolGateway(CustomLogger):

    def __init__(self, policy=None, alias=None):
        super().__init__()
        self.policy = policy if policy is not None else Policy()
        self.alias = alias if alias is not None else load_alias_map()

    # -- measurement (pure; the tests call this directly) --------------------
    def measure(self, data, call_type=None):
        """Inventory the request's tool payload and compute what the policy
        would remove. Pure: takes a request dict, returns a report, mutates
        nothing. FILTER mode applies the result; REPORT mode only prints it."""
        tools = (data or {}).get("tools") or []
        model = (data or {}).get("model")
        client = detect_client(data)
        route = detect_route(data, call_type)
        capability = capability_of(model, self.alias)

        items = inventory(tools)
        total_bytes = sum(b for _, b in items)

        # Split off what LiteLLM discards on its own before attributing any
        # saving to this gateway. `reachable` is the payload that actually costs
        # the model prompt-eval time, and it is the only base against which a
        # reduction claim means anything.
        reachable = [(t, n, s) for t, (n, s) in zip(tools, items)
                     if reaches_backend(t, route)]
        reachable_bytes = sum(s for _, _, s in reachable)
        prefiltered_bytes = total_bytes - reachable_bytes

        allowed = self.policy.allowed_names(client, capability)
        keep, drop = [], []
        for tool, (name, size) in zip(tools, items):
            (keep if self.policy.permits(name, allowed) else drop).append(
                (name, size, tool))

        kept_bytes = sum(s for _, s, _ in keep)
        # Only bytes that would otherwise have reached the backend count as a
        # saving. Dropping something LiteLLM was going to drop anyway saves the
        # model nothing.
        dropped_bytes = sum(s for n, s, t in drop if reaches_backend(t, route))
        dropped_bytes_moot = sum(s for n, s, t in drop
                                 if not reaches_backend(t, route))

        # Whole-array encodings, so the token estimate reflects the actual
        # serialised payload rather than the sum of per-tool estimates (which
        # would double-count nothing but round differently).
        all_json = encode(tools)
        kept_json = encode([t for _, _, t in keep])

        report = {
            "route": route,
            "client": client,
            "model": model,
            "capability": capability,
            "policy": self.policy.state,
            "tools_in": len(items),
            "bytes_in": total_bytes,
            # What survives LiteLLM's own translation — the real cost base.
            "tools_reachable": len(reachable),
            "bytes_reachable": reachable_bytes,
            "bytes_prefiltered_by_litellm": prefiltered_bytes,
            "tools_kept": len(keep),
            "tools_dropped": len(drop),
            "bytes_kept": kept_bytes,
            # Saving attributable to THIS gateway (reachable bytes only)...
            "bytes_dropped": dropped_bytes,
            # ...versus bytes it would drop that LiteLLM discards regardless.
            "bytes_dropped_moot": dropped_bytes_moot,
            "tokens_est_in": tokens_est(all_json, model),
            "tokens_est_kept": tokens_est(kept_json, model),
            "tokenizer": "cl100k-proxy",
            "dropped_names": sorted(n for n, _, _ in drop),
            # Per-tool inventory: the input to any policy decision. Sorted by
            # cost, because that is the order in which trimming pays.
            "largest": sorted(items, key=lambda p: -p[1])[:10],
        }
        if not self.policy.loaded:
            # No policy: the inventory stands, the savings do not exist. Say so
            # in the record itself so a downstream reader cannot mistake a
            # zero-drop report for "measured, nothing to save".
            report["savings_claim"] = "none — policy %s" % self.policy.state
        return report, keep

    # -- capture -------------------------------------------------------------
    def capture(self, data, report):
        """Write the raw tool payload to disk for offline analysis. This is how
        real Claude Code / Codex payloads get into the test corpus — captured
        from the live path, never hand-written to look plausible."""
        if not CAPTURE_DIR:
            return
        try:
            os.makedirs(CAPTURE_DIR, exist_ok=True)
            stamp = "%d-%s-%s" % (time.time() * 1000, report["client"],
                                  report["route"].strip("/").replace("/", "-"))
            path = os.path.join(CAPTURE_DIR, stamp + ".json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"report": report, "tools": data.get("tools") or [],
                           "model": data.get("model")}, f, indent=2,
                          default=str)
        except Exception as exc:
            emit({"event": "capture_failed", "error": str(exc)})

    # -- LiteLLM entrypoint --------------------------------------------------
    async def async_pre_call_hook(self, user_api_key_dict, cache, data,
                                  call_type):
        current = mode()
        if current == "off":
            return data
        if not (data or {}).get("tools"):
            return data

        started = time.perf_counter()
        report, keep = self.measure(data, call_type)
        report["mode"] = current
        report["overhead_ms"] = round(
            (time.perf_counter() - started) * 1000, 3)

        if current == "filter" and report["tools_dropped"]:
            data["tools"] = [t for _, _, t in keep]
            report["applied"] = True
        else:
            report["applied"] = False

        emit(report)
        self.capture(data, report)
        return data


proxy_handler_instance = ToolGateway()
