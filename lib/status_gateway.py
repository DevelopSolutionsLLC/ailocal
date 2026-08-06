"""Last gateway negotiation, read from the gateway's own metric stream.
Kept in a file rather than inlined in the shell: nested heredoc quoting has
broken this repo's scripts twice, and a helper that cannot parse is worse than
one that is slightly less convenient to read."""
import json
import sys

rows = []
for line in sys.stdin:
    if "tool_gateway_metric " not in line:
        continue
    try:
        d = json.loads(line.split("tool_gateway_metric ", 1)[1])
    except Exception:
        continue
    if not d.get("event"):
        rows.append(d)

if not rows:
    print("  \033[2m—\033[0m no gateway activity in this window "
          "(not evidence of a problem)")
else:
    d = max(rows, key=lambda r: r.get("bytes_in", 0))
    base = d.get("bytes_reachable") or 1
    got = d.get("bytes_kept_reachable")
    if got is None:
        got = d.get("bytes_kept") or 0
    cut = 100.0 * (base - got) / base
    print(f"  \033[32m✓\033[0m last request   {d.get('client')}  "
          f"{d.get('tools_in')} -> {d.get('tools_kept')} tools, "
          f"{base} -> {got} B ({cut:.0f}% cut)")
    dropped = d.get("dropped_groups") or []
    if dropped:
        print(f"                 removed: {', '.join(dropped)}")
    print(f"  {len(rows)} request(s) seen; ailocal metrics for detail")
