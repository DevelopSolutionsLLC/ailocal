"""Recent request traces for the status dashboard."""
import glob
import json
import os
import sys
import time

rows = []
for f in glob.glob(os.path.join(sys.argv[1], "*.jsonl")):
    for line in open(f, encoding="utf-8", errors="replace"):
        try:
            rows.append(json.loads(line))
        except Exception:
            pass

if not rows:
    print("  \033[2m—\033[0m no traces yet")
    raise SystemExit

fails = [r for r in rows if r.get("outcome") == "failure"]
# >60s to first byte is the shape that produces a client-side "API error" while
# every server component reports success. Surfaced here so it is noticed without
# anyone having to already know to look for it.
slow = [r for r in rows
        if isinstance(r.get("ttfb_ms"), (int, float)) and r["ttfb_ms"] > 60000]

for r in rows[-3:]:
    when = time.strftime("%H:%M:%S", time.localtime(r.get("ts") or 0))
    t = r.get("ttfb_ms")
    tt = f"{t:.0f}ms" if isinstance(t, (int, float)) else "-"
    print(f"  {when}  {str(r.get('client') or '?'):11} "
          f"{str(r.get('capability') or '?'):13} ttfb={tt:>9}  {r.get('outcome')}")

msg = f"  {len(rows)} trace(s)"
if fails:
    msg += f", \033[31m{len(fails)} failure(s)\033[0m"
if slow:
    msg += f", \033[33m{len(slow)} with >60s first byte\033[0m"
print(msg)
if fails or slow:
    print("  -> ailocal trace --failures")
