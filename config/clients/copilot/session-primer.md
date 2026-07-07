---
applyTo: "**"
---

# Session Constraints — Local AI Stack

You are running against **local Ollama models via LiteLLM at `http://localhost:4000`**, not a
cloud model. Local models have high first-token latency and VS Code's terminal detection can time
out before a slow model responds. Follow these rules every time you run a terminal command.

## Terminal rule — run normally, NEVER `exit`

VS Code's agent terminal detects when a command finishes on its own (via shell
integration). Run the command directly and let the tool wait for it:

```bash
docker ps
```

**Never append `exit`, `exit 0`, or `& exit 0`.** That closes the integrated terminal
before VS Code registers completion — the turn freezes and nothing else runs. This is
the number-one cause of the agent getting stuck.

## Long-running commands only (servers, installs, watchers)

These are the only commands that need backgrounding. Detach with a trailing `&` and a
log — but NO `exit`:

```bash
npm run dev > /tmp/dev.log 2>&1 &
```

Then read the log in a follow-up step:

```bash
cat /tmp/dev.log
```

## Chaining steps

```bash
step1 && step2
```

For a long chain you want detached, wrap it — still no `exit`:

```bash
(step1 && step2) > /tmp/run.log 2>&1 &
# follow up:
cat /tmp/run.log
```

## Fast commands — run inline (no detach needed)

```bash
git status
git diff | cat
docker ps
ls -la
cat file.txt
```

## Never run

- `tail -f`, `watch`, `less`, `man` — block forever
- Any command that prompts mid-run without `-y` or `--force`
- `git log` or `git diff` without `| cat` (pager blocks)
- **`pkill -f node`, `killall node`, or any broad `kill` of node** — the integrated
  terminal shares VS Code's process tree. Killing node here kills the extension host
  and the litellm-connector, which drops your model connection and freezes the session.
  To free a stuck dev server, kill it by its specific port or PID instead:
  `lsof -ti tcp:3000 | xargs kill` (only that port), never a blanket `node` match.

## Model roles (this machine)

`router` (32k) → fast tasks | `coder` (256k) → code | `reasoner` (128k) → planning |
`supervisor` (128k, vision) → review | `embed` → search only
