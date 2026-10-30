#!/usr/bin/env python3
"""install.py — bootstrapping a machine: provisioning, tier selection, audit.

The provenance invariants: a config file is never replaced once the operator
has edited it, a default they deleted is not resurrected, a file the
distribution retires is removed only while it still matches what was shipped,
and no manifest state can license an overwrite.

Shipped assets (deploy/, clients/) are NOT provisioned — they are read in place
from the package — so there is no data root here to assert about.
"""
from __future__ import annotations

import plistlib
import json
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch
from types import SimpleNamespace
import contextlib
import io

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from harness import RESOURCES, Suite  # noqa: E402
from ailocal import install as I  # noqa: E402

_suite = Suite()
check = _suite.check


def _roots():
    box = Path(tempfile.mkdtemp(prefix="install-"))
    return box, box / "config", box / "state"


def main() -> None:
    _suite.section("PINNED MODEL SOURCES (SIMULATED HUB AND NATIVE IMPORT)")
    from ailocal import model_sources as M
    source = {"repository": "fixture/model", "revision": "a" * 40}
    files = {"config.json": b'{}', "model.safetensors": b'fixture-weights'}
    entries = [{"rfilename": name, "size": len(body),
                "lfs": {"sha256": hashlib.sha256(body).hexdigest()}}
               for name, body in files.items()]
    metadata = {"sha": source["revision"], "siblings": entries + [
        {"rfilename": "execute.py"}, {"rfilename": "../outside.json"}]}

    def hub(url, **kwargs):
        if "/api/models/" in url:
            return io.BytesIO(json.dumps(metadata).encode())
        return io.BytesIO(files[url.rsplit('/', 1)[-1]])

    with tempfile.TemporaryDirectory() as box, \
            patch.object(M.policy, "state_root", return_value=Path(box)), \
            patch.object(M.policy, "ollama_binary", return_value="ollama"), \
            patch.object(M.urllib.request, "urlopen", side_effect=hub) as fetch:
        root = M.fetch(source)
        check(set(p.name for p in root.iterdir()) == set(files),
              "model source downloads only assets; code and traversal paths are excluded")
        fetch.reset_mock()
        M.fetch(source)
        check(fetch.call_count == 1, "verified cached weights are not downloaded again")
        metadata["sha"] = "b" * 40
        try:
            M.fetch(source)
            check(False, "changed Hub revision is refused")
        except ValueError:
            check(True, "changed Hub revision is refused")
        metadata["sha"] = source["revision"]
        with patch.object(M.subprocess, "run", return_value=SimpleNamespace(returncode=1)):
            check(not M.install("fixture:mlx", source) and root.exists(),
                  "native import failure preserves verified assets and returns failure")
        with patch.object(M.subprocess, "run", side_effect=[SimpleNamespace(returncode=0),
                                                            SimpleNamespace(returncode=1)]):
            check(not M.install("fixture:mlx", source) and root.exists(),
                  "a successful import invisible to the daemon is a failure")
        with patch.object(M.subprocess, "run", return_value=SimpleNamespace(returncode=0)), \
                patch.object(M, "verify_load", return_value=False):
            check(not M.install("fixture:mlx", source) and root.exists(),
                  "visible but unloadable model fails installation and preserves verified assets")
        check(not M.current("fixture:mlx", source), "an import without a receipt is not current")
        with patch.object(M.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as create, \
                patch.object(M, "verify_load", return_value=True), \
                patch.object(M, "_digest", return_value="sha256:one"):
            check(M.install("fixture:mlx", source) and not root.exists(),
                  "successful native import removes the duplicate source weight copy")
            check(M.current("fixture:mlx", source),
                  "a successful import records its pinned revision and manifest")
            check(not M.current("fixture:mlx", {**source, "revision": "c" * 40}),
                  "a changed pinned revision makes the installed tag stale")
        with patch.object(M, "_digest", return_value="sha256:other"):
            check(not M.current("fixture:mlx", source),
                  "a tag whose manifest was replaced is stale")
        with patch.object(M, "_digest", return_value=None):
            check(not M.current("fixture:mlx", source),
                  "an absent tag is never current")
            check(create.call_args_list[0].args[0][:3] == ["ollama", "create", "fixture:mlx"]
                  and create.call_args_list[0].kwargs["env"]["OLLAMA_MODELS"]
                  == str(M.policy.ollama_model_store()),
                  "Ollama native create owns import, without force or a replacement backend")
        files["model.safetensors"] = b'corrupt-weights'
        try:
            M.fetch(source)
            check(False, "corrupt weights are refused")
        except ValueError:
            check(not (root / "model.safetensors").exists()
                  and not list(root.glob('*.partial')), "corrupt weights are refused and partial file removed")

    _suite.section("NATIVE GGUF RENDERING (SIMULATED DAEMON, NO WEIGHT CONVERSION)")
    native = {"model": "hf.co/fixture/model:weights.gguf", "sha256": "d" * 64,
              "renderer": "qwen3.5", "parser": "qwen3.5"}
    expected = M.policy.ollama_model_store() / "blobs" / ("sha256-" + native["sha256"])
    calls = []

    def daemon(request, **kwargs):
        body = json.loads(request.data)
        calls.append((request.full_url, body))
        response = ({"modelfile": f"FROM {expected}\n", "renderer": "qwen3.5", "parser": "qwen3.5"} if request.full_url.endswith('/api/show')
                    else {"status": "success"})
        return io.BytesIO(json.dumps(response).encode())

    with tempfile.TemporaryDirectory() as box, \
            patch.object(M.policy, "state_root", return_value=Path(box)), \
            patch.object(M.policy, "ollama_binary", return_value="ollama"), \
            patch.object(M.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as pull, \
            patch.object(M.urllib.request, "urlopen", side_effect=daemon), \
            patch.object(M, "verify_load", return_value=True), \
            patch.object(M, "_digest", return_value="sha256:native"):
        check(M.install("fixture:mtp", native), "native GGUF recipe loads through Ollama")
        check(pull.call_args.args[0] == ["ollama", "pull", native["model"]]
              and next(body for url, body in calls if url.endswith('/api/create'))
                  == {"model": "fixture:mtp", "from": native["model"],
                                   "renderer": "qwen3.5", "parser": "qwen3.5", "stream": False},
              "native pull/create supplies built-in rendering without converted files or quantization")
        check(M.current("fixture:mtp", native)
              and not M.current("fixture:mtp", {**native, "parser": "other"}),
              "receipt binds the native renderer/parser recipe and weight pin")
        calls.clear()
        check(not M.install("fixture:wrong", {**native, "sha256": "e" * 64})
              and not any(url.endswith('/api/create') for url, _ in calls),
              "a different weight digest fails before creating an alias")

        wrong = expected.with_name("sha256-" + "e" * 64)
        for header in (f'FROM {wrong}\nTEMPLATE """\nFROM {expected}\n"""\n',
                       f'FROM {wrong}\nFROM {expected}\n',
                       f'FROM {wrong}\nLICENSE """\nFROM {expected}\n"""\n'):
            calls.clear()
            def spoofed(request, **kwargs):
                calls.append((request.full_url, json.loads(request.data)))
                return io.BytesIO(json.dumps({"modelfile": header, "renderer": "qwen3.5",
                                               "parser": "qwen3.5"}).encode())
            with patch.object(M.urllib.request, "urlopen", side_effect=spoofed):
                check(not M.install("fixture:spoofed", native)
                      and not any(url.endswith('/api/create') for url, _ in calls),
                      "template/license/projector text cannot impersonate the pinned target")
                check(not M.current("fixture:mtp", native),
                      "a matching legacy receipt cannot grandfather a spoofed native target")

        paired = {**native, "draft_model": "hf.co/fixture/model:head.gguf",
                  "draft_sha256": "f" * 64}
        draft_path = M.policy.ollama_model_store() / "blobs" / ("sha256-" + "f" * 64)

        def paired_daemon(request, **kwargs):
            body = json.loads(request.data)
            calls.append((request.full_url, body))
            if request.full_url.endswith('/api/create'):
                return io.BytesIO(b'{"status":"success"}')
            modelfile = (f"FROM {draft_path}\n" if body["model"] == paired["draft_model"]
                         else f"FROM {expected}\nDRAFT {draft_path}\n")
            return io.BytesIO(json.dumps({"modelfile": modelfile,
                                         "renderer": "qwen3.5", "parser": "qwen3.5"}).encode())

        calls.clear()
        with patch.object(M.urllib.request, "urlopen", side_effect=paired_daemon):
            check(M.install("fixture:paired", paired), "native separate MTP head loads with its target")
            created = next(body for url, body in calls if url.endswith('/api/create'))
            check(created.get("draft_files") == {"draft.gguf": "sha256:" + "f" * 64},
                  "native draft_files attaches the exact pulled head without conversion")
            check(M.current("fixture:paired", paired)
                  and not M.current("fixture:paired", {**paired, "draft_sha256": "a" * 64})
                  and not M.current("fixture:paired", native),
                  "receipt binds the MTP head pin, including its removal from policy")
            def contained_head(request, **kwargs):
                response = paired_daemon(request, **kwargs)
                if json.loads(request.data).get("model") == paired["draft_model"]:
                    return io.BytesIO(response.read().replace(b'FROM ', b'DRAFT '))
                return response

            with patch.object(M.urllib.request, "urlopen", side_effect=contained_head):
                check(M.install("fixture:contained-head", {**paired, "model": "gemma4:26b-a4b-it-qat"}),
                      "native original target and a pinned DRAFT inside a source manifest are supported")
            calls.clear()
            check(not M.install("fixture:bad-head", {**paired, "draft_sha256": "a" * 64})
                  and not any(url.endswith('/api/create') for url, _ in calls),
                  "a mismatched draft digest fails before alias creation")
        calls.clear()
        with patch.object(M.urllib.request, "urlopen", side_effect=daemon):
            # A daemon which silently drops the draft must fail after create.
            with patch.object(M.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
                check(not M.install("fixture:dropped-head", {**paired, "draft_sha256": "d" * 64}),
                      "native create which omits DRAFT fails verification")
        fake_head = {"modelfile": f'FROM {expected}\nTEMPLATE """\nDRAFT {draft_path}\n"""\n',
                     "renderer": "qwen3.5", "parser": "qwen3.5"}
        check(not M._native_matches(fake_head, paired),
              "a DRAFT line inside a template cannot impersonate an attached head")
        unclaimed = {"modelfile": f"FROM {expected}\nDRAFT {draft_path}\n",
                     "renderer": "qwen3.5", "parser": "qwen3.5"}
        check(not M._native_matches(unclaimed, native),
              "an undeclared external draft cannot pass native source verification")

    _suite.section("NATIVE LOAD CHECK (SIMULATED DAEMON, NO GENERATION)")
    with patch.object(M.urllib.request, "urlopen", return_value=io.BytesIO(
            b'{"done":true,"done_reason":"load"}')) as request:
        check(M.verify_load("fixture:mlx"), "completed native load passes")
        payload = json.loads(request.call_args.args[0].data)
        check(payload["messages"] == [] and payload["keep_alive"] == "1s"
              and payload["options"] == {"num_ctx": 2048}
              and request.call_args.kwargs["timeout"] == 120,
              "load check generates no tokens, uses a small context and bounded residency/timeout")
    for body in (b'{}', b'[]', b'{"done":false,"done_reason":"load"}',
                 b'{"done":true,"done_reason":"unload"}',
                 b'{"done":true,"done_reason":"load","error":"private detail"}', b'invalid'):
        output = io.StringIO()
        with patch.object(M.urllib.request, "urlopen", return_value=io.BytesIO(body)), \
                contextlib.redirect_stdout(output):
            check(not M.verify_load("fixture:mlx"), "incomplete, invalid or failed load is refused")
        check("private detail" not in output.getvalue(), "daemon body is not disclosed")
    for error in (TimeoutError(), urllib.error.HTTPError(
            'http://127.0.0.1:11434/api/chat', 500, 'private detail', {}, None)):
        with patch.object(M.urllib.request, "urlopen", side_effect=error), \
                contextlib.redirect_stdout(io.StringIO()):
            check(not M.verify_load("fixture:mlx"), "timeout or unsupported runtime is a load failure")

    _suite.section("RETIRED PRESETS ARE REMOVED WITHOUT TOUCHING AUTHORED FILES")
    with tempfile.TemporaryDirectory(prefix="retired-clients-") as root:
        clients_root = Path(root) / "clients"
        codex = clients_root / "codex"
        codex.mkdir(parents=True)
        generated = codex / "plan.config.toml"
        generated.write_text('# Generated by ailocal. Do not edit.\nmodel = "old"\n')
        authored = codex / "review.config.toml"
        authored.write_text('model = "operator-choice"\n')
        outside = Path(root) / "outside.toml"
        outside.write_text('# Generated by ailocal. Do not edit.\n')
        symlink = codex / "cyber.config.toml"
        symlink.symlink_to(outside)
        with patch.object(I.policy, "deployed_client_root", return_value=clients_root), \
             contextlib.redirect_stdout(io.StringIO()):
            I.retire_generated_clients()
            I.retire_generated_clients()
        check(not generated.exists(), "retired generated preset is removed idempotently")
        check(authored.read_text() == 'model = "operator-choice"\n',
              "authored native profile is preserved")
        check(symlink.is_symlink() and outside.exists(),
              "retirement never follows or removes symlinks")

    _suite.section("MODEL PULLS REQUIRE THE EXACT SELECTED VARIANT")
    for requested, installed, pulls in (
        ("vendor/model:mlx-4bit", "vendor/model:mlx-4bit-custom", True),
        ("vendor/model:mlx-4bit", "vendor/model:latest", True),
        ("vendor/model:mlx-4bit", "vendor/model:mlx-4bit", False),
        ("vendor/model", "vendor/model:latest", False),
        ("vendor/model:latest", "vendor/model", False),
    ):
        with patch.object(I, "_has", return_value=True), \
             patch.object(I, "_out", return_value="Ollama model list"), \
             patch.object(I, "_installed_models", return_value={installed: 16.0}), \
             patch.object(I.policy, "effective_summary", return_value={
                 "roles": {"implementation": {"model": requested, "enabled": True}}}), \
             patch.object(I, "_run", return_value=SimpleNamespace(returncode=0)) as run, \
             patch.object(I.policy, "ollama_binary", return_value="ollama"), \
             patch.object(M, "verify_load", return_value=True) as load, \
             contextlib.redirect_stdout(io.StringIO()):
            result = I.pull_models()
        check(run.call_args_list == ([(("ollama", "pull", requested), {})] if pulls else []),
              f"{installed} {'does not satisfy' if pulls else 'satisfies'} {requested}")
        check(result == 0 and load.call_args.args == (requested,),
              "new and already installed models must pass the native loader")

    with patch.object(I, "_has", return_value=True), \
            patch.object(I, "_out", return_value="Ollama model list"), \
            patch.object(I, "_installed_models", return_value={"fixture:mlx": 2.0}), \
            patch.object(I.policy, "effective_summary", return_value={"roles": {
                "cyber": {"model": "fixture:mlx", "source": source}}}), \
            patch.object(M, "current", return_value=True), \
            patch.object(M, "verify_load", return_value=False), \
            patch.object(M, "install") as reimport, \
            patch.object(I, "_run") as download, \
            contextlib.redirect_stdout(io.StringIO()):
        result = I.pull_models()
    check(result == 1 and not reimport.called and not download.called,
          "reinstall rejects unloadable existing models without repeating an unchanged download/import")

    with patch.object(I, "_has", return_value=True), \
            patch.object(I, "_out", return_value="Ollama model list"), \
            patch.object(I, "_installed_models", return_value={"fixture-mlx:latest": 2.0}), \
            patch.object(I.policy, "effective_summary", return_value={"roles": {
                "cyber": {"model": "fixture-mlx", "source": source}}}), \
            patch.object(M, "current", return_value=False), \
            patch.object(M, "verify_load", return_value=True) as load, \
            patch.object(M, "install", return_value=True) as reimport, \
            contextlib.redirect_stdout(io.StringIO()):
        result = I.pull_models()
    check(result == 0 and reimport.call_args.args == ("fixture-mlx", source) and not load.called,
          "an imported tag that is not the pinned revision is re-imported, not trusted by name")

    _suite.section("INSTALL VALIDATION CONTROLS THE EXIT STATUS")
    from ailocal import clients, generation, runtime
    from ailocal.checks import run as checks_run
    for verdict, docs_ok, required in ((0, True, None), (1, True, None),
                                      (0, False, None), (0, True, "gemma4-switch-glu")):
        output = io.StringIO()
        with contextlib.ExitStack() as stack:
            for name in ("require_prerequisites", "configure_ollama_services",
                         "_write_env", "_print_plan"):
                stack.enter_context(patch.object(I, name))
            order = []
            stack.enter_context(patch.object(I, "pull_models", side_effect=lambda: order.append("models") or 0))
            stack.enter_context(patch.object(I, "_write_model_warmup", side_effect=lambda: order.append("warmup")))
            stack.enter_context(patch.object(I.policy, "required_runtime_patch", return_value=required))
            persist_runtime = stack.enter_context(patch("ailocal.environment.set_ollama_patch"))
            stack.enter_context(patch("ailocal.ollama_patch.prepare", return_value=Path("/fixture/ollama")))
            stack.enter_context(patch.object(I, "distribution_source"))
            stack.enter_context(patch.object(I, "provision", return_value={
                "installed": [], "preserved": [], "absent": [], "retired": []}))
            stack.enter_context(patch.object(I, "_memory_gb", return_value=64))
            stack.enter_context(patch.object(I, "select_tier", return_value="64gb"))
            stack.enter_context(patch.object(generation, "main", return_value=0))
            stack.enter_context(patch.object(runtime, "compose"))
            stack.enter_context(patch.object(runtime, "cmd_start", return_value=0))
            stack.enter_context(patch.object(clients, "present", return_value=False))
            stack.enter_context(patch.object(clients, "report_missing"))
            docs = stack.enter_context(patch.object(clients, "provision_documentation", return_value=docs_ok))
            stack.enter_context(patch.object(checks_run, "main", return_value=verdict))
            stack.enter_context(contextlib.redirect_stdout(output))
            result = I.cmd_install(["--yes"])
        check(docs.called, "unattended fresh install provisions documentation MCPs")
        check(order == ["models", "warmup"], "model warmup follows all short-lived native load checks")
        check(persist_runtime.called == bool(required),
              "profile-native runtime requirements are activated and saved without a separate flag")
        check(result == (verdict or int(not docs_ok)), f"install returns validation status {verdict}, docs {docs_ok}")
        check(("proxy is ready" in output.getvalue()) == (verdict == 0 and docs_ok),
              f"install only claims readiness after passing validation (status {verdict})")

    _suite.section("--yes PROVISIONS AILOCAL-OWNED ROOTS ONLY")
    with contextlib.ExitStack() as stack:
        for name in ("require_prerequisites", "configure_ollama_services",
                     "_write_env", "_print_plan", "_write_model_warmup"):
            stack.enter_context(patch.object(I, name))
        stack.enter_context(patch.object(I, "pull_models", return_value=0))
        stack.enter_context(patch.object(I.policy, "required_runtime_patch", return_value=None))
        stack.enter_context(patch.object(I, "distribution_source"))
        stack.enter_context(patch.object(I, "provision", return_value={
            "installed": [], "preserved": [], "absent": [], "retired": []}))
        stack.enter_context(patch.object(I, "_memory_gb", return_value=64))
        stack.enter_context(patch.object(I, "select_tier", return_value="64gb"))
        stack.enter_context(patch.object(generation, "main", return_value=0))
        stack.enter_context(patch.object(runtime, "compose"))
        stack.enter_context(patch.object(runtime, "cmd_start", return_value=0))
        stack.enter_context(patch.object(clients, "present", return_value=True))
        stack.enter_context(patch.object(clients, "report_missing"))
        stack.enter_context(patch.object(clients, "provision_documentation", return_value=True))
        targets = {n: MagicMock() for n in clients.TARGETS}
        stack.enter_context(patch.dict(clients._TARGETS, targets))
        shell = stack.enter_context(patch.object(clients, "ensure_shell_sourcing"))
        agent = stack.enter_context(patch.object(clients, "provision_local_agent"))
        stack.enter_context(patch.object(checks_run, "main", return_value=0))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        I.cmd_install(["--yes"])
    check(targets["claude"].called and targets["codex"].called,
          "--yes provisions the claude-local and codex-local roots its own check requires")
    check(not targets["vscode"].called and not shell.called,
          "--yes never merges VS Code settings or edits ~/.zshrc")
    check(agent.call_args is not None and sorted(agent.call_args.args[0]) == ["claude", "codex"],
          "--yes registers the agent MCP in the claude-local and codex-local roots only")

    _suite.section("CONCURRENT LIFECYCLE COMMANDS FAIL FAST")
    from ailocal import cli
    with tempfile.TemporaryDirectory() as box, \
            patch.object(I.policy, "state_root", return_value=Path(box)):
        with cli._lifecycle_lock():
            try:
                with cli._lifecycle_lock():
                    check(False, "overlapping lifecycle commands cannot acquire the same lock")
            except SystemExit as error:
                check("lifecycle command is running" in str(error),
                      "overlapping lifecycle commands fail promptly with an actionable message")
        with cli._lifecycle_lock():
            check(True, "lifecycle lock is released after the command finishes")

    with patch.object(I.policy, "ollama_patch_selection", return_value="unknown-runtime"), \
            patch.object(I, "require_prerequisites") as prerequisites:
        try:
            I.cmd_install(["--yes"])
            check(False, "unknown authored runtime selection is rejected")
        except SystemExit:
            check(not prerequisites.called, "unknown authored runtime is rejected before any installation mutation")

    with tempfile.TemporaryDirectory() as box, \
            patch.object(I, "LA_DIR", Path(box)), \
            patch.object(I, "_bootout", side_effect=OSError("agent still running")), \
            patch("ailocal.ollama_patch.remove") as remove_payload, \
            contextlib.redirect_stdout(io.StringIO()):
        agent = Path(box) / "com.ailocal.ollama.plist"
        agent.write_bytes(b"preserve running service")
        try:
            I.remove_login_agents()
            check(False, "a running agent prevents runtime deletion")
        except OSError:
            check(agent.exists() and not remove_payload.called,
                  "failure to stop an agent preserves its service file and runtime")

    _suite.section("STOCK OLLAMA STARTUP RESTORATION (SIMULATED LAUNCHD)")
    for initial, expected in (("enabled", False), ("disabled", True),
                              ("true", True), ("false", False), (None, False)):
        with tempfile.TemporaryDirectory() as box, patch.object(I, "LA_DIR", Path(box)):
            line = f'"com.ollama.ollama" => {initial}' if initial else ''
            response = SimpleNamespace(returncode=0, stdout=f'disabled services = {{\n{line}\n}}')
            with patch.object(I, "_run", return_value=response) as native:
                I._save_ollama_startup()
                record = I._read_ollama_startup()
                check(record["disabled"] is expected, "capture the original stock startup preference")
                native.reset_mock()
                I._save_ollama_startup()
                check(not native.called, "reinstall never overwrites the first startup preference")
            receipt = Path(box) / "com.ailocal.ollama.restore.json"
            check(receipt.stat().st_mode & 0o777 == 0o600, "startup receipt is private")
            calls = []
            def launchctl(*args, **kwargs):
                calls.append(args)
                return SimpleNamespace(returncode=0, stdout=(
                    'disabled services = {"com.ollama.ollama" => disabled}'))
            with patch.object(I, "_run", side_effect=launchctl), \
                    patch.object(I, "_bootout", side_effect=lambda label: calls.append(("stop", label))), \
                    patch("ailocal.ollama_patch.remove", return_value=([], [])), \
                    contextlib.redirect_stdout(io.StringIO()):
                I.remove_login_agents()
            action = "disable" if expected else "enable"
            change = next(n for n, args in enumerate(calls) if args[:2] == ("launchctl", action))
            last_stop = max(n for n, args in enumerate(calls) if args[0] == "stop")
            check(change > last_stop and not receipt.exists(),
                  "teardown restores startup only after all owned services stop, then retires its receipt")

    for response in (SimpleNamespace(returncode=1, stdout="private failure"),
                     SimpleNamespace(returncode=0, stdout=""),
                     SimpleNamespace(returncode=0, stdout='disabled services = {"com.ollama.ollama" => unknown}')):
        with tempfile.TemporaryDirectory() as box, patch.object(I, "LA_DIR", Path(box)), \
                patch.object(I, "_run", return_value=response) as native:
            try:
                I._save_ollama_startup()
                check(False, "failed or unrecognized discovery must stop before mutating startup")
            except RuntimeError:
                check(native.call_count == 1 and I._read_ollama_startup() is None,
                      "failed discovery does not guess a preference or create a restore receipt")

    with tempfile.TemporaryDirectory() as box, patch.object(I, "LA_DIR", Path(box)):
        agent = Path(box) / "com.ailocal.ollama.plist"
        agent.write_text("legacy managed agent")
        with patch.object(I, "_run") as native:
            I._save_ollama_startup()
            record = I._read_ollama_startup()
        check(record["disabled"] is None and not native.called,
              "legacy installations never present the already managed setting as an original preference")
        output = io.StringIO()
        with patch.object(I, "_run") as native, contextlib.redirect_stdout(output):
            I._restore_ollama_startup(record)
        check(not native.called and "launchctl enable" in output.getvalue(),
              "unknown historical startup state gives an explicit recovery command without guessing")

    with tempfile.TemporaryDirectory() as box, patch.object(I, "LA_DIR", Path(box)):
        receipt = Path(box) / "com.ailocal.ollama.restore.json"
        receipt.write_text(json.dumps({"owner": "ailocal", "schema": 1, "disabled": False}))
        with patch.object(I, "_stock_ollama_disabled", return_value=True), \
                patch.object(I, "_run", return_value=SimpleNamespace(returncode=1)), \
                patch.object(I, "_bootout"), patch("ailocal.ollama_patch.remove") as remove_payload, \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                I.remove_login_agents()
                check(False, "failed startup restoration must fail teardown")
            except RuntimeError:
                check(receipt.exists() and not remove_payload.called,
                      "failed restoration retains its receipt and runtime so teardown can be retried")
        with patch.object(I, "_stock_ollama_disabled", return_value=False), \
                patch.object(I, "_run") as native, contextlib.redirect_stdout(io.StringIO()):
            I._restore_ollama_startup({"disabled": True})
        check(not native.called, "teardown preserves a startup override changed outside ailocal")

        for invalid in ('{', '{}', '[]', '{"owner":"other","schema":1,"disabled":false}',
                        '{"owner":"ailocal","schema":1,"disabled":"false"}'):
            receipt.write_text(invalid)
            with patch.object(I, "_bootout") as stop:
                try:
                    I.remove_login_agents()
                    check(False, "invalid receipt must stop teardown")
                except RuntimeError:
                    check(not stop.called and receipt.read_text() == invalid,
                          "invalid or foreign receipt is preserved and rejected before stopping services")
        receipt.unlink()
        outside = Path(box) / "outside"
        outside.write_text("operator data")
        receipt.symlink_to(outside)
        try:
            I._save_ollama_startup()
            check(False, "startup receipt must not follow symlinks")
        except RuntimeError:
            check(outside.read_text() == "operator data", "startup receipt never follows a symlink")

    for disable_ok in (True, False):
        with tempfile.TemporaryDirectory() as box, patch.object(I, "LA_DIR", Path(box)), \
                patch.object(I, "_prepare_model_store"), patch.object(I, "_migrate_model_store"), \
                patch.object(I, "_stock_ollama_disabled", return_value=False), \
                patch.object(I, "_ollama_is_current", return_value=True), \
                patch.object(I, "_write_ollama_setenv_agent"), \
                patch.object(I, "_activate_ollama") as activate, \
                contextlib.redirect_stdout(io.StringIO()):
            def native(*args, **kwargs):
                if args[:2] == ("launchctl", "disable"):
                    check(I._read_ollama_startup()["disabled"] is False,
                          "install records the operator preference before disabling stock startup")
                    return SimpleNamespace(returncode=0 if disable_ok else 1)
                return SimpleNamespace(returncode=0)
            with patch.object(I, "_run", side_effect=native):
                try:
                    I.configure_ollama_services(False, binary="fixture-ollama")
                    check(disable_ok, "startup disable failure cannot report successful configuration")
                except RuntimeError:
                    check(not disable_ok, "startup disable failure is explicit")
            check(not activate.called,
                  "healthy reinstall and failed startup disable never replace the managed server")

    _suite.section("AN ALREADY MIGRATED STORE DOES NOT STOP THE DAEMON")
    with tempfile.TemporaryDirectory() as box:
        home = Path(box) / "home"
        source = home / ".ollama/models/manifests/fixture"
        dest = Path(box) / "shared/manifests/fixture"
        source.parent.mkdir(parents=True)
        dest.parent.mkdir(parents=True)
        source.write_text("existing model")
        dest.write_text("existing model")
        with patch.object(I.Path, "home", return_value=home), \
                patch.object(I, "MODEL_STORE", str(Path(box) / "shared")), \
                patch.object(I, "_out") as probe, patch.object(I, "_run") as mutate:
            I._migrate_model_store()
        check(not probe.called and not mutate.called and source.exists() and dest.exists(),
              "duplicate metadata does not stop Ollama or change either store")

    _suite.section("LOGIN WARMUP RESOLVES CURRENT PROFILE GEOMETRY")
    with tempfile.TemporaryDirectory() as box:
        root = Path(box)
        commands = root / "bin"
        commands.mkdir()
        cli = commands / "ailocal"
        cli.write_text('#!/bin/sh\ncase "$5" in\n'
                       'model) echo fixture-model:tag ;;\n'
                       'num_ctx) cat "$FIXTURE_CONTEXT" ;;\n'
                       'keep_alive) echo 5m ;;\nesac\n')
        curl = commands / "curl"
        curl.write_text('#!/bin/sh\nwhile [ "$#" -gt 0 ]; do\n'
                        'if [ "$1" = -d ]; then shift; printf %s "$1" > "$FIXTURE_REQUEST"; exit 0; fi\n'
                        'shift\ndone\nexit 0\n')
        cli.chmod(0o755)
        curl.chmod(0o755)
        with (patch.object(I, "_write_agent") as agent,
              patch.object(I.shutil, "which", return_value=str(cli)),
              patch.object(I.policy, "state_root", return_value=root),
              contextlib.redirect_stdout(io.StringIO())):
            I._write_model_warmup()
            legacy = root / "agents/preload.sh"
            legacy.parent.mkdir()
            legacy.write_text("#!/bin/sh\n# operator script\n")
            I._write_model_warmup()
            check(legacy.exists(), "unmarked legacy warmup files are preserved")
            legacy.write_text("#!/bin/sh\n# Generated by ailocal. Do not edit.\n")
            I._write_model_warmup()
            check(not legacy.exists(), "recognized generated warmup scripts retire after native registration")
        program = agent.call_args.args[1]
        check(program[:2] == ["/bin/sh", "-c"] and not (root / "agents/preload.sh").exists(),
              "native warmup has no executable dependency on disposable state")
        context = root / "context"
        request = root / "request.json"
        env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ["PATH"],
                   FIXTURE_CONTEXT=str(context), FIXTURE_REQUEST=str(request))
        for size in (12345, 23456):
            context.write_text(str(size))
            result = subprocess.run(program, env=env, capture_output=True, text=True, timeout=5)
            check(result.returncode == 0, "generated login warmup executes without a shell error")
            payload = json.loads(request.read_text())
            check(payload == {"model": "fixture-model:tag", "keep_alive": "5m",
                              "options": {"num_ctx": size}},
                  f"login warmup resolves the current model, keep-alive and {size}-token geometry")
        request.unlink()
        context.write_text("not a context size")
        subprocess.run(program, env=env,
                       capture_output=True, text=True, timeout=5, check=True)
        check(not request.exists(), "invalid profile geometry cannot fall back to a server-default load")

    _suite.section("NATIVE RUNTIME ACTIVATION ROLLS BACK ON FAILURE")
    with patch.object(I, "_run", side_effect=[
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="state = running", stderr=""),
            SimpleNamespace(returncode=0, stdout="state = exiting", stderr=""),
            SimpleNamespace(returncode=113, stdout="", stderr="Could not find service fixture")
            ]) as native, patch.object(I.time, "sleep"):
        I._bootout("fixture")
        check(native.call_count == 4,
              "successful bootout waits until launchd actually forgets the draining job")
    with tempfile.TemporaryDirectory() as box, \
            patch.object(I, "LA_DIR", Path(box)), \
            patch.object(I, "LOG_DIR", Path(box) / "logs"), \
            patch.object(I, "_bootout"), \
            patch.object(I, "_run", return_value=SimpleNamespace(
                returncode=5, stdout="", stderr="Bootstrap failed: 5: Input/output error")) as native:
        try:
            I._write_agent("fixture", ["/bin/true"])
            check(False, "registration failure is explicit")
        except OSError:
            check(native.call_count == 1,
                  "bootstrap failure cannot be hidden by legacy load's false zero exit status")
    with tempfile.TemporaryDirectory() as box, \
            patch.object(I, "LA_DIR", Path(box)), \
            patch.object(I, "LOG_DIR", Path(box) / "logs"):
        target = Path(box) / "com.ailocal.ollama.plist"
        target.write_bytes(b"old configuration")
        calls = []
        def stopped(label):
            check(target.read_bytes() == b"old configuration",
                  "the old launch agent stops before its configuration is replaced")
        def launched(*args, **kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(I, "_bootout", side_effect=stopped), patch.object(I, "_run", side_effect=launched):
            I._write_agent("com.ailocal.ollama", ["/bin/true"], keep_alive=True)
        check(any(args[:2] == ("launchctl", "kickstart") for args in calls),
              "a persistent native job is explicitly started after registration")
    from ailocal.checks import services
    with tempfile.TemporaryDirectory() as box, \
            patch.object(I, "LA_DIR", Path(box)), \
            patch.object(I, "LOG_DIR", Path(box) / "logs"), \
            patch.object(I, "_bootout"), \
            patch.object(I, "_ollama_binary_version", return_value="candidate-version"), \
            patch.object(services, "http_json", return_value={"version": "candidate-version"}):
        def pending_start(*args, **kwargs):
            pending = args[:2] == ("launchctl", "kickstart")
            return SimpleNamespace(returncode=3 if pending else 0, stdout="", stderr="start pending" if pending else "")
        with patch.object(I, "_run", side_effect=pending_start):
            I._activate_ollama("fixture-ollama")
        check(plistlib.loads((Path(box) / "com.ailocal.ollama.plist").read_bytes())["ProgramArguments"][0] == "fixture-ollama",
              "a transient start result is settled by the selected daemon's actual version")
    with tempfile.TemporaryDirectory() as box, patch.object(I, "LA_DIR", Path(box)):
        agent = Path(box) / "com.ailocal.ollama.plist"
        desired = {"ProgramArguments": ["fixture-ollama", "serve"],
                   "EnvironmentVariables": I.OLLAMA_ENV, "KeepAlive": True}
        agent.write_bytes(plistlib.dumps(desired))
        with patch.object(I, "_run", return_value=SimpleNamespace(returncode=0, stdout="state = running\n")), \
                patch.object(I, "_ollama_binary_version", return_value="fixture-version"), \
                patch.object(services, "http_json", return_value={"version": "fixture-version"}) as api:
            check(I._ollama_is_current("fixture-ollama"), "an intact healthy managed runtime needs no restart")
            api.return_value = {"version": "different-version"}
            check(not I._ollama_is_current("fixture-ollama"), "a different running version is never treated as current")
            desired["EnvironmentVariables"] = dict(I.OLLAMA_ENV, OLLAMA_MODELS="/different-store")
            agent.write_bytes(plistlib.dumps(desired))
            api.reset_mock()
            check(not I._ollama_is_current("fixture-ollama") and not api.called,
                  "changed native configuration still requires service replacement")

    with tempfile.TemporaryDirectory() as box:
        agent = Path(box) / "com.ailocal.ollama.plist"
        agent.write_bytes(b"previous managed agent")

        def replace_agent(*args, **kwargs):
            agent.write_bytes(b"candidate managed agent")

        with patch.object(I, "LA_DIR", Path(box)), \
                patch.object(I, "_write_agent", side_effect=replace_agent), \
                patch.object(I, "_wait", return_value=False), \
                patch.object(I, "_run", return_value=SimpleNamespace(
                    returncode=0, stdout="Warning: client version is fixture-patched\n"
                    "state = exited\nruns = 1\nlast exit code = 78\nTOKEN = private-value\n")) as native, \
                patch.object(I, "_bootout"):
            try:
                I._activate_ollama("fixture-ollama")
                check(False, "wrong daemon version fails activation")
            except RuntimeError as error:
                check("last exit code = 78" in str(error) and "private-value" not in str(error),
                      "activation reports launchd failure state without exposing its environment")
                check(agent.read_bytes() == b"previous managed agent",
                      "failed activation restores the previous service definition")
                check(any(call.args[:2] == ("launchctl", "bootstrap") for call in native.call_args_list),
                      "failed activation reloads the previous managed service")

    _suite.section("FAILED TEARDOWN DOES NOT CLAIM SUCCESS OR REMOVE LOGIN AGENTS")
    output = io.StringIO()
    with patch.object(runtime, "_confirm", return_value=True), \
            patch.object(runtime, "compose", return_value=SimpleNamespace(returncode=1)), \
            patch.object(I, "remove_login_agents") as remove_agents, \
            patch.object(I, "retire_generated_clients") as retire_clients, \
            contextlib.redirect_stdout(output):
        result = runtime.cmd_teardown([])
    check(result != 0, "Docker removal failure returns a nonzero exit status")
    check(not remove_agents.called, "failed Docker removal preserves login agents")
    check(not retire_clients.called, "failed Docker removal does not retire client files")
    check("Teardown incomplete" in output.getvalue()
          and "Teardown complete." not in output.getvalue(),
          "failed teardown reports an incomplete uninstall with recovery instructions")

    _suite.section("SUCCESSFUL TEARDOWN RETIRES GENERATED PRESETS THROUGH OWNED ROOTS")
    with tempfile.TemporaryDirectory(prefix="teardown-clients-") as root:
        clients_root = Path(root) / "clients"
        codex = clients_root / "codex"
        codex.mkdir(parents=True)
        for name in ("plan", "review", "cyber"):
            (codex / f"{name}.config.toml").write_text(
                '# Generated by ailocal. Do not edit.\nmodel = "old"\n')
        authored = clients_root / "profiles/operator.toml"
        authored.parent.mkdir()
        authored.write_text('operator = true\n')
        with patch.object(runtime, "_confirm", return_value=True), \
             patch.object(runtime, "compose", return_value=SimpleNamespace(returncode=0)) as compose, \
             patch.object(runtime, "_docker") as docker, \
             patch.object(I, "remove_login_agents"), \
             patch.object(I.policy, "deployed_client_root", return_value=clients_root), \
             contextlib.redirect_stdout(io.StringIO()):
            result = runtime.cmd_teardown([])
        check(result == 0 and not list(codex.glob("*.config.toml")),
              "successful teardown removes the retired generated presets")
        check(authored.read_text() == 'operator = true\n',
              "teardown preserves authored configuration")
        check(compose.call_args.args == ("down", "--volumes", "--remove-orphans")
              and not docker.called,
              "Compose owns network removal; teardown does not target a hardcoded network")

    _suite.section("A FRESH INSTALL POPULATES THE CONFIG ROOT, AND ONLY THAT")
    box, cfg, state = _roots()
    I.provision(RESOURCES, cfg, state)

    check(sorted(p.name for p in cfg.iterdir()) == ["profiles"],
          "the config root receives profiles/ and nothing else")
    check((cfg / "profiles" / "64gb.toml").is_file(),
          "config root receives the authored profiles")
    check((cfg / "profiles" / "clients.toml").is_file(),
          "config root receives client policy")
    check((state / I.MANIFEST_NAME).is_file(), "a manifest is recorded")

    _suite.section("AN EDITED PROFILE SURVIVES AN UPGRADE")
    edited = cfg / "profiles" / "64gb.toml"
    edited.write_text(edited.read_text() + "\n# operator edit\n")
    keep = edited.read_text()
    untouched = cfg / "profiles" / "32gb.toml"

    report = I.provision(RESOURCES, cfg, state)
    check("profiles/64gb.toml" in report["preserved"],
          "an edited profile is reported as preserved")
    check(edited.read_text() == keep, "an edited profile is NOT overwritten")
    check(untouched.read_text() == (RESOURCES / "profiles" / "32gb.toml").read_text(),
          "an unedited profile still matches what was shipped")

    report = I.provision(RESOURCES, cfg, state)
    check("profiles/64gb.toml" in report["preserved"],
          "an edit survives a SECOND upgrade (the manifest records what was "
          "shipped, not what is on disk)")
    check(edited.read_text() == keep, "and is still not overwritten")

    _suite.section("--reset-config TAKES THE DEFAULT, AFTER BACKING THE EDIT UP")
    # The whole point of the flag is to override the preservation asserted
    # above, so it is tested immediately after it and against the SAME edit.
    shipped_64 = (RESOURCES / "profiles" / "64gb.toml").read_text()
    report = I.provision(RESOURCES, cfg, state, reset=True)
    check(report["preserved"] == [],
          "reset preserves nothing: an edited file is no longer exempt")
    check(edited.read_text() == shipped_64,
          "the edited profile now matches what was shipped")
    backup = report.get("backup")
    check(backup is not None and (backup / "profiles" / "64gb.toml").is_file(),
          "the operator's version was copied to a backup FIRST",
          repr(backup))
    check(backup is not None
          and (backup / "profiles" / "64gb.toml").read_text() == keep,
          "and the backup holds the edit byte-for-byte, not the default")
    check(not (backup / "profiles" / "32gb.toml").exists(),
          "only files it was about to replace are backed up, not the whole tree")

    report = I.provision(RESOURCES, cfg, state, reset=True)
    check(report.get("backup") is None,
          "a reset with nothing edited creates no backup directory")

    # Restore the edit so the sections below still describe an edited tree.
    edited.write_text(keep)
    I.provision(RESOURCES, cfg, state)

    _suite.section("A DELETED DEFAULT IS REPORTED, NEVER RESURRECTED")
    (cfg / "profiles" / "16gb.toml").unlink()
    report = I.provision(RESOURCES, cfg, state)
    check("profiles/16gb.toml" in report["absent"],
          "a default the operator removed is reported")
    check(not (cfg / "profiles" / "16gb.toml").exists(),
          "and it is not written back")

    I.provision(RESOURCES, cfg, state, reset=True)
    check((cfg / "profiles" / "16gb.toml").is_file(),
          "reset DOES write back a deleted default: the operator asked for the "
          "shipped tree, and a hole is not part of it")
    # That reset also took the default for 64gb.toml, by design. Put the edit
    # back so the sections below still describe an edited tree.
    edited.write_text(keep)
    I.provision(RESOURCES, cfg, state)

    _suite.section("A CORRUPT MANIFEST NEVER LICENSES AN OVERWRITE")
    (state / I.MANIFEST_NAME).write_text("{ not json")
    report = I.provision(RESOURCES, cfg, state)
    check(edited.read_text() == keep,
          "with no provenance, an edited file is still preserved")

    _suite.section("A RETIRED POLICY FILE IS REMOVED, UNLESS IT WAS EDITED")
    stale = cfg / "profiles" / "99gb.toml"
    stale.write_text("# a format or tier we no longer ship\n")
    I.provision(RESOURCES, cfg, state)          # records it as shipped? no: not in source
    check(stale.is_file(),
          "a file the distribution never shipped is left alone")
    stale.unlink()

    _suite.section("THE SOURCE IS THE PACKAGE, NEVER A GUESSED CHECKOUT")
    check(I.distribution_source() == RESOURCES,
          "the distribution is the package's own resource tree")
    check((I.distribution_source() / "profiles" / "64gb.toml").is_file()
          and (I.distribution_source() / "deploy").is_dir()
          and (I.distribution_source() / "clients").is_dir(),
          "every shipped component is reachable from the package alone")
    # A config root pointed at the package's own resources would copy profiles/
    # onto itself, truncating the files it is reading.
    try:
        I.provision(RESOURCES, RESOURCES, state)
        refused = False
    except SystemExit:
        refused = True
    check(refused, "installing a shipped tree over itself is refused")

    _suite.section("EXPLICIT MODEL UPGRADE PRESERVES GEOMETRY AND BACKS UP POLICY")
    from ailocal import policy as P
    with tempfile.TemporaryDirectory() as upgrade_box:
        config = Path(upgrade_box) / 'config'
        shutil.copytree(RESOURCES / 'profiles', config / 'profiles')
        path = config / 'profiles/32gb.toml'
        shipped_arch_input = P.resolve_role('32gb', 'architecture', RESOURCES)['context_input']
        original = path.read_text().replace(f'context_input = {shipped_arch_input}', 'context_input = 65536')
        original = original.replace('[completion]\n', '[completion]\nenabled = false\n')
        original = original.replace('temperature = 0.7', 'temperature = 0.2')
        original = original.replace('disk_gb = 21', 'disk_gb = 16')
        original = original.replace('role = "Unrestricted coding (separate model)"',
                                    'role = "Unrestricted assistant (MLX 9B)"')
        path.write_text('# operator comment\n' + original)
        before = path.read_bytes()
        clients_before = (config / 'profiles/clients.toml').read_bytes()
        authored = P.load_profile('32gb', config)
        shared_total = max(cfg['context_input'] + cfg['max_output'] for role, cfg in authored.items()
                           if role in P.ROLES and cfg['active'] == authored['architecture']['active'])
        result = I.upgrade_model_defaults(RESOURCES, config)
        after = P.load_profile('32gb', config)
        check('32gb' in result['updated'] and (result['backup'] / '32gb.toml').read_bytes() == before,
              'model upgrade retains complete original policy in backup')
        check(after['architecture']['max_output'] == 16384
              and after['architecture']['context_input'] + after['architecture']['max_output'] == shared_total,
              'shared runner aligns to largest authored window, preserving output reserve')
        check(after['implementation']['temperature'] == 0.7
              and after['completion'].get('enabled', True),
              'explicit upgrade adopts shipped sampling and enables the working FIM model')
        shipped32 = P.load_profile('32gb', RESOURCES)
        check(after['disk_gb'] == shipped32['disk_gb'] and after['cyber']['role'] == shipped32['cyber']['role'],
              'explicit upgrade adopts model-describing role text and download footprint')
        check(path.read_text().startswith('# operator comment\n')
              and (config / 'profiles/clients.toml').read_bytes() == clients_before,
              'model upgrade preserves unrelated comments and client routing policy')
        check(not I.upgrade_model_defaults(RESOURCES, config)['updated'],
              'repeating model upgrade is a no-op')
        authored = path.read_text().replace(
            'weaknesses = ["shares its budget with every other chat capability"]\n',
            'weaknesses = [\n  "old",\n]\n# keep this note with top_p\n', 1)
        path.write_text(authored)
        I.upgrade_model_defaults(RESOURCES, config)
        check('# keep this note with top_p\n' in path.read_text()
              and P.load_profile('32gb', config)['implementation']['weaknesses']
                  == P.load_profile('32gb', RESOURCES)['implementation']['weaknesses'],
              'replacing a multi-line value keeps the authored comment that follows it')
        for tier in ('16gb', '32gb'):
            target = config / f'profiles/{tier}.toml'
            target.write_text(target.read_text().replace('temperature = 0.7', 'temperature = 0.3'))
        originals = {t: (config / f'profiles/{t}.toml').read_bytes() for t in ('16gb', '32gb')}
        real_replace, calls = I.os.replace, []
        def flaky(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError(28, 'No space left on device')
            real_replace(src, dst)
        try:
            with patch.object(I.os, 'replace', side_effect=flaky):
                I.upgrade_model_defaults(RESOURCES, config)
            check(False, 'a failed profile write restores every profile already replaced')
        except OSError:
            check(all((config / f'profiles/{t}.toml').read_bytes() == b for t, b in originals.items())
                  and not list((config / 'profiles').glob('*.upgrade-tmp')),
                  'a failed profile write restores every profile already replaced')
        path.write_text(path.read_text().replace('temperature = 0.7', 'temperature = 0.2'))
        (config / 'profiles/128gb.toml').write_text('invalid = [')
        before = path.read_bytes()
        try:
            I.upgrade_model_defaults(RESOURCES, config)
            check(False, 'invalid later tier prevents every model-policy write')
        except P.ProfileError:
            check(path.read_bytes() == before, 'invalid later tier prevents every model-policy write')

    _suite.section("TIER SELECTION NEVER ROUNDS UP")
    for gb, expected in ((8, None), (16, "16gb"), (31, "16gb"), (32, "32gb"),
                         (63, "32gb"), (64, "64gb"), (127, "64gb"),
                         (128, "128gb"), (192, "128gb")):
        got = I.tier_for_memory(gb)
        check(got == expected, f"{gb} GB selects {expected or 'nothing'}", f"got {got}")

    _suite.section("OLLAMA ENVIRONMENT HAS ONE SOURCE")
    # Both launch agents are generated from I.OLLAMA_ENV: the SERVER plist bakes
    # it into the daemon, and com.ailocal.ollama-env publishes it to user shells.
    # They drifted on a real machine -- daemon on KEEP_ALIVE 6h / MAX_LOADED 4
    # while the shell agent still said -1 / 5 -- because a normal install
    # rewrote the first and only `--env-only` rewrote the second. Reading either
    # one then implied a different runtime than the server actually had.
    #
    # Written into a temp LA_DIR through the REAL writer, with launchctl
    # neutralised: this asserts what the installer GENERATES, which is the thing
    # that regressed. It says nothing about what is installed on this machine or
    # what the running daemon holds -- those are separate claims.
    agents = box / "LaunchAgents"
    saved_la, saved_log, saved_run, saved_bootout = (
        I.LA_DIR, I.LOG_DIR, I._run, I._bootout)
    I.LA_DIR, I.LOG_DIR = agents, box / "Logs"
    I._run = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": ""})()
    I._bootout = lambda label: None
    try:
        server = I._write_agent("com.ailocal.ollama", ["/bin/true", "serve"],
                                keep_alive=True, env=I.OLLAMA_ENV)
        I._write_ollama_setenv_agent()
        shell = agents / "com.ailocal.ollama-env.plist"
        check(server.is_file() and shell.is_file(),
              "a normal install writes BOTH the server and shell env agents")
        baked = plistlib.loads(server.read_bytes()).get("EnvironmentVariables", {})
        argv = plistlib.loads(shell.read_bytes())["ProgramArguments"][-1]
        # `launchctl setenv K V; launchctl setenv K V; ...` -> {K: V}
        published = dict(
            part.split()[-2:] for part in argv.split(";") if "setenv" in part)
        for key, want in I.OLLAMA_ENV.items():
            check(baked.get(key) == want,
                  f"server plist carries {key}={want}", f"got {baked.get(key)!r}")
            check(published.get(key) == want,
                  f"shell agent publishes {key}={want}",
                  f"got {published.get(key)!r}")
        check(set(published) == set(I.OLLAMA_ENV) == set(baked),
              "neither artifact carries a key the other lacks",
              f"baked={sorted(baked)} published={sorted(published)}")
    finally:
        I.LA_DIR, I.LOG_DIR = saved_la, saved_log
        I._run, I._bootout = saved_run, saved_bootout

    shutil.rmtree(box, ignore_errors=True)
    sys.exit(_suite.report())


if __name__ == "__main__":
    main()
