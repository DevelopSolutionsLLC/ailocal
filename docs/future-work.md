# Future work register

What is deliberately **not** done, and what would make each item worth doing.
Reviewed 2026-07-28.

The default is **do nothing**. This project reached a stable baseline; further
changes should be driven by a measured regression or a new upstream capability,
not by speculation. Every item below names the trigger that would justify
reopening it.

## Immediate

**Make the conversational class hold across a multi-turn loop.**
*Why:* it is the single biggest lever on how the system feels, and it currently
works only on the FIRST request. Measured over 9 gateway turns of one
`claude -p`: turn 1 kept 1/61 tools, later turns drifted to `class=None` and
48/61, and the model went on to call rg/Bash/Write. The advertised "61 -> 1" is
true of turn 1 only.
*Blocker:* not a patch. `first_user_text()` resolves differently on later turns
(subagent context — `tools_in` shifts 61 -> 64 — and/or compaction), so the
classifier stops seeing the original question. Two guard bugs were already found
and fixed on the way (continuation turns; tool results arriving as `role: user`
messages) and neither was sufficient. The fix is classification that survives
context changes, which is a design change and was deliberately not attempted
during close-out.
*Trigger:* the next time the repo-crawling behaviour is annoying in daily use —
`benchmark-baseline.sh`'s `conversational` scenario already fails on it, so the
regression signal exists.

## Soon

**Measure unprompted delegation.**
*Why:* delegation is verified only when explicitly requested ("delegate this
to the reviewer"). Whether the 30B reaches for `Agent` on a task that merely
*warrants* it is unmeasured, and it is the difference between a working feature
and a used one.
*Blocker:* none — needs a scenario set where delegation is clearly correct but
unstated, run several times, since one sample proves nothing about a
probabilistic system.
*Trigger:* next time the baseline is run; add scenarios to
`benchmark-baseline.sh`.

**Broaden benchmark coverage.**
*Why:* five scenarios cover the paths that broke historically, not the whole
surface. Codex and VS Code have no baseline at all.
*Blocker:* Codex's MCP gap makes a codex-local baseline mostly meaningless
until upstream moves.
*Trigger:* after the Codex blocker clears, or when a regression escapes the
current five.

**Verify hosted Codex MCP.**
*Why:* the compatibility matrix records it as `?`. It is the only untested cell.
*Blocker:* costs OpenAI credits; nobody has needed it.
*Trigger:* first real use of hosted Codex, or any claim that depends on it.

**Trim `CLAUDE.md`.**
*Why:* 208 lines against its own stated ~70-line budget, and it loads every
session. The cheat sheet and ADRs now carry the deep material, so the primer
could shrink to pointers.
*Blocker:* none, but it is dense verified knowledge; careless deletion loses
things that cost real time to learn. Move, never delete.
*Trigger:* when startup context becomes a measured problem.

## Future

**Reasoning tier for architecture work.**
*Why:* no installed model emits `<think>` except `review`. Architecture-class
work has no reasoning option.
*Blocker:* candidates traded away too much prompt-eval speed (gpt-oss:20b reasons
but its prompt eval is far slower, and prompt eval is the mechanism behind
first-byte timeouts). A commented `reasoning` slot in the profile restores the
tier in one repoint.
*Trigger:* a reasoner that keeps prompt-eval competitive at this memory budget.

**Promote `implementation` to agentic.**
*Why:* the 14B is a good single-shot coder that cannot sustain a tool loop, so
`architecture` carries work it does not need to.
*Blocker:* measured non-agentic — described an edit and emitted a fenced JSON
block instead of a `tool_use`.
*Trigger:* a new 14B-class model; re-measure with `benchmark-models.sh` before
changing the launch default.

**Second Qdrant collection / index sharding.**
*Blocker:* none; 1139 points is nowhere near a limit.
*Trigger:* retrieval quality degrading as the workspace grows.

**Retire the tool-call repair layer.**
*Why:* it exists because Ollama's parser drops tool calls when a model omits the
opening tag.
*Blocker:* ollama#16686 is open and stale; #16693 (proposed fix) unmerged,
#16732 closed unmerged.
*Trigger:* the upstream parser fix landing — but keep the layer anyway as a
general compatibility shim; the next model will drift differently.

## Blocked by upstream

**Codex namespace/MCP — the one real capability gap.**
LiteLLM discards Codex's `namespace`-typed tools translating `/v1/responses` →
Chat Completions (measured 27,239 bytes), so codex-local's registered MCP
servers are unreachable by the model. Gateway-side flattening works but Codex's
dispatcher rejects flattened names.
*Upstream, two tracks — either would fix it:*
[BerriAI/litellm#29854](https://github.com/BerriAI/litellm/issues/29854)
(namespace tools stripped in conversion — the layer doing the dropping), and
[openai/codex#20652](https://github.com/openai/codex/issues/20652) with the fix
in unreleased [PR #17556](https://github.com/openai/codex/pull/17556).
CLIProxyAPI#3298 is the same bug from another proxy.
*Trigger:* any Codex upgrade → run `scripts/validate-codex-e2e.sh`. The verdict
is version-pinned to codex-cli 0.145.0, not permanent.

**`workspace_symbol_search` does not fan out (mcpls 0.3.7).**
It answers for whichever language server became ready first and returns
`{"symbols":[]}` for every other language — indistinguishable from "not found".
Undocumented upstream.
*Workaround in place:* document-scoped tools route by extension and work for all
languages; the verifier reports this without gating on it.
*Trigger:* an mcpls release that fans out, or an alternative bridge.

**`reasoning_effort` maps unreliably.**
Reaches the backend, but `none` produced *more* reasoning than `high`.
*Upstream:* BerriAI/litellm#15059. Per-role defaults are the control that works.
*Trigger:* that issue closing.

**Claude Code renames.**
`Task` → `Agent` in v2.1.63 silently disabled delegation here and cost two full
misdiagnoses.
*Trigger:* every Claude Code upgrade — diff the tool names in a captured payload
(`AILOCAL_TOOL_GATEWAY_CAPTURE=/app/captures`) against `registry.yaml` groups.
This is the single highest-value upgrade check in the project.

**LiteLLM upgrades.**
`routes.drops_tool_types` is read from LiteLLM's transformation source, not
inferred, and is pinned to the 1.93.0 image. Persona injection on `/v1/messages`
was broken in 1.83.10 (#27518) and works on ours.
*Trigger:* any LiteLLM version change — re-verify both, and re-run persona
propagation probes after a **downgrade** especially.

**MCP protocol evolution.**
Cadence generates client configs per surface format. A protocol or schema change
would land in `generate_mcp_config.py`, not in five client files.
*Trigger:* a spec revision that changes stdio server declaration.
