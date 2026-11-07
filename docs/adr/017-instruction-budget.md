# ADR 017: Local client roots publish an instruction budget; delegated denials are reported

**Status:** Accepted · **Date:** 2026-10-29 · **Scope:** #39, #47, DevelopSolutionsLLC/cadence#64

## Context

ailocal is the base install and runs without Cadence; Cadence enhances it and runs without ailocal. Their only seam is the versioned `integration-contract.json` ailocal publishes and Cadence reads, failing closed on an unknown schema. Cadence composes `CLAUDE.md` and `AGENTS.md` into ailocal's client roots; ailocal writes neither.

[REAL claude-local 2.1.308, 32gb Qwen3.5 9B with thinking, runtime 0.40.8-ailocal.18631.4; APPROX three fixture tasks, 2026-10-29] Those roots carried the same 17 KB policy as hosted ones. With auto memory on, every run first read the task file under the auto-memory directory and 3/6 left it unchanged. With auto memory off, 4/6 passed: one run re-read `CLAUDE.md` about 90 times until the timeout, one wrote the task file into the client root. With that file excluded through `claudeMdExcludes`, 9/9 passed in about 36 s instead of 55 s. A delegated Claude child re-issued one denied `Read` of the same file 228 times.

In each failing run the model then replied "DONE". Claude Code reports a headless run whose tool calls were denied as `subtype: success`, exit 0 (anthropics/claude-code#76867, #90578); the `permission_denials` array of the result message is the authoritative record. Codex `exec --json` can omit sandbox-denied calls from its stream (openai/codex#47433, #54782).

## Decision

1. **Each product enforces its own side; neither requires the other.**
   - ailocal publishes `instructions.<client>.max_bytes` (contract schema 3) from `[claude]`/`[codex] instruction_max_bytes` in `clients.toml`, default 4096, replaced for a tier by `instruction_max_bytes_by_tier` where that tier was measured (#47). It is a measured fact about its models, not a policy for anyone's content. The published value is the budget unchanged: subtracting the guide hook's payload (#50) left 1,333 bytes, below Cadence's compact policy, so the root carried none (#56).
   - Cadence, when a root publishes a budget, composes a compact policy for it and refuses to write a file over budget (cadence#64). Without a budget, or without ailocal, it composes exactly as before.
   - `ailocal check` warns when the always-on instructions in its roots exceed the budget, whoever wrote them, naming the file's owner from its header. It never rewrites the file. The total includes the output of every hook ailocal registers there, because each client adds it to the same context: the scratchpad hook in both roots and, in the Claude root, the guide hook. The guide hook's larger case, launch-default model or no model, is the one counted (#60).
2. **`claude-local` defaults `autoMemoryEnabled: false`** in its own settings root, as delegation already did; an explicit operator setting wins. Hosted `~/.claude` is untouched.
3. **Delegated Claude runs with `--permission-prompts none`**, which tells the model that nobody can approve a denied call and not to retry it, and the result reports `permission_denials` (tool and clipped target). A reply claiming success beside refused writes is shown as such; ailocal still makes no task-success verdict.

## Consequences

The full policy stays available to hosted clients; local models receive a compact form sized to what they handle. A Cadence without schema-3 support fails closed on the new contract, leaving its ailocal targets `BLOCKED` until upgraded; ailocal itself is unaffected. Codex denials remain invisible where its JSON stream omits them, so the Codex result reports none; that limit is upstream's.

[REAL 2026-11-04, shipped profiles] The guide hook prints 2,092–3,087 bytes for a named model and 6,994–9,544 bytes when the event names none, so a CLAUDE.md that Cadence fits to 4,096 bytes still leaves the always-on total over budget. Cadence enforces the published budget on the file it composes; the hook payload is ailocal's to reduce or to measure a larger budget for. `ailocal check` reports the combined total; #47 measures the 128gb budget before either side shrinks.

[REAL 2026-11-04, shipped profiles] #60 moved the guide's reference data out of the hook: it now prints 1,368–1,742 bytes for a named model and 913–939 bytes when the event names none; the scratchpad hook adds 351 bytes in either root. With Cadence's 2,812-byte compact CLAUDE.md the Claude root totals 4,905 of 4,096 bytes on the 64/128gb tiers, down from 10,556 worst case.

[REAL 2026-11-04, captured first request] The budget did not see a larger leak: with cwd under `$HOME`, Claude Code also loaded the hosted `~/.claude/CLAUDE.md` and `~/.claude/rules/` as ancestor project memory, 13,335 bytes. #62 excludes those two hosted paths by absolute path with `claudeMdExcludes`. That is not the alternative rejected below: the local root's own file still loads, and nothing a co-installed tool writes into this root is overridden. #66 applies the same mechanism to tool schemas: the eight grepai tools Cadence does not expose to codex-local or Copilot are denied by name in claude-local, 7.2 KB per request.

The 4,096-byte budget was measured on the 32gb tier with Qwen3.5 9B and still governs every tier without its own measurement.

[REAL claude-local, 128gb gemma4:26b-qat-mtp on stock Ollama 0.40.14, 2026-11-07; APPROX three fixture tasks] #47 measured the 128gb tier at two always-on sizes: the shipped root (2,487-byte Cadence `CLAUDE.md` + guide and scratchpad hooks ≈ 4.6 KB) and the same root with `CLAUDE.md` padded to 6,044 bytes (≈ 8.2 KB). Both arms edited the fix task correctly 3/3 and kept the scope task inside its file 3/3; neither re-read the instruction file or worked in the client root. On a task whose only write was denied, both arms failed 5/5, by delegating to a background agent, repeating one call up to 365 times, or claiming a file was created, so that failure does not depend on size and the denial reporting of decision 3 remains the guard. `clients.toml` therefore sets `claude.instruction_max_bytes_by_tier = { "128gb" = 8192 }`, published in the contract and checked by `ailocal check` for that tier only. Codex and the other tiers keep 4,096 bytes: none was measured.

## Alternatives

- **ailocal excludes the file with `claudeMdExcludes`.** It works (9/9) but makes ailocal silently override a co-installed tool's content, and leaves Codex's `AGENTS.md` unaddressed.
- **ailocal writes its own instruction file.** Two writers for one path, and ailocal must stay usable with no policy at all.
- **Fail a delegation on any denial.** Some denials are harmless exploration; the caller decides with the record in hand.

## Sources

- [Claude Code `claudeMdExcludes`](https://code.claude.com/docs/en/settings-reference#claudemdexcludes), [auto memory](https://code.claude.com/docs/en/memory#enable-or-disable-auto-memory), [`--permission-prompts none`](https://code.claude.com/docs/en/headless#turn-off-permission-prompts-in-unattended-runs), [`permission_denials`](https://code.claude.com/docs/en/agent-sdk/typescript#sdkpermissiondeniedmessage)
- anthropics/claude-code#76867, #90578; openai/codex#47433, #54782
