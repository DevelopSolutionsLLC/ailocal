"""Fetch pinned MLX assets and verify that Ollama can load selected models.

Ollama imports local Safetensors directories but cannot pull these unpublished
MLX snapshots. This adapter only retrieves verified assets, never model code.
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


def install(model: str, source: dict) -> bool:
    try:
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
        # Ollama owns the imported blobs; retaining a second weight copy would
        # double the smaller tiers' disk requirement without serving inference.
        shutil.rmtree(root)
        return True
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"  Model source installation failed ({type(error).__name__}); "
              "no alternate model was selected", flush=True)
        return False
