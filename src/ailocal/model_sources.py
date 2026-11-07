"""Provision reviewed model assets through Ollama's native import interfaces.

GGUF recipes use Ollama's pull/create with a pinned weight hash; draft heads
and projectors are pinned the same way. No model weights are rewritten.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from . import policy
from .primitives import Unreachable
from .primitives import fetch as http_fetch


def verify_load(model: str) -> bool:
    """Exercise the native loader without generating tokens or testing quality.

    A listed checkpoint may still have a tensor layout the runner rejects.
    Small context, short residency: install must not pin every model.
    """
    try:
        _, raw = http_fetch(f"{policy.ollama_url().rstrip('/')}/api/chat",
                       payload={"model": model, "messages": [], "stream": False,
                                "options": {"num_ctx": 2048}, "keep_alive": "1s"}, timeout=120)
        result = json.loads(raw[:65536])
        if not isinstance(result, dict) or result.get("done") is not True \
                or result.get("done_reason") != "load" or result.get("error"):
            raise ValueError("no completed model load")
        return True
    except (OSError, ValueError) as error:
        # Report the failure type; upstream bodies may hold paths or secrets.
        print(f"  {model}: Ollama load check failed ({type(error).__name__}). "
              "Inspect the Ollama logs and confirm runtime/model-format compatibility; "
              "no alternate model was selected.", flush=True)
        return False


def _valid(path: Path, entry: dict) -> bool:
    if not path.is_file() or path.stat().st_size != entry["size"]:
        return False
    lfs = entry.get("lfs")
    digest = hashlib.sha256() if lfs else hashlib.sha1()
    if not lfs:
        digest.update(f'blob {entry["size"]}\0'.encode())
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest() == (lfs["sha256"] if lfs else entry["blobId"])


def fetch(source: dict, *, timeout: int = 900) -> Path:
    """Cache a commit-pinned snapshot with size and Git/LFS digest checks."""
    repo, revision = source["repository"], source["revision"]
    root = policy.state_root() / "model-sources" / revision
    root.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout

    def remaining() -> float:
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("model download budget exhausted")
        return min(60, seconds)

    _, raw = http_fetch(f"https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true",
                   timeout=remaining())
    metadata = json.loads(raw)
    if metadata["sha"] != revision:
        raise ValueError("model source revision mismatch")
    weights = False
    for entry in metadata["siblings"]:
        name = entry["rfilename"]
        if Path(name).name != name or not (
                name.endswith((".json", ".safetensors", ".jinja", ".txt")) or name == "LICENSE"):
            continue
        weights |= name.endswith(".safetensors")
        path = root / name
        if _valid(path, entry):
            continue
        print(f"  Fetching {name} ({entry['size'] / 1e6:.1f} MB)", flush=True)
        temporary = path.with_name(name + ".partial")
        written, reported = 0, time.monotonic()
        try:
            with urllib.request.urlopen(
                    f"https://huggingface.co/{repo}/resolve/{revision}/{name}",
                    timeout=remaining()) as response, temporary.open("wb") as stream:
                while chunk := response.read(4 * 1024 * 1024):
                    remaining()
                    stream.write(chunk)
                    written += len(chunk)
                    if time.monotonic() - reported >= 10:
                        print(f"    {written / 1e6:.0f}/{entry['size'] / 1e6:.0f} MB", flush=True)
                        reported = time.monotonic()
            if not _valid(temporary, entry):
                raise ValueError("model asset integrity mismatch")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    if not weights or not (root / "config.json").is_file():
        raise ValueError("model source lacks Safetensors weights or config")
    return root


def _receipts() -> Path:
    return policy.state_root() / "model-sources" / "imports.json"


def _digest(model: str) -> str | None:
    """Return the serving daemon's manifest digest for `model`, or None."""
    try:
        _, raw = http_fetch(f"{policy.ollama_url().rstrip('/')}/api/tags", timeout=15)
        listed = json.loads(raw).get("models") or []
    except (Unreachable, ValueError):
        return None
    for entry in listed:
        if entry.get("name") in (model, f"{model}:latest"):
            return entry.get("digest")
    return None


def _native_assets(shown: dict) -> dict[str, list[str]]:
    """Read only Ollama's leading asset directives, never template/license text.

    Parses the /api/show header, not a Modelfile: first FROM is the target,
    later FROMs projectors; stops before publisher-controlled TEMPLATE text.
    """
    assets: dict[str, list[str]] = {"FROM": [], "DRAFT": []}
    text = shown.get("modelfile")
    if not isinstance(text, str):
        return assets
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        directive, separator, value = line.partition(" ")
        if not separator or directive not in assets:
            break
        assets[directive].append(value)
    return assets


def _native_matches(shown: dict, source: dict) -> bool:
    if not isinstance(shown, dict):
        return False
    root = policy.ollama_model_store() / "blobs"
    assets = _native_assets(shown)
    drafts = ([str(root / ("sha256-" + source["draft_sha256"]))]
              if "draft_sha256" in source else [])
    # Declared projector = second FROM; else unpinned (architecture.md).
    froms = [str(root / ("sha256-" + source["sha256"]))]
    if "projector_sha256" in source:
        froms.append(str(root / ("sha256-" + source["projector_sha256"])))
    return (assets["FROM"][:len(froms)] == froms
            and assets["DRAFT"] == drafts
            and shown.get("renderer") == source["renderer"]
            and shown.get("parser") == source["parser"])


