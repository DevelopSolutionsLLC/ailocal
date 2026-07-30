#!/usr/bin/env python3
"""test-readiness-isolated.py — E2. Does readiness track the UPSTREAM, or only the process?

THE QUESTION. `doctor.sh` reports "LiteLLM reachable" from /health/liveliness, and
start.sh waits on the same endpoint. Neither says anything about Ollama. If an
operator reads "healthy" while the backend is unreachable, the health output is
worse than none: it sends them to look at the wrong layer.

WHY AN ISOLATED STACK AND NOT THE LIVE ONE. Proving this needs the upstream to be
DOWN, and the live upstream is the shared Ollama daemon that Cadence's semantic
index also depends on (see CLAUDE.md, "Two shared boundaries with Cadence").
Stopping it to run a test would break indexing silently. So this test never touches
it: it starts its own LiteLLM on its own port, pointed at a FAKE Ollama-compatible
upstream on another port, and turns THAT off and on. Isolation is the whole design,
not a detail — hence a distinct container name, port, and config file, and a
cleanup path that runs on every exit including a failed assertion.

WHAT IT ESTABLISHES. The transition sequence liveness/readiness must survive:

    upstream down   -> liveness true,  readiness ?
    repeated probes -> no restart loop (generation and restart count are checked)
    upstream up     -> readiness true
    upstream down   -> readiness false or degraded
    upstream up     -> readiness recovers

MEASURED RESULT (the reason this file exists): LiteLLM's /health/readiness answers
`{"status":"healthy"}` in single-digit milliseconds with NOTHING listening on the
upstream port. It reports the proxy's own liveness and DB state, never the backend.
So /health/readiness is not a readiness probe in the sense an operator assumes, and
swapping doctor.sh onto it would have changed the endpoint without fixing the lie.
The endpoint that DOES contact the backend is /health, which is why the correction
uses it. That distinction is the finding.

Run:  python3 scripts/test-readiness-isolated.py [--keep]
Exit: 0 all transitions as asserted, 1 otherwise.
"""
from __future__ import annotations

import argparse
import http.server
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Isolated identifiers. None of these may collide with the live stack, which is
# ailocal-litellm on 4000 talking to 11434.
CONTAINER = "ailocal-e2-readiness"
PROXY_PORT = 14000
UPSTREAM_PORT = 21434
WORKDIR = REPO / "data" / "e2-readiness"      # under /data/ -> gitignored

PROBE_TIMEOUT_S = 5.0
PROBE_INTERVAL_S = 1.0

FAKE_MODEL = "fake-model"

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    results.append((bool(ok), label))
    print(f"  {'OK  ' if ok else 'FAIL'}  {label}")


def ts() -> str:
    return time.strftime("%H:%M:%S", time.localtime()) + f".{int(time.time() * 1000) % 1000:03d}"


# ── the fake Ollama-compatible upstream ──────────────────────────────────────
# Only the routes LiteLLM's ollama_chat provider and /health actually touch. It
# answers instantly, so a slow response can never be confused with an unreachable
# one — this test is about reachability, and nothing else.

class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # silence
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/tags"):
            self._send({"models": [{"name": FAKE_MODEL, "model": FAKE_MODEL}]})
        else:
            self._send({"ok": True})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        if self.path.startswith("/api/show"):
            self._send({"model_info": {"context_length": 4096}, "capabilities": ["completion"]})
            return
        self._send({
            "model": FAKE_MODEL, "created_at": "2026-01-01T00:00:00Z",
            "message": {"role": "assistant", "content": "ok"},
            "done": True, "done_reason": "stop",
            "prompt_eval_count": 1, "eval_count": 1,
        })


class FakeUpstream:
    def __init__(self, port: int):
        self.port = port
        self.srv = None
        self.thread = None

    def start(self):
        if self.srv:
            return
        self.srv = http.server.ThreadingHTTPServer(("0.0.0.0", self.port), _Handler)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if not self.srv:
            return
        self.srv.shutdown()
        self.srv.server_close()
        self.srv = None
        self.thread = None
        # The port must actually be free again, or "upstream down" is a lie and
        # every assertion after it is measuring the wrong thing.
        for _ in range(50):
            if not _port_open("127.0.0.1", self.port):
                return
            time.sleep(0.1)
        raise RuntimeError(f"port {self.port} still accepting after shutdown")


