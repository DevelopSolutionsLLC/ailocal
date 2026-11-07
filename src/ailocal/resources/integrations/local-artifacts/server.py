#!/usr/bin/env python3
"""Render artifacts locally for API-key Claude Code sessions (no Artifacts).

Forked from xiagaohui/local-artifacts-for-claude-code @ ddb4796 (MIT), NOTICE.

    Claude Code --spawns--> this process   (one per session, stdio MCP)
                              `-- publish -> state file -> shared preview server

`--serve` is one per machine, started on demand and parented to no session: a
listener in the MCP process dies at session end (docs/decisions.md, 013).
`/` is the trusted viewer; model content is served at `/content`, framed with
`sandbox="allow-scripts"` (opaque origin) under `connect-src 'none'`.
"""
from __future__ import annotations

import html
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, NoReturn

import architecture
import mermaid_validate

PORT = int(os.environ.get("LOCAL_ARTIFACTS_PORT", "7891"))
MAX_SIZE = 16 * 1024 * 1024  # 16 MiB

# Idle seconds (no request, publish or viewer) before the server exits; 0=never.
# Bounds Mermaid RSS growth; a cold restart costs only ~170ms (see README).
IDLE_EXIT = int(os.environ.get("LOCAL_ARTIFACTS_IDLE_EXIT", "1800"))  # 30 min

# Tab reload delay after its EventSource drops; must match VIEWER_JS's
# setTimeout (test_autoopen.py). A self-healing tab needs no new window.
VIEWER_RELOAD_MS = 3000

# Tab reattach wait; must exceed VIEWER_RELOAD_MS (see _needs_presentation).
PRESENT_GRACE = float(os.environ.get("LOCAL_ARTIFACTS_PRESENT_GRACE",
                                     str(VIEWER_RELOAD_MS / 1000 + 1.5)))

# Own XDG state; never ~/.claude, which belongs to Claude Code.
_xdg_state = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
STATE_DIR = Path(_xdg_state) / "local-artifacts"
STATE_FILE = STATE_DIR / "state.json"
CONTENT_FILE = STATE_DIR / "current_content.json"
STATE_DIR.mkdir(parents=True, exist_ok=True)
try:
    STATE_DIR.chmod(0o700)
except OSError:
    pass

# Approved file_path root: override, then CLAUDE_PROJECT_DIR (launch dir, not
# git root; verified 2.1.231), then cwd. One session = one project.
def _resolve_root() -> Path:
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


def _vendor(path: Path) -> str:
    """Read a bundled library for inlining; generated content never fetches."""
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


MARKED_JS = _vendor(MARKED_JS_PATH)

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
# Last presentation outcome for `--diagnose` (the opener fails invisibly).
# In-memory: the shared state file would let sessions overwrite each other.
_last_present: str = "not-attempted"
_last_activity = time.time()      # serve mode: drives the idle reaper
_content_mtime = 0.0              # serve mode: last CONTENT_FILE we ingested


def _atomic_write(path: Path, text: str) -> None:
    """Write 0600 atomically; another process reads these files."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _load_persisted_state() -> None:
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

# Trusted viewer: SSE (connect-src 'self') and framing /content.
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

# Untrusted artifact: connect-src 'none' blocks all fetches, even no-cors,
# keeping generated JS off other loopback services.
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
  es.onerror = function(){ setTimeout(function(){ window.location.reload(); }, 3000); };
})();
</script>
</body></html>"""


def build_viewer(state: dict) -> str:
    return VIEWER_PAGE.format(
        title=html.escape(state.get("title") or "Artifact"),
        emoji=html.escape(state.get("emoji") or "\U0001F4C4"),
        published_at=html.escape(state.get("published_at") or ""),
    )


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
/* A rendered Mermaid fence is a figure, not a code block: it must not inherit
   the <pre> chrome, and it stays hidden until mermaid.run() replaces the
   source text with the SVG so the reader never sees the raw diagram source. */