def current(model: str, source: dict) -> bool:
    """Return whether the installed `model` is this source's import.

    Tags omit the revision, so the receipt binds tag → pin → manifest; any
    mismatch or missing receipt means re-import.
    """
    try:
        entry = json.loads(_receipts().read_text()).get(model) or {}
    except (OSError, ValueError, AttributeError):
        return False
    matched = (isinstance(entry, dict)
            and {key: value for key, value in entry.items() if key != "digest"} == source
            and entry.get("digest") is not None
            and entry.get("digest") == _digest(model))
    if not matched or "model" not in source:
        return matched
    # Recheck bindings even for old receipts: they may predate a stricter check.
    try:
        _, raw = http_fetch(f"{policy.ollama_url().rstrip('/')}/api/show", payload={"model": model}, timeout=15)
        return _native_matches(json.loads(raw), source)
    except (Unreachable, ValueError):
        return False


def _record(model: str, source: dict) -> None:
    path = _receipts()
    try:
        receipts = json.loads(path.read_text())
    except (OSError, ValueError):
        receipts = {}
    receipts[model] = {**source, "digest": _digest(model)}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(json.dumps(receipts, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _native(model: str, source: dict) -> bool:
    """Configure supported native rendering on a digest-checked GGUF pull."""
    if subprocess.run([policy.ollama_binary(), "pull", source["model"]],
                      timeout=3600).returncode:
        return False
    base = policy.ollama_url().rstrip("/")

    def call(path: str, body: dict) -> dict:
        _, raw = http_fetch(base + path, payload=body, timeout=180)
        return json.loads(raw)

    shown = call("/api/show", {"model": source["model"]})
    expected = policy.ollama_model_store() / "blobs" / ("sha256-" + source["sha256"])
    if _native_assets(shown)["FROM"][:1] != [str(expected)]:
        raise ValueError("native source weight digest differs from the reviewed pin")
    recipe = {"model": model, "renderer": source["renderer"], "parser": source["parser"],
              "stream": False}
    attached = {}
    for kind in ("draft", "projector"):
        if f"{kind}_model" not in source:
            continue
        if subprocess.run([policy.ollama_binary(), "pull", source[f"{kind}_model"]],
                          timeout=3600).returncode:
            return False
        head = call("/api/show", {"model": source[f"{kind}_model"]})
        blob = policy.ollama_model_store() / "blobs" / ("sha256-" + source[f"{kind}_sha256"])
        head_assets = _native_assets(head)
        if (head_assets["FROM"][:1] != [str(blob)]
                and (kind != "draft" or head_assets["DRAFT"] != [str(blob)])):
            raise ValueError(f"native {kind} weight digest differs from the reviewed pin")
        attached[kind] = "sha256:" + source[f"{kind}_sha256"]
    if "draft" in attached:
        # Native GGUF attachment; reuses the digest-checked blob unrewritten.
        recipe["draft_files"] = {"draft.gguf": attached["draft"]}
    if "projector" in attached:
        # `files`, not `from`: binds target and reviewed projector by digest.
        recipe["files"] = {"model.gguf": "sha256:" + source["sha256"],
                           "mmproj.gguf": attached["projector"]}
    else:
        recipe["from"] = source["model"]
    result = call("/api/create", recipe)
    if result.get("status") != "success":
        return False
    created = call("/api/show", {"model": model})
    if not _native_matches(created, source):
        raise ValueError("native alias did not retain its reviewed weights and rendering")
    # Alias owns the pinned blobs; drop the pull's manifest so `ollama list`
    # shows only policy names. Shared layers survive; `current()` checks alias.
    for name in (source["model"], source.get("draft_model"), source.get("projector_model")):
        if not name or name == model:
            continue
        try:
            http_fetch(base + "/api/delete", payload={"model": name}, method="DELETE", timeout=60)
        except Unreachable:
            print(f"  Source pull {name} was not removed; `ollama rm {name}` tidies the list",
                  flush=True)
    return verify_load(model)


def install(model: str, source: dict) -> bool:
    try:
        if "model" in source:
            if not _native(model, source):
                return False
            _record(model, source)
            return True
        root = fetch(source)
        modelfile = root / "Modelfile"
        modelfile.write_text(f'FROM "{root}"\n', encoding="utf-8")
        binary = policy.ollama_binary()
        if subprocess.run([binary, "create", model, "-f", str(modelfile)],
                          env=dict(os.environ, OLLAMA_MODELS=str(policy.ollama_model_store())),
                          timeout=180).returncode:
            return False
        if subprocess.run([binary, "show", model], capture_output=True,
                          timeout=15).returncode:
            print("  Imported model is not visible to the serving daemon", flush=True)
            return False
        if not verify_load(model):
            return False
        _record(model, source)
        # Ollama owns the blobs; a second copy would double small-tier disk use.
        shutil.rmtree(root)
        return True
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"  Model source installation failed ({type(error).__name__}); "
              "no alternate model was selected", flush=True)
        return False
