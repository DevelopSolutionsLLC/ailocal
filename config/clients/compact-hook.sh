#!/usr/bin/env bash
# compact-hook.sh — records that compaction happened, and nothing else.
#
# Deliberately METADATA ONLY. The hook payload contains the generated compact
# summary and session details; writing those to disk would persist private source
# content and conversation text outside the client's own storage. This records
# size and trigger so compaction can be VERIFIED, never what was said.
set -euo pipefail
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/ailocal"
mkdir -p "$STATE"
payload="$(cat)"
python3 - "$STATE/compaction.jsonl" <<'PY' <<<"$payload" 2>/dev/null || true
import json,sys,datetime
out=sys.argv[1]
try: d=json.load(sys.stdin)
except Exception: d={}
rec={"ts":datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
     "session":(d.get("session_id") or "")[:8],          # prefix only
     "trigger":d.get("trigger") or d.get("compact_trigger"),
     "summary_chars":len(d.get("compact_summary") or ""), # SIZE, not content
     "hook":d.get("hook_event_name")}
open(out,"a").write(json.dumps(rec)+"\n")
PY
exit 0
