"""Gate Mermaid artifacts on the real Mermaid grammar before publish.

Normalisers have no opinion on grammar; a corrupted diagram once shipped. Three
states, since "could not tell" must never read as valid:

    VALID        the grammar accepted it            -> publish
    INVALID      the grammar rejected it            -> refuse, quote the parser
    UNAVAILABLE  no verdict was obtained at all     -> refuse, say why

Headless Chrome loads vendor/mermaid.min.js by <script src>, so the verdict is
the viewer's grammar; under Node DOMPurify lacks a DOM, and a regex pre-check
is a weaker second grammar. Chrome uses a throwaway profile and is killed once
the verdict marker prints: with a profile `--dump-dom` never exits.
"""
from __future__ import annotations

import html
import json
import os
import re
import select
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

VENDOR = Path(__file__).resolve().parent / "vendor"
MERMAID_JS = VENDOR / "mermaid.min.js"

#: Wall-clock ceiling for one verdict; a real one lands at ~1.8s.
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
    """Hold a verdict: `state` VALID / INVALID / UNAVAILABLE, with `detail`."""

    __slots__ = ("state", "detail")

    def __init__(self, state: str, detail: str = "") -> None:
        self.state, self.detail = state, detail

    def __repr__(self) -> str:
        return f"Result({self.state!r}, {self.detail!r})"


def find_chrome() -> str | None:
    """Return the absolute path of a Chrome/Chromium binary, or None."""
    for c in _CHROME_CANDIDATES:
        if os.path.isabs(c):
            if os.path.exists(c):
                return c
        else:
            found = shutil.which(c)
            if found:
                return found
    return None


def available() -> bool:
    """Return whether a real verdict is obtainable on this machine."""
    return bool(find_chrome()) and MERMAID_JS.is_file()


def _read_until_verdict(proc: subprocess.Popen[bytes], deadline_at: float) -> str:
    """Return stdout read until a marker or timeout; Chrome never exits."""
    import time
    buf = ""
    while time.monotonic() < deadline_at:
        if proc.poll() is not None:
            try:
                buf += (proc.stdout.read() or b"").decode("utf-8", "replace")
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


def _run_chrome(chrome: str, page_uri: str,
                timeout: float) -> tuple[str | None, str | None]:
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


def parse(source: str,
          runner: Callable[[str], tuple[str | None, str | None]] | None = None,
          timeout: float = TIMEOUT) -> Result:
    """Return a Result for `source`, the post-normalisation rendered text.

    Args:
        source: Text as it will render, i.e. after normalisation.
        runner: Test seam, runner(page_uri) -> (dom or None, error or None).
        timeout: Wall-clock ceiling in seconds.
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
