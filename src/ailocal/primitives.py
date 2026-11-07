"""Shared primitives, one implementation each so commands cannot disagree."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable


class Unreachable(OSError):
    """A bounded transport call got no HTTP answer (DNS, TLS, refused, timeout).

    An OSError, so generic transport handlers keep catching it.
    """


def sha256_file(path: Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch(url: str, *, payload: dict | None = None, headers: dict | None = None,
          method: str | None = None, timeout: float = 20) -> tuple[int, bytes]:
    """Return `(status, body)` for one bounded request.

    An HTTP error status is an answer and is returned; only a transport
    failure raises Unreachable.
    """
    data = json.dumps(payload).encode() if payload is not None else None
    hdrs = dict(headers or {})
    if data is not None:
        hdrs.setdefault("Content-Type", "application/json")
    request = (url if data is None and not hdrs and method is None
               else urllib.request.Request(url, data=data, headers=hdrs, method=method))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return getattr(response, "status", 200), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        raise Unreachable(str(exc) or type(exc).__name__) from exc


def wait_until(predicate: Callable[[], bool], attempts: int, delay: float,
               on_wait: Callable[[int], None] | None = None) -> bool:
    """Poll `predicate` `attempts` times, `delay` s apart, plus a final check.

    `on_wait(attempt)` runs before each sleep.
    """
    for attempt in range(attempts):
        if predicate():
            return True
        if on_wait:
            on_wait(attempt)
        time.sleep(delay)
    return predicate()
