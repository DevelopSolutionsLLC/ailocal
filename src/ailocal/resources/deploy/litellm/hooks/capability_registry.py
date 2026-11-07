"""Load registry.yaml and answer capability questions; owns its shape.

The negotiator holds no conditional on a model, client or tool name, so swapping
one is a config edit. Commitments:

1. Fail open: absent, unparseable or silent registry → change nothing.
2. `absent` / `unavailable` (no PyYAML) / `error` (malformed) stay distinct.
3. max_context comes from capabilities.generated.json; the registry's is only a
   fallback for classes w/o a generated entry (cloud models).
4. Classify by backend model, then capability, then raw name, then `unknown`:
   roles share weights, so role-first gave one model conflicting classes.
"""

import fnmatch
import json
import os

REGISTRY_PATH = os.environ.get("AILOCAL_REGISTRY",
                               "/app/config/registry.yaml")
CAPS_JSON = os.environ.get("AILOCAL_CAPABILITIES_JSON",
                           "/app/generated/capabilities.json")
CONFIG_PATH = os.environ.get("AILOCAL_CONFIG_PATH", "/app/generated/config.yaml")


def truthy_flag(v):
    """Return True for YAML `true`/`yes`/`1`; a typo is False, keeping tools."""
    return str(v).strip().lower() in ("1", "true", "yes", "on")


