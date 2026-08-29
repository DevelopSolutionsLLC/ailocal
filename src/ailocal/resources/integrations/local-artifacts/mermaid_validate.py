"""mermaid_validate.py — authoritative syntax gate for Mermaid artifacts.

    Mermaid source  ->  real Mermaid grammar  ->  publish, or a parser error

[REAL] the failure this exists for. A local model published a classDiagram whose
line 37 read `+render_nodes(ctx: Episode%} -> list[dict]` -- token-level
corruption, not a style slip. `strip_mermaid_presentation` and
`normalise_mermaid_labels` both returned ZERO changes: they are normalisers, and
a normaliser has no opinion about grammar. The broken source was written to
.artifacts/ and served, and the first thing that noticed was a human looking at
a bomb icon. The model had to be TOLD by the user that its own output did not
parse.

THREE STATES, NOT TWO. This sits on a correctness boundary, so "I could not
tell" must never be reported as "valid":

    VALID        the grammar accepted it            -> publish
    INVALID      the grammar rejected it            -> refuse, quote the parser
    UNAVAILABLE  no verdict was obtained at all     -> refuse, say why

UNAVAILABLE covers a missing browser, a launch failure, a timeout and a run that
produced no verdict. Treating it as VALID is exactly the original defect --
an unvalidated diagram reported as a successful publish -- so it is refused
instead. Only the Mermaid path is affected: html, markdown-without-diagrams and
architecture artifacts never reach this module.

Why a browser and not Node. The verdict has to come from the SAME grammar the
viewer runs, or it is a second opinion pretending to be the first. Two cheaper
routes were measured and rejected:

  * a regex/bracket-counting pre-check -- that is a second, weaker Mermaid
    grammar, and it would produce exactly the false confidence this module
    exists to remove;
  * `vendor/mermaid.min.js` under Node. It LOADS (via vm.runInThisContext -- the
    bundle is a classic script, so a CJS import leaves its top-level `var` off
    globalThis) and `mermaid.parse` is reachable, but it dies in
    `sanitizeText -> DOMPurify.addHook`: DOMPurify drops addHook when there is
    no DOM. A real verdict needs a real DOM, and jsdom is a large new dependency
    for a guard. Stubbing sanitisation would validate with a mermaid that is not
    the mermaid that renders.

So: headless Chrome over vendor/mermaid.min.js, which is REFERENCED by <script
src> rather than copied -- the validator cannot drift to a different Mermaid
than the page.

PROCESS LIFETIME. Chrome runs in a throwaway --user-data-dir and is killed by
us, never left to exit on its own. [REAL] with a profile, `--dump-dom` prints
the finished DOM at ~2.0s and then never exits; without one it exits cleanly but
runs against the user's real profile. Neither is acceptable on its own, so this
reads stdout until the verdict marker appears, then terminates Chrome and
removes the profile. A validator must not depend on, mutate or lock the state of
the browser a person is using.
"""
import html
import json
import os
import re
import select
import shutil
import subprocess
import tempfile
from pathlib import Path

VENDOR = Path(__file__).resolve().parent / "vendor"
MERMAID_JS = VENDOR / "mermaid.min.js"

#: Wall-clock ceiling for one verdict. [REAL] a real verdict lands at ~1.8s.
TIMEOUT = 30

VALID = "valid"
INVALID = "invalid"
UNAVAILABLE = "unavailable"

_CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
)

_OK = "__MERMAID_OK__"
_ERR = "__MERMAID_ERR__"

_PAGE = """<!doctype html><meta charset="utf-8"><body><pre id="o"></pre>
<script src="{lib}"></script>
<script>
try {{
  mermaid.initialize({{ startOnLoad: false }});
  mermaid.parse({source}).then(function () {{
    document.getElementById('o').textContent = {ok};
  }}).catch(function (e) {{
    document.getElementById('o').textContent =
      {err} + ((e && e.message) ? e.message : String(e));
  }});
}} catch (e) {{
  document.getElementById('o').textContent =
    {err} + ((e && e.message) ? e.message : String(e));
}}
</script>"""


class Result:
    """A verdict. `state` is VALID / INVALID / UNAVAILABLE; `detail` explains."""

    __slots__ = ("state", "detail")

    def __init__(self, state, detail=""):
        self.state, self.detail = state, detail

    def __repr__(self):
        return f"Result({self.state!r}, {self.detail!r})"


