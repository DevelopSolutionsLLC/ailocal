
# Working with a local model

## Context

Local models degrade well before their advertised context window. Keep sessions short and diffs small; a fast session can stall once a turn misses the KV cache. Prefer targeted reads over whole files, and start a fresh session once responses slow down.

## Verification

- Run the real command and report its real output; never predict a result.
- Check shell scripts with `bash -n <script>` before calling them done.
- After changing a profile or any generated configuration, regenerate and confirm a second regeneration produces zero diff.
- When runtime configuration changed, finish with `ailocal check` (0 healthy, 1 unresolved profile, 2 degraded).
- If verification fails twice, stop and report rather than retrying the same approach.

## Secrets

Never commit `.env*` files or keys. The generated environment lives in `$AILOCAL_STATE/env` and personal keys in `~/.config/ailocal/.env.local`; neither belongs in a diff.
