# Working against a local model

Everything here exists because the model is LOCAL. Generic engineering practice
— read before editing, small diffs, match existing conventions, don't refactor
unrelated code — is not repeated: current coding models already do it, and the
shared operating instructions the proxy injects cover the rest.

## Context

Local models degrade well before their advertised window. Keep sessions short
and diffs small; a session that was fast can stall once a turn misses the KV
cache. Prefer targeted reads and summaries over whole files, and start a fresh
session rather than growing one past the point where responses slow down.

## Verification

- Run the real command; paste the real output. A local model asked to predict a
  result will produce a plausible one.
- Shell scripts: `bash -n <script>` before calling it done.
- After changing a profile or any generated config, regenerate and confirm the
  regeneration produces **zero diff**.
- `ailocal check` (0 healthy, 1 unresolved profile, 2 degraded) is the final
  sanity check when runtime configuration changed.
- If verification fails twice, stop and report. Local models loop on a failing
  approach more readily than hosted ones.

## Secrets

Never commit `.env*` or a key. The generated environment lives in
`$AILOCAL_STATE/env` and your own keys in `~/.config/ailocal/.env.local`;
neither belongs in a diff.
