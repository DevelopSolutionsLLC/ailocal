#!/usr/bin/env python3
"""Direct tests for local-artifacts. Run: .venv/bin/python test_server.py"""
import importlib.util, json, os, shutil, socket, subprocess, sys, tempfile, time, urllib.request, urllib.error
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="la-root-"))
STATE = Path(tempfile.mkdtemp(prefix="la-state-"))
PORT = 7899
os.environ.pop("CLAUDE_PROJECT_DIR", None)
os.environ.update(LOCAL_ARTIFACTS_PORT=str(PORT), LOCAL_ARTIFACTS_ROOT=str(ROOT),
                  XDG_STATE_HOME=str(STATE), LOCAL_ARTIFACTS_AUTO_OPEN="0")

spec = importlib.util.spec_from_file_location("srv", str(Path(__file__).parent / "server.py"))
srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)
srv.start_http_thread(); srv._http_ready.wait(5)

PASS = FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  PASS  {name}")
    else: FAIL += 1; print(f"  FAIL  {name}  {detail}")

def get(path, host=None, raw=False):
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}")
    if host: r.add_header("Host", host)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")

print("\n=== E: publish input validation ===")
ok, msg = srv.publish(title="", content="<p>x</p>")
check("empty title rejected", not ok and "title" in msg, msg)
ok, msg = srv.publish(title="T", content="<p>x</p>", fmt="pdf")
check("bad format rejected", not ok and "format" in msg, msg)
ok, msg = srv.publish(title="T")
check("no content and no file_path rejected", not ok and "content" in msg, msg)
ok, msg = srv.publish(title="T", content="x" * (srv.MAX_SIZE + 1))
check("oversize content rejected", not ok and "exceeds" in msg, msg)

print("\n=== E: file_path policy ===")
(ROOT / "ok.html").write_text("<h1>inside root</h1>")
(ROOT / "notes.md").write_text("# hi")
(ROOT / "secret.env").write_text("KEY=1")
ok, msg = srv.publish(title="T", file_path=str(ROOT / "ok.html"))
check("file inside root publishes", ok, msg)
ok, msg = srv.publish(title="T", file_path=str(ROOT / "secret.env"))
check("disallowed suffix rejected", not ok and "refusing" in msg, msg)
ok, msg = srv.publish(title="T", file_path=str(ROOT / "missing.md"))
check("missing file rejected", not ok and "not found" in msg, msg)

OUT = Path(tempfile.mkdtemp(prefix="la-outside-"))
(OUT / "elsewhere.md").write_text("# outside the root")
ok, msg = srv.publish(title="T", file_path=str(OUT / "elsewhere.md"))
check("file outside root rejected", not ok and "outside the approved root" in msg, msg)

# symlink INSIDE root pointing OUTSIDE root
link = ROOT / "escape.md"
try: link.symlink_to(OUT / "elsewhere.md")
except FileExistsError: pass
ok, msg = srv.publish(title="T", file_path=str(link))
check("symlink escaping root rejected", not ok and "outside the approved root" in msg, msg)

# symlink to a sensitive suffixless file
link2 = ROOT / "passwd.md"
try: link2.symlink_to("/etc/passwd")
except FileExistsError: pass
ok, msg = srv.publish(title="T", file_path=str(link2))
check("symlink to /etc/passwd rejected", not ok, msg)

print("\n=== 3: external subresources are refused, not silently broken ===")
ok, msg = srv.publish(title="T", content='<script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js"></script><div class="mermaid">graph TD;</div>')
check("remote <script src> refused", not ok and "cdn.jsdelivr.net" in msg, msg[:120])
check("refusal explains how to fix it", "inline" in msg and "svg" in msg.lower(), msg[:120])
ok, msg = srv.publish(title="T", content='<style>@import url("https://fonts.googleapis.com/css");</style><p>x</p>')
check("remote @import refused", not ok and "fonts.googleapis.com" in msg, msg[:120])
ok, msg = srv.publish(title="T", content='<style>body{background:url("https://e.com/b.png")}</style><p>x</p>')
check("remote css url() refused", not ok and "e.com" in msg, msg[:120])
ok, msg = srv.publish(title="T", content='<img src="https://e.com/x.png">')
check("remote <img src> refused", not ok and "e.com" in msg, msg[:120])
ok, msg = srv.publish(title="T", content='<p>See <a href="https://example.com">the docs</a></p>')
check("remote <a href> is ALLOWED (navigation, not a subresource)", ok, msg[:120])
ok, msg = srv.publish(title="T", content='<img src="data:image/gif;base64,R0lGOD"><style>body{color:red}</style>')
check("data: URI and inline style allowed", ok, msg[:120])
ok, msg = srv.publish(title="T", content='<svg viewBox="0 0 10 10"><rect width="5" height="5"/></svg>')
check("inline SVG allowed", ok, msg[:120])

