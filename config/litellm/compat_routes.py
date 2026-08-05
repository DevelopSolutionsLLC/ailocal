"""
compat_routes.py — client compatibility endpoints LiteLLM does not implement.

Claude Code is pointed at this proxy through ANTHROPIC_BASE_URL. It does not
send only `/v1/messages`: several auxiliary probes are built against the shape of
api.anthropic.com, and one of them is derived from ANTHROPIC_BASE_URL, so it
lands here. LiteLLM has no such route, so it 404s:

    INFO: 172.18.0.1:58848 - "HEAD /api/hello HTTP/1.1" 404 Not Found

The probe originates in the client, not in this middleware. claude-cli has three
`<base>/api/hello` call sites:

  a. HEAD, base = `ANTHROPIC_BASE_URL || BASE_API_URL`. The one that arrives
     here. Skipped entirely when a proxy / unix socket / client cert /
     Bedrock-style env var is set, and its result is discarded
     (`.catch(()=>{})`, no status inspection).
  b. GET, base = BASE_API_URL — connectivity telemetry, classified `http_<status>`.
  c. GET, base = BASE_API_URL — `preflight_endpoint`, which treats any status
     != 200 as a connection failure.

(b) and (c) read the BUILT-IN base, so they do not reach this proxy. GET is
served anyway: it costs nothing, and a future client build that redirects either
onto ANTHROPIC_BASE_URL gets a 200 rather than a fresh 404.

HONEST SCOPE — what this does and does not fix
----------------------------------------------
It removes the 404 from the proxy log and makes the probe succeed. Note that
call site (a), the only one that actually arrives here, discards its result
(`.catch(()=>{})` with no status inspection), so this route is NOT proven to be
what silences any particular in-terminal API warning. Serving a 200 to a probe
the client asks for is correct on its own terms; treat warning removal as a
separate, separately-verified claim.

DESIGN CONSTRAINTS
------------------
- No model invocation, no router lookup, no backend call.
- No authentication dependency: this is a reachability probe, and the client
  sends it before/without credentials. It discloses nothing — a fixed empty 200.
- Static response. Deliberately NOT an internal redirect to /health/liveliness:
  a probe answer must not depend on, or be confused with, real health state.
- Registered on the live FastAPI app; no LiteLLM package file is patched.
- Idempotent: re-importing the module never double-registers the route.
- Touches nothing under /v1/*, no existing health route, and no routing config.

Listing this module as a `litellm_settings.callbacks` entry is only the mechanism
that makes LiteLLM import it; the class below is an intentional no-op. Same
pattern as model_registrar.
"""

import logging
import sys

from litellm.integrations.custom_logger import CustomLogger

log = logging.getLogger(__name__)

# Path -> methods. Keep this table tiny and boring: every entry is a static 200
# with an empty body, and anything needing real logic does not belong here.
COMPAT_ROUTES = {
    "/api/hello": ["GET", "HEAD"],
}


def _empty_ok():
    """Endpoint body. Fixed 200, no content, no side effects."""
    from fastapi import Response

    return Response(status_code=200)


def _get_app():
    """Return the running proxy's FastAPI app, or None.

    Prefer the already-imported module. LiteLLM loads callbacks from its own
    startup path, so `litellm.proxy.proxy_server` is normally in sys.modules
    already and this is a dict lookup. The import fallback exists for the
    standalone-import case (the test-all hook-importability check), where
    nothing has imported the proxy yet.
    """
    mod = sys.modules.get("litellm.proxy.proxy_server")
    if mod is None:
        try:
            from litellm.proxy import proxy_server as mod  # type: ignore[no-redef]
        except Exception as exc:  # pragma: no cover - depends on litellm layout
            log.error(
                "compat_routes: cannot import litellm.proxy.proxy_server "
                "(%s: %s). Compatibility routes are NOT registered; %s will "
                "keep returning 404.",
                type(exc).__name__,
                exc,
                ", ".join(sorted(COMPAT_ROUTES)),
            )
            return None
    return getattr(mod, "app", None)


def register_compat_routes():
    """Add the compatibility routes to the proxy app. Returns the paths added.

    Never raises: a failure here must not take the proxy down over a probe
    endpoint. It logs at ERROR instead, because a silent no-op would leave the
    original 404 in place with nothing to say so.
    """
    app = _get_app()
    if app is None:
        log.error(
            "compat_routes: no FastAPI app found on litellm.proxy.proxy_server. "
            "Compatibility routes are NOT registered."
        )
        return []

    existing = {getattr(r, "path", None) for r in app.routes}
    added = []
    for path, methods in COMPAT_ROUTES.items():
        if path in existing:
            # Either we already ran, or LiteLLM grew its own implementation.
            # Both mean: leave it alone. Self-retiring by construction.
            log.info("compat_routes: %s already registered, leaving it alone", path)
            continue
        try:
            app.add_api_route(
                path,
                _empty_ok,
                methods=methods,
                include_in_schema=False,
            )
        except Exception as exc:
            log.error(
                "compat_routes: failed to register %s (%s: %s)",
                path,
                type(exc).__name__,
                exc,
            )
            continue
        added.append(path)

    if added:
        log.info(
            "compat_routes: registered %d client compatibility route(s): %s",
            len(added),
            ", ".join(f"{p} [{'/'.join(COMPAT_ROUTES[p])}]" for p in added),
        )
    return added


class CompatRoutes(CustomLogger):
    """No-op logger. Registration happens at import; this exists so the module
    can be listed in litellm_settings.callbacks, which is what triggers the
    import. It observes nothing and mutates no request."""


# Run at import. LiteLLM loads callbacks during config load, before the server
# accepts traffic, and Starlette resolves app.routes per request — so a route
# added at this point is served from the first request onward.
REGISTERED = register_compat_routes()

proxy_handler_instance = CompatRoutes()
