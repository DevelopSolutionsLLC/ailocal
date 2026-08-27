# ADR 013 — The artifact preview server outlives the session

**Status:** Accepted · **Date:** 2026-08

## Problem

Artifact documents opened in the browser and intermittently produced "127.0.0.1 refused to
connect". The cause had been guessed at (fixed port 7891, port collision) but never
established.

It is not collision. The HTTP listener was a daemon thread **inside the stdio MCP process**.
Claude Code terminates that process when the session ends — the MCP spec has the client close
stdin, then send `SIGTERM`, then `SIGKILL` — so the listener died with the session while the
`preview_url` stayed in the transcript, and the artifact stayed on disk. Every artifact URL
was valid only until its own session ended.

[REAL] reproduced from live state on this machine, with no harness: `current_content.json`
written 13:56 by a real session, no process and no listener on 7891, `GET /` →
`[Errno 61] Connection refused`. [REAL] reproduced deliberately with real processes: publish,
`SIGTERM` the publisher, listener count drops 1 → 0, `Connection refused`.

A second, separate defect was measured at the same time: a session that started while another
held the port set `_http_error` once and **never retried**, so it could not publish for its
entire life even after the port was free. That failure was at least honest — it returned
"was NOT published" rather than a dead URL — but it made concurrent sessions unusable.

[SOURCE] upstream `xiagaohui/local-artifacts-for-claude-code` has the identical
daemon-thread design and no shutdown logic, so this is inherited, not introduced. Upstream's
README claim of persistence refers to *content* restored from the state file, not to the
server.

## Constraints

- **No new HTTP write surface.** Upstream's `POST /publish` accepted an unauthenticated
  cross-origin write; the security audit removed it and `test_server.py` pins it removed.
- **The rendering boundary is unchanged.** `/` stays a trusted viewer, `/content` stays
  sandboxed with `connect-src 'none'`.
- **Minimal memory.** [REAL] the server is 23.6 MiB RSS.
- No zombie processes, and an automated gate must not leave one behind.

## Alternatives considered

1. **OS-assigned ephemeral port (`port=0`).** Rejected: it addresses collision, which is not
   the defect. The listener would still die with the session, and a per-session port makes a
   stale URL *harder* to recognise rather than fixing it.
2. **Run the preview server in a container**, or fold it into the existing `ailocal-litellm`
   service. Rejected on both stated constraints. [REAL] measured on this machine: the host
   process is 23.6 MiB against 854 MiB / 121 MiB / 306 MiB for the running
   litellm / searxng / qdrant containers — Docker moves memory the wrong way. Reusing the
   LiteLLM container additionally means overlaying a vendor image pinned by digest and gated
   at 1.98.0 by `ailocal check`, to host an unrelated viewer.
3. **A long-running service installed at provisioning time.** Rejected: a session that never
   draws anything should cost no process, and `restart: unless-stopped` semantics are the
   opposite of what is wanted — the server should disappear when nobody is looking at it.
4. **One shared, on-demand, idle-exiting server, decoupled from every MCP process.** Chosen.

## Decision

`server.py --serve` is the preview server. The first `publish()` that finds nothing on the
port starts it with `start_new_session=True`, so it is not in the MCP process's process group
and the session's `SIGTERM` does not reach it. Every later session probes `/status`, finds it,
and reuses it. Whoever loses the bind race exits; the winner serves everyone.

Publishing crosses the process boundary through the existing 0600 state file, which the
server polls (4 Hz) and ingests. The state file is now written atomically via `os.replace`,
because a reader in another process can otherwise observe a half-written artifact. No write
endpoint is reintroduced, so the audit's finding stays fixed.

`publish()` waits for the shared server to confirm it is serving that exact artifact before
returning the URL and opening a browser. Otherwise the success message describes the
publishing process's own memory rather than what the URL serves, and the tab can open on the
empty page.

The server exits after `LOCAL_ARTIFACTS_IDLE_EXIT` (default **30 minutes**) of genuine
disuse. Three distinct things defer it: any HTTP request, an incoming publish, and an open
viewer — a tab holds an SSE connection and blocks the reaper outright. The publish case is
deliberately explicit in the watcher rather than left to the publisher's own `/status`
probes, so the idle policy does not depend on how `ensure_preview_server` is implemented.

30 minutes rather than hours because reaping costs almost nothing: [REAL] a cold start is
0.351s against 0.18s warm, so the next publish pays ~170 ms and transparently gets a fresh
server. The only case that wants a longer window is a transcript URL reopened with no tab
still open and nothing republishing; past the timeout the source is still under `.artifacts/`
and republishing brings it straight back.

A publish whose artifact the viewer never confirms recovers once, and if that fails reports
"Artifact saved, but NOT viewable" with the source path rather than handing back a URL
nothing is listening on.

## Tradeoffs

- **A process now outlives the session.** That is the point, and the idle reaper is the bound
  on it: one ~24 MiB process, gone 30 minutes after the last use. The gate sets
  `LOCAL_ARTIFACTS_IDLE_EXIT=5` so an automated run leaves nothing.
- **Up to 250 ms between publishing and the tab updating.** A file poll, not a signal:
  signals need a pidfile and have no Windows equivalent, and a `stat()` four times a second
  costs nothing.
- **All sessions share one viewer.** They already did — one port, one URL, one current
  artifact. The shared server makes the existing behaviour work rather than changing it.
- **`start_http_thread()` survives for the test suite**, which drives publish and HTTP in one
  interpreter. Production no longer uses it.

## Measurements

- Publish, kill the publisher, `GET /` → 200 serving that artifact. [REAL]
- Five sessions publishing in sequence → one listener throughout, no additional process. [REAL]
- Cross-process publish reaches the shared server and the viewer updates. [REAL]
- `POST /publish` still not a success, nothing injected. [REAL]
- Idle server exits on its own; the next publish brings a fresh one up and returns a working
  URL, with a genuinely new process. [REAL]
- Killing the publisher's whole **process group** leaves the viewer serving. [REAL]
- Four publishers racing with no server running leave exactly one viewer. [REAL]
- A foreign HTTP server on the port is refused, not adopted, and the error names the port and
  `LOCAL_ARTIFACTS_PORT`. [REAL]
- A transient ingest failure is recovered; a persistent one is reported as not viewable with
  the source path. [REAL]
- Cold start 0.351s vs warm 0.18s. [REAL]
- `test_lifetime.py`: 32 passed. Full bundled suite: 5 files passed, 0 failed, no stray
  processes. [REAL]
- **Windows is [UNVERIFIED].** `start_new_session` is POSIX-shaped and the suite uses
  `os.killpg` and `lsof`; nothing here was exercised on Windows.

## Revisit if

- Claude Code registers `Artifact` for API-key auth — ADR 011's trigger deletes all of this.
- Artifacts stop being "one current artifact at one URL". A server addressing artifacts by id
  would need a different state contract than one shared file.

## Deeper reference

- `src/ailocal/resources/integrations/local-artifacts/test_lifetime.py` — the regression suite.
- `src/ailocal/resources/integrations/local-artifacts/README.md` — the process diagram.
- ADR 011 — why the component is bundled at all.