def find_chrome():
    """Absolute path to a Chrome/Chromium binary, or None."""
    for c in _CHROME_CANDIDATES:
        if os.path.isabs(c):
            if os.path.exists(c):
                return c
        else:
            found = shutil.which(c)
            if found:
                return found
    return None


def available():
    """True when a real verdict is obtainable on this machine."""
    return bool(find_chrome()) and MERMAID_JS.is_file()


def _read_until_verdict(proc, deadline_at):
    """Stream stdout until a marker appears or time runs out. Returns the buffer.

    Chrome is not expected to exit -- see the module docstring -- so waiting on
    it would always hit the timeout. The marker IS the completion signal.
    """
    import time
    buf = ""
    while time.monotonic() < deadline_at:
        if proc.poll() is not None:
            try:
                buf += proc.stdout.read() or ""
            except Exception:
                pass
            return buf
        ready, _, _ = select.select([proc.stdout], [], [], 0.25)
        if ready:
            try:
                chunk = os.read(proc.stdout.fileno(), 65536)
            except OSError:
                return buf
            if not chunk:
                return buf
            buf += chunk.decode("utf-8", "replace")
            if _OK in buf or _ERR in buf:
                return buf
    return buf


def _run_chrome(chrome, page_uri, timeout):
    """Launch, capture the verdict, then kill Chrome and drop its profile."""
    import time
    profile = tempfile.mkdtemp(prefix="mermaid-validate-profile-")
    proc = None
    try:
        cmd = [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
               "--no-first-run", "--no-default-browser-check",
               "--disable-features=Translate",
               "--allow-file-access-from-files",
               f"--user-data-dir={profile}",
               "--virtual-time-budget=15000", "--dump-dom", page_uri]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=False)
        except OSError as e:
            return None, f"could not start the browser ({e})"
        return _read_until_verdict(proc, time.monotonic() + timeout), None
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
        if proc is not None and proc.stdout:
            try:
                proc.stdout.close()
            except Exception:
                pass
        shutil.rmtree(profile, ignore_errors=True)


def parse(source, runner=None, timeout=TIMEOUT):
    """Return a Result for `source`.

    `source` must be the text that will actually be rendered -- i.e. AFTER
    normalisation -- so the verdict describes the page the user will see.

    `runner` exists so the state machine can be tested without a browser. It is
    called as runner(page_uri) and returns (dom_text_or_None, error_or_None);
    the real one is _run_chrome. Nothing in production passes it.
    """
    if not MERMAID_JS.is_file():
        return Result(UNAVAILABLE,
                      "mermaid.min.js is missing from vendor/. Reinstall "
                      "local-artifacts.")
    chrome = None
    if runner is None:
        chrome = find_chrome()
        if not chrome:
            return Result(UNAVAILABLE,
                          "no Chrome or Chromium was found on this machine, so "
                          "the diagram could not be checked against the Mermaid "
                          "grammar.")

    page_dir = tempfile.mkdtemp(prefix="mermaid-validate-")
    try:
        page = Path(page_dir) / "v.html"
        page.write_text(_PAGE.format(
            lib=MERMAID_JS.as_uri(), source=json.dumps(source),
            ok=json.dumps(_OK), err=json.dumps(_ERR)), encoding="utf-8")
        if runner is not None:
            out, err = runner(page.as_uri())
        else:
            out, err = _run_chrome(chrome, page.as_uri(), timeout)
    finally:
        shutil.rmtree(page_dir, ignore_errors=True)

    if err:
        return Result(UNAVAILABLE, err)
    if not out:
        return Result(UNAVAILABLE,
                      "the Mermaid parser produced no verdict within "
                      f"{timeout}s.")
    m = re.search(r'<pre id="o">(.*?)</pre>', out, re.S)
    if not m:
        # The marker can also arrive mid-stream before the DOM is complete.
        raw = html.unescape(out)
        if _ERR in raw:
            m = None
        elif _OK in raw:
            return Result(VALID)
        else:
            return Result(UNAVAILABLE,
                          "the Mermaid parser produced no verdict within "
                          f"{timeout}s.")
        detail = raw.split(_ERR, 1)[1]
        return Result(INVALID, detail.split("</pre>")[0].strip())

    verdict = html.unescape(m.group(1))
    if verdict.startswith(_ERR):
        return Result(INVALID,
                      verdict[len(_ERR):].strip() or "Mermaid rejected the diagram.")
    if verdict.startswith(_OK):
        return Result(VALID)
    return Result(UNAVAILABLE,
                  f"the Mermaid parser produced no verdict within {timeout}s.")
