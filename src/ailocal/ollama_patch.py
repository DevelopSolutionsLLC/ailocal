"""Build the reviewed Go-only Ollama patch beside its exact native payload."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import platform
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

from . import policy

#: Settings that exempt modules from the public checksum database; the build
#: drops each and sets GOENV=off so a `go env -w` file cannot restore one (#78).
GO_SUMDB_EXEMPTIONS = ("GOSUMDB", "GONOSUMDB", "GONOSUMCHECK", "GOPRIVATE",
                       "GONOPROXY", "GOINSECURE", "GOFLAGS", "GOENV")


def _sha(path: Path) -> str:
    from .primitives import sha256_file
    return sha256_file(path)


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
        origin = (receipt.get("native_origin") or {}) if isinstance(receipt, dict) else {}
        return (isinstance(receipt, dict) and receipt.get("owner") == "ailocal"
                and isinstance(origin, dict)
                and origin.get("vendor_binary_sha256", receipt.get("stock_sha256")) == spec["native_cli_sha256"]
                and path.name == "ollama" and path.is_file()
                and os.access(path, os.X_OK)
                and all(receipt.get(k) == spec[k] for k in
                        ("version", "source_sha256", "patch_sha256", "security_patch_sha256", "router_patch_sha256", "stop_patch_sha256", "qwen35_patch_sha256"))
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


#: Ollama's Developer ID team. The release tarball's CLI is pinned by hash;
#: every Mach-O copied beside it must carry this signature (measured on
#: v0.40.14: all 32 pass, a modified or other-team binary fails).
OLLAMA_SIGNATURE = 'anchor apple generic and certificate leaf[subject.OU] = "3MU9H2V9Y9"'
_MACHO = (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe")


def _verify_native_signatures(payload: Path, deadline: float) -> None:
    signed = []
    for path in sorted(payload.rglob("*")):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as stream:
                if stream.read(4) in _MACHO:
                    _run(["codesign", "--verify", "--strict", f"-R={OLLAMA_SIGNATURE}", str(path)], deadline)
                    signed.append(path.name)
    if "llama-server" not in signed:
        raise RuntimeError("Ollama native payload has no signed llama-server")


def _release_native_payload(spec: dict, work: Path, deadline: float) -> Path:
    """Acquire the exact release tarball the ollama-binary cask installs, in build space."""
    print(f"  Fetching the pinned Ollama {spec['base_version']} native base", flush=True)
    archive = work / "native-base.tgz"
    _download(spec["native_archive_url"], archive, spec["native_archive_sha256"], deadline)
    destination = (work / "native-base").resolve()
    destination.mkdir()
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            link = (target.parent / member.linkname).resolve() if member.issym() else destination
            if (not target.is_relative_to(destination) or not link.is_relative_to(destination)
                    or not (member.isfile() or member.isdir() or member.issym())):
                raise RuntimeError("Unsafe member in pinned Ollama native archive")
        bundle.extractall(destination, filter="data")
    stock = destination / "ollama"
    if not stock.is_file() or _sha(stock) != spec["native_cli_sha256"]:
        raise RuntimeError("Downloaded Ollama native CLI checksum mismatch")
    return stock


def _cached_native_payload(root: Path, spec: dict) -> tuple[Path, dict] | None:
    """Reuse intact owned native files for the exact source base, never a newer app's ABI."""
    if root.is_symlink() or not root.is_dir():
        return None
    for child in sorted(root.iterdir(), reverse=True):
        try:
            if (not child.is_dir() or child.is_symlink()
                    or any(p.is_symlink() for p in child.rglob("*"))):
                continue
            receipt = json.loads((child / "receipt.json").read_text())
            origin = (receipt.get("native_origin") or {}) if isinstance(receipt, dict) else {}
            if (not isinstance(receipt, dict) or receipt.get("owner") != "ailocal"
                    or not isinstance(origin, dict)
                    or origin.get("vendor_binary_sha256", receipt.get("stock_sha256")) != spec["native_cli_sha256"]
                    or child.name != receipt.get("version")
                    or not str(receipt.get("version", "")).startswith(spec["base_version"] + "-ailocal.")
                    or receipt.get("source_sha256") != spec["source_sha256"]
                    or not isinstance(receipt.get("native_files"), dict)
                    or "llama-server" not in receipt["native_files"]
                    or receipt.get("files") != _inventory(child)
                    or not all(receipt["files"].get(k) == v for k, v in receipt["native_files"].items())):
                continue
            binary = child / "ollama"
            if not binary.is_file() or not os.access(binary, os.X_OK):
                continue
            return binary, receipt
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return None


