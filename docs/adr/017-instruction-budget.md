# ADR 017: Local client roots publish an instruction budget; delegated denials are reported

**Status:** Accepted · **Date:** 2026-10-29 · **Scope:** #39, DevelopSolutionsLLC/cadence#64

## Context

ailocal is the base install and runs without Cadence; Cadence enhances it and runs without ailocal. Their only seam is the versioned `integration-contract.json` ailocal publishes and Cadence reads, failing closed on an unknown schema. Cadence composes `CLAUDE.md` and `AGENTS.md` into ailocal's client roots; ailocal writes neither.

[REAL claude-local 2.1.308, 32gb Qwen3.5 9B with thinking, runtime 0.40.8-ailocal.18631.4; APPROX three fixture tasks, 2026-10-29] Those roots carried the same 17 KB policy as hosted ones. With auto memory on, every run first read the task file under the auto-memory directory and 3/6 left it unchanged. With auto memory off, 4/6 passed: one run re-read `CLAUDE.md` about 90 times until the timeout, one wrote the task file into the client root. With that file excluded through `claudeMdExcludes`, 9/9 passed in about 36 s instead of 55 s. A delegated Claude child re-issued one denied `Read` of the same file 228 times.

In each failing run the model then replied "DONE". Claude Code reports a headless run whose tool calls were denied as `subtype: success`, exit 0 (anthropics/claude-code#76867, #90578); the `permission_denials` array of the result message is the authoritative record. Codex `exec --json` can omit sandbox-denied calls from its stream (openai/codex#47433, #54782).

## Decision

1. **Each product enforces its own side; neither requires the other.**
   - ailocal publishes `instructions.<client>.max_bytes` (contract schema 3) from `[claude]`/`[codex] instruction_max_bytes` in `clients.toml`, default 4096. It is a measured fact about its models, not a policy for anyone's content.
   - Cadence, when a root publishes a budget, composes a compact policy for it and refuses to write a file over budget (cadence#64). Without a budget, or without ailocal, it composes exactly as before.
   - `ailocal check` warns about an instruction file over budget in its roots, whoever wrote it, naming the owner from the file's header. It never rewrites the file.
2. **`claude-local` defaults `autoMemoryEnabled: false`** in its own settings root, as delegation already did; an explicit operator setting wins. Hosted `~/.claude` is untouched.
3. **Delegated Claude runs with `--permission-prompts none`**, which tells the model that nobody can approve a denied call and not to retry it, and the result reports `permission_denials` (tool and clipped target). A reply claiming success beside refused writes is shown as such; ailocal still makes no task-success verdict.

## Consequences

The full policy stays available to hosted clients; local models receive a compact form sized to what they handle. A Cadence without schema-3 support fails closed on the new contract, leaving its ailocal targets `BLOCKED` until upgraded; ailocal itself is unaffected. Codex denials remain invisible where its JSON stream omits them, so the Codex result reports none; that limit is upstream's.

## Alternatives

- **ailocal excludes the file with `claudeMdExcludes`.** It works (9/9) but makes ailocal silently override a co-installed tool's content, and leaves Codex's `AGENTS.md` unaddressed.
- **ailocal writes its own instruction file.** Two writers for one path, and ailocal must stay usable with no policy at all.
- **Fail a delegation on any denial.** Some denials are harmless exploration; the caller decides with the record in hand.

## Sources

- [Claude Code `claudeMdExcludes`](https://code.claude.com/docs/en/settings-reference#claudemdexcludes), [auto memory](https://code.claude.com/docs/en/memory#enable-or-disable-auto-memory), [`--permission-prompts none`](https://code.claude.com/docs/en/headless#turn-off-permission-prompts-in-unattended-runs), [`permission_denials`](https://code.claude.com/docs/en/agent-sdk/typescript#sdkpermissiondeniedmessage)
- anthropics/claude-code#76867, #90578; openai/codex#47433, #54782
