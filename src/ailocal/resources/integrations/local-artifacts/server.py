#!/usr/bin/env python3
"""
local-artifacts — a local artifact renderer for Claude Code sessions that
authenticate with an API key (e.g. ailocal's `claude-local`), where the hosted
Artifact tool is not registered.

Forked from xiagaohui/local-artifacts-for-claude-code @ ddb4796 (MIT).
See NOTICE for what changed and why.

    Claude Code --spawns--> this process
                              |-- stdio MCP server  (publish_artifact)
                              `-- HTTP thread on 127.0.0.1:PORT

Rendering boundary: the top-level document at `/` is a TRUSTED viewer we
generate. Model-generated content is served separately at `/content` and framed
inside `sandbox="allow-scripts"` (no allow-same-origin -> opaque origin) under a
`connect-src 'none'` CSP. Scripts still run; they just cannot reach anything.
"""

import html
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import architecture

# ── Config ────────────────────────────────────────────────────────────────────
PORT = int(os.environ.get("LOCAL_ARTIFACTS_PORT", "7891"))
MAX_SIZE = 16 * 1024 * 1024  # 16 MiB

# State lives in a local-artifacts-owned XDG location. Never ~/.claude: that
# tree belongs to Claude Code, and ailocal's invariants forbid touching it.
_xdg_state = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
STATE_DIR = Path(_xdg_state) / "local-artifacts"
STATE_FILE = STATE_DIR / "state.json"
CONTENT_FILE = STATE_DIR / "current_content.json"
STATE_DIR.mkdir(parents=True, exist_ok=True)
try:
    STATE_DIR.chmod(0o700)
except OSError:
    pass

# Approved root for file_path publishing.
#
# Claude Code spawns stdio MCP servers with cwd = the directory Claude Code was
# launched in [REAL, probed]. One server process serves one session, which is
# one project, so cwd-at-startup is both safe and practical: a file Claude just
# wrote in the active project publishes, while ~/.ssh/notes.txt does not.
# An explicit override wins when the operator sets one.
# Precedence: explicit override, then Claude Code's supported project-root
# contract, then cwd. [REAL] verified on 2.1.231: stdio MCP servers DO receive
# CLAUDE_PROJECT_DIR, and it equals the directory Claude Code was launched in --
# it is NOT the git root (probed from a subdirectory of a real repo). So this is
# the same value cwd would give today, but it is the supported contract rather
# than an inference, and it survives anything that changes the child's cwd.
def _resolve_root():
    for candidate in (os.environ.get("LOCAL_ARTIFACTS_ROOT"),
                      os.environ.get("CLAUDE_PROJECT_DIR")):
        if candidate:
            try:
                pth = Path(candidate).expanduser().resolve()
            except OSError:
                continue
            if pth.is_dir():
                return pth
    try:
        return Path.cwd().resolve()
    except OSError:
        return Path.home().resolve()


APPROVED_ROOT = _resolve_root()
ARTIFACT_DIR = APPROVED_ROOT / ".artifacts"

SOURCE_EXT = {"architecture": ".architecture.json", "mermaid": ".mmd",
              "html": ".html", "markdown": ".md"}

ALLOWED_SUFFIXES = (".html", ".htm", ".md", ".markdown", ".txt",
                    ".json", ".mmd", ".mermaid")
FORMATS = ("html", "markdown", "mermaid", "architecture")

VENDOR = Path(__file__).parent / "vendor"
MARKED_JS_PATH = VENDOR / "marked.min.js"
MERMAID_JS_PATH = VENDOR / "mermaid.min.js"


def _vendor(path):
    """Bundled libraries are read from disk by the TRUSTED server and inlined
    into the page. Generated content never fetches them, which is what keeps the
    zero-network boundary intact while still giving artifacts a real renderer."""
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


MARKED_JS = _vendor(MARKED_JS_PATH)

# ── Shared state ──────────────────────────────────────────────────────────────
_state: dict = {
    "title": "",
    "emoji": "\U0001F4C4",
    "content": "",
    "format": "html",
    "published_at": "",
    "artifact_id": "",
}
_sse_clients: list = []
_state_lock = threading.Lock()
_sse_lock = threading.Lock()
_http_error: str = ""
_http_ready = threading.Event()   # set once bind succeeded OR failed
_opened_once = False