def prepare() -> Path:
    """Build/test atomically; never switch a service or change the installed cask."""
    spec = policy.ollama_patch_spec()
    root = policy.ollama_runtime_root()
    target = root / spec["version"]
    binary = target / "ollama"
    if target.exists():
        if verify(binary):
            return binary
        raise RuntimeError(f"Existing owned Ollama runtime failed integrity checks: {target}")
    # The product supports macOS on Apple Silicon only (AGENTS.md); this is the
    # one place that states it in code, as a precondition of a native build.
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("The reviewed Ollama patch supports macOS arm64 only")
    missing = [name for name in ("go", "git", "codesign") if not shutil.which(name)]
    if missing:
        raise RuntimeError("Ollama patch build prerequisites missing: " + ", ".join(missing))
    deadline = time.monotonic() + 1200
    cached = _cached_native_payload(root, spec)
    if cached:
        stock, parent_receipt = cached
        native_origin = {"kind": "validated-owned-runtime", "path": str(stock.parent),
                         "version": parent_receipt["version"],
                         "source_sha256": parent_receipt["source_sha256"],
                         "vendor_binary_sha256": (parent_receipt.get("native_origin") or {}).get(
                             "vendor_binary_sha256", parent_receipt.get("stock_sha256"))}
    else:
        # The ollama-binary cask installs the pinned release tarball unmodified;
        # its CLI hash proves which one, and the payload signatures are checked
        # after copying. Any other installed Ollama fetches the pinned tarball.
        stock = Path(policy.stock_ollama_binary()).resolve()
        if stock.is_file() and _sha(stock) == spec["native_cli_sha256"]:
            native_origin = {"kind": "signed-vendor-cask", "path": str(stock.parent),
                             "version": spec["base_version"]}
        else:
            stock = None
            native_origin = {"kind": "signed-release-archive", "version": spec["base_version"],
                             "url": spec["native_archive_url"],
                             "archive_sha256": spec["native_archive_sha256"]}
    stock_hash = _sha(stock) if stock is not None else ""
    if not cached and stock is not None:
        if stock_hash != spec["native_cli_sha256"]:
            raise RuntimeError("Installed Ollama native CLI checksum mismatch")
        native_origin["vendor_binary_sha256"] = stock_hash
    patch = Path(spec["patch_file"])
    if _sha(patch) != spec["patch_sha256"]:
        raise RuntimeError("Bundled Ollama patch checksum mismatch")
    security_patch = Path(spec["security_patch_file"])
    if _sha(security_patch) != spec["security_patch_sha256"]:
        raise RuntimeError("Bundled Ollama security patch checksum mismatch")
    router_patch = Path(spec["router_patch_file"])
    if _sha(router_patch) != spec["router_patch_sha256"]:
        raise RuntimeError("Bundled Ollama router patch checksum mismatch")
    stop_patch = Path(spec["stop_patch_file"])
    if _sha(stop_patch) != spec["stop_patch_sha256"]:
        raise RuntimeError("Bundled Ollama stop patch checksum mismatch")
    qwen35_patch = Path(spec["qwen35_patch_file"])
    if _sha(qwen35_patch) != spec["qwen35_patch_sha256"]:
        raise RuntimeError("Bundled Ollama Qwen3.5 history patch checksum mismatch")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".build-", dir=root) as temporary:
        work = Path(temporary)
        if stock is None:
            stock = _release_native_payload(spec, work, deadline)
            stock_hash = _sha(stock)
            native_origin["vendor_binary_sha256"] = stock_hash
        payload = work / "payload"
        payload.mkdir()
        native = _copy_payload(stock, payload)
        if not cached:  # a cached payload was verified when built and is pinned by its receipt
            _verify_native_signatures(payload, deadline)
        if cached and native != parent_receipt["native_files"]:
            raise RuntimeError("Cached native payload does not match its validated receipt")
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
        _run(["git", "apply", "--check", str(router_patch)], deadline, cwd=source)
        _run(["git", "apply", str(router_patch)], deadline, cwd=source)
        _run(["git", "apply", "--check", str(stop_patch)], deadline, cwd=source)
        _run(["git", "apply", str(stop_patch)], deadline, cwd=source)
        _run(["git", "apply", "--check", str(qwen35_patch)], deadline, cwd=source)
        _run(["git", "apply", str(qwen35_patch)], deadline, cwd=source)
        # Vendor tests must neither import fixtures into the operator's store
        # nor contact their serving daemon. Keep test state inside this build.
        test_models = work / "test-models"
        test_models.mkdir()
        # Go's module and build caches default to ~/go and the user cache
        # directory, where they outlive the build and no receipt names them.
        # Keep them in this build's temporary space; -modcacherw lets it be removed.
        # A user's go-env file or GONOSUMDB/GOPRIVATE/GOINSECURE would exempt
        # modules from the public checksum database; this build allows none (#78).
        env = {k: v for k, v in os.environ.items() if k not in GO_SUMDB_EXEMPTIONS}
        env.update(CGO_ENABLED="1", GOOS="darwin", GOARCH="arm64", GOENV="off",
                   GOSUMDB="sum.golang.org",
                   GOTOOLCHAIN="local", GOFLAGS="-mod=readonly -modcacherw", GOWORK="off",
                   GOPATH=str(work / "gopath"), GOMODCACHE=str(work / "gomodcache"),
                   GOCACHE=str(work / "gocache"),
                   OLLAMA_MODELS=str(test_models), OLLAMA_HOST="127.0.0.1:0")
        toolchain = _run(["go", "version"], deadline, cwd=source, env=env).strip()
        flags = (f"-s -w -X=github.com/ollama/ollama/version.Version={spec['version']} "
                 "-X=github.com/ollama/ollama/server.mode=release")
        _run(["go", "build", "-trimpath", "-ldflags", flags,
              "-o", str(payload / "ollama"), "."], deadline, cwd=source, env=env)
        test = payload / "gemma4.test"
        _run(["go", "test", "-c", "-o", str(test), "./mlxrunner/model/gemma4"],
             deadline, cwd=source, env=env)
        result = _run([str(test), "-test.v", "-test.timeout=120s"], deadline, cwd=payload, env=env)
        if ("--- SKIP:" in result or "--- PASS: TestLoadStackedExpertsSwitchGLU" not in result
                or "--- PASS: TestMoERouterMixedPrecisionConfig" not in result):
            raise RuntimeError("Gemma MoE tests did not execute the SwitchGLU regression successfully")
        test.unlink()
        stop_test = payload / "stop.test"
        _run(["go", "test", "-c", "-o", str(stop_test), "./mlxrunner"],
             deadline, cwd=source, env=env)
        stop_result = _run([str(stop_test), "-test.v", "-test.run=^TestStopBuffer$", "-test.timeout=60s"],
                           deadline, cwd=payload, env=env)
        if "--- SKIP:" in stop_result or "--- PASS: TestStopBuffer" not in stop_result:
            raise RuntimeError("MLX stop regression did not execute successfully")
        result += "\n" + stop_result
        stop_test.unlink()
        history_test = payload / "qwen35-history.test"
        _run(["go", "test", "-c", "-o", str(history_test), "./model/renderers"],
             deadline, cwd=source, env=env)
        # Run upstream reference cases too. Optional tokenizer/model parity
        # tests may skip; the three required history regressions may not.
        # The suite reads `testdata/` relative to its package, as `go test`
        # runs it; from any other directory two fixture tests fail on open.
        history_result = _run([str(history_test), "-test.v", "-test.timeout=60s"],
                              deadline, cwd=source / "model" / "renderers", env=env)
        if any(f"--- SKIP: {name}" in history_result or f"--- PASS: {name}" not in history_result for name in (
                    "TestQwen35HistoryOmitsEmptyLoopThinkBlocks", "TestQwen35HistoryKeepsReplayedReasoning",
                    "TestQwen35HistoryKeepsNonThinkingAndPrefill")):
            raise RuntimeError("Qwen3.5 history regression did not execute successfully")
        result += "\n" + history_result
        history_test.unlink()
        if native != {name: _sha(payload / name) for name in native}:
            raise RuntimeError("Native Ollama payload changed during preparation")
        receipt = {k: spec[k] for k in
                   ("version", "source_sha256", "patch_sha256", "security_patch_sha256", "router_patch_sha256", "stop_patch_sha256", "qwen35_patch_sha256")}
        receipt.update(owner="ailocal", stock_binary=str(stock), stock_sha256=stock_hash,
                       native_origin=native_origin,
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