pre.mermaid{{background:none;padding:0;text-align:center;visibility:hidden;}}
pre.mermaid[data-processed]{{visibility:visible;}}
pre.mermaid svg{{max-width:100%;height:auto;}}
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
{mermaid_tail}
</body>
</html>"""


#: Only for Markdown with a Mermaid fence (~3.4 MB). startOnLoad is false:
#: marked injects after load, so run() renders it.
MARKDOWN_MERMAID_TAIL = """<script>{mermaid_js}</script>
<script>
{mermaid_init}
mermaid.run({{ querySelector: 'pre.mermaid' }});
</script>"""

#: Mermaid fence in Markdown; tilde and indented closing fences are CommonMark.
_MARKDOWN_MERMAID_FENCE = re.compile(
    r"^(?P<indent>[ ]{0,3})(?P<fence>`{3,}|~{3,})[ \t]*mermaid[ \t]*\r?\n"
    r"(?P<body>.*?)"
    r"^[ ]{0,3}(?P=fence)[ \t]*$",
    re.M | re.S)


def build_markdown_content(content: str) -> str:
    """Build Markdown content under the same untrusted boundary as HTML.

    Raw HTML is not sanitized: the sandbox+CSP contains it. The '</' escape
    only keeps the payload inside its <script> string; it is not a control.
    """
    content, fences = _inline_markdown_mermaid(content)
    escaped = json.dumps(content).replace("</", "<\\/")
    tail = ""
    if fences:
        js = _vendor(MERMAID_JS_PATH)
        if js:
            tail = MARKDOWN_MERMAID_TAIL.format(
                mermaid_js=js, mermaid_init=_mermaid_init(start_on_load=False))
    return MARKDOWN_PAGE.format(marked_js=MARKED_JS, escaped_content=escaped,
                                mermaid_tail=tail)


def _inline_markdown_mermaid(content: str) -> tuple[str, int]:
    """Rewrite ```mermaid fences into `<pre class="mermaid">` HTML blocks.

    Here, not in marked, so fences share the `mermaid` format's normalisation.

    Returns:
        The rewritten Markdown and how many fences were converted.
    """
    n = 0

    def one(m: re.Match[str]) -> str:
        nonlocal n
        n += 1
        src, _ = strip_mermaid_presentation(m.group("body"))
        src, _ = normalise_mermaid_labels(src)
        return ('\n<pre class="mermaid">' + html.escape(src.strip())
                + "</pre>\n")

    return _MARKDOWN_MERMAID_FENCE.sub(one, content), n


def build_html_content(content: str) -> str:
    """Return model HTML as-is; the viewer and sandbox do the rest."""
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
{mermaid_init}
</script>
</body>
</html>"""


#: The one design-token → Mermaid themeVariables mapping, shared by both pages
#: (mermaid.js.org/config/theming.html).
MERMAID_INIT = """\
// Mermaid reads the SAME canonical tokens as the architecture renderer.
var _dark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
var T = _dark ? {dark_json} : {light_json};
mermaid.initialize({{
  startOnLoad: {start_on_load},
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
}});"""


def _mermaid_init(start_on_load: bool) -> str:
    """Format MERMAID_INIT against the live theme."""
    import architecture as _arch
    theme = _arch.THEME
    return MERMAID_INIT.format(
        start_on_load="true" if start_on_load else "false",
        light_json=json.dumps(theme["light"]),
        dark_json=json.dumps(theme["dark"]),
        sans_json=json.dumps(theme["typography"]["sans"]))


#: Presentation statements; model fills are unreadable on the theme. `class`
#: goes with `classDef` so nothing references a removed definition.
_MERMAID_STYLE_STMT = re.compile(
    r"^[ \t]*(?:style|classDef|linkStyle|class)\b[^\n]*$", re.M)

#: Same minus `class`: in a classDiagram `class Foo {` declares the class.
_MERMAID_STYLE_STMT_CLASSDIAGRAM = re.compile(
    r"^[ \t]*(?:style|classDef|linkStyle)\b[^\n]*$", re.M)

#: A leading `---`-fenced frontmatter block may precede the diagram keyword.
_MERMAID_FRONTMATTER = re.compile(r"\A\s*---.*?^---[ \t]*$", re.M | re.S)


def _is_class_diagram(source: str) -> bool:
    body = _MERMAID_FRONTMATTER.sub("", source, count=1)
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("%%"):
            continue
        return line.startswith("classDiagram")
    return False


def strip_mermaid_presentation(source: str) -> tuple[str, int]:
    """Drop model-authored colour directives so the theme applies.

    Returns:
        The cleaned source and how many statements were removed.
    """
    pattern = (_MERMAID_STYLE_STMT_CLASSDIAGRAM if _is_class_diagram(source)
               else _MERMAID_STYLE_STMT)
    cleaned, n = pattern.subn("", source)
    if not n:
        return source, 0
    # Collapse the blank lines the removal leaves behind.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip() + "\n"
    return cleaned, n


#: `[labels]` with parentheses, which Mermaid needs quoted; `((circle))` and
#: quoted labels are left alone.
_MERMAID_UNQUOTED_LABEL = re.compile(r'\[(?!")([^\[\]"\n]*[()][^\[\]"\n]*)\]')


def normalise_mermaid_labels(source: str) -> tuple[str, int]:
    """Quote `[label]` text containing parentheses; return source and count."""
    n = 0
    def q(m: re.Match[str]) -> str:
        nonlocal n
        n += 1
        return '["' + m.group(1).strip() + '"]'
    return _MERMAID_UNQUOTED_LABEL.sub(q, source), n


def build_mermaid_content(source: str) -> str:
    """Build a Mermaid page for the sandboxed frame, library inlined."""
    js = _vendor(MERMAID_JS_PATH)
    if not js:
        return ("<!DOCTYPE html><html><body><p>mermaid.min.js is missing from "
                "vendor/. Reinstall local-artifacts.</p></body></html>")
    # Silently: stdout is the MCP protocol channel.
    source, _dropped = strip_mermaid_presentation(source)
    source, _quoted = normalise_mermaid_labels(source)
    import architecture as _arch
    theme = _arch.THEME
    return MERMAID_PAGE.format(
        source=html.escape(source), mermaid_js=js,
        mermaid_init=_mermaid_init(start_on_load=True),
        canvas_light=theme["light"]["canvas"], canvas_dark=theme["dark"]["canvas"],
        surface_light=theme["light"]["surface"], surface_dark=theme["dark"]["surface"],
        ink_light=theme["light"]["ink"], ink_dark=theme["dark"]["ink"],
        border_light=theme["light"]["border"], border_dark=theme["dark"]["border"])


#: Grammar gate on the normalised (served) source; UNAVAILABLE refuses too.
def _mermaid_verdict(source: str) -> mermaid_validate.Result:
    normalised, _ = strip_mermaid_presentation(source)
    normalised, _ = normalise_mermaid_labels(normalised)
    return mermaid_validate.parse(normalised)


def _mermaid_refusal(result: mermaid_validate.Result, where: str) -> str:
    if result.state == mermaid_validate.INVALID:
        return (f"Publish failed: {where} does not parse, so the artifact was "
                "NOT published -- it would have rendered as \"Syntax error in "
                "text\".\n" + result.detail.strip()
                + "\nFix that line and publish again.")
    return (f"Publish failed: {where} could NOT be checked against the Mermaid "
            "grammar, so the artifact was not published -- publishing it would "
            "mean claiming a diagram is valid without having verified it.\n"
            + result.detail.strip()
            + "\nInstall Google Chrome or Chromium to enable diagram "
              "validation, or publish this as format=\"html\" with the diagram "
              "drawn as inline SVG.")


def check_mermaid(text: str, fmt: str) -> str | None:
    """Return a model-facing refusal, or None; Markdown checks each fence."""
    if fmt == "mermaid":
        result = _mermaid_verdict(text)
        if result.state == mermaid_validate.VALID:
            return None
        return _mermaid_refusal(result, "the Mermaid source")
    if fmt == "markdown":
        for i, m in enumerate(_MARKDOWN_MERMAID_FENCE.finditer(text), 1):
            result = _mermaid_verdict(m.group("body"))
            if result.state != mermaid_validate.VALID:
                return _mermaid_refusal(
                    result, f"the Mermaid block #{i} in this Markdown")
    return None


def build_content_page(state: dict) -> str:
    fmt = state.get("format")
    if fmt == "markdown":
        return build_markdown_content(state.get("content", ""))
    if fmt == "mermaid":
        return build_mermaid_content(state.get("content", ""))
    # `architecture` was rendered to static SVG HTML at publish time.
    return build_html_content(state.get("content", ""))


def _notify_sse() -> None:
    ts = str(int(time.time()))
    with _sse_lock:
        clients = list(_sse_clients)
    for q in clients:
        try:
            q.put_nowait(ts)
        except Exception:
            pass


def _host_ok(host: str) -> bool:
    """Return whether Host is one we emit; blocks DNS-rebinding reads."""
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

    def log_message(self, fmt: str, *args: object) -> None:
        pass  # MCP speaks stdio; stdout must stay clean

    # No do_POST: upstream's unauthenticated cross-origin publish is removed.

    def do_GET(self) -> None:
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
            # Same count, list and lock as the reaper: the two cannot disagree.
            with _sse_lock:
                s["watchers"] = len(_sse_clients)
            self._send(200, "application/json; charset=utf-8",
                       json.dumps(s, ensure_ascii=False).encode("utf-8"))

        else:
            self._send(404, "text/plain; charset=utf-8", b"Not found")

    def _sse(self) -> None:
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

    def _send(self, code: int, ct: str, body: bytes, csp: str | None = None,
              extra: Mapping[str, str] | None = None) -> None:
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


def _publish(title: str, emoji: str, content: str, fmt: str, published_at: str,
             artifact_id: str = "") -> None:
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


def _run_http() -> None:
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


def _watch_state(interval: float = 0.25) -> NoReturn:
    """Ingest artifacts other processes publish via the 0600 state file.

    A file, not an HTTP endpoint, so there is no write surface (test_server E).
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
            # A publish is use; do not rely on the publisher's /status probes.
            _last_activity = time.time()
            _notify_sse()


def _idle_reaper() -> None:
    """Exit once there are no requests and no SSE watchers."""
    if IDLE_EXIT <= 0:
        return
    while True:
        time.sleep(min(60, max(1, IDLE_EXIT // 10)))
        with _sse_lock:
            watchers = len(_sse_clients)
        if watchers == 0 and (time.time() - _last_activity) > IDLE_EXIT:
            os._exit(0)


def _probe(timeout: float = 0.6) -> bool:
    """Return whether OUR server answers on PORT: our JSON, not just a 200."""
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


def _await_ingest(artifact_id: str, published_at: str, timeout: float = 3.0) -> bool:
    """Block until the shared server serves the artifact, before a tab opens."""
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


def ensure_preview_server(timeout: float = 8.0) -> bool:
    """Ensure a preview server owns PORT, in its own session, not this process.

    The MCP process dies at session end; bind-race losers exit and reuse the
    winner.
    """
    global _http_error
    if _probe():
        _http_error = ""
        return True

    # Pass import-time port and state dir explicitly: the state file is the
    # transport, so both processes must agree on it.
    env = dict(os.environ,
               LOCAL_ARTIFACTS_PORT=str(PORT),
               XDG_STATE_HOME=str(STATE_DIR.parent))
    try:
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--serve"],
                         cwd=str(Path(__file__).resolve().parent), env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         start_new_session=True)   # immune to our SIGTERM
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


def auto_open_enabled() -> tuple[bool, str]:
    """Return whether publish may open a window, and which switch decided.

    ailocal's variable, then CLAUDE_CODE_ARTIFACT_AUTO_OPEN, then enabled; any
    value but "0" enables, as upstream.

    Returns:
        (enabled, source); `source` is the deciding variable or "default".
    """
    for var in ("LOCAL_ARTIFACTS_AUTO_OPEN", "CLAUDE_CODE_ARTIFACT_AUTO_OPEN"):
        val = os.environ.get(var)
        if val is not None:
            return val != "0", var
    return True, "default"


def _watchers(timeout: float = 0.6) -> int | None:
    """Return the live viewer count from /status, or None if no answer."""
    import urllib.request
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/status",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(json.loads(r.read().decode("utf-8")).get("watchers", 0))
    except Exception:
        return None


def _needs_presentation(grace: float | None = None) -> bool:
    """Return True if no tab watches now or reattaches within PRESENT_GRACE.

    Sampled over time: a healing tab reads zero watchers for VIEWER_RELOAD_MS,
    and a session flag outlives the viewer it described.
    """
    if _watchers():
        return False                      # someone is already watching
    deadline = time.time() + (PRESENT_GRACE if grace is None else grace)
    while time.time() < deadline:
        time.sleep(0.25)
        if _watchers():
            return False                  # a healing tab came back
    return True


def _open_browser(url: str) -> str:
    """Hand `url` to the OS default browser, never the validation Chrome.

    Returns:
        One of "opened", "disabled:<var>", "watched", "failed:<reason>" or
        "unsupported-platform". The caller records it; nothing here logs.
    """
    enabled, source = auto_open_enabled()
    if not enabled:
        return f"disabled:{source}"
    if not _needs_presentation():
        return "watched"
    if sys.platform != "darwin":          # the product runs on macOS only (AGENTS.md)
        return "unsupported-platform"
    try:
        subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "opened"
    except Exception as e:
        return f"failed:{type(e).__name__}"


class PublishError(Exception):
    pass


# Subresources the CSP refuses; <a href> is navigation, so not matched.
_SUBRESOURCE_PATTERNS = (
    re.compile(r"""<script[^>]*\ssrc\s*=\s*["']?\s*(https?:)?//([^"'\s>]+)""", re.I),
    re.compile(r"""<link[^>]*\shref\s*=\s*["']?\s*(https?:)?//([^"'\s>]+)""", re.I),
    re.compile(r"""<(?:img|iframe|video|audio|source|embed|object)[^>]*\s(?:src|data)\s*=\s*["']?\s*(https?:)?//([^"'\s>]+)""", re.I),
    re.compile(r"""@import\s+(?:url\()?["']?\s*(https?:)?//([^"'\s)]+)""", re.I),
    re.compile(r"""url\(\s*["']?\s*(https?:)?//([^"'\s)]+)""", re.I),
)


def external_subresources(content: str | None) -> list[str]:
    """Return remote subresources the CSP blocks, so a blank page is refused."""
    found = []
    for pat in _SUBRESOURCE_PATTERNS:
        for m in pat.finditer(content or ""):
            host = m.group(2).split("/")[0]
            if host and host not in found:
                found.append(host)
    return found


def resolve_input(content: str, file_path: str
                  ) -> tuple[None, str] | tuple[tuple[str, str | None], None]:
    """Return (content, format_hint).

    Raises:
        PublishError: With a plain reason.
    """
    if file_path and not content:
        try:
            p = Path(file_path).expanduser()
            if not p.is_absolute():
                p = APPROVED_ROOT / p
            # Follow symlinks so the containment check catches escaping links.
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


def slugify(text: str | None, fallback: str = "artifact") -> str:
    """Return a non-empty capped [a-z0-9-] id; cannot escape or be a dotfile."""
    out = []
    for ch in (text or "").lower():
        if ch.isalnum() and ch.isascii():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    slug = "".join(out).strip("-")[:60]
    return slug or fallback


def _source_path(artifact_id: str, fmt: str) -> Path:
    return ARTIFACT_DIR / (artifact_id + SOURCE_EXT.get(fmt, ".txt"))


def persist_source(artifact_id: str, fmt: str, source: str) -> tuple[Path | None, str | None]:
    """Write the canonical source, not rendered output (that is cache).

    Never raises: an unwritable project still gets a preview, and says so.

    Returns:
        (path, note).
    """
    try:
        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        path = _source_path(artifact_id, fmt)
        path.write_text(source, encoding="utf-8")
        return path, None
    except Exception as e:
        return None, f"could not write the source file ({e})"


def resolve_artifact_id(title: str, artifact_id: str | None) -> str:
    """Map one title to one artifact id deterministically, across sessions.

    Same-titled artifacts share a file; pass distinct `artifact_id`s to split.
    """
    if artifact_id and artifact_id.strip():
        return slugify(artifact_id)
    return slugify(title, fallback="artifact")


def publish(title: str, content: str = "", file_path: str = "", fmt: str = "html",
            emoji: str = "\U0001F4C4", artifact_id: str = "") -> tuple[bool, str]:
    """Publish an artifact; return (ok, message)."""
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
        # Model supplies meaning, ELK geometry, tokens presentation; no script.
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

    # Gate before persisting: a rejection leaves no trace and tells the model.
    mermaid_err = check_mermaid(text, fmt)
    if mermaid_err:
        return False, mermaid_err

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

    # Persist automatically: models always use content=, never file_path.
    src_path, src_note = persist_source(aid, source_fmt, source_text)

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    _publish(title=title, emoji=emoji, content=text, fmt=fmt, published_at=now,
             artifact_id=aid)

    # The shared viewer can die before ingest; recover once (a fresh server
    # loads the state file), else say so rather than report a dead URL.
    preview_note = ""
    if not _await_ingest(aid, now):
        if not (ensure_preview_server() and _await_ingest(aid, now)):
            preview_note = ("the preview server is not serving this artifact "
                            f"({_http_error or 'it stopped responding'}). The "
                            "source below is saved; publish again to retry.")

    url = f"http://127.0.0.1:{PORT}"
    global _last_present
    if not preview_note:
        _last_present = _open_browser(url)
    else:
        _last_present = "not-attempted:publish-not-viewable"
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


# Not relied on: Claude Code 2.1.231 did not show it to the model. Routing
# lives in TOOL_DESCRIPTION, format guidance in the skill.
SERVER_INSTRUCTIONS = (
    "Renders and publishes local visual artifacts. Routing and format rules are "
    "carried by the mcp__artifact__publish tool description."
)

TOOL_DESCRIPTION = (
    # The routing contract: server instructions never reach the model. Short
    # exact name first (longer ones lost `mcp__`); verbs listed were measured.
    "Use mcp__artifact__publish to publish, create, draw, sketch, make, render, "
    "show, visualize or present a visual artifact: a flowchart, diagram, "
    "architecture or system diagram, class/relationship diagram, "
    "request/data-flow diagram, chart, dashboard, interactive "
    "visualization, polished comparison or styled report.\n"
    "When the result is a visual artifact, CALL THIS TOOL instead of returning "
    "Mermaid, HTML or architecture JSON as a fenced code block -- an artifact "
    "only exists once the tool is called.\n"
    # State the rule, not just words, so it generalises to unlisted synonyms.
    "A visual verb (draw, sketch, make, render, show, diagram) asking for a "
    "picture of code, classes, objects, services or a process MEANS call this "
    "tool -- the user does not have to say \"publish\". Only return the source "
    "in your reply instead when they explicitly ask for the source, the raw "
    "Mermaid, or text.\n"
    # Form from the question: models defaulted to classDiagram for architecture.
    "Pick the format from what was asked: architecture/topology -> architecture; "
    "class or type relationships -> mermaid classDiagram; runtime interaction -> "
    "mermaid sequenceDiagram; process/control flow -> mermaid flowchart.\n"
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


def build_mcp() -> Any:
    """Build the MCP server; mcp 2.x derives the schema from annotations."""
    from mcp.server.mcpserver import MCPServer
    from pydantic import Field
    from typing import Annotated, Literal

    # Visible while ToolSearch defers schemas; truncated at 2 KB, so lead first.
    server = MCPServer(name="local-artifacts", instructions=SERVER_INSTRUCTIONS)

    # `anthropic/alwaysLoad`: ToolSearch deferred this tool by default.
    # searchHint is what ToolSearch matches if it is deferred anyway.
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
            description="Stable id for this artifact, e.g. 'release-notes'. "
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


def start_http_thread() -> threading.Thread:
    """Start an in-process listener for tests; production uses `--serve`."""
    t = threading.Thread(target=_run_http, daemon=True)
    t.start()
    return t


def serve_main() -> int:
    """Run the shared preview server until IDLE_EXIT after the last viewer."""
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


def diagnose_main() -> int:
    """Print why presentation would or would not open a window right now.

    Reads publish()'s signals; prints no content or env values, only names.
    """
    enabled, source = auto_open_enabled()
    watchers = _watchers()
    print(f"auto-open:      {'enabled' if enabled else 'disabled'} (decided by {source})")
    print(f"port:           127.0.0.1:{PORT}")
    print(f"viewer:         {'up' if watchers is not None else 'not running'}")
    print(f"watchers:       {'unknown' if watchers is None else watchers}")
    print(f"present grace:  {PRESENT_GRACE:.1f}s (tab self-heal {VIEWER_RELOAD_MS}ms)")
    print(f"last attempt:   {_last_present}   (this process only)")
    if enabled and not watchers:
        print("\nA publish now WOULD open a window: nothing is watching the viewer.")
    elif enabled:
        print("\nA publish now would NOT open a window: a tab is already watching.")
    else:
        print(f"\nA publish now would NOT open a window: {source}=0.")
    return 0


if __name__ == "__main__":
    if "--diagnose" in sys.argv:
        sys.exit(diagnose_main())
    if "--serve" in sys.argv:
        sys.exit(serve_main())
    # MCP mode binds nothing; the first publish starts the preview server.
    try:
        build_mcp().run("stdio")
    except KeyboardInterrupt:
        pass
