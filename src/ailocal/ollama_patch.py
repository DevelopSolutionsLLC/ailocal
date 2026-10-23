"""Build the reviewed Go-only Ollama patch beside its exact native payload."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
import json
import os
import platform
import plistlib
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

from . import policy


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _inventory(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): _sha(p) for p in sorted(root.rglob("*"))
            if p.is_file() and p.name != "receipt.json"}


def verify(path: Path) -> bool:
    """Verify the complete owned payload, including its pinned build identity."""
    try:
        spec = policy.ollama_patch_spec()
        if path.parent.is_symlink() or any(p.is_symlink() for p in path.parent.rglob("*")):
            return False
        receipt = json.loads((path.parent / "receipt.json").read_text())
        return (isinstance(receipt, dict) and receipt.get("owner") == "ailocal"
                and path.name == "ollama" and path.is_file()
                and os.access(path, os.X_OK)
                and all(receipt.get(k) == spec[k] for k in
                        ("version", "source_sha256", "patch_sha256", "security_patch_sha256"))
                and receipt.get("files") == _inventory(path.parent))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def remove() -> tuple[list[str], list[str]]:
    """Remove intact owned versions after services stop; preserve everything else."""
    removed, preserved = [], []
    root = policy.ollama_runtime_root()
    if root.is_symlink():
        return removed, [str(root)]
    if not root.exists():
        return removed, preserved
    try:
        children = list(root.iterdir())
    except OSError:
        return removed, [str(root)]
    for child in children:
        try:
            if not child.is_dir() or child.is_symlink() or any(
                    item.is_symlink() for item in child.rglob("*")):
                raise ValueError("unowned path")
            receipt = json.loads((child / "receipt.json").read_text())
            if (not isinstance(receipt, dict) or receipt.get("owner") != "ailocal"
                    or not isinstance(receipt.get("files"), dict)
                    or "ollama" not in receipt["files"]
                    or receipt["files"] != _inventory(child)):
                raise ValueError("unrecognized or modified runtime")
            shutil.rmtree(child)
            removed.append(str(child))
        except (OSError, ValueError, TypeError):
            preserved.append(str(child))
    return removed, preserved


def _run(args: list[str], deadline: float, **kwargs) -> str:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("Ollama patch preparation exceeded its 1200-second budget")
    with subprocess.Popen(args, text=True, start_new_session=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs) as process:
        try:
            output, _ = process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise RuntimeError("Ollama patch preparation exceeded its 1200-second budget") from None
        if process.returncode:
            raise RuntimeError(f"Ollama patch step {args[0]} failed:\n{output[-6000:]}")
    return output


def _download(url: str, destination: Path, digest: str, deadline: float) -> None:
    with urllib.request.urlopen(url, timeout=min(30, max(1, deadline - time.monotonic()))) as response:
        with destination.open("wb") as stream:
            while chunk := response.read(1024 * 1024):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Ollama source download exceeded the build budget")
                stream.write(chunk)
    if _sha(destination) != digest:
        raise RuntimeError("Ollama source archive checksum mismatch")


def _extract(archive: Path, destination: Path) -> Path:
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if (not target.is_relative_to(destination.resolve())
                    or not (member.isfile() or member.isdir())):
                raise RuntimeError("Unsafe member in pinned Ollama source archive")
        bundle.extractall(destination, filter="data")
    roots = list(destination.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise RuntimeError("Unexpected Ollama source archive layout")
    return roots[0]


def _copy_payload(stock: Path, destination: Path) -> dict[str, str]:
    """Copy native files, never symlink back into the mutable vendor install."""
    selected = [p for p in stock.parent.iterdir()
                if p.name.startswith(("mlx_", "llama-"))
                or p.suffix in (".dylib", ".so", ".metallib")
                or "LICENSE" in p.name or "NOTICE" in p.name]
    if not any(p.name == "llama-server" for p in selected) or not any(
            p.name.startswith("mlx_metal_") for p in selected):
        raise RuntimeError("Stock Ollama is missing its native llama/MLX payload")
    for item in selected:
        target = destination / item.name
        if item.is_dir():
            shutil.copytree(item, target, symlinks=False)
        else:
            shutil.copy2(item, target, follow_symlinks=True)
    return _inventory(destination)


def prepare() -> Path:
    """Build/test atomically; never switch a service or change the vendor app."""
    spec = policy.ollama_patch_spec()
    root = policy.ollama_runtime_root()
    target = root / spec["version"]
    binary = target / "ollama"
    if target.exists():
        if verify(binary):
            return binary
        raise RuntimeError(f"Existing owned Ollama runtime failed integrity checks: {target}")
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("The reviewed Ollama patch supports macOS arm64 only")
    missing = [name for name in ("go", "git", "codesign") if not shutil.which(name)]
    if missing:
        raise RuntimeError("Ollama patch build prerequisites missing: " + ", ".join(missing))
    deadline = time.monotonic() + 1200
    stock = Path(policy.stock_ollama_binary()).resolve()
    stock_hash = _sha(stock)
    output = _run([str(stock), "--version"], deadline,
                  env=dict(os.environ, OLLAMA_HOST="127.0.0.1:0"))
    versions = re.findall(r"(?:ollama version is|client version is)\s+(\S+)", output)
    if not versions or any(v != spec["base_version"] for v in versions):
        raise RuntimeError(f"Ollama patch requires exactly {spec['base_version']} stock CLI")
    app = next((p for p in stock.parents if p.suffix == ".app"), None)
    if app is None:
        raise RuntimeError("The reviewed native payload requires the signed Ollama.app installation")
    _run(["codesign", "--verify", "--deep", "--strict", str(app)], deadline)
    with (app / "Contents/Info.plist").open("rb") as stream:
        if plistlib.load(stream).get("CFBundleShortVersionString") != spec["base_version"]:
            raise RuntimeError("Ollama app/native payload version does not match the patch base")
    patch = Path(spec["patch_file"])
    if _sha(patch) != spec["patch_sha256"]:
        raise RuntimeError("Bundled Ollama patch checksum mismatch")
    security_patch = Path(spec["security_patch_file"])
    if _sha(security_patch) != spec["security_patch_sha256"]:
        raise RuntimeError("Bundled Ollama security patch checksum mismatch")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".build-", dir=root) as temporary:
        work = Path(temporary)
        payload = work / "payload"
        payload.mkdir()
        native = _copy_payload(stock, payload)
        if stock_hash != _sha(stock) or native != {
                name: _sha(stock.parent / name) for name in native}:
            raise RuntimeError("Stock Ollama changed while copying its native payload")
        archive = work / "source.tar.gz"
        _download(spec["source_url"], archive, spec["source_sha256"], deadline)
        source_root = work / "source"
        source_root.mkdir()
        source = _extract(archive, source_root)
        _run(["git", "apply", "--check", str(patch)], deadline, cwd=source)
        _run(["git", "apply", str(patch)], deadline, cwd=source)
        _run(["git", "apply", "--check", str(security_patch)], deadline, cwd=source)
        _run(["git", "apply", str(security_patch)], deadline, cwd=source)
        env = dict(os.environ, CGO_ENABLED="1", GOOS="darwin", GOARCH="arm64",
                   GOTOOLCHAIN="local", GOFLAGS="-mod=readonly", GOWORK="off")
        toolchain = _run(["go", "version"], deadline, cwd=source, env=env).strip()
        flags = (f"-s -w -X=github.com/ollama/ollama/version.Version={spec['version']} "
                 "-X=github.com/ollama/ollama/server.mode=release")
        _run(["go", "build", "-trimpath", "-ldflags", flags,
              "-o", str(payload / "ollama"), "."], deadline, cwd=source, env=env)
        test = payload / "gemma4.test"
        _run(["go", "test", "-c", "-o", str(test), "./mlxrunner/model/gemma4"],
             deadline, cwd=source, env=env)
        result = _run([str(test), "-test.v", "-test.timeout=120s"], deadline, cwd=payload, env=env)
        if "--- SKIP:" in result or "--- PASS: TestLoadStackedExpertsSwitchGLU" not in result:
            raise RuntimeError("Gemma MoE tests did not execute the SwitchGLU regression successfully")
        test.unlink()
        if native != {name: _sha(payload / name) for name in native}:
            raise RuntimeError("Native Ollama payload changed during preparation")
        receipt = {k: spec[k] for k in
                   ("version", "source_sha256", "patch_sha256", "security_patch_sha256")}
        receipt.update(owner="ailocal", stock_binary=str(stock), stock_sha256=stock_hash,
                       native_files=native, files=_inventory(payload),
                       built_at=datetime.now(timezone.utc).isoformat(),
                       toolchain=toolchain, regression_output=result)
        (payload / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        if not verify(payload / "ollama"):
            raise RuntimeError("Prepared Ollama payload failed integrity verification")
        if target.exists():
            raise RuntimeError("Ollama destination appeared during preparation; refusing overwrite")
        payload.rename(target)
    return binary
