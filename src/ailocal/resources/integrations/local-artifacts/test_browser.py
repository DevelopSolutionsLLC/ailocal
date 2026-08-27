#!/usr/bin/env python3
"""Behavioural proof of the rendering boundary.

Each probe target is a honeypot socket that counts real TCP connections, so the
verdict is "did a packet leave", not "what did the exception say". A control run
serves the identical page WITHOUT the CSP header: if the honeypots do not light
up there, the test cannot detect an escape and its silence means nothing.

`--host-resolver-rules` maps example.com onto a honeypot, so the internet case
is measured the same way as the loopback ones.
"""
import http.server, importlib.util, os, socket, subprocess, tempfile, threading, time, sys, shutil
from pathlib import Path

STATE = Path(tempfile.mkdtemp(prefix="la-bstate-"))
PORT, CTRL_PORT = 7896, 7894
NAMES = ["ollama", "qdrant", "litellm", "internet", "extra"]
HONEY = {n: 7880 + i for i, n in enumerate(NAMES)}
CH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

hits = {n: 0 for n in NAMES}
lock = threading.Lock()
stop = threading.Event()

def honeypot(name, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port)); s.listen(16); s.settimeout(0.3)
    while not stop.is_set():
        try:
            c, _ = s.accept()
            with lock: hits[name] += 1
            try:
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                          b"Access-Control-Allow-Origin: *\r\n\r\nhi")
            except Exception: pass
            c.close()
        except socket.timeout: pass
        except Exception: break
    s.close()

for n, p in HONEY.items():
    threading.Thread(target=honeypot, args=(n, p), daemon=True).start()
time.sleep(0.4)

URLS = {
    "ollama":   f"http://127.0.0.1:{HONEY['ollama']}/api/tags",
    "qdrant":   f"http://127.0.0.1:{HONEY['qdrant']}/collections",
    "litellm":  f"http://127.0.0.1:{HONEY['litellm']}/v1/models",
    "internet": "http://example.com/beacon",          # resolver-mapped to a honeypot
    "extra":    f"http://127.0.0.1:{HONEY['extra']}/x",
}
ART = ("<!doctype html><html><head><meta charset=utf-8></head><body><h1>probe</h1>"
       "<script>\n" +
       "".join(f"try{{fetch({u!r},{{mode:'no-cors'}}).catch(function(){{}});}}catch(e){{}}\n"
               for u in URLS.values()) +
       f"try{{new Image().src={URLS['extra']!r}+'?img';}}catch(e){{}}\n"
       "</script></body></html>")

spec = importlib.util.spec_from_file_location("srv", str(Path(__file__).parent / "server.py"))
os.environ.update(LOCAL_ARTIFACTS_PORT=str(PORT), XDG_STATE_HOME=str(STATE),
                  LOCAL_ARTIFACTS_AUTO_OPEN="0", LOCAL_ARTIFACTS_ROOT=str(STATE))
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)
srv.start_http_thread(); srv._http_ready.wait(5)
ok, msg = srv.publish(title="Boundary probe", content=ART); assert ok, msg

# ── control server: identical bytes, no CSP ────────────────────────────────
class Ctrl(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        b = ART.encode()
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
threading.Thread(target=lambda: http.server.ThreadingHTTPServer(
    ("127.0.0.1", CTRL_PORT), Ctrl).serve_forever(), daemon=True).start()
time.sleep(0.3)

def load(url, seconds=7):
    prof = tempfile.mkdtemp(prefix="la-chrome-")
    rules = f"MAP example.com 127.0.0.1:{HONEY['internet']}"
    p = subprocess.Popen([CH, "--headless=new", "--disable-gpu", f"--user-data-dir={prof}",
                          f"--host-resolver-rules={rules}", "--no-first-run",
                          "--disable-features=Translate", url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(seconds)
    p.terminate()
    try: p.wait(timeout=10)
    except Exception: p.kill()
    shutil.rmtree(prof, ignore_errors=True)

def snapshot():
    with lock: return dict(hits)

print("=== CONTROL: identical page, NO CSP header (test must detect escape) ===")
before = snapshot(); load(f"http://127.0.0.1:{CTRL_PORT}/"); after = snapshot()
ctrl = {n: after[n] - before[n] for n in NAMES}
for n in NAMES: print(f"  {n:9s} connections: {ctrl[n]}")
control_works = sum(ctrl.values()) > 0

print("\n=== ARTIFACT served by local-artifacts (CSP: connect-src 'none') ===")
before = snapshot(); load(f"http://127.0.0.1:{PORT}/content"); after = snapshot()
direct = {n: after[n] - before[n] for n in NAMES}
for n in NAMES: print(f"  {n:9s} connections: {direct[n]}")

print("\n=== ARTIFACT inside the sandboxed iframe (the real viewing path) ===")
before = snapshot(); load(f"http://127.0.0.1:{PORT}/"); after = snapshot()
framed = {n: after[n] - before[n] for n in NAMES}
for n in NAMES: print(f"  {n:9s} connections: {framed[n]}")

print("\n=== BUNDLED MERMAID running in the sandbox (real JS, big library) ===")
mmd = ("flowchart LR\n"
       "  A[\"Claude Code\"] --> B[\"LiteLLM\"]\n"
       "  B --> C[\"Ollama\"]\n"
       "  A -.-> D[\"MCP tools\"]\n")
ok, msg = srv.publish(title="Mermaid probe", content=mmd, fmt="mermaid")
assert ok, msg
before = snapshot(); load(f"http://127.0.0.1:{PORT}/", 9); mm = snapshot()
mermaid_hits = {n: mm[n] - before[n] for n in NAMES}
for n in NAMES: print(f"  {n:9s} connections: {mermaid_hits[n]}")

stop.set()
shutil.rmtree(STATE, ignore_errors=True)

PASS = FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  PASS  {name}")
    else: FAIL += 1; print(f"  FAIL  {name}  {detail}")

print("\n=== verdict ===")
check("CONTROL escaped, so the test can detect an escape", control_works,
      "no-CSP control produced zero connections -- test is blind, results meaningless")
for n in NAMES:
    check(f"{n}: blocked when served by local-artifacts", direct[n] == 0, f"{direct[n]} connections")
for n in NAMES:
    check(f"{n}: blocked inside sandboxed iframe", framed[n] == 0, f"{framed[n]} connections")
for n in NAMES:
    check(f"{n}: blocked with bundled mermaid running", mermaid_hits[n] == 0,
          f"{mermaid_hits[n]} connections")
print(f"\n  PASS {PASS}   FAIL {FAIL}")
sys.exit(1 if FAIL else 0)
