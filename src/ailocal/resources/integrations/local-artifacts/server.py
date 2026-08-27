#!/usr/bin/env python3
"""
local-artifacts — a local artifact renderer for Claude Code sessions that
authenticate with an API key (e.g. ailocal's `claude-local`), where the hosted
Artifact tool is not registered.

Forked from xiagaohui/local-artifacts-for-claude-code @ ddb4796 (MIT).
See NOTICE for what changed and why.

    Claude Code --spawns--> this process   (one per session, stdio MCP)
                              `-- publish -> state file -> shared preview server

    server.py --serve       ONE per machine, started on demand by the first
                            publish, reused by every session, NOT a child of any
                            of them. A listener inside the MCP process dies when
                            Claude Code terminates that process at session end,
                            which is what made preview URLs refuse connections.
                            See docs/adr/013-artifact-preview-lifetime.md.

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

# The preview server outlives the session that published to it, so it needs its
# own way to die. It exits after this many seconds with no request, no publish
# and no connected viewer. 0 disables the timeout. One shared ~25 MiB process is
# the entire budget: every session reuses it instead of starting its own. Reaping
# also bounds the one thing that does grow: [REAL] rendering Mermaid inlines a
# 3.4 MB library per page and RSS climbs to a ~361 MiB plateau after ~60 renders
# (allocator high-water, not a leak -- live allocations stay flat). See README.
#
# 30 minutes, not longer, because nothing is lost by reaping: [REAL] a cold
# start costs 0.351s against 0.18s warm, so the next publish pays ~170ms and
# transparently gets a fresh server. Nothing is lost by reaping *early* either,
# because everything that constitutes use defers it -- an open tab holds an SSE
# connection and blocks the reaper outright, any GET resets the clock, and so
# does an incoming publish. What remains is the one case worth the wait: a
# transcript URL reopened later with no tab still open and nothing republishing.
# Half an hour covers the pauses inside a working session; past that the artifact
# is still on disk under .artifacts/ and republishing brings it straight back.
IDLE_EXIT = int(os.environ.get("LOCAL_ARTIFACTS_IDLE_EXIT", "1800"))  # 30 min

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
_last_activity = time.time()      # serve mode: drives the idle reaper
_content_mtime = 0.0              # serve mode: last CONTENT_FILE we ingested


def _atomic_write(path: Path, text: str) -> None:
    """Write 0600, atomically. The preview server reads these files from another
    process, so a reader must never observe a partially written artifact."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _load_persisted_state():
    global _content_mtime
    try:
        if CONTENT_FILE.exists():
            _content_mtime = CONTENT_FILE.stat().st_mtime
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


#: Mermaid statements that carry PRESENTATION rather than meaning. The model is
#: asked for semantics and supplies colour anyway: [REAL] 4 of 18 captured
#: artifacts hard-coded fills, including `fill:#f9f` and `fill:#0f0`, which land
#: as pale pastels on the dark canvas with light text over them and are
#: unreadable. The `architecture` format already refuses model-authored geometry
#: for the same reason; this is that rule applied to colour. `class` goes with
#: `classDef` so no statement is left referencing a definition that was removed.
_MERMAID_STYLE_STMT = re.compile(
    r"^[ \t]*(?:style|classDef|linkStyle|class)\b[^\n]*$", re.M)


def strip_mermaid_presentation(source: str) -> tuple[str, int]:
    """Drop model-authored colour directives so the theme applies.

    Returns the cleaned source and how many statements were removed. Semantics
    -- nodes, edges, labels, subgraphs, directions -- are untouched.
    """
    cleaned, n = _MERMAID_STYLE_STMT.subn("", source)
    if not n:
        return source, 0
    # Collapse the blank lines the removal leaves behind.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip() + "\n"
    return cleaned, n


