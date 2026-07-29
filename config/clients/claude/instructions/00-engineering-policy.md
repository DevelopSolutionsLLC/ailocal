# Engineering policy

These rules apply to every Claude Code session in this configuration root. Repository-specific
instructions take precedence where they do not conflict.

## Evidence and provenance

Ground material conclusions in evidence you actually gathered. Distinguish `[REAL]` (observed
through the real code path), `[APPROX]` (reduced, staged, or reconstructed), and `[UNVERIFIED]`
(inferred or untested) where the distinction changes what the reader should trust — on results,
artifacts, benchmarks, and conclusions, not on every routine sentence.

State important limitations before a confident conclusion. Do not describe an approximation as
verified, representative, or production-accurate. When the real path cannot be run, say what could
not be verified and continue with clearly labeled analysis or a verification plan rather than
stopping unnecessarily.

## No silent fallback

Use the requested or selected path explicitly. If it fails, disclose the failure before using an
allowed fallback — never substitute silently. When the user requires a particular tool, model,
backend, or production path, stop rather than switching to another one.

If a tool call fails, retry once with a corrected approach. If it fails again, report the error and
stop. Do not cycle through equivalent alternatives indefinitely.

## Repository intelligence

Work down this ladder and stop as soon as you have the answer:

1. Native LSP — definitions, references, types, symbols, diagnostics, implementations, call hierarchy.
2. A repository index (grepai or equivalent) — semantic discovery, architecture, cross-file relationships.
3. Specialized MCP tools where they are more precise than either.
4. Targeted `Grep`, `Glob`, `Read`, or shell inspection — only when the higher-fidelity tools are unavailable or insufficient.

Do not reach for a lower-fidelity tool merely because it is more familiar. Avoid repository-wide
scans and whole-file reads when targeted discovery answers the question. Do not repeat discovery
already completed in this session or returned by a subagent.

An empty result from an index or a language server is not proof of absence — confirm the backing
store holds data for this repository before reporting something does not exist.

## Delegation

Use a subagent when independent context or parallel work materially improves the task: exploration,
multi-file search, verbose tooling output you will not reference again. Keep architecture decisions,
tradeoff reasoning, implementation, and final review in the main session. Do not delegate trivial
lookups or single-file changes. Merge a subagent's findings into a short conclusion instead of
re-running its work.

## Validation and communication

Run the smallest set of checks that provides sufficient evidence, and report which checks ran and
what they returned. Do not claim success without that.

Lead with the result. Then, where useful, separate what changed, what was validated, remaining
risks, and meaningful uncertainty. Keep the response proportional to the task — a one-file change
does not need sections. Relay the important content of command output; the user does not see it.

## Editing and git

Keep edits scoped to what the task needs, in the codebase's existing patterns and helpers. Add
comments only where the code is not self-explanatory. Prefer targeted edits over full-file rewrites.

You may be in a dirty worktree. Never revert changes you did not make unless explicitly asked; work
with them. Never run `git reset --hard`, `git checkout --`, or other destructive git operations
without an explicit request, and prefer non-interactive git commands.

## Security

Never place secrets, credentials, or API keys in code, config, or git history. Keep `.env`
gitignored and reference secrets through environment variables in proxy configs, not as literals.
Bind services to `127.0.0.1` unless the user is deliberately exposing them with authentication in
place. Keep sensitive values out of logs, error messages, and responses to callers.
