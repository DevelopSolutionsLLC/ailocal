<!-- SOURCE for repo-instructions.md. The capabilities table below is filled
     by sync-models.py from profiles/<tier>.yaml; the generated file is
     git-ignored. Edit this template, then run `ailocal sync`. -->
# Copilot Agent Instructions — ailocal

You are running inside VS Code connected to **local Ollama models via a LiteLLM proxy at
`http://localhost:4000`**. This is not a cloud model. Understanding this shapes everything below.

---

## Why terminal commands must be detached

Local models have high first-token latency (5–30 seconds for a cold 35B model). VS Code's terminal
shell integration uses prompt-sequence markers to know when a command finishes. When a local model
is thinking, the shell integration timeout fires before the model responds, causing VS Code to
believe the terminal is hung — and it stops reading output. The result: you issue a command, the
terminal sits there, the agent stalls, and nothing progresses.

**The fix**: detach every non-trivial command, log its output to `/tmp`, exit the shell
immediately, and follow up with a read of the log in a separate step. The shell returns to the
prompt instantly, VS Code gets control back, and the output is still available.

---

## Terminal execution rules (always follow these)

### Standard pattern — use this for everything

```bash
SOME_COMMAND > /tmp/cmd-label.log 2>&1 & exit 0
```

Then in a follow-up terminal call, check the result:

```bash
cat /tmp/cmd-label.log
```

### Concrete examples

```bash
# Starting services
ailocal start > /tmp/compose-up.log 2>&1 & exit 0

# Running install scripts
./ailocal install > /tmp/install.log 2>&1 & exit 0

# Building a project
npm run build > /tmp/build.log 2>&1 & exit 0
make > /tmp/make.log 2>&1 & exit 0

# Running tests
npm test > /tmp/test.log 2>&1 & exit 0
pytest > /tmp/pytest.log 2>&1 & exit 0

# Git operations (these are fast — no need to detach)
git status
git add -p
git diff | cat
git log --oneline -10 | cat

# Docker inspection (fast — no need to detach)
docker ps
docker logs --tail 50 container_name | cat
```

### When to detach vs. run inline

| Situation | Pattern |
|---|---|
| Command takes > ~2 seconds | Detach: `cmd > /tmp/label.log 2>&1 & exit 0` |
| Simple read/inspect/status | Run inline — fast commands are fine |
| Command prompts for input | Never run — use `-y`, `--no-input`, or skip |
| Long-running service/watcher | Detach to log, check with `cat /tmp/label.log` |
| Git reads (`log`, `diff`, `status`) | Inline with `| cat` to prevent paging |

### Checking on a background command

After detaching, always follow up to confirm success before proceeding:

```bash
# Check if still running
pgrep -f "npm run build" && echo "still running" || echo "done"

# Check the log
cat /tmp/build.log

# Check exit code (tail of log usually shows errors)
tail -20 /tmp/build.log
```

### Chaining async steps

When a workflow has multiple steps (install → build → test), fire them as a chain in one shot
rather than waiting between steps:

```bash
./ailocal install > /tmp/step1.log 2>&1 && \
npm run build >> /tmp/step1.log 2>&1 && \
npm test >> /tmp/step1.log 2>&1 & exit 0
```

Then check:
```bash
cat /tmp/step1.log
```

---

## Never do these

- `tail -f` — blocks forever
- `watch` — blocks forever
- `less`, `more`, `man` — interactive pagers, block the terminal
- `ollama run` — interactive REPL
- Any command that prompts mid-run without a `-y` / `--force` / `--no-input` flag
- Paged git output without `| cat` or `--no-pager`

---

## Local model roles

<!-- >>> BEGIN GENERATED capabilities (sync-models.py) — do not edit <<< -->
<!-- >>> END GENERATED capabilities <<< -->

---

## This repo — ailocal

This is the configuration and tooling repo for the local AI stack itself.

`AGENTS.md` is the authoritative description of this repository's layout and
policy. It is not restated here.

**Authored inputs you may edit:**
- `profiles/<tier>.yaml` — what each capability IS (backend, context, sampling,
  keep_alive)
- `profiles/clients.yaml` — which capability each client surface uses

**Never hand-edit generated output.** All of it lives under `$AILOCAL_STATE`,
outside the checkout, and is rewritten by `ailocal sync`.

**To change a model:**
```bash
$EDITOR profiles/64gb.yaml
ailocal sync > /tmp/sync.log 2>&1 & exit 0
# then: cat /tmp/sync.log
# then: ailocal start > /tmp/restart.log 2>&1 & exit 0
```

---

## Engineering standard

`AGENTS.md` in this repository owns the engineering standard, ownership
boundaries, change workflow and validation requirements. Read it before
changing anything here; it is not restated in this file.
