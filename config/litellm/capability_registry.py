"""
capability_registry.py — loads registry.yaml and answers capability questions.

The only module that knows the registry's shape. Everything downstream asks it
questions ("can this model use tools?", "does this route drop namespace tools?",
"is this client known?") rather than testing model or tool names itself. That is
the whole point: the negotiator must contain no conditional about any specific
model, client, or tool, so that swapping any of them is a config edit.

DESIGN COMMITMENTS
------------------
1. **Fail open, and say why.** Every lookup has a defined answer when the
   registry is absent, unparseable, or silent about the subject — and that answer
   is always "change nothing". A capability layer that fails closed turns a
   config typo into a client that has lost its tools.
2. **States are distinguished.** `absent` / `unavailable` (no PyYAML) / `error`
   (malformed) are separate, because a corrupt registry demands a different
   response from a deliberately missing one.
3. **max_context is not restated.** It is read from
   config/capabilities.generated.json, which sync-models.py derives from the
   active profile. Duplicating it here would create two sources that drift apart
   after a profile change. The registry's own max_context is a fallback for
   classes with no generated entry (cloud models).
4. **Capability match precedes model-name match.** ailocal's compat aliases
   (claude-sonnet-4-6, gpt-4o) resolve through model_group_alias to LOCAL
   capabilities. Matching names first would classify them as frontier and pass
   them through unfiltered — the exact opposite of intent.
"""

import fnmatch
import json
import os

REGISTRY_PATH = os.environ.get("AILOCAL_REGISTRY",
                               "/app/config/registry.yaml")
CAPS_JSON = os.environ.get("AILOCAL_CAPABILITIES_JSON",
                           "/app/config/capabilities.generated.json")
CONFIG_PATH = os.environ.get("AILOCAL_CONFIG_PATH", "/app/config/config.yaml")