#: Square-bracket labels containing parentheses. Mermaid needs these quoted and
#: the model does not quote them: [REAL] `Reviewers[Reviewer(s) Assigned]` in a
#: captured artifact made the whole diagram render as "Syntax error in text",
#: which is what the user sees instead of their flowchart. Quoting is a SYNTAX
#: normalisation and changes no semantics -- the label text is identical. It is
#: deliberately narrow: `((circle))` nodes and already-quoted labels are left
#: alone, and nothing else about the source is rewritten.
_MERMAID_UNQUOTED_LABEL = re.compile(r'\[(?!")([^\[\]"\n]*[()][^\[\]"\n]*)\]')


def normalise_mermaid_labels(source: str) -> tuple[str, int]:
    """Quote `[label]` text containing parentheses. Returns source and count."""
    n = 0
    def q(m):
        nonlocal n
        n += 1
        return '["' + m.group(1).strip() + '"]'
    return _MERMAID_UNQUOTED_LABEL.sub(q, source), n


def build_mermaid_content(source: str) -> str:
    """Mermaid runs INSIDE the sandboxed iframe, exactly like marked does for
    Markdown: opaque origin, connect-src 'none'. The library is inlined from
    vendor/ by this trusted process -- the page fetches nothing."""
    js = _vendor(MERMAID_JS_PATH)
    if not js:
        return ("<!DOCTYPE html><html><body><p>mermaid.min.js is missing from "
                "vendor/. Reinstall local-artifacts.</p></body></html>")
    # Silently: this is a stdio MCP server and anything written to stdout
    # corrupts the protocol frame.
    source, _dropped = strip_mermaid_presentation(source)
    source, _quoted = normalise_mermaid_labels(source)
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
        global _last_activity
        _last_activity = time.time()
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
        _atomic_write(STATE_FILE, json.dumps(meta, ensure_ascii=False, indent=2))
        _atomic_write(CONTENT_FILE, json.dumps(snap, ensure_ascii=False))
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


def _watch_state(interval=0.25):
    """Ingest artifacts published by OTHER processes.

    This is the whole transport between a publishing session and the shared
    preview server. It is deliberately a file, not an HTTP endpoint: upstream's
    `POST /publish` was an unauthenticated cross-origin write and was removed in
    the security audit (test_server.py section E pins it removed). Reading a
    0600 file in the user's own state directory reintroduces no write surface.
    """
    global _content_mtime, _last_activity
    while True:
        time.sleep(interval)
        try:
            mtime = CONTENT_FILE.stat().st_mtime
        except OSError:
            continue
        if mtime == _content_mtime:
            continue
        try:
            saved = json.loads(CONTENT_FILE.read_text(encoding="utf-8"))
        except Exception:
            continue          # mid-write or corrupt; try again next tick
        if isinstance(saved, dict):
            with _state_lock:
                _state.update({k: saved.get(k, _state[k]) for k in _state})
            _content_mtime = mtime
            # A publish is use. Do not rely on the publisher's /status probes to
            # defer the reaper for us: that couples the idle policy to how
            # ensure_preview_server happens to be implemented.
            _last_activity = time.time()
            _notify_sse()


