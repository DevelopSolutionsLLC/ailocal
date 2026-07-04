---
applyTo: "**"
---

# Session Constraints — Local AI Stack

You are running against **local Ollama models via LiteLLM at `http://localhost:4000`**, not a
cloud model. Local models have high first-token latency and VS Code's terminal detection can time
out before a slow model responds. Follow these rules every time you run a terminal command.

## Terminal rule — always detach, log, then check

**Run this pattern for any command that takes more than ~2 seconds:**

```bash
your-command > /tmp/label.log 2>&1 & exit 0
```

**Then check the result in a follow-up step:**

```bash
cat /tmp/label.log
```

**Why:** `& exit 0` returns the shell to VS Code immediately so it stops waiting. The command
keeps running in the background. The log captures all output so you can read it back and verify
success before moving on. Without this, the terminal hangs, the agent stalls, and nothing works.

## Chaining steps

```bash
step1 > /tmp/run.log 2>&1 && step2 >> /tmp/run.log 2>&1 & exit 0
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

## Model roles (this machine)

`router` (32k) → fast tasks | `coder` (256k) → code | `reasoner` (128k) → planning |
`supervisor` (128k, vision) → review | `embed` → search only
