#!/usr/bin/env python3
"""replay-tool-captures.py — run the real captured client payloads through the
real policy, offline.

This is the step between "the policy parses" and "turn FILTER on in production".
It answers, per captured payload: exactly which tools would disappear, how many
bytes that saves at the BACKEND (not merely on the wire), and — most usefully —
which retained tools are the expensive ones left.

It deliberately reports the drop list in full. A summary percentage is easy to
be wrong about quietly; a list of 20 tool names is something you can read and
object to.

Run inside the proxy container, where PyYAML and the mounted policy both exist:

    scripts/replay-tool-captures.sh

Captures come from AILOCAL_TOOL_GATEWAY_CAPTURE=/app/captures (see the compose
file); they are real client requests, never hand-written.
"""

import glob
import importlib.util
import json
import os
import sys

MODULE = os.environ.get("AILOCAL_GATEWAY_MODULE",
                        "/app/config/tool_gateway.py")
CAPTURES = os.environ.get("AILOCAL_CAPTURES", "/app/captures")
POLICY = os.environ.get("AILOCAL_TOOL_POLICY", "/app/config/tool-policy.yaml")

_spec = importlib.util.spec_from_file_location("tool_gateway", MODULE)
tg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tg)

policy = tg.Policy(POLICY)
if not policy.loaded:
    print(f"REFUSING TO REPORT: policy state is '{policy.state}' "
          f"({policy.error or POLICY}).")
    print("Without a loaded policy there is nothing to replay and any "
          "'savings' figure would be fabricated.")
    sys.exit(1)

gw = tg.ToolGateway(policy=policy, alias=tg.load_alias_map())

files = sorted(glob.glob(os.path.join(CAPTURES, "*.json")))
if not files:
    print(f"No captures in {CAPTURES}. Run a real client with "
          f"AILOCAL_TOOL_GATEWAY_CAPTURE set first — do not substitute "
          f"synthetic payloads here.")
    sys.exit(1)

# Only the largest capture per (client, route) is interesting: the small ones
# are follow-up turns that re-declare a subset. Keeping the max avoids reporting
# an averaged figure that describes no actual request.
best = {}
for path in files:
    doc = json.load(open(path))
    rep = doc.get("report") or {}
    key = (rep.get("client"), rep.get("route"))
    if key not in best or rep.get("bytes_in", 0) > best[key][1].get("bytes_in", 0):
        best[key] = (doc, rep)

print(f"Policy: {POLICY} ({len(policy.rules)} rules)")
print(f"Captures: {len(files)} files, {len(best)} distinct client/route pairs\n")

for (client, route), (doc, rep) in sorted(best.items(), key=lambda kv: str(kv[0])):
    data = {"model": doc.get("model"), "tools": doc.get("tools") or []}
    if route == "/v1/responses":
        data["input"] = ""
    call_type = ("anthropic_messages" if route == "/v1/messages"
                 else "aresponses" if route == "/v1/responses" else "acompletion")
    # detect_client reads headers, which captures do not retain; the capture
    # already recorded the detected client, so replay it explicitly rather than
    # letting it silently degrade to "unknown" and match no rule.
    data["proxy_server_request"] = {"headers": {
        "user-agent": "claude-cli/replay" if client == "claude-code" else "",
        "originator": "codex_cli_rs" if client == "codex" else ""}}

    new, keep = gw.measure(data, call_type)
    if new["client"] != client:
        print(f"!! replay detected client '{new['client']}' but the capture "
              f"recorded '{client}' — rule matching would differ in production")

    base = new["bytes_reachable"]
    saved = new["bytes_dropped"]
    pct = (100.0 * saved / base) if base else 0.0

    print("=" * 72)
    print(f"{client}  {route}   model={new['model']}  "
          f"capability={new['capability']}")
    print(f"  declared      {new['tools_in']:3} tools  {new['bytes_in']:7} B")
    if new["bytes_prefiltered_by_litellm"]:
        print(f"  of which LiteLLM already discards: "
              f"{new['bytes_prefiltered_by_litellm']} B "
              f"(namespace/shell types — never reach the model)")
    print(f"  reaches model {new['tools_reachable']:3} tools  {base:7} B  "
          f"<- the cost base")
    print(f"  gateway drops {new['tools_dropped']:3} tools  {saved:7} B  "
          f"= {pct:.1f}% of what the model would have seen")
    if new["bytes_dropped_moot"]:
        print(f"  (a further {new['bytes_dropped_moot']} B dropped were "
              f"already moot — not counted above)")
    print(f"  tokens_est    {new['tokens_est_in']} -> {new['tokens_est_kept']} "
          f"({new['tokenizer']}; approximate, see calibrate-tokens.py)")

    enc = tg.tool_bytes
    idx = {tg.tool_name(t): enc(t) for t in data["tools"]}
    print(f"\n  DROPPED ({len(new['dropped_names'])}):")
    for n in sorted(new["dropped_names"], key=lambda n: -idx.get(n, 0)):
        print(f"    - {n:42} {idx.get(n, 0):6} B")
    print(f"\n  KEPT, most expensive first:")
    for n, s, _t in sorted(keep, key=lambda k: -k[1])[:12]:
        print(f"    + {n:42} {s:6} B")
    print()