print("\n=== 12: new formats cannot open a network hole ===")
ok, msg = srv.publish(title="T", fmt="mermaid",
    content='flowchart LR\n A["<img src=https://evil.example/x.png>"] --> B')
check("remote img inside mermaid source refused", not ok and "evil.example" in msg, msg[:110])
ok, msg = srv.publish(title="T", fmt="mermaid", content="flowchart LR\n A --> B")
check("clean mermaid publishes", ok, msg[:110])
_, hdrs, page = get("/content")
check("mermaid page served under content CSP",
      "connect-src 'none'" in hdrs.get("Content-Security-Policy", ""))
check("mermaid library is inlined, not fetched",
      "mermaid.initialize" in page and 'src="http' not in page)
check("mermaid runs with securityLevel strict", "securityLevel: 'strict'" in page)

import json as _json
arch = {"nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
        "edges": [{"from": "a", "to": "b"}]}
ok, msg = srv.publish(title="T", fmt="architecture", content=_json.dumps(arch))
check("architecture publishes", ok, msg[:110])
_, hdrs, page = get("/content")
check("architecture artifact contains NO script", "<script" not in page)
check("architecture served under content CSP",
      "connect-src 'none'" in hdrs.get("Content-Security-Policy", ""))
ok, msg = srv.publish(title="T", fmt="architecture", content="{not json")
check("malformed architecture JSON refused", not ok and "not valid JSON" in msg, msg[:110])
ok, msg = srv.publish(title="T", fmt="architecture",
                      content='{"nodes":[{"id":"a","label":"A"}],"edges":[{"from":"a","to":"zz"}]}')
check("architecture bad edge ref refused", not ok and "not a declared node id" in msg, msg[:110])
ok, msg = srv.publish(title="T", fmt="pdf", content="x")
check("unknown format refused", not ok and "must be one of" in msg, msg[:110])

print("\n=== E: removed /publish endpoint (the executed CSRF from the audit) ===")
req = urllib.request.Request(
    f"http://127.0.0.1:{PORT}/publish", method="POST",
    data=json.dumps({"title": "PWNED", "content": "<h1>injected</h1>"}).encode(),
    headers={"Content-Type": "text/plain", "Origin": "https://evil.example",
             "Host": "attacker.rebind.example"})
try:
    with urllib.request.urlopen(req, timeout=5) as r: code, body = r.status, r.read()
except urllib.error.HTTPError as e: code, body = e.code, e.read()
except Exception as e: code, body = "conn-error", str(e).encode()
check("POST /publish is not a success", code != 200, f"got {code}")

srv.publish(title="Sentinel", content="<h1>sentinel content</h1>")
_, _, page = get("/content")
check("CSRF payload did not reach artifact state", "injected" not in page and "sentinel" in page)

print("\n=== 2: Host validation (DNS rebinding) ===")
code, _, _ = get("/", host="attacker.rebind.example")
check("forged Host rejected on /", code == 421, f"got {code}")
code, _, _ = get("/content", host="evil.example:7899")
check("forged Host rejected on /content", code == 421, f"got {code}")
code, _, _ = get("/", host=f"127.0.0.1:{PORT}")
check("real Host accepted", code == 200, f"got {code}")
code, _, _ = get("/", host=f"localhost:{PORT}")
check("localhost Host accepted", code == 200, f"got {code}")

print("\n=== 3: rendering boundary ===")
code, hdrs, viewer = get("/")
csp_v = hdrs.get("Content-Security-Policy", "")
check("viewer sends CSP", "default-src 'none'" in csp_v)
check("viewer frames content sandboxed",
      'sandbox="allow-scripts"' in viewer and 'src="/content"' in viewer)
check("viewer sandbox omits allow-same-origin", "allow-same-origin" not in viewer)
check("viewer allows its own SSE", "connect-src 'self'" in csp_v)
check("viewer is not framable", hdrs.get("X-Frame-Options") == "DENY")

