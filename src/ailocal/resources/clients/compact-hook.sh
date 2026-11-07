#!/usr/bin/env bash
# Record compaction metadata only (size, trigger), never conversation text.
set -euo pipefail
# Mirrors policy.state_root(); deployed outside the package.
STATE="${AILOCAL_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/ailocal}"
mkdir -p "$STATE"
# Script via -c: a heredoc would compete with the payload for stdin.
python3 -c '
import json,sys,datetime
out=sys.argv[1]
try: d=json.load(sys.stdin)
except Exception: d={}
rec={"ts":datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
     "session":(d.get("session_id") or "")[:8],
     "trigger":d.get("trigger") or d.get("compact_trigger"),
     "summary_chars":len(d.get("compact_summary") or ""),
     "hook":d.get("hook_event_name")}
open(out,"a").write(json.dumps(rec)+"\n")
' "$STATE/compaction.jsonl" 2>/dev/null || true
exit 0
