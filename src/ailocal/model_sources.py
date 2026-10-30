"""Provision reviewed model assets through Ollama's native import interfaces.

Ollama imports local Safetensors directories but cannot pull these unpublished
MLX snapshots. Native GGUF recipes use Ollama's own pull/create, with a pinned
weight hash and its built-in renderer/parser. No model weights are rewritten.
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


def verify_load(model: str) -> bool:
    """Exercise the native loader without generating tokens or testing quality.

    Metadata is not loadability: an imported checkpoint can be listed and shown
    while its tensor layout is unsupported by the serving runner. Keep this a
    small-context check and expire its residency promptly, so installation does
    not pin every selected model or spend tokens evaluating them.
    """
    request = urllib.request.Request(
        f"{policy.ollama_url().rstrip('/')}/api/chat",
        data=json.dumps({"model": model, "messages": [], "stream": False,
                         "options": {"num_ctx": 2048}, "keep_alive": "1s"}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read(65536))
        if not isinstance(result, dict) or result.get("done") is not True \
                or result.get("done_reason") != "load" or result.get("error"):
            raise ValueError("no completed model load")
        return True
    except (OSError, ValueError) as error:
        # Upstream response bodies can contain local paths or other sensitive
        # values. Report the failure type, not an untrusted daemon response.
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

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("model download budget exhausted")
        return min(60, seconds)

    with urllib.request.urlopen(
            f"https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true",
            timeout=remaining()) as response:
        metadata = json.load(response)
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
    """The serving daemon's manifest digest for `model`, or None."""
    try:
        with urllib.request.urlopen(f"{policy.ollama_url().rstrip('/')}/api/tags",
                                    timeout=15) as response:
            listed = json.load(response).get("models") or []
    except (OSError, ValueError):
        return None
    for entry in listed:
        if entry.get("name") in (model, f"{model}:latest"):
            return entry.get("digest")
    return None


def _native_assets(shown: dict) -> dict[str, list[str]]:
    """Read only Ollama's leading asset directives, never template/license text.

    This is the generated /api/show header, not a general Modelfile parser.
    The first FROM is the target; later FROMs can name projectors. Stop before
    TEMPLATE/RENDERER/etc., whose quoted contents are publisher-controlled.
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
    return (assets["FROM"][:1] == [str(root / ("sha256-" + source["sha256"]))]
            and assets["DRAFT"] == drafts
            and shown.get("renderer") == source["renderer"]
            and shown.get("parser") == source["parser"])


def current(model: str, source: dict) -> bool:
    """Whether the installed `model` is this source's import.

    Imported tags do not name their revision, so a changed pin would otherwise
    leave the old weights serving under the same tag. The receipt binds the tag
    to the pinned revision and to the manifest Ollama created; a missing
    receipt, a different pin or a replaced manifest means re-import.
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
    # Recheck native asset bindings even for old receipts: they may predate a
    # stricter verifier. A receipt alone cannot grandfather an invalid pin.
    try:
        request = urllib.request.Request(
            f"{policy.ollama_url().rstrip('/')}/api/show",
            data=json.dumps({"model": model}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=15) as response:
            return _native_matches(json.load(response), source)
    except (OSError, ValueError):
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

    def call(path, body):
        request = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.load(response)

    shown = call("/api/show", {"model": source["model"]})
    expected = policy.ollama_model_store() / "blobs" / ("sha256-" + source["sha256"])
    if _native_assets(shown)["FROM"][:1] != [str(expected)]:
        raise ValueError("native source weight digest differs from the reviewed pin")
    recipe = {"model": model, "from": source["model"],
              "renderer": source["renderer"], "parser": source["parser"], "stream": False}
    draft = None
    if "draft_model" in source:
        if subprocess.run([policy.ollama_binary(), "pull", source["draft_model"]],
                          timeout=3600).returncode:
            return False
        head = call("/api/show", {"model": source["draft_model"]})
        draft = policy.ollama_model_store() / "blobs" / ("sha256-" + source["draft_sha256"])
        head_assets = _native_assets(head)
        if (head_assets["FROM"][:1] != [str(draft)]
                and head_assets["DRAFT"] != [str(draft)]):
            raise ValueError("native draft weight digest differs from the reviewed pin")
        # DRAFT/draft_files is Ollama's native GGUF attachment interface. It
        # reuses the pulled, digest-checked blob without rewriting its tensors.
        recipe["draft_files"] = {"draft.gguf": "sha256:" + source["draft_sha256"]}
    result = call("/api/create", recipe)
    if result.get("status") != "success":
        return False
    created = call("/api/show", {"model": model})
    if not _native_matches(created, source):
        raise ValueError("native alias did not retain its reviewed weights and rendering")
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
        # Ollama owns the imported blobs; retaining a second weight copy would
        # double the smaller tiers' disk requirement without serving inference.
        shutil.rmtree(root)
        return True
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"  Model source installation failed ({type(error).__name__}); "
              "no alternate model was selected", flush=True)
        return False