code, hdrs, _ = get("/content")
csp_c = hdrs.get("Content-Security-Policy", "")
check("content sends CSP", "default-src 'none'" in csp_c)
check("content blocks ALL network (connect-src 'none')", "connect-src 'none'" in csp_c)
check("content blocks remote frames", "frame-src 'none'" in csp_c)
check("content blocks form submission", "form-action 'none'" in csp_c)
check("content blocks plugins/objects", "object-src 'none'" in csp_c)
check("content pins base-uri", "base-uri 'none'" in csp_c)
check("content framable only by us", "frame-ancestors 'self'" in csp_c)
check("content still allows inline script (interactivity)", "script-src 'unsafe-inline'" in csp_c)
check("nosniff set", hdrs.get("X-Content-Type-Options") == "nosniff")
check("no referrer leak", hdrs.get("Referrer-Policy") == "no-referrer")

print("\n=== C: HTML artifact passthrough ===")
srv.publish(title="Dash", content='<!doctype html><html><body><button id="b">go</button>'
                                  '<script>document.getElementById("b").onclick=()=>1</script></body></html>')
_, _, page = get("/content")
check("model HTML served verbatim", "<button id=\"b\">go</button>" in page)
check("no banner injected into artifact", "_ab" not in page)

print("\n=== D: Markdown ===")
srv.publish(title="MD", fmt="markdown", content="# H\n\n- a\n- b\n\n| x | y |\n|---|---|\n| 1 | 2 |\n\n`code`\n\n[l](http://e.com)\n\n<script>alert(1)</script>\n")
_, hdrs, page = get("/content")
check("markdown uses marked", "marked.parse" in page)
check("marked is inlined, no CDN", "marked v4.3.0" in page and "cdn" not in page.lower())
check("markdown served under the SAME content CSP",
      "connect-src 'none'" in hdrs.get("Content-Security-Policy", ""))
check("raw <script> in markdown IS present in source (contained, not sanitized)",
      "alert(1)" in page)

print("\n=== F: persistence ===")
srv.publish(title="Persisted", content="<h1>survive me</h1>")
check("state file written", srv.CONTENT_FILE.exists())
check("state dir is 0700", oct(srv.STATE_DIR.stat().st_mode)[-3:] == "700",
      oct(srv.STATE_DIR.stat().st_mode))
check("state file is 0600", oct(srv.CONTENT_FILE.stat().st_mode)[-3:] == "600",
      oct(srv.CONTENT_FILE.stat().st_mode))
saved = json.loads(srv.CONTENT_FILE.read_text())
check("persisted payload round-trips", saved["title"] == "Persisted" and "survive me" in saved["content"])
check("state is NOT under ~/.claude", ".claude/artifacts" not in str(srv.CONTENT_FILE))

print("\n=== B: republish updates in place ===")
before = f"http://127.0.0.1:{PORT}"
srv.publish(title="V1", content="<h1>one</h1>")
_, _, p1 = get("/content")
srv.publish(title="V2", content="<h1>one</h1><h2>two</h2>")
_, _, p2 = get("/content")
check("same URL serves updated content", "two" in p2 and "two" not in p1)
_, _, v = get("/")
check("banner reflects new title", "V2" in v)
check("auto-open fired at most once", srv._opened_once is True)

print("\n=== E: occupied port is reported honestly ===")
blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
blocker.bind(("127.0.0.1", 7898)); blocker.listen(1)
probe = subprocess.run(
    [sys.executable, "-c",
     "import importlib.util,os,sys;"
     "spec=importlib.util.spec_from_file_location('s',sys.argv[1]);"
     "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
     "m.start_http_thread();m._http_ready.wait(5);"
     "print(m.publish(title='T',content='<p>x</p>'))",
     str(Path(__file__).parent / "server.py")],
    env={**os.environ, "LOCAL_ARTIFACTS_PORT": "7898"},
    capture_output=True, text=True, timeout=30)
out = probe.stdout.strip()
check("occupied port -> publish returns failure", "False" in out, out[:200])
check("occupied port -> says NOT published", "was NOT published" in out, out[:200])
check("occupied port -> names the port", "7898" in out, out[:200])
blocker.close()

print("\n=== unknown routes ===")
code, _, _ = get("/etc/passwd"); check("unknown path 404s", code == 404, str(code))
code, _, _ = get("/status"); check("/status responds", code == 200, str(code))

shutil.rmtree(ROOT, ignore_errors=True); shutil.rmtree(OUT, ignore_errors=True)
shutil.rmtree(STATE, ignore_errors=True)
print(f"\n{'='*46}\n  PASS {PASS}   FAIL {FAIL}\n{'='*46}")
sys.exit(1 if FAIL else 0)
