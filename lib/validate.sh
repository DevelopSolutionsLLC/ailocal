#!/usr/bin/env bash
# validate.sh — deterministic consistency of the capability platform.
#
#   ailocal validate [--profile <tier>]        (or: ailocal validate [...])
#
# Answers one question from files on disk: does the active profile (or
# --profile <tier>) hang together? Source, generated and deployed state must
# agree, aliases must resolve, and the container must be running this repo's
# config.
#
# DETERMINISTIC BY CONTRACT. It runs with LiteLLM and Ollama stopped, makes no
# inference request, and mutates nothing. Runtime verification is `ailocal
# smoke`; this used to fail with "could not read `ollama list`" when a daemon
# was down, which made a configuration check depend on a service.
#
# Exit 0 clean, 1 if any check failed. Docker being unavailable blocks the
# mounted-config comparison; it does not fail the run.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 "$ROOT_DIR/lib/checks/run.py" validate "$@"
