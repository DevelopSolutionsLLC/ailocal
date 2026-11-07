#!/usr/bin/env python3
"""Own each environment variable and where it lives.

    ~/.config/ailocal/.env.local   user-owned     provider keys, overrides
    ~/.local/state/ailocal/env     ailocal-owned  generated secrets

Both 0600; the user file wins. `value()` is the one reader, so precedence
lives in one place. Defaults belong in code, not in either file.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import policy

#: From os.urandom; never rotated on upgrade: client cfgs embed the value.
GENERATED = ("LITELLM_MASTER_KEY", "SEARXNG_SECRET")

#: Always in the user file, even empty: empty means "not using this provider".
USER_ALWAYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "BRAVE_API", "ENABLE_CLOUD")

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


# the four paths

def generated_file() -> Path:
    return policy.state_root() / "env"


def user_file() -> Path:
    return policy.config_root() / ".env.local"


# reading

def parse(path: Path) -> dict[str, str]:
    """Return name → value, dropping comments, blanks and one quote layer."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _LINE.match(line)
        if not m:
            continue
        v = m.group(2).strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[m.group(1)] = v
    return out


def value(name: str) -> str:
    """Return the user override, else the generated value, else empty."""
    user = parse(user_file())
    if name in user:
        return user[name]
    gen = parse(generated_file())
    return gen.get(name, "")


def env_files() -> list[Path]:
    """Return the files a consumer must load, in order, later winning."""
    return [p for p in (generated_file(), user_file()) if p.is_file()]


# writing

def _write(path: Path, header: str, values: dict[str, str]) -> None:
    """Write atomically, 0600, parent 0700; never partially visible."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass                                   # a shared parent we do not own
    body = header + "".join(f"{k}={v}\n" for k, v in values.items())
    tmp = path.with_name(path.name + ".incoming")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    path.chmod(0o600)


_GENERATED_HEADER = """\
# ailocal — GENERATED STATE. Do not edit; do not commit; do not copy.
#
# Secrets ailocal made for this machine. They are regenerated only when absent,
# never on upgrade: the master key is embedded in every generated client config,
# so rotating it here would break all of them at once and say nothing.
#
# Your own keys and overrides go in the USER file, which wins over this one:
#   {user}
"""

_USER_HEADER = """\
# ailocal — YOURS. ailocal reads this file and never rewrites your values.
#
# It is loaded AFTER generated state, so anything here overrides it.
# Permissions are 0600; do not commit it.
#
# Provider keys are listed even when empty — an empty key means "not using this
# provider", and ailocal treats it as that rather than as a broken install.
# ENABLE_CLOUD is currently documentation only: no code reads it. Setting it
# true additionally requires uncommenting the matching model block in the
# generated litellm config.
#
# Overrides below this point exist only where you differ from the default.
"""


def ensure_generated() -> dict[str, str]:
    """Create the generated file if absent, else leave it; return its values."""
    path = generated_file()
    current = parse(path)
    if all(current.get(k) for k in GENERATED):
        return current
    made = dict(current)
    if not made.get("LITELLM_MASTER_KEY"):
        made["LITELLM_MASTER_KEY"] = "sk-" + os.urandom(24).hex()
    if not made.get("SEARXNG_SECRET"):
        # SearXNG refuses to start without a real secret.
        made["SEARXNG_SECRET"] = os.urandom(32).hex()
    _write(path, _GENERATED_HEADER.format(user=user_file()),
           {k: made[k] for k in GENERATED})
    return parse(path)


def ensure_user() -> dict[str, str]:
    """Create the user file if absent, provider keys empty; never rewrite it."""
    path = user_file()
    if path.is_file():
        path.chmod(0o600)
        return parse(path)
    _write(path, _USER_HEADER, {k: "false" if k == "ENABLE_CLOUD" else ""
                                for k in USER_ALWAYS})
    return parse(path)


#: Proxy listen address; Ollama and SearXNG never follow (no auth).
BIND_LOCAL = "127.0.0.1"
BIND_LAN = "0.0.0.0"


def bind() -> str:
    """Return the proxy bind address; the one reader of AILOCAL_BIND."""
    return value("AILOCAL_BIND") or BIND_LOCAL


def set_bind(address: str) -> None:
    """Persist the proxy bind address; `ailocal start` applies it."""
    _set_user_value("AILOCAL_BIND", address)


def set_ollama_patch(selected: str) -> None:
    """Persist an explicit installer choice, keeping other overrides."""
    _set_user_value("AILOCAL_OLLAMA_PATCH", selected)


def _set_user_value(name: str, selected: str) -> None:
    """Replace one line of the user file, keeping every other override."""
    ensure_user()
    path = user_file()
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    lines = [line for line in lines if not (
        (match := _LINE.match(line)) and match.group(1) == name)]
    text = "".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    text += f"{name}={selected}\n"
    temporary = path.with_name(path.name + ".incoming")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def ensure() -> None:
    """Ensure both files exist with correct modes; safe on every install."""
    ensure_generated()
    ensure_user()


# migration