class Registry:
    """Loaded capability registry; read-only after construction."""

    def __init__(self, path=None, caps_json=None, config_path=None):
        self.path = path or REGISTRY_PATH
        self.state = "absent"
        self.error = None
        self.doc = {}
        self.contexts = {}      # capability -> max_context (generated)
        self.alias = {}         # compat name -> ailocal-<capability>
        self.backends = {}      # capability -> backing model (generated)
        self._load(caps_json or CAPS_JSON, config_path or CONFIG_PATH)

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
            # capability → backing model (commitment 4); patterns name models,
            # so the provider prefix is stripped.
            for entry in (cfg.get("model_list") or []):
                name = entry.get("model_name") or ""
                backend = ((entry.get("litellm_params") or {}).get("model")
                           or "")
                if not name or not backend:
                    continue
                if name.startswith("ailocal-"):
                    name = name[len("ailocal-"):]
                self.backends[name] = backend.split("/", 1)[-1]
        except Exception:
            pass

    @property
    def loaded(self):
        return self.state == "loaded"

    def capability_of(self, model):
        """Map a requested model name to its capability (model_group_alias)."""
        name = self.alias.get(model, model) or ""
        return name[len("ailocal-"):] if name.startswith("ailocal-") else name

    def backend_of(self, model):
        """Return the model serving a requested name; None means not hosted."""
        return self.backends.get(self.capability_of(model))

    def model_class(self, model):
        """Return (class_name, class_dict) in commitment-4 order.

        (None, {}) means no opinion.
        """
        classes = (self.doc.get("model_classes") or {})
        capability = self.capability_of(model)
        backend = self.backend_of(model)

        def by_name(candidate, allow_universal):
            for name, spec in classes.items():
                for pattern in spec.get("match_models") or []:
                    # `*` is last resort: winning here would starve the
                    # capability stage (local_completion would be unreachable).
                    if pattern == "*" and not allow_universal:
                        continue
                    if candidate and fnmatch.fnmatch(candidate, pattern):
                        return name, spec
            return None, None

        name, spec = by_name(backend, False)
        if spec is not None:
            return name, spec
        for name, spec in classes.items():
            if capability and capability in (spec.get("match_capabilities") or []):
                return name, spec
        name, spec = by_name(model, True)
        if spec is not None:
            return name, spec
        return None, {}

    def supports(self, model, feature):
        """Look up a capability flag; None (unknown) must not read as False."""
        _, spec = self.model_class(model)
        return spec.get("supports_" + feature)

    def is_passthrough(self, model):
        """Return whether the model is forwarded as-is; unknown ones are."""
        name, spec = self.model_class(model)
        if name is None:
            return True
        return bool(spec.get("passthrough"))

    def max_context(self, model):
        """Return the generated context window, else the class's own."""
        capability = self.capability_of(model)
        if capability in self.contexts:
            return self.contexts[capability]
        _, spec = self.model_class(model)
        return spec.get("max_context")

    def routing_hints(self, model):
        _, spec = self.model_class(model)
        return dict(spec.get("routing_hints") or {})

    def group_members(self, group):
        return list((self.doc.get("groups") or {}).get(group) or [])

    def all_groups(self):
        """Return every declared group name; callers never read `self.doc`."""
        return set(self.doc.get("groups") or {})

    def expand(self, groups):
        """Expand group names to the tool names/patterns they contain."""
        out = set()
        for g in groups or []:
            out.update(self.group_members(g))
        return out

    @staticmethod
    def matches(name, patterns):
        """Return whether a name/prefix pattern set covers a tool name."""
        if not name:
            return False
        if name in patterns:
            return True
        return any(p.endswith("*") and name.startswith(p[:-1])
                   for p in patterns)

    def group_of(self, name):
        """Return a tool's group (for reports), or None."""
        for group in (self.doc.get("groups") or {}):
            if self.matches(name, set(self.group_members(group))):
                return group
        return None

    def is_protected(self, name):
        """Return whether a tool survives every rule; unnamed entries do."""
        if name and name.startswith("<") and name.endswith(">"):
            return True
        return self.matches(name, set(self.doc.get("protected") or []))

    def route_for_call_type(self, call_type, has_input_key=False):
        """Resolve the API dialect from the proxy's call_type.

        `has_input_key` marks /v1/responses (top-level `input`); passed in to
        keep this a pure lookup.
        """
        routes = self.doc.get("routes") or {}
        for route, spec in routes.items():
            if call_type and call_type in (spec.get("call_types") or []):
                return route
        return "/v1/responses" if has_input_key else "/v1/chat/completions"

    def route_drops_type(self, route, tool_type):
        """Return whether LiteLLM drops this tool type on the route itself."""
        spec = (self.doc.get("routes") or {}).get(route) or {}
        return tool_type in (spec.get("drops_tool_types") or [])

    def result_status_mode(self, route):
        spec = (self.doc.get("routes") or {}).get(route) or {}
        return spec.get("result_status", "unknown")

    def detect_client(self, headers):
        """Identify the client from headers, or "unknown"; never by model."""
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

    def removable_groups(self, client, model):
        """Return groups both client and model class agree to drop.

        Intersection, so neither side strips what the other needs; an unknown
        participant contributes nothing and nothing is filtered.
        """
        profile = self.client_profile(client)
        _, spec = self.model_class(model)
        client_drops = set(profile.get("drop_groups") or [])
        model_denies = set(spec.get("denied_groups") or [])
        return client_drops & model_denies

    def namespace_expansion(self):
        """Return namespace-flattening config; off by default (renames)."""
        cfg = dict(self.doc.get("namespace_expansion") or {})
        cfg.setdefault("enabled", False)
        cfg.setdefault("source_type", "namespace")
        cfg.setdefault("name_template", "{namespace}__{tool}")
        cfg.setdefault("only_groups", [])
        cfg.setdefault("max_tools_per_namespace", 40)
        return cfg

    def classify_task(self, text):
        """Return (class_name, groups, hits), or (None, None, 0).

        First class with enough case-insensitive substring hits wins. None
        groups means no opinion, never "needs nothing": lost tools stall.
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
                # `override_always` drops below the floor, opt-in per class:
                # conversational asks otherwise searched the repository first.
                groups = set(cls.get("groups") or [])
                if not truthy_flag(cls.get("override_always")):
                    groups = always | groups
                return cls.get("name"), groups | self._mentioned(spec, lowered), hits
        return None, None, 0

    @staticmethod
    def _mentioned(spec, lowered):
        """Return groups the request names (mention_overrides); beat topic."""
        out = set()
        for group, pats in (spec.get("mention_overrides") or {}).items():
            if any(str(p).lower() in lowered for p in pats or []):
                out.add(group)
        return out

    def task_always_groups(self):
        return set((self.doc.get("task_classes") or {}).get("always") or [])

    def rewrite_rules(self, client):
        """Return defaults overlaid by the client's `schema_rewrites`.

        `enabled` is False unless configured.
        """
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
        """Return (definite, ambiguous) mutating-tool name sets."""
        spec = self.doc.get("mutating_tools") or {}
        return (set(spec.get("definite") or []),
                set(spec.get("ambiguous") or []))

    def loop_guard_limit(self, model):
        """Return the reasoning-char limit for a looping turn, or None.

        Keyed on backend (commitment 4): only Gemma was measured looping.
        """
        spec = self.doc.get("reasoning_loop_guard") or {}
        limit = spec.get("reasoning_chars")
        backend = self.backend_of(model)
        if not backend or not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            return None
        return limit if self.matches(backend, spec.get("backends") or []) else None

    def describe(self):
        """Return a compact state summary for metrics and doctor."""
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
