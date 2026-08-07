#!/usr/bin/env bash
# compact-hook.sh — records that compaction happened, and nothing else.
#
# Deliberately METADATA ONLY. The hook payload carries the generated compact
# summary and a transcript path; writing those would persist private conversation
# text outside the client's own storage. This records trigger and summary SIZE so
# compaction can be confirmed, never what was said.
#
# The first version had TWO stdin redirections on one command (a heredoc for the
# script plus a here-string for the payload). The here-string won, python received
# the payload as its SCRIPT, and the hook silently wrote nothing -- it would have
# reported "no compaction" for a session that compacted. Payload now arrives on a
# pipe and the script is passed with -c.
set -euo pipefail
# Deployed outside the checkout, so it cannot call lib/profile-config.
# It honours AILOCAL_STATE and otherwise mirrors policy.state_root().
STATE="${AILOCAL_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/ailocal}"
mkdir -p "$STATE"
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