def _port_open(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


# ── probes ───────────────────────────────────────────────────────────────────

def probe(path: str, timeout: float = PROBE_TIMEOUT_S) -> tuple[int | None, dict | str | None, float]:
    url = f"http://127.0.0.1:{PROXY_PORT}{path}"
    t0 = time.monotonic()
    req = urllib.request.Request(url, headers={"Authorization": "Bearer sk-e2-isolated"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            code = r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read().decode(), e.code
    except Exception as e:
        return None, f"{type(e).__name__}: {e}", time.monotonic() - t0
    try:
        return code, json.loads(raw), time.monotonic() - t0
    except Exception:
        return code, raw, time.monotonic() - t0


def container_state() -> dict:
    """Generation and restart count. A container that silently restarts under
    repeated probes would otherwise look identical to one that stayed up."""
    r = subprocess.run(
        ["docker", "inspect", CONTAINER,
         "--format", "{{.Id}}|{{.RestartCount}}|{{.State.Status}}|{{.State.StartedAt}}"],
        capture_output=True, text=True)
    if r.returncode != 0:
        return {"id": None, "restarts": None, "status": "absent", "started": None}
    cid, restarts, status, started = r.stdout.strip().split("|")
    return {"id": cid[:12], "restarts": int(restarts), "status": status, "started": started}


# ── isolated stack lifecycle ─────────────────────────────────────────────────

CONFIG = """
model_list:
  - model_name: e2-probe
    litellm_params:
      model: ollama_chat/{model}
      api_base: http://host.docker.internal:{port}
      num_ctx: 4096

litellm_settings:
  drop_params: true

general_settings:
  master_key: sk-e2-isolated
"""


def teardown(keep: bool = False) -> dict:
    subprocess.run(["docker", "rm", "-f", CONTAINER],
                   capture_output=True, text=True)
    if not keep and WORKDIR.exists():
        shutil.rmtree(WORKDIR, ignore_errors=True)
    return {
        "container_removed": container_state()["status"] == "absent",
        "workdir_removed": keep or not WORKDIR.exists(),
    }


def launch() -> str:
    WORKDIR.mkdir(parents=True, exist_ok=True)
    (WORKDIR / "config.yaml").write_text(
        CONFIG.format(model=FAKE_MODEL, port=UPSTREAM_PORT))

    digest = None
    for line in (REPO / "deploy" / "litellm" / "docker-compose.yml").read_text().splitlines():
        if "image:" in line and "berriai/litellm" in line:
            digest = line.split("image:", 1)[1].strip()
            break
    if not digest:
        raise SystemExit("could not read the pinned LiteLLM image from docker-compose.yml")

    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)
    r = subprocess.run([
        "docker", "run", "-d", "--name", CONTAINER,
        "--label", "ailocal.test=e2-readiness",
        "-p", f"127.0.0.1:{PROXY_PORT}:4000",
        "--add-host", "host.docker.internal:host-gateway",
        "-v", f"{WORKDIR}:/app/e2:ro",
        # restart policy DELIBERATELY absent (docker default "no"): the test asks
        # whether the process restart-loops on its own, so Docker must not be the
        # thing restarting it.
        digest,
        "--config", "/app/e2/config.yaml", "--port", "4000", "--num_workers", "1",
    ], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"could not start isolated proxy: {r.stderr.strip()}")
    return r.stdout.strip()[:12]


def wait_live(max_s: float = 90.0) -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < max_s:
        code, _, _ = probe("/health/liveliness", timeout=3)
        if code == 200:
            return time.monotonic() - t0
        time.sleep(PROBE_INTERVAL_S)
    raise SystemExit("isolated proxy never became live")


def upstream_healthy() -> tuple[bool, object]:
    """/health is the endpoint that actually CONTACTS the backend."""
    code, body, _ = probe("/health", timeout=30)
    if code != 200 or not isinstance(body, dict):
        return False, body
    unhealthy = body.get("unhealthy_count")
    healthy = body.get("healthy_count")
    return (healthy or 0) > 0 and (unhealthy or 0) == 0, body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="leave the isolated stack up")
    args = ap.parse_args()

    if shutil.which("docker") is None:
        print("docker unavailable — cannot run the isolated readiness test")
        return 1
    if _port_open("127.0.0.1", PROXY_PORT) or _port_open("127.0.0.1", UPSTREAM_PORT):
        print(f"ports {PROXY_PORT}/{UPSTREAM_PORT} already in use — refusing to collide")
        return 1

    up = FakeUpstream(UPSTREAM_PORT)
    timeline: list[dict] = []

    def mark(step: str, **kw):
        row = {"t": ts(), "step": step, **kw}
        timeline.append(row)
        return row

    print((__doc__ or "").strip().split("\n\n")[0])
    print(f"\nisolation: container={CONTAINER} proxy=127.0.0.1:{PROXY_PORT} "
          f"upstream=127.0.0.1:{UPSTREAM_PORT}")
    print(f"probe: timeout={PROBE_TIMEOUT_S}s interval={PROBE_INTERVAL_S}s\n")

    try:
        # 1-2. proxy alive, upstream unreachable (never started).
        cid = launch()
        mark("launch", container=cid)
        check(not _port_open("127.0.0.1", UPSTREAM_PORT),
              "upstream is unreachable before the proxy starts")
        live_after = wait_live()
        gen0 = container_state()
        mark("live", after_s=round(live_after, 2), **gen0)

        # 3-4. liveness true; what does readiness say?
        code, body, dt = probe("/health/liveliness")
        check(code == 200, f"liveness is true with the upstream down (http {code}, {dt*1000:.0f}ms)")

        rcode, rbody, rdt = probe("/health/readiness")
        mark("readiness_upstream_down", http=rcode, body=rbody, ms=round(rdt * 1000))
        readiness_lies = (rcode == 200 and isinstance(rbody, dict)
                          and rbody.get("status") == "healthy")
        print(f"        /health/readiness -> http {rcode} {rbody} in {rdt*1000:.0f}ms")
        check(readiness_lies,
              "MEASURED: /health/readiness reports healthy while the upstream is DOWN "
              "(this is the bug; it proves readiness is not upstream-aware)")

        # And the endpoint that does contact the backend disagrees.
        ok_down, hbody = upstream_healthy()
        mark("health_upstream_down", healthy=ok_down)
        check(not ok_down, "/health (which contacts the backend) reports NOT healthy when it is down")

        # 5-6. bounded repeated probes; no restart loop.
        for _ in range(8):
            probe("/health/readiness")
            time.sleep(PROBE_INTERVAL_S)
        gen1 = container_state()
        mark("after_repeated_probes", **gen1)
        check(gen1["restarts"] == gen0["restarts"] == 0,
              f"no restart loop under repeated probes (restarts={gen1['restarts']})")
        check(gen1["id"] == gen0["id"],
              f"container generation unchanged ({gen0['id']} -> {gen1['id']})")

        # 7-8. upstream becomes reachable.
        up.start()
        t_up = time.monotonic()
        mark("upstream_up")
        ok_up, hbody = None, None
        while time.monotonic() - t_up < 30:
            ok_up, hbody = upstream_healthy()
            if ok_up:
                break
            time.sleep(PROBE_INTERVAL_S)
        mark("health_upstream_up", healthy=ok_up, after_s=round(time.monotonic() - t_up, 2))
        check(bool(ok_up), f"/health becomes healthy once the upstream is reachable ({hbody})")

        # 9-10. upstream disappears.
        up.stop()
        t_dn = time.monotonic()
        mark("upstream_down_again")
        ok_again, hbody2 = upstream_healthy()
        mark("health_upstream_down_again", healthy=ok_again,
             after_s=round(time.monotonic() - t_dn, 2))
        check(not ok_again, "/health returns to NOT healthy when the upstream disappears")

        rcode2, rbody2, _ = probe("/health/readiness")
        check(rcode2 == 200 and isinstance(rbody2, dict) and rbody2.get("status") == "healthy",
              "/health/readiness STILL reports healthy here too (consistent, and consistently wrong)")

        # 11-12. upstream returns; readiness recovers.
        up.start()
        t_rec = time.monotonic()
        ok_rec = False
        while time.monotonic() - t_rec < 30:
            ok_rec, _ = upstream_healthy()
            if ok_rec:
                break
            time.sleep(PROBE_INTERVAL_S)
        mark("recovered", healthy=ok_rec, after_s=round(time.monotonic() - t_rec, 2))
        check(ok_rec, "/health recovers after the upstream returns")

        gen2 = container_state()
        check(gen2["restarts"] == 0 and gen2["id"] == gen0["id"],
              f"no restart across the whole sequence (restarts={gen2['restarts']})")

    finally:
        up.stop()
        clean = teardown(keep=args.keep)
        mark("cleanup", **clean)
        if not args.keep:
            check(clean["container_removed"], "isolated container removed")
            check(clean["workdir_removed"], "isolated workdir removed")
        check(not _port_open("127.0.0.1", PROXY_PORT), "isolated proxy port released")
        check(not _port_open("127.0.0.1", UPSTREAM_PORT), "isolated upstream port released")

    print("\ntransition timeline")
    for row in timeline:
        extra = " ".join(f"{k}={v}" for k, v in row.items() if k not in ("t", "step"))
        print(f"  {row['t']}  {row['step']:<28} {extra}")

    failed = [lbl for ok, lbl in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("\nFAILED:")
        for f in failed:
            print(f"  - {f}")
        return 1
    print("\nE2 READINESS: all transitions as asserted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