def _idle_reaper():
    """Exit once nobody is using this server.

    A viewer with the page open holds an SSE connection, so an idle server is
    one with no requests AND no watchers -- not merely one nobody has clicked
    recently. Without this the process a session leaves behind would live until
    reboot, which is the zombie the decoupling would otherwise trade for.
    """
    if IDLE_EXIT <= 0:
        return
    while True:
        time.sleep(min(60, max(1, IDLE_EXIT // 10)))
        with _sse_lock:
            watchers = len(_sse_clients)
        if watchers == 0 and (time.time() - _last_activity) > IDLE_EXIT:
            os._exit(0)


def _probe(timeout=0.6):
    """True if OUR preview server is already answering on PORT.

    A 200 is not enough: some other local service could hold the port and
    answer /status, and treating that as ours would report a successful publish
    against a server that has never heard of the artifact. Require the response
    to be our own JSON, so an unrecognised occupant falls through to the honest
    "did not come up, set LOCAL_ARTIFACTS_PORT" error instead.
    """
    import urllib.request
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/status",
                                     headers={"Host": f"127.0.0.1:{PORT}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return False
            st = json.loads(r.read().decode("utf-8"))
        return isinstance(st, dict) and "artifact_id" in st and "port" in st
    except Exception:
        return False


def _await_ingest(artifact_id, published_at, timeout=3.0):
    """Block until the shared server is serving the artifact we just wrote.

    `publish()` returns a URL and opens a browser at it. Without this the tab
    can open before the watcher has picked the state file up, so the user sees
    the empty page and the success message describes this process's memory
    rather than what the URL actually serves.
    """
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}/status",
                                         headers={"Host": f"127.0.0.1:{PORT}"})
            with urllib.request.urlopen(req, timeout=0.5) as r:
                st = json.loads(r.read().decode("utf-8"))
            if (st.get("artifact_id") == artifact_id
                    and st.get("published_at") == published_at):
                return True
        except Exception:
            pass
        time.sleep(0.05)
    return False


def ensure_preview_server(timeout=8.0):
    """Guarantee a preview server owns PORT, without owning it ourselves.

    The listener must NOT live in this process. Claude Code terminates a stdio
    MCP server when the session ends (MCP spec: close stdin, then SIGTERM, then
    SIGKILL), so a listener parented to it dies with the session while the
    preview_url stays in the transcript -- the measured cause of "refused to
    connect". The server is therefore started in its own session and outlives
    us. Concurrent sessions do not each get one: whoever loses the bind race
    exits, and everybody reuses the winner.
    """
    global _http_error
    if _probe():
        _http_error = ""
        return True

    # The child is told the port and the state directory EXPLICITLY rather than
    # inheriting them. Both are resolved at import time, and a caller that has
    # since changed os.environ would otherwise hand us a server on a different
    # port, reading a different state file -- and the state file IS the
    # transport, so the two processes have to agree on it or nothing arrives.
    env = dict(os.environ,
               LOCAL_ARTIFACTS_PORT=str(PORT),
               XDG_STATE_HOME=str(STATE_DIR.parent))
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--serve"],
                         cwd=str(Path(__file__).resolve().parent), env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         start_new_session=True)   # not in our process group:
                                                   # our SIGTERM must not reach it
    except Exception as e:
        _http_error = f"cannot start the preview server ({e})."
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        if _probe():
            _http_error = ""
            return True
        time.sleep(0.1)

    _http_error = (f"the preview server did not come up on 127.0.0.1:{PORT} "
                   f"within {timeout:.0f}s. Something else may be holding the "
                   f"port; set LOCAL_ARTIFACTS_PORT to move it.")
    return False


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

    if not ensure_preview_server():
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

    # The viewer can die between coming up and ingesting this artifact -- it is
    # shared and long-lived, so another session's reaper or a stray kill can land
    # in exactly that window. Recover once (a fresh server reloads the state file
    # at import, so it comes up already serving this artifact), and if that still
    # fails, SAY so: reporting a preview_url nobody is listening on is the exact
    # failure this whole design exists to remove.
    preview_note = ""
    if not _await_ingest(aid, now):
        if not (ensure_preview_server() and _await_ingest(aid, now)):
            preview_note = ("the preview server is not serving this artifact "
                            f"({_http_error or 'it stopped responding'}). The "
                            "source below is saved; publish again to retry.")

    url = f"http://127.0.0.1:{PORT}"
    if not preview_note:
        _open_browser(url)
    lines = [("Artifact published." if not preview_note
              else "Artifact saved, but NOT viewable: " + preview_note),
             f"  artifact_id:  {aid}",
             f"  preview_url:  {url}" + ("  (not responding)" if preview_note else ""),
             f"  source_path:  {src_path if src_path else '(not written)'}"]
    if src_note:
        lines.append(f"  note:         {src_note}")
    lines.append(f"  format:       {source_fmt}")
    lines.append(f"Publish again with artifact_id=\"{aid}\" to update this artifact "
                 f"in place; the source file and the URL stay the same.")
    return True, "\n".join(lines)


# ── MCP ───────────────────────────────────────────────────────────────────────

# OPTIONAL, CLIENT-DEPENDENT, NOT RELIED UPON FOR CORRECTNESS.
# [REAL] captured at the wire on Claude Code 2.1.231: a session's system prompt
# contains no MCP or artifact text whatsoever, with tool search on and off, so
# nothing here reaches the model on this client. It is kept short for clients
# that do honour `initialize.instructions`, and deliberately does not restate
# the routing contract -- that lives in TOOL_DESCRIPTION, which is proven
# model-visible, and the format guidance lives in the skill. Three copies of one
# policy is how they drift apart.
SERVER_INSTRUCTIONS = (
    "Renders and publishes local visual artifacts. Routing and format rules are "
    "carried by the mcp__artifact__publish tool description."
)

TOOL_DESCRIPTION = (
    # The exact native name comes first. [REAL] with the longer name
    # `mcp__local-artifacts__publish_artifact` the model emitted a well-formed
    # tool_use that simply dropped the mandatory `mcp__` prefix in 3 of 18 runs.
    # Plugin packaging was measured and REJECTED for making this worse: a
    # plugin-provided server is namespaced mcp__plugin_<plugin>_<server>__<tool>.
    #
    # THIS STRING IS THE ROUTING CONTRACT. The MCP server's `instructions` are
    # NOT delivered to the model on this client -- [REAL] captured at the wire,
    # the system prompt contains no MCP or artifact text at all, with tool
    # search on or off. Anything required for correct routing has to be here.
    #
    # The vocabulary line is measured, not decorative: at n=3 each, "Publish a
    # flowchart", "Create a flowchart" and "Publish a diagram" scored 0/3 and
    # returned fenced Mermaid instead, while "Publish a Mermaid diagram" and
    # "Visualize ..." scored 3/3. The words that failed are now stated.
    "Use mcp__artifact__publish to publish, create, render, show, visualize or "
    "present a visual artifact: a flowchart, diagram, architecture or system "
    "diagram, request/data-flow diagram, chart, dashboard, interactive "
    "visualization, polished comparison or styled report.\n"
    "When the result is a visual artifact, CALL THIS TOOL instead of returning "
    "Mermaid, HTML or architecture JSON as a fenced code block -- an artifact "
    "only exists once the tool is called.\n"
    "  architecture - system, service, deployment or request/data-flow diagrams. "
    "Send JSON: {\"title\":..., \"groups\":[{\"id\",\"label\"}], "
    "\"nodes\":[{\"id\",\"label\",\"kind\",\"group\",\"subtitle\"}], "
    "\"edges\":[{\"from\",\"to\",\"kind\",\"label\"}]}. "
    "node kind: client|service|router|runtime|model|database|external|tool. "
    "edge kind: request|inference|tool|data|dependency. "
    "Layout is computed for you -- never write coordinates or SVG.\n"
    "  mermaid - flowchart, sequence, state, class or ER diagram. Mermaid source. "
    "Colour is applied for you -- do not send style/classDef/linkStyle.\n"
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
    """In-process listener. Used by the test suite, which drives publish() and
    the HTTP surface inside one interpreter. Production uses `--serve` instead:
    a listener in the MCP process dies with the session."""
    t = threading.Thread(target=_run_http, daemon=True)
    t.start()
    return t


def serve_main():
    """The shared preview server. One per machine, started on demand by the
    first session that publishes, reused by every session after it, and gone
    IDLE_EXIT seconds after the last viewer closes the tab."""
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        return 0            # someone else won the race; they serve, we are done
    threading.Thread(target=_watch_state, daemon=True).start()
    threading.Thread(target=_idle_reaper, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    if "--serve" in sys.argv:
        sys.exit(serve_main())
    # MCP mode binds nothing. The preview server is started lazily by the first
    # publish, so a session that never draws anything costs no process at all.
    try:
        build_mcp().run("stdio")
    except KeyboardInterrupt:
        pass