def _load_persisted_state():
    try:
        if CONTENT_FILE.exists():
            saved = json.loads(CONTENT_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                _state.update({k: saved.get(k, _state[k]) for k in _state})
    except Exception:
        pass


_load_persisted_state()

# ── CSP ───────────────────────────────────────────────────────────────────────
# The viewer is ours. It needs SSE (connect-src 'self') and must be able to
# frame /content (frame-src 'self').
VIEWER_CSP = (
    "default-src 'none'; "
    "script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; "
    "connect-src 'self'; "
    "frame-src 'self'; "
    "img-src data:; "
    "base-uri 'none'; "
    "form-action 'none'"
)

# The artifact is untrusted. connect-src 'none' blocks fetch/XHR/WebSocket/
# EventSource outright -- including no-cors requests, which are otherwise sent
# for their side effects even though the response is opaque. That is what keeps
# generated JS away from Ollama/Qdrant/LiteLLM on other loopback ports.
CONTENT_CSP = (
    "default-src 'none'; "
    "script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; "
    "img-src data:; "
    "font-src data:; "
    "media-src data:; "
    "connect-src 'none'; "
    "frame-src 'none'; "
    "child-src 'none'; "
    "object-src 'none'; "
    "form-action 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'self'"
)

# ── Viewer (trusted, top-level) ───────────────────────────────────────────────

VIEWER_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>
html,body{{margin:0;padding:0;height:100%;background:#1e1e2e;}}
#_ab{{position:fixed;top:0;left:0;right:0;height:34px;
  background:#1e1e2e;color:#cdd6f4;
  font:12px/34px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  padding:0 16px;z-index:99999;display:flex;gap:12px;align-items:center;
  border-bottom:1px solid #313244;box-sizing:border-box;}}
#_ab ._title{{font-weight:600;color:#89b4fa;}}
#_ab ._sep{{opacity:.3;}}
#_ab ._time{{opacity:.5;font-size:11px;}}
#_ab ._tag{{margin-left:auto;opacity:.45;font-size:11px;}}
#_ab ._dot{{width:8px;height:8px;border-radius:50%;background:#a6e3a1;
  animation:_pulse 2s infinite;flex:none;}}
@keyframes _pulse{{0%,100%{{opacity:1;}}50%{{opacity:.4;}}}}
/* An iframe is a REPLACED element: with height:auto an absolutely positioned
   one uses its intrinsic 150px and ignores `bottom`, so the frame must be
   given an explicit height or the artifact renders in a 150px letterbox. */
#_frame{{position:fixed;top:35px;left:0;width:100%;height:calc(100% - 35px);
  border:0;background:#fff;}}
</style>
</head>
<body>
<div id="_ab">
  <span class="_dot"></span><span>{emoji}</span>
  <span class="_title">{title}</span>
  <span class="_sep">&middot;</span>
  <span class="_time">local artifact &middot; {published_at}</span>
  <span class="_tag">sandboxed</span>
</div>
<iframe id="_frame" src="/content" sandbox="allow-scripts"
        title="artifact content"></iframe>
<script>
(function(){{
  var es = new EventSource('/events');
  es.onmessage = function(){{ window.location.reload(); }};
  es.onerror = function(){{ setTimeout(function(){{ window.location.reload(); }}, 3000); }};
}})();
</script>
</body>
</html>"""

EMPTY_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Local artifacts</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  display:flex;align-items:center;justify-content:center;height:100vh;margin:0;
  background:#1e1e2e;color:#a6adc8;}
.box{text-align:center;padding:2rem;}
h2{color:#cdd6f4;margin-bottom:.5rem;font-weight:600;}
code{background:#313244;color:#cdd6f4;padding:.25em .6em;border-radius:4px;}
</style></head>
<body><div class="box">
<h2>No artifact yet</h2>
<p>Ask Claude for one, e.g. <code>show this as an artifact</code>.</p>
</div>
<script>
(function(){
  var es = new EventSource('/events');
  es.onmessage = function(){ window.location.reload(); };
})();
</script>
</body></html>"""


def build_viewer(state: dict) -> str:
    return VIEWER_PAGE.format(
        title=html.escape(state.get("title") or "Artifact"),
        emoji=html.escape(state.get("emoji") or "\U0001F4C4"),
        published_at=html.escape(state.get("published_at") or ""),
    )


# ── Artifact content (untrusted, framed) ──────────────────────────────────────

MARKDOWN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  color:#24292f;max-width:900px;margin:0 auto;padding:2rem;line-height:1.6;}}
h1,h2,h3,h4{{margin-top:1.5em;border-bottom:1px solid #d0d7de;padding-bottom:.3em;}}
h1{{border-bottom:2px solid #d0d7de;}}
code{{font-family:'SFMono-Regular',Consolas,monospace;font-size:.9em;
  background:#f6f8fa;padding:.2em .4em;border-radius:4px;}}
pre{{background:#f6f8fa;padding:1rem;border-radius:6px;overflow-x:auto;}}
pre code{{background:none;padding:0;}}
blockquote{{border-left:4px solid #d0d7de;margin:0;padding:0 1em;color:#57606a;}}
table{{border-collapse:collapse;width:100%;margin:1em 0;}}
th,td{{border:1px solid #d0d7de;padding:6px 13px;}}
th{{background:#f6f8fa;font-weight:600;}}
tr:nth-child(2n){{background:#f6f8fa;}}
img{{max-width:100%;}}
a{{color:#0969da;}}
hr{{border:none;border-top:1px solid #d0d7de;margin:2em 0;}}
</style>
</head>
<body>
<div id="_content"></div>
<script>{marked_js}</script>
<script>
var _md = {escaped_content};
document.getElementById('_content').innerHTML =
  (typeof marked !== 'undefined')
    ? marked.parse(_md)
    : '<pre>'+_md.replace(/&/g,'&amp;').replace(/</g,'&lt;')+'</pre>';
</script>
</body>
</html>"""


def build_markdown_content(content: str) -> str:
    """Markdown renders under the SAME untrusted boundary as HTML.

    marked v4.3.0 passes raw HTML through and we do not sanitize it. That is a
    deliberate, documented choice: sanitizing would break legitimate inline HTML
    in Markdown, and the sandbox+CSP boundary already contains anything that
    runs. json.dumps plus the '</' escape only keep the payload from breaking
    out of the <script> string -- they are not a security control.
    """
    escaped = json.dumps(content).replace("</", "<\\/")
    return MARKDOWN_PAGE.format(marked_js=MARKED_JS, escaped_content=escaped)


def build_html_content(content: str) -> str:
    """Model HTML is served as-is. No banner or SSE is injected into it -- both
    live in the trusted viewer now, so this document stays exactly what the
    model wrote and the sandbox does the containing."""
    lo = content.lower()
    if "<body" in lo or "<!doctype" in lo or "<html" in lo:
        return content
    return ("<!DOCTYPE html>\n<html lang=\"en\">\n<head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "</head>\n<body>\n" + content + "\n</body>\n</html>")


MERMAID_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{{--bg:{canvas_light};--surface:{surface_light};--ink:{ink_light};--line:{border_light}}}
@media (prefers-color-scheme:dark){{
  :root{{--bg:{canvas_dark};--surface:{surface_dark};--ink:{ink_dark};--line:{border_dark}}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
 font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}}
.wrap{{max-width:1400px;margin:0 auto;padding:32px 24px 48px}}
.figure{{background:var(--surface);border:1px solid var(--line);border-radius:10px;
 padding:24px;overflow-x:auto}}
.mermaid{{margin:0;text-align:center}}
.mermaid svg{{max-width:100%;height:auto}}
</style>
</head>
<body>
<div class="wrap"><div class="figure"><pre class="mermaid">{source}</pre></div></div>
<script>{mermaid_js}</script>
<script>
// Mermaid reads the SAME canonical tokens as the architecture renderer, mapped
// onto its documented themeVariables, so a Mermaid diagram and an ELK diagram
// look like one product rather than two. base + themeVariables is Mermaid's
// supported customisation path (mermaid.js.org/config/theming.html).
var _dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
var T = _dark ? {dark_json} : {light_json};
mermaid.initialize({{
  startOnLoad: true,
  // strict: no HTML labels, no click handlers -- the diagram source is model
  // output and gets no more trust than any other artifact content.
  securityLevel: 'strict',
  theme: 'base',
  themeVariables: {{
    background: T.canvas,
    primaryColor: T.surface,
    primaryTextColor: T.ink,
    primaryBorderColor: T.accent.client,
    secondaryColor: T.group_surface,
    tertiaryColor: T.group_surface,
    lineColor: T.muted,
    textColor: T.ink,
    mainBkg: T.surface,
    nodeBorder: T.border,
    clusterBkg: T.group_surface,
    clusterBorder: T.border,
    edgeLabelBackground: T.surface,
    fontFamily: {sans_json},
    fontSize: '14px'
  }},
  flowchart: {{ curve: 'basis', useMaxWidth: true }}
}});
</script>
</body>
</html>"""


def build_mermaid_content(source: str) -> str:
    """Mermaid runs INSIDE the sandboxed iframe, exactly like marked does for
    Markdown: opaque origin, connect-src 'none'. The library is inlined from
    vendor/ by this trusted process -- the page fetches nothing."""
    js = _vendor(MERMAID_JS_PATH)
    if not js:
        return ("<!DOCTYPE html><html><body><p>mermaid.min.js is missing from "
                "vendor/. Reinstall local-artifacts.</p></body></html>")
    import architecture as _arch
    theme = _arch.THEME
    return MERMAID_PAGE.format(
        source=html.escape(source), mermaid_js=js,
        light_json=json.dumps(theme["light"]), dark_json=json.dumps(theme["dark"]),
        sans_json=json.dumps(theme["typography"]["sans"]),
        canvas_light=theme["light"]["canvas"], canvas_dark=theme["dark"]["canvas"],
        surface_light=theme["light"]["surface"], surface_dark=theme["dark"]["surface"],
        ink_light=theme["light"]["ink"], ink_dark=theme["dark"]["ink"],
        border_light=theme["light"]["border"], border_dark=theme["dark"]["border"])


def build_content_page(state: dict) -> str:
    fmt = state.get("format")
    if fmt == "markdown":
        return build_markdown_content(state.get("content", ""))
    if fmt == "mermaid":
        return build_mermaid_content(state.get("content", ""))
    # `architecture` was already rendered to a complete static-SVG document at
    # publish time, so by the time it is served it is just html.
    return build_html_content(state.get("content", ""))


# ── SSE ───────────────────────────────────────────────────────────────────────

def _notify_sse():
    ts = str(int(time.time()))
    with _sse_lock:
        clients = list(_sse_clients)
    for q in clients:
        try:
            q.put_nowait(ts)
        except Exception:
            pass


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _host_ok(host: str) -> bool:
    """Reject DNS-rebinding-style Host headers, permit the URLs we emit.

    A browser pointed at http://127.0.0.1:PORT sends exactly one of these. A
    remote page that has rebound a name to 127.0.0.1 sends its own hostname, so
    this is what stops it reading the artifact back."""
    if not host:
        return False
    h = host.strip().lower()
    allowed_hosts = ("127.0.0.1", "localhost", "[::1]", "::1")
    for a in allowed_hosts:
        if h == a or h == f"{a}:{PORT}":
            return True
    return False


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "local-artifacts"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass  # MCP speaks stdio; stdout must stay clean

    # No do_POST at all. Publishing happens only in-process via _publish().
    # Upstream's POST /publish accepted a cross-origin simple request with a
    # forged Host and no auth; it was unused by the MCP path, so it is gone
    # rather than patched.

    def do_GET(self):
        if not _host_ok(self.headers.get("Host", "")):
            self._send(421, "text/plain; charset=utf-8",
                       b"Misdirected request: unrecognised Host header.")
            return

        path = self.path.split("?")[0]

        if path in ("/", "/artifact", "/artifact/latest"):
            with _state_lock:
                s = dict(_state)
            if not s.get("content"):
                self._send(200, "text/html; charset=utf-8",
                           EMPTY_PAGE.encode("utf-8"), csp=VIEWER_CSP,
                           extra={"X-Frame-Options": "DENY"})
                return
            self._send(200, "text/html; charset=utf-8",
                       build_viewer(s).encode("utf-8"), csp=VIEWER_CSP,
                       extra={"X-Frame-Options": "DENY"})

        elif path == "/content":
            with _state_lock:
                s = dict(_state)
            if not s.get("content"):
                self._send(404, "text/plain; charset=utf-8", b"No artifact")
                return
            self._send(200, "text/html; charset=utf-8",
                       build_content_page(s).encode("utf-8"), csp=CONTENT_CSP)

        elif path == "/events":
            self._sse()

        elif path == "/status":
            with _state_lock:
                s = dict(_state)
            s.pop("content", None)
            s["http_error"] = _http_error
            s["port"] = PORT
            s["approved_root"] = str(APPROVED_ROOT)
            self._send(200, "application/json; charset=utf-8",
                       json.dumps(s, ensure_ascii=False).encode("utf-8"))

        else:
            self._send(404, "text/plain; charset=utf-8", b"Not found")

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        q: queue.Queue = queue.Queue(maxsize=8)
        with _sse_lock:
            _sse_clients.append(q)
        try:
            while True:
                try:
                    msg = q.get(timeout=25)
                    self.wfile.write(f"data: {msg}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except Exception:
            pass
        finally:
            with _sse_lock:
                try:
                    _sse_clients.remove(q)
                except ValueError:
                    pass

    def _send(self, code, ct, body, csp=None, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        if csp:
            self.send_header("Content-Security-Policy", csp)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass


def _publish(title, emoji, content, fmt, published_at, artifact_id=""):
    now = published_at or time.strftime("%Y-%m-%d %H:%M:%S")
    with _state_lock:
        _state.update({"title": title, "emoji": emoji, "content": content,
                       "format": fmt, "published_at": now,
                       "artifact_id": artifact_id or _state.get("artifact_id", "")})
        snap = dict(_state)
    try:
        meta = {k: v for k, v in snap.items() if k != "content"}
        STATE_FILE.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        CONTENT_FILE.write_text(json.dumps(snap, ensure_ascii=False))
        for f in (STATE_FILE, CONTENT_FILE):
            try:
                f.chmod(0o600)
            except OSError:
                pass
    except Exception:
        pass
    _notify_sse()


def _run_http():
    global _http_error
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        _http_error = (f"cannot bind 127.0.0.1:{PORT} ({e}). Another "
                       f"local-artifacts process is probably already serving "
                       f"this port; close that session or set "
                       f"LOCAL_ARTIFACTS_PORT.")
        _http_ready.set()
        return
    _http_ready.set()
    srv.serve_forever()


def _open_browser(url):
    global _opened_once
    if _opened_once:
        return
    if os.environ.get("LOCAL_ARTIFACTS_AUTO_OPEN", "1") == "0":
        _opened_once = True
        return
    if sys.platform == "darwin":
        cmd = ["open", url]
    elif sys.platform.startswith("linux"):
        cmd = ["xdg-open", url]
    elif sys.platform.startswith("win"):
        cmd = ["cmd", "/c", "start", "", url]
    else:
        return
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _opened_once = True
    except Exception:
        pass


# ── Publish logic shared by MCP and tests ─────────────────────────────────────

class PublishError(Exception):
    pass


# Subresources the sandbox CSP will refuse to load. An <a href> is navigation,
# not a subresource, so it is deliberately NOT matched here.
_SUBRESOURCE_PATTERNS = (
    re.compile(r"""<script[^>]*\ssrc\s*=\s*["']?\s*(https?:)?//([^"'\s>]+)""", re.I),
    re.compile(r"""<link[^>]*\shref\s*=\s*["']?\s*(https?:)?//([^"'\s>]+)""", re.I),
    re.compile(r"""<(?:img|iframe|video|audio|source|embed|object)[^>]*\s(?:src|data)\s*=\s*["']?\s*(https?:)?//([^"'\s>]+)""", re.I),
    re.compile(r"""@import\s+(?:url\()?["']?\s*(https?:)?//([^"'\s)]+)""", re.I),
    re.compile(r"""url\(\s*["']?\s*(https?:)?//([^"'\s)]+)""", re.I),
)


def external_subresources(content):
    """Remote subresources the artifact will never be able to load.

    [REAL] gemma4:26b-mlx answered "draw an architecture diagram" with a page
    whose only content was a mermaid <script src> from cdn.jsdelivr.net and no
    inline SVG. Under this CSP that page renders blank. Publishing it and
    reporting success would be reporting a success that is not one, so the
    reference is detected and the publish is refused with something the model
    can act on.
    """
    found = []
    for pat in _SUBRESOURCE_PATTERNS:
        for m in pat.finditer(content or ""):
            host = m.group(2).split("/")[0]
            if host and host not in found:
                found.append(host)
    return found


def resolve_input(content: str, file_path: str):
    """Returns (content, format_hint). Raises PublishError with a plain reason."""
    if file_path and not content:
        try:
            p = Path(file_path).expanduser()
            if not p.is_absolute():
                p = APPROVED_ROOT / p
            # resolve() follows symlinks, so a link inside the root pointing
            # outside it is caught by the containment check below.
            p = p.resolve()
        except OSError as e:
            return None, f"cannot resolve path: {e}"
        if p.suffix.lower() not in ALLOWED_SUFFIXES:
            return None, (f"refusing to publish '{p.suffix or 'no suffix'}' files. "
                          f"Allowed: {', '.join(ALLOWED_SUFFIXES)}.")
        try:
            p.relative_to(APPROVED_ROOT)
        except ValueError:
            return None, (f"path is outside the approved root {APPROVED_ROOT}. "
                          f"Publish files from the current project, or pass "
                          f"content directly.")
        if not p.exists() or not p.is_file():
            return None, f"file not found: {p}"
        try:
            if p.stat().st_size > MAX_SIZE:
                return None, f"file exceeds the {MAX_SIZE // 1024 // 1024} MiB limit."
            text = p.read_text(encoding="utf-8")
        except Exception as e:
            return None, f"cannot read file: {e}"
        sfx = p.suffix.lower()
        if sfx == ".json":
            fmt = "architecture"
        elif sfx in (".mmd", ".mermaid"):
            fmt = "mermaid"
        elif sfx in (".md", ".markdown", ".txt"):
            fmt = "markdown"
        else:
            fmt = "html"
        return (text, fmt), None
    if not content:
        return None, "provide either 'content' or 'file_path'."
    if len(content.encode("utf-8")) > MAX_SIZE:
        return None, f"content exceeds the {MAX_SIZE // 1024 // 1024} MiB limit."
    return (content, None), None


def slugify(text, fallback="artifact"):
    """A filesystem-safe id. Deliberately conservative: lowercase, [a-z0-9-],
    collapsed dashes, length-capped, and never empty, so an id can never escape
    the artifact directory or collide with a dotfile."""
    out = []
    for ch in (text or "").lower():
        if ch.isalnum() and ch.isascii():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    slug = "".join(out).strip("-")[:60]
    return slug or fallback


def _source_path(artifact_id, fmt):
    return ARTIFACT_DIR / (artifact_id + SOURCE_EXT.get(fmt, ".txt"))


def persist_source(artifact_id, fmt, source):
    """Write the canonical SOURCE -- the semantic graph, the Mermaid text, the
    HTML the model wrote -- not the rendered SVG. Rendered output is cache; the
    source is what stays editable and reproducible.

    Returns (path, note). Never raises: a project that cannot be written to
    (read-only mount, permissions) must still get a working preview, and the
    tool result says so rather than pretending a file exists.
    """
    try:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        path = _source_path(artifact_id, fmt)
        path.write_text(source, encoding="utf-8")
        return path, None
    except Exception as e:
        return None, f"could not write the source file ({e})"


def resolve_artifact_id(title, artifact_id):
    """One title maps to one artifact, deterministically.

    An earlier version appended -2/-3 when a derived id already existed, but the
    result depended on whether persisted state happened to be restored, so the
    SAME action produced a different filename in a fresh session than in a warm
    one. Predictability matters more here: a derived id always addresses the same
    file, so regenerating a diagram across sessions updates it instead of
    littering .artifacts/ with near-duplicates.

    The trade-off, stated plainly: two genuinely different artifacts that share a
    title share a file. That is why every result reports the artifact_id and
    source_path it used, and why artifact_id is an explicit parameter -- passing
    distinct ids is how you keep distinct artifacts apart.
    """
    if artifact_id and artifact_id.strip():
        return slugify(artifact_id)
    return slugify(title, fallback="artifact")


def publish(title, content="", file_path="", fmt="html", emoji="\U0001F4C4",
            artifact_id=""):
    """Full publish path. Returns (ok: bool, message: str)."""
    if not isinstance(title, str) or not title.strip():
        return False, "Publish failed: 'title' is required and must be a string."
    if fmt not in FORMATS:
        return False, ("Publish failed: 'format' must be one of "
                       + ", ".join(FORMATS) + ".")

    resolved, err = resolve_input(content or "", file_path or "")
    if err:
        return False, f"Publish failed: {err}"

    text, fmt_hint = resolved
    if fmt_hint:
        fmt = fmt_hint

    source_text, source_fmt = text, fmt

    if fmt == "architecture":
        # The model supplies MEANING (nodes/groups/edges/kinds); ELK supplies
        # geometry and the design system supplies presentation. What gets served
        # is a complete static-SVG document with no script in it at all.
        try:
            spec = json.loads(text)
        except json.JSONDecodeError as e:
            return False, (f"Publish failed: format 'architecture' expects a JSON "
                           f"object describing nodes/groups/edges, but the content "
                           f"is not valid JSON ({e.msg} at line {e.lineno}).")
        try:
            text = architecture.build(spec)
        except architecture.SpecError as e:
            return False, f"Publish failed: {e}"
        except Exception as e:
            return False, f"Publish failed: could not lay out the diagram ({e})."
        fmt = "html"

    remote = external_subresources(text)
    if remote:
        return False, (
            "Publish failed: the artifact loads resources from "
            + ", ".join(remote[:4])
            + ". Artifacts run sandboxed with no network access, so those would "
              "silently fail and the page would render blank or unstyled. "
              "Rewrite it self-contained -- inline the CSS and JS, draw diagrams "
              "as inline <svg> rather than a charting library, use system fonts, "
              "and embed any image as a data: URI -- then publish again."
        )

    _http_ready.wait(timeout=5)
    if _http_error:
        return False, (f"Publish failed: the artifact was NOT published because "
                       f"the local server is not running. {_http_error}")

    aid = resolve_artifact_id(title, artifact_id)

    # Persistence is automatic. [REAL] across the routing benchmark the model
    # published substantial artifacts with content= every single time and never
    # chose file_path, so leaving durability to the model's judgement left
    # nothing behind when the session exited.
    src_path, src_note = persist_source(aid, source_fmt, source_text)

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    _publish(title=title, emoji=emoji, content=text, fmt=fmt, published_at=now,
             artifact_id=aid)
    url = f"http://127.0.0.1:{PORT}"
    _open_browser(url)
    lines = [f"Artifact published.",
             f"  artifact_id:  {aid}",
             f"  preview_url:  {url}",
             f"  source_path:  {src_path if src_path else '(not written)'}"]
    if src_note:
        lines.append(f"  note:         {src_note}")
    lines.append(f"  format:       {source_fmt}")
    lines.append(f"Publish again with artifact_id=\"{aid}\" to update this artifact "
                 f"in place; the source file and the URL stay the same.")
    return True, "\n".join(lines)


# ── MCP ───────────────────────────────────────────────────────────────────────

SERVER_INSTRUCTIONS = (
    "Renders and publishes local visual artifacts. Use this server whenever the "
    "user asks to create, show, display, preview, diagram, visualize, present or "
    "update an artifact, dashboard, chart, architecture or system diagram, "
    "request/data-flow diagram, interactive visualization, polished comparison, "
    "or styled report.\n"
    "Publish the finished visual with mcp__artifact__publish. Do NOT describe the "
    "artifact, print its JSON, or emit it in a fenced code block in chat -- an "
    "artifact only exists once the tool is called.\n"
    "formats: architecture (JSON of nodes/groups/edges; layout is computed for "
    "you, never write coordinates), mermaid (Mermaid source), html (one "
    "self-contained document), markdown (prose).\n"
    "Not for files the user wants kept in the repository -- write those normally."
)

TOOL_DESCRIPTION = (
    # The exact native name comes first. [REAL] with the longer name
    # `mcp__local-artifacts__publish_artifact` the model emitted a well-formed
    # tool_use that simply dropped the mandatory `mcp__` prefix in 3 of 18 runs.
    # Plugin packaging was measured and REJECTED for making this worse: a
    # plugin-provided server is namespaced mcp__plugin_<plugin>_<server>__<tool>.
    "Use mcp__artifact__publish to create or update a local artifact preview.\n"
    "  architecture - system, service, deployment or request/data-flow diagrams. "
    "Send JSON: {\"title\":..., \"groups\":[{\"id\",\"label\"}], "
    "\"nodes\":[{\"id\",\"label\",\"kind\",\"group\",\"subtitle\"}], "
    "\"edges\":[{\"from\",\"to\",\"kind\",\"label\"}]}. "
    "node kind: client|service|router|runtime|model|database|external|tool. "
    "edge kind: request|inference|tool|data|dependency. "
    "Layout is computed for you -- never write coordinates or SVG.\n"
    "  mermaid - flowchart, sequence, state, class or ER diagram. Mermaid source.\n"
    "  html - dashboards and interactive UI. One self-contained document.\n"
    "  markdown - document or report previews.\n"
    "Pass artifact_id to update an existing artifact in place. The source file "
    "is saved for you under .artifacts/ automatically. "
    "Artifacts have no network access, so remote scripts, styles, fonts and "
    "images are refused. "
    "Do not use this for a file the user asked to keep in the repository."
)


def build_mcp():
    """Build the MCP server.

    mcp 2.x renamed FastMCP to MCPServer and dropped the v1
    @app.list_tools()/@app.call_tool() decorators the upstream project used;
    the schema is derived from the annotations and Field descriptions below.
    """
    from mcp.server.mcpserver import MCPServer
    from pydantic import Field
    from typing import Annotated, Literal

    # Server instructions. With ToolSearch enabled Claude Code keeps these
    # visible while tool SCHEMAS are deferred, so this is the routing signal
    # that survives deferral -- and the documented place to say what the server
    # is for and when to reach for it. Claude Code truncates at 2 KB, so the
    # important semantics come first.
    server = MCPServer(name="local-artifacts", instructions=SERVER_INSTRUCTIONS)

    # Claude Code's ToolSearch defers most MCP schemas to keep the model's tool
    # surface small -- measured here at 70 tools -> 23. That is exactly the
    # pressure a local model buckles under, but the default deferral picked the
    # WRONG tools: it deferred this one and kept 11 grepai tools eager.
    # `anthropic/alwaysLoad` is the supported per-tool opt-out, declared by the
    # server that owns the tool, so nothing in ailocal or the operator's config
    # has to know about it. searchHint is what ToolSearch matches on when a tool
    # IS deferred, so it is worth setting either way.
    @server.tool(name="publish", description=TOOL_DESCRIPTION,
                 structured_output=False,
                 meta={"anthropic/alwaysLoad": True,
                       "anthropic/searchHint": "create, show, preview, diagram "
                                               "or update an artifact, chart, "
                                               "dashboard, diagram or report"})
    def publish_tool(
        title: Annotated[str, Field(
            description="Short title shown in the viewer banner.")],
        content: Annotated[str, Field(
            description="The artifact body, matching 'format': a complete HTML "
                        "document, Markdown text, Mermaid source, or an "
                        "architecture JSON object. Required unless file_path "
                        "is given.")] = "",
        format: Annotated[Literal["html", "markdown", "mermaid", "architecture"], Field(
            description="architecture = JSON of nodes/groups/edges, laid out for "
                        "you (use for architecture and system diagrams); "
                        "mermaid = Mermaid source; html = a self-contained page; "
                        "markdown = prose. Default html.")] = "html",
        emoji: Annotated[str, Field(
            description="Optional emoji for the banner.")] = "\U0001F4C4",
        artifact_id: Annotated[str, Field(
            description="Stable id for this artifact, e.g. 'ailocal-architecture'. "
                        "Pass the same id again to update it in place. Derived "
                        "from the title when omitted.")] = "",
        file_path: Annotated[str, Field(
            description="Optional: publish an existing file from the project "
                        "instead of inline content. Format follows the suffix.")] = "",
    ) -> str:
        ok, msg = publish(title=title, content=content, file_path=file_path,
                          fmt=format, emoji=emoji or "\U0001F4C4",
                          artifact_id=artifact_id)
        return msg

    return server


def start_http_thread():
    t = threading.Thread(target=_run_http, daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    # HTTP first: publish() waits on _http_ready, so a bind failure is known
    # before the first tool call rather than racing it.
    start_http_thread()
    try:
        build_mcp().run("stdio")
    except KeyboardInterrupt:
        pass