class Registry:
    """Loaded capability registry. Construct once; it is read-only afterwards."""

    def __init__(self, path=None, caps_json=None, config_path=None):
        self.path = path or REGISTRY_PATH
        self.state = "absent"
        self.error = None
        self.doc = {}
        self.contexts = {}      # capability -> max_context (generated)
        self.alias = {}         # compat name -> ailocal-<capability>
        self._load(caps_json or CAPS_JSON, config_path or CONFIG_PATH)

    # ── loading ─────────────────────────────────────────────────────────────
    def _load(self, caps_json, config_path):
        try:
            import yaml
        except ImportError:
            self.state = "unavailable"
            self.error = "PyYAML not importable"
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                self.doc = yaml.safe_load(f) or {}
            self.state = "loaded"
        except FileNotFoundError:
            self.state = "absent"
            return
        except Exception as exc:
            self.state = "error"
            self.error = "%s: %s" % (type(exc).__name__, exc)
            return

        # Context windows: generated, never restated in the registry.
        try:
            with open(caps_json, encoding="utf-8") as f:
                for cap in (json.load(f) or {}).get("capabilities") or []:
                    if cap.get("name"):
                        self.contexts[cap["name"]] = cap.get("context")
        except Exception:
            pass        # absent generated file is not fatal; fallbacks apply

        try:
            with open(config_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            self.alias = dict((cfg.get("router_settings") or {}).get(
                "model_group_alias") or {})
        except Exception:
            pass

    @property
    def loaded(self):
        return self.state == "loaded"

    # ── model resolution ────────────────────────────────────────────────────
    def capability_of(self, model):
        """Requested model name -> ailocal capability key, via model_group_alias."""
        name = self.alias.get(model, model) or ""
        return name[len("ailocal-"):] if name.startswith("ailocal-") else name

    def model_class(self, model):
        """(class_name, class_dict) for a requested model name.

        Capability match first (see commitment 4), then glob on the raw name.
        Returns (None, {}) when nothing matches, which every caller must treat
        as 'no opinion'.
        """
        classes = (self.doc.get("model_classes") or {})
        capability = self.capability_of(model)

        for name, spec in classes.items():
            if capability and capability in (spec.get("match_capabilities") or []):
                return name, spec
        for name, spec in classes.items():
            for pattern in spec.get("match_models") or []:
                if model and fnmatch.fnmatch(model, pattern):
                    return name, spec
        return None, {}

    def supports(self, model, feature):
        """Capability flag lookup, e.g. supports(m, "tools") -> bool|None.

        None means the registry does not say. Callers must not coerce that to
        False: 'unknown' and 'unsupported' license different behaviour.
        """
        _, spec = self.model_class(model)
        return spec.get("supports_" + feature)

    def is_passthrough(self, model):
        """True when this model must be forwarded unmodified regardless of flags.

        Unknown models are passthrough too — an unmatched class has no
        `passthrough` key, so the absence of a class is itself a passthrough
        signal via the `unknown` catch-all.
        """
        name, spec = self.model_class(model)
        if name is None:
            return True
        return bool(spec.get("passthrough"))

    def max_context(self, model):
        """Generated context window, falling back to the class's declared one."""
        capability = self.capability_of(model)
        if capability in self.contexts:
            return self.contexts[capability]
        _, spec = self.model_class(model)
        return spec.get("max_context")

    def routing_hints(self, model):
        _, spec = self.model_class(model)
        return dict(spec.get("routing_hints") or {})

    # ── groups and tool names ───────────────────────────────────────────────
    def group_members(self, group):
        return list((self.doc.get("groups") or {}).get(group) or [])

    def all_groups(self):
        """Every declared group name. Exposed so callers never have to reach
        into `self.doc` — the registry's shape stays this module's business."""
        return set(self.doc.get("groups") or {})

    def expand(self, groups):
        """Group names -> the set of tool names/patterns they contain."""
        out = set()
        for g in groups or []:
            out.update(self.group_members(g))
        return out

    @staticmethod
    def matches(name, patterns):
        """Whether a tool name is covered by a name/prefix pattern set."""
        if not name:
            return False
        if name in patterns:
            return True
        return any(p.endswith("*") and name.startswith(p[:-1])
                   for p in patterns)

    def group_of(self, name):
        """Which group a tool belongs to, or None. Used for reporting, so a
        human reading a drop list sees categories rather than 20 bare names."""
        for group in (self.doc.get("groups") or {}):
            if self.matches(name, set(self.group_members(group))):
                return group
        return None

    def is_protected(self, name):
        """Protected tools survive every rule. Unnamed entries — normalised to
        `<web_search>`, `<namespace>` — are protected implicitly: a registry is
        written in names and cannot have formed an intent about an entry without
        one."""
        if name and name.startswith("<") and name.endswith(">"):
            return True
        return self.matches(name, set(self.doc.get("protected") or []))

    # ── routes ──────────────────────────────────────────────────────────────
    def route_for_call_type(self, call_type, has_input_key=False):
        """Resolve the API dialect from the call_type the proxy reports.

        `has_input_key` disambiguates the OpenAI family: /v1/responses payloads
        carry a top-level `input`. Passed in rather than inspected here so this
        stays a pure lookup.
        """
        routes = self.doc.get("routes") or {}
        for route, spec in routes.items():
            if call_type and call_type in (spec.get("call_types") or []):
                return route
        return "/v1/responses" if has_input_key else "/v1/chat/completions"

    def route_drops_type(self, route, tool_type):
        """Whether LiteLLM discards this tool type on this route before the
        backend. Bytes it drops are not the gateway's to claim as a saving."""
        spec = (self.doc.get("routes") or {}).get(route) or {}
        return tool_type in (spec.get("drops_tool_types") or [])

    def result_status_mode(self, route):
        spec = (self.doc.get("routes") or {}).get(route) or {}
        return spec.get("result_status", "unknown")

    # ── clients ─────────────────────────────────────────────────────────────
    def detect_client(self, headers):
        """Identify the client from request headers. Returns a name from the
        registry, or "unknown" — never a guess derived from the model name,
        which is an alias any client may request."""
        lowered = {str(k).lower(): str(v or "")
                   for k, v in (headers or {}).items()}
        ua = lowered.get("user-agent", "").lower()
        originator = lowered.get("originator", "").lower()

        for name, spec in (self.doc.get("clients") or {}).items():
            detect = spec.get("detect") or {}
            if any(s in originator
                   for s in detect.get("originator_contains") or []):
                return name
            if any(s in ua for s in detect.get("user_agent_contains") or []):
                return name
            for header, value in (detect.get("headers") or {}).items():
                if lowered.get(str(header).lower()) == str(value):
                    return name
        return "unknown"

    def client_profile(self, client):
        return dict((self.doc.get("clients") or {}).get(client) or {})

    # ── the negotiation rule ────────────────────────────────────────────────
    def removable_groups(self, client, model):
        """Groups removable for this (client, model) pair.

        The INTERSECTION of what the client profile is willing to drop and what
        the model class does not want. Requiring both sides to agree means
        neither can unilaterally strip a tool the other depends on, and a client
        or model the registry does not describe contributes an empty set — so an
        unknown participant results in no filtering rather than maximal
        filtering.
        """
        profile = self.client_profile(client)
        _, spec = self.model_class(model)
        client_drops = set(profile.get("drop_groups") or [])
        model_denies = set(spec.get("denied_groups") or [])
        return client_drops & model_denies

    # ── task classification (Phase D) ───────────────────────────────────────
    def classify_task(self, text):
        """(class_name, groups, hits) for a request, or (None, None, 0).

        Substring matching, case-insensitive, first class with enough hits wins.
        Returns groups=None when unclassified, which the caller MUST treat as
        "no opinion" and leave the Phase B allowlist alone. Returning an empty
        set instead would mean "this task needs nothing", which is the dangerous
        misreading — a misclassified task that loses its tools produces a stuck
        agent, while an unnecessary tool only costs tokens.
        """
        spec = self.doc.get("task_classes") or {}
        always = set(spec.get("always") or [])
        if not text:
            return None, None, 0
        lowered = text.lower()
        for cls in spec.get("classes") or []:
            hits = sum(1 for pat in cls.get("patterns") or []
                       if str(pat).lower() in lowered)
            if hits >= int(cls.get("min_confidence_hits", 1) or 1):
                return (cls.get("name"),
                        always | set(cls.get("groups") or []), hits)
        return None, None, 0

    def task_always_groups(self):
        return set((self.doc.get("task_classes") or {}).get("always") or [])

    # ── schema rewrites (Phase C) ───────────────────────────────────────────
    def rewrite_rules(self, client):
        """Effective rewrite rules for a client: defaults overlaid by the
        client's own `schema_rewrites` block, if any.

        Returns a dict with enabled/strip_keys/max_description_chars/
        max_param_description_chars/truncation_marker. When the registry says
        nothing, `enabled` is False — a rewrite the operator never configured
        must not happen."""
        block = self.doc.get("schema_rewrites") or {}
        rules = dict(block.get("defaults") or {})
        rules.update(dict(self.client_profile(client).get("schema_rewrites") or {}))
        rules.setdefault("enabled", False)
        rules.setdefault("strip_keys", [])
        rules.setdefault("max_description_chars", None)
        rules.setdefault("max_param_description_chars", None)
        rules.setdefault("truncation_marker", " ...")
        return rules

    def mutating_tools(self):
        """(definite, ambiguous) name sets for the verification pipeline."""
        spec = self.doc.get("mutating_tools") or {}
        return (set(spec.get("definite") or []),
                set(spec.get("ambiguous") or []))

    def describe(self):
        """Compact state summary for metrics and doctor output."""
        return {
            "state": self.state,
            "error": self.error,
            "path": self.path,
            "groups": len(self.doc.get("groups") or {}),
            "model_classes": len(self.doc.get("model_classes") or {}),
            "clients": len(self.doc.get("clients") or {}),
            "routes": len(self.doc.get("routes") or {}),
            "contexts_from_generated": len(self.contexts),
            "aliases": len(self.alias),
        }
