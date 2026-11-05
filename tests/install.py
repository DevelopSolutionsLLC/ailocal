#!/usr/bin/env python3
"""Install: model sources, launchd services, teardown, config provenance, tier
selection, install parity and client idempotence. Writes only temp roots, except
the idempotence check, which runs `ailocal clients` against the env-selected roots.
"""
import contextlib
import hashlib
import io
import json
import os
import plistlib
import re
import shutil
import sys
import tempfile
import urllib.error
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import REPO, RESOURCES, Suite, run  # noqa: E402
from ailocal import install as I, model_sources as M, policy as P  # noqa: E402

S = Suite("INSTALL")
check, po = S.check, patch.object
OK = NS(returncode=0, stdout="", stderr="")
quiet = lambda: contextlib.redirect_stdout(io.StringIO())  # noqa: E731
body = lambda request: json.loads(request.data)  # noqa: E731


def raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc:
        return True
    return False


@contextlib.contextmanager
def la():
    with tempfile.TemporaryDirectory() as box, po(I, "LA_DIR", Path(box)), po(I, "LOG_DIR", Path(box) / "logs"):
        yield Path(box)


def model_sources():
    S.section("pinned model sources")
    src = {"repository": "fixture/model", "revision": "a" * 40}
    files = {"config.json": b"{}", "model.safetensors": b"fixture-weights"}
    meta = {"sha": src["revision"], "siblings": [
        {"rfilename": n, "size": len(b), "lfs": {"sha256": hashlib.sha256(b).hexdigest()}}
        for n, b in files.items()] + [{"rfilename": "execute.py"}, {"rfilename": "../outside.json"}]}
    hub = lambda url, **k: io.BytesIO(json.dumps(meta).encode() if "/api/models/" in url  # noqa: E731
                                      else files[url.rsplit("/", 1)[-1]])
    with tempfile.TemporaryDirectory() as box, po(M.policy, "state_root", return_value=Path(box)), \
            po(M.policy, "ollama_binary", return_value="ollama"), po(M.urllib.request, "urlopen", side_effect=hub) as fetch:
        root = M.fetch(src)
        fetch.reset_mock()
        M.fetch(src)
        check({p.name for p in root.iterdir()} == set(files) and fetch.call_count == 1,
              "fetch downloads assets only, excluding code and traversal paths, and reuses verified weights")
        meta["sha"] = "b" * 40
        check(raises(ValueError, M.fetch, src), "a changed Hub revision is refused")
        meta["sha"] = src["revision"]
        for runs, load, label in (([NS(returncode=1)], True, "import failure"),
                                  ([NS(returncode=0), NS(returncode=1)], True, "import invisible to the daemon"),
                                  ([NS(returncode=0)] * 2, False, "unloadable import")):
            with po(M.subprocess, "run", side_effect=runs), po(M, "verify_load", return_value=load):
                check(not M.install("fixture:mlx", src) and root.exists() and not M.current("fixture:mlx", src),
                      f"{label} fails, preserves verified assets and leaves no current receipt")
        with po(M.subprocess, "run", return_value=NS(returncode=0)), po(M, "verify_load", return_value=True), \
                po(M, "_digest", return_value="sha256:one"):
            check(M.install("fixture:mlx", src) and not root.exists() and M.current("fixture:mlx", src)
                  and not M.current("fixture:mlx", {**src, "revision": "c" * 40}),
                  "a successful import drops the weight copy and its receipt binds the tag to the pinned revision")
        for dig in ("sha256:other", None):
            with po(M, "_digest", return_value=dig):
                check(not M.current("fixture:mlx", src), f"a replaced or absent tag ({dig}) is stale")
        files["model.safetensors"] = b"corrupt-weights"
        check(raises(ValueError, M.fetch, src) and not (root / "model.safetensors").exists()
              and not list(root.glob("*.partial")), "corrupt weights are refused and the partial file removed")


def native_gguf():
    S.section("native GGUF import and load check")
    native = {"model": "hf.co/fixture/model:weights.gguf", "sha256": "d" * 64, "renderer": "qwen3.5", "parser": "qwen3.5"}
    paired = {**native, "draft_model": "hf.co/fixture/model:head.gguf", "draft_sha256": "f" * 64}
    blobs = M.policy.ollama_model_store() / "blobs"
    target, head, wrong = (blobs / ("sha256-" + c * 64) for c in "dfe")
    calls = []
    show = lambda mf: io.BytesIO(json.dumps({"modelfile": mf, "renderer": "qwen3.5", "parser": "qwen3.5"}).encode())  # noqa: E731
    sent = lambda ep: [b for u, b in calls if u.endswith(ep)]  # noqa: E731
    def daemon(modelfile):
        def answer(request, **k):
            calls.append((request.full_url, body(request)))
            if request.full_url.endswith("/api/show"):
                return show(modelfile(body(request)["model"]))
            return io.BytesIO(b'{"status":"success"}')
        return lambda: po(M.urllib.request, "urlopen", side_effect=answer)
    single = daemon(lambda m: f"FROM {target}\n")
    pair = daemon(lambda m: f"FROM {head}\n" if m == paired["draft_model"] else f"FROM {target}\nDRAFT {head}\n")
    with tempfile.TemporaryDirectory() as box, po(M.policy, "state_root", return_value=Path(box)), \
            po(M.policy, "ollama_binary", return_value="ollama"), po(M, "verify_load", return_value=True), \
            po(M.subprocess, "run", return_value=NS(returncode=0)) as pull, po(M, "_digest", return_value="sha256:n"):
        with single():
            check(M.install("fixture:mtp", native), "a native GGUF recipe loads through Ollama")
            urls = [u for u, _ in calls]
            check(pull.call_args.args[0] == ["ollama", "pull", native["model"]] and sent("/api/create") == [
                {"model": "fixture:mtp", "from": native["model"], "renderer": "qwen3.5", "parser": "qwen3.5", "stream": False}],
                "native pull/create supplies the renderer without conversion")
            # The repository pull's manifest used to linger as a second tag.
            check(sent("/api/delete") == [{"model": native["model"]}]
                  and max(i for i, u in enumerate(urls) if u.endswith("/api/delete"))
                  > max(i for i, u in enumerate(urls) if u.endswith("/api/create")),
                  "the repository pull's manifest is dropped only after the alias verifies")
            check(M.current("fixture:mtp", native) and not M.current("fixture:mtp", {**native, "parser": "other"}),
                  "the receipt binds renderer, parser and weight pin")
            calls.clear()
            check(not M.install("fixture:wrong", {**native, "sha256": "e" * 64}) and not sent("/api/create"),
                  "a different weight digest fails before creating an alias")
            check(not M.install("fixture:dropped", {**paired, "draft_sha256": "d" * 64}), "a create that omits DRAFT fails")
        for hide in ('TEMPLATE """\nFROM {t}\n"""', "FROM {t}", 'LICENSE """\nFROM {t}\n"""'):
            calls.clear()
            with daemon(lambda m: f"FROM {wrong}\n" + hide.format(t=target) + "\n")():
                check(not M.install("fixture:spoofed", native) and not sent("/api/create")
                      and not M.current("fixture:mtp", native),
                      f"a wrong FROM hiding the target in {hide.split()[0]} neither installs nor keeps a legacy receipt")
        calls.clear()
        with pair():
            check(M.install("fixture:paired", paired), "a separate MTP head loads with its target")
            check(sent("/api/create")[0].get("draft_files") == {"draft.gguf": "sha256:" + "f" * 64}
                  and sent("/api/delete") == [{"model": paired["model"]}, {"model": paired["draft_model"]}],
                  "draft_files attaches the pulled head; both repository pulls are dropped")
            check(M.current("fixture:paired", paired) and not M.current("fixture:paired", {**paired, "draft_sha256": "a" * 64})
                  and not M.current("fixture:paired", native), "the receipt binds the head pin, including its removal")
            calls.clear()
            check(not M.install("fixture:bad", {**paired, "draft_sha256": "a" * 64}) and not sent("/api/create"),
                  "a mismatched draft digest fails before alias creation")
    fake = {"modelfile": f'FROM {target}\nTEMPLATE """\nDRAFT {head}\n"""\n', "renderer": "qwen3.5", "parser": "qwen3.5"}
    check(not M._native_matches(fake, paired) and not M._native_matches({**fake, "modelfile": f"FROM {target}\nDRAFT {head}\n"},
                                                                         native),
          "a templated or undeclared DRAFT line cannot impersonate an attached head")

    with po(M.urllib.request, "urlopen", return_value=io.BytesIO(b'{"done":true,"done_reason":"load"}')) as req:
        check(M.verify_load("fixture:mlx") and body(req.call_args.args[0])["messages"] == [] and req.call_args.kwargs["timeout"]
              == 120, "a completed native load passes; the check generates nothing and is bounded")
    for resp in (b"{}", b"[]", b'{"done":false,"done_reason":"load"}', b'{"done":true,"done_reason":"unload"}',
                 b'{"done":true,"done_reason":"load","error":"private detail"}', b"invalid",
                 TimeoutError(), urllib.error.HTTPError("http://x/api/chat", 500, "private detail", {}, None)):
        out, kw = io.StringIO(), {"side_effect": resp} if isinstance(resp, Exception) else {"return_value": io.BytesIO(resp)}
        with po(M.urllib.request, "urlopen", **kw), contextlib.redirect_stdout(out):
            check(not M.verify_load("fixture:mlx") and "private detail" not in out.getvalue(),
                  f"failed load {resp!r:.40} is refused without disclosing the daemon body")


def model_pulls():
    S.section("model pulls")
    src = {"repository": "fixture/model", "revision": "a" * 40}
    def pulls(st, models, roles):
        for o, n, v in ((I, "_has", True), (I, "_out", "list"), (I, "_installed_models", models),
                        (I.policy, "effective_summary", {"roles": roles}), (I.policy, "ollama_binary", "ollama")):
            st.enter_context(po(o, n, return_value=v))
        st.enter_context(quiet())
    for want, have, pull in (("v/m:mlx-4bit", "v/m:mlx-4bit-custom", True), ("v/m:mlx-4bit", "v/m:latest", True),
                             ("v/m:mlx-4bit", "v/m:mlx-4bit", False), ("v/m", "v/m:latest", False), ("v/m:latest", "v/m", False)):
        with contextlib.ExitStack() as st:
            pulls(st, {have: 16.0}, {"implementation": {"model": want, "enabled": True}})
            r = st.enter_context(po(I, "_run", return_value=OK))
            load = st.enter_context(po(M, "verify_load", return_value=True))
            rc = I.pull_models()
        check(r.call_args_list == ([(("ollama", "pull", want), {})] if pull else []) and rc == 0
              and load.call_args.args == (want,), f"{have} {'does not satisfy' if pull else 'satisfies'} {want}; loads verified")
    for current, loads, label in ((True, False, "an unloadable current import fails without re-downloading"),
                                  (False, True, "an import not at the pinned revision is re-imported")):
        with contextlib.ExitStack() as st:
            pulls(st, {"fixture-mlx:latest": 2.0}, {"cyber": {"model": "fixture-mlx", "source": src}})
            st.enter_context(po(M, "current", return_value=current))
            st.enter_context(po(M, "verify_load", return_value=loads))
            imp = st.enter_context(po(M, "install", return_value=True))
            dl = st.enter_context(po(I, "_run"))
            rc = I.pull_models()
        check((rc == 1 and not imp.called and not dl.called) if current else
              (rc == 0 and imp.call_args.args == ("fixture-mlx", src)), label)


def install_flow():
    S.section("cmd_install")
    from ailocal import cli, clients, generation, runtime
    from ailocal.checks import run as checks_run
    def stack(st, verdict=0, required=None, present=False):
        for n in ("require_prerequisites", "configure_ollama_services", "_write_env", "_print_plan", "distribution_source"):
            st.enter_context(po(I, n))
        for obj, n, v in ((I.policy, "required_runtime_patch", required), (I, "_memory_gb", 64), (I, "select_tier", "64gb"),
                          (generation, "main", 0), (runtime, "cmd_start", 0), (clients, "present", present),
                          (checks_run, "main", verdict), (runtime, "compose", None), (clients, "report_missing", None),
                          (I, "provision", {"installed": [], "preserved": [], "absent": [], "retired": []})):
            st.enter_context(po(obj, n, return_value=v))
        st.enter_context(patch("ailocal.ollama_patch.prepare", return_value=Path("/fixture/ollama")))
        return st.enter_context(patch("ailocal.environment.set_ollama_patch"))
    for verdict, required in ((0, None), (1, None), (0, "gemma4-switch-glu")):
        out, order = io.StringIO(), []
        with contextlib.ExitStack() as st:
            persist = stack(st, verdict, required)
            st.enter_context(po(I, "pull_models", side_effect=lambda: order.append("models") or 0))
            st.enter_context(po(I, "_retire_model_warmup", side_effect=lambda: order.append("retire")))
            st.enter_context(contextlib.redirect_stdout(out))
            rc = I.cmd_install(["--yes"])
        check(order == ["retire", "models"] and persist.called == bool(required) and rc == verdict
              and ("proxy is ready" in out.getvalue()) == (verdict == 0),
              f"install (verdict {verdict}, patch {required}) retires warmup first, returns validation, claims readiness only on pass")
    with contextlib.ExitStack() as st:
        stack(st, present=True)
        st.enter_context(po(I, "pull_models", return_value=0))
        st.enter_context(po(I, "_retire_model_warmup"))
        targets = {n: MagicMock() for n in clients.TARGETS}
        st.enter_context(patch.dict(clients._TARGETS, targets))
        shell = st.enter_context(po(clients, "ensure_shell_sourcing"))
        agent = st.enter_context(po(clients, "provision_local_agent"))
        st.enter_context(quiet())
        I.cmd_install(["--yes"])
    check(targets["claude"].called and targets["codex"].called and not targets["vscode"].called and not shell.called
          and sorted(agent.call_args.args[0]) == ["claude", "codex"],
          "--yes provisions and registers the agent MCP in the claude/codex local roots only")
    # e6a9167: an unrecognised answer at the client prompt finished install with exit 0.
    with contextlib.ExitStack() as st:
        stack(st, present=True)
        st.enter_context(po(I, "pull_models", return_value=0))
        st.enter_context(po(I, "_retire_model_warmup"))
        st.enter_context(po(I, "_ask", side_effect=lambda q, d="": "bogus" if q.startswith("Configure which") else "y"))
        validate = checks_run.main
        out = io.StringIO()
        st.enter_context(contextlib.redirect_stdout(out))
        st.enter_context(contextlib.redirect_stderr(io.StringIO()))
        rc = I.cmd_install([])
    check(rc == 1 and not validate.called and "proxy is ready" not in out.getvalue()
          and "Client configuration failed" in out.getvalue(),
          "an unrecognised client answer fails install before validation and claims no readiness")
    with po(I.policy, "ollama_patch_selection", return_value="unknown"), po(I, "require_prerequisites") as prereq:
        check(raises(SystemExit, I.cmd_install, ["--yes"]) and not prereq.called,
              "an unknown runtime selection is rejected before any mutation")
    with tempfile.TemporaryDirectory() as box, po(I.policy, "state_root", return_value=Path(box)):
        with cli._lifecycle_lock():
            held = raises(SystemExit, cli._lifecycle_lock().__enter__)
        with cli._lifecycle_lock():
            check(held, "overlapping lifecycle commands fail fast; the lock is released afterwards")


def launchd():
    S.section("launchd services")
    # #74: the app's com.ollama.ollama override is cleared once, after every owned agent stops.
    for receipt_present, enable_rc in ((True, 0), (True, 1), (False, 0)):
        with la() as box:
            receipt = box / "com.ailocal.ollama.restore.json"
            if receipt_present:
                receipt.write_text(json.dumps({"owner": "ailocal", "schema": 1, "disabled": False}))
            calls = []
            with po(I, "_run", side_effect=lambda *a, **k: calls.append(a) or NS(returncode=enable_rc, stdout="", stderr="")), \
                    po(I, "_bootout", side_effect=lambda label: calls.append(("stop", label))), \
                    patch("ailocal.ollama_patch.remove", return_value=([], [])), quiet():
                I.remove_login_agents()
            enables = [n for n, a in enumerate(calls) if a[:2] == ("launchctl", "enable")]
            stops = [n for n, a in enumerate(calls) if a[0] == "stop"]
            if not receipt_present:
                check(not enables, "teardown with no legacy receipt changes no stock startup setting")
            else:
                check(len(enables) == 1 and enables[0] > max(stops) and receipt.exists() == bool(enable_rc),
                      f"a legacy receipt is cleared after every owned agent stops (enable rc={enable_rc})")
    with la() as box, po(I, "_prepare_model_store"), po(I, "_migrate_model_store"), \
            po(I, "_write_ollama_setenv_agent"), quiet():
        for holder, agent, current in (("", "", False), ("7", "7", False), ("7", "-", False), ("", "", True)):
            calls = []
            with po(I, "_ollama_is_current", return_value=current), po(I, "_port_owner", return_value=(holder, agent)), \
                    po(I, "_activate_ollama") as act, po(I, "_run", side_effect=lambda *a, **k: calls.append(a) or OK):
                failed = raises(RuntimeError, I.configure_ollama_services, binary="fixture-ollama")
            foreign = bool(holder) and holder != agent
            check(failed == foreign and act.called == (not current and not foreign)
                  and not any(a[0] in ("osascript", "pkill") or a[:2] == ("launchctl", "disable") for a in calls),
                  f"holder={holder or 'none'} agent={agent or 'none'} current={current}: launchd only, "
                  "no app quit or disable, a foreign listener refuses")
    with tempfile.TemporaryDirectory() as box, po(I, "OLLAMA_APP", Path(box)), po(I, "_has", return_value=True), \
            po(I, "_run", return_value=OK), quiet():
        check(raises(SystemExit, I.require_prerequisites), "install refuses while Ollama.app exists")
    with po(P.shutil, "which", return_value="/opt/homebrew/bin/ollama"):
        check(P.stock_ollama_binary() == "/opt/homebrew/bin/ollama",
              "the stock binary is the PATH entry, unresolved, so a cask upgrade cannot strand launchd")
    # #76: a cask upgrade deletes the files under the running daemon; the watcher restarts it.
    with la() as box:
        cask = box.resolve() / "Caskroom/ollama-binary"
        (real := cask / "0.40.14/ollama").parent.mkdir(parents=True)
        for exe in (real, real.parent / "llama-server"):
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
        (link := box / "bin/ollama").parent.mkdir()
        link.symlink_to(real)
        with po(I, "_run", return_value=OK), po(I, "_bootout"), quiet():
            I._write_ollama_watch_agent(str(link))
        plist = plistlib.loads((box / "com.ailocal.ollama-watch.plist").read_bytes())
        check(plist["WatchPaths"] == [str(cask)] and plist["StartInterval"] == 300
              and plist["ProgramArguments"][-1] == str(link) and "KeepAlive" not in plist,
              "a cask binary gets a watcher on its Caskroom directory with a periodic backstop")
        script = box / "watch.sh"
        script.write_text("#!/bin/sh\n" + I.OLLAMA_WATCH)
        if shutil.which("shellcheck"):
            lint = I._run("shellcheck", "-S", "warning", "-s", "sh", script, capture=True)
            check(lint.returncode == 0, "the watcher script is shellcheck-clean", lint.stdout[-300:])
        log = box / "launchctl.log"
        (fake_launchctl := box / "launchctl").write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
        fake_launchctl.chmod(0o755)
        for running, restart in ((str(real), False), ("/Applications/Ollama.app/Contents/Resources/ollama", True)):
            (fake_lsof := box / "lsof").write_text(
                '#!/bin/sh\ncase "$*" in *-p*) printf "p7\\nftxt\\nn%s\\n" "$RUNNING" ;; *) echo 7 ;; esac\n')
            fake_lsof.chmod(0o755)
            log.unlink(missing_ok=True)
            done = I._run("/bin/sh", "-c", I.OLLAMA_WATCH, "ollama-watch", link, capture=True, timeout=20,
                          env=dict(os.environ, AILOCAL_LSOF=str(fake_lsof), AILOCAL_LAUNCHCTL=str(fake_launchctl),
                                   RUNNING=running))
            kicked = log.read_text() if log.exists() else ""
            check(done.returncode == 0 and ("kickstart -k" in kicked and "com.ailocal.ollama" in kicked) == restart,
                  f"the watcher {'restarts a daemon on a replaced binary' if restart else 'leaves a current daemon alone'}",
                  done.stdout + done.stderr + kicked)
        link.unlink()
        link.symlink_to(box / "runtimes/ollama")
        with po(I, "_run", return_value=OK), po(I, "_bootout") as bo, quiet():
            I._write_ollama_watch_agent(str(link))
        check(not (box / "com.ailocal.ollama-watch.plist").exists() and bo.called,
              "a binary outside a Caskroom has no watcher")
    with la() as box, po(I, "_bootout") as bo, po(I.policy, "state_root", return_value=box):
        (legacy := box / "agents/preload.sh").parent.mkdir()
        legacy.write_text("#!/bin/sh\n# operator script\n")
        (box / "com.ailocal.preload.plist").write_text("old")
        with quiet():
            I._retire_model_warmup()
        check(not (box / "com.ailocal.preload.plist").exists() and bo.call_args.args == ("com.ailocal.preload",)
              and legacy.exists(), "the login preload agent is retired; an unmarked script is preserved")
        legacy.write_text("#!/bin/sh\n# Generated by ailocal. Do not edit.\n")
        I._retire_model_warmup()
        check(not legacy.exists(), "a generated legacy warmup script is retired")


def activation():
    S.section("native runtime activation")
    from ailocal.checks import services
    states = [OK] + [NS(returncode=0, stdout=f"state = {s}", stderr="") for s in ("running", "exiting")]
    with po(I, "_run", side_effect=states + [NS(returncode=113, stdout="", stderr="Could not find service")]) as r, po(I.time, "sleep"):
        I._bootout("fixture")
        check(r.call_count == 4, "bootout waits until launchd forgets the draining job")
    with la() as box, po(I, "_bootout"):
        with po(I, "_run", return_value=NS(returncode=5, stdout="", stderr="Bootstrap failed: 5")) as r:
            check(raises(OSError, I._write_agent, "fixture", ["/bin/true"]) and r.call_count == 1,
                  "a bootstrap failure is explicit, not hidden by legacy load")
        (target := box / "com.ailocal.ollama.plist").write_bytes(b"old")
        calls, stopped_on = [], []
        with po(I, "_bootout", side_effect=lambda label: stopped_on.append(target.read_bytes())), \
                po(I, "_run", side_effect=lambda *a, **k: calls.append(a) or OK):
            I._write_agent("com.ailocal.ollama", ["/bin/true"], keep_alive=True)
        check(stopped_on == [b"old"] and any(a[:2] == ("launchctl", "kickstart") for a in calls),
              "the old agent stops before replacement; a persistent job is kickstarted")
        (binary := box / "ollama").write_text("")
        want = {"ProgramArguments": [str(binary), "serve"], "EnvironmentVariables": I._ollama_env(), "KeepAlive": True}
        target.write_bytes(plistlib.dumps(want))
        with po(I, "_run", return_value=NS(returncode=0, stdout="state = running\n")), \
                po(I, "_ollama_binary_version", return_value="v"), po(services, "http_json", return_value={"version": "v"}):
            intact = I._ollama_is_current(str(binary))
            target.write_bytes(plistlib.dumps({**want, "EnvironmentVariables": {**I._ollama_env(), "OLLAMA_MODELS": "/x"}}))
            check(intact and not I._ollama_is_current(str(binary)), "an intact runtime needs no restart; changed config does")
            target.write_bytes(plistlib.dumps(want))
            binary.unlink()
            check(not I._ollama_is_current(str(binary)),
                  "a deleted binary needs a restart even when the running version matches (#74)")
        target.write_bytes(b"previous")
        job = "Warning: client version is fixture-patched\nstate = exited\nlast exit code = 78\nTOKEN = private-value\n"
        with po(I, "_write_agent", side_effect=lambda *a, **k: target.write_bytes(b"candidate")), \
                po(I, "_wait", return_value=False), po(I, "_run", return_value=NS(returncode=0, stdout=job)) as r:
            try:
                I._activate_ollama("fixture-ollama")
                err = ""
            except RuntimeError as e:
                err = str(e)
        check("last exit code = 78" in err and "private-value" not in err and target.read_bytes() == b"previous"
              and any(c.args[:2] == ("launchctl", "bootstrap") for c in r.call_args_list),
              "failed activation reports launchd state without its environment and reloads the previous agent")


def ollama_env():
    S.section("Ollama environment")
    # The server and shell agents once drifted (KEEP_ALIVE/MAX_LOADED) because only one was rewritten.
    with la() as box, po(I, "_run", return_value=OK), po(I, "_bootout"):
        for parallel in ("1", "2"):  # NUM_PARALLEL is the operator's measured setting, preserved
            with patch("ailocal.environment.value", return_value=parallel):
                env = I._ollama_env()
                server = I._write_agent("com.ailocal.ollama", ["/bin/true", "serve"], keep_alive=True, env=env)
                I._write_ollama_setenv_agent()
            argv = plistlib.loads((box / "com.ailocal.ollama-env.plist").read_bytes())["ProgramArguments"][-1]
            shell = dict(part.split()[-2:] for part in argv.split(";") if "setenv" in part)
            check(plistlib.loads(server.read_bytes())["EnvironmentVariables"] == env == shell
                  and env.get("OLLAMA_NUM_PARALLEL") == parallel, f"parallel={parallel}: server and shell agents agree")
        with patch("ailocal.environment.value", return_value="3"), po(I, "_prepare_model_store") as prep:
            check(raises(ValueError, I._ollama_env) and raises(ValueError, I.configure_ollama_services, binary="x")
                  and not prep.called, "unsupported parallelism is rejected before service changes")


def teardown():
    S.section("teardown and retired presets")
    from ailocal import runtime
    out = io.StringIO()
    with po(runtime, "_confirm", return_value=True), po(runtime, "compose", return_value=NS(returncode=1)), \
            po(I, "remove_login_agents") as ra, po(I, "retire_generated_clients") as rc, contextlib.redirect_stdout(out):
        result = runtime.cmd_teardown([])
    check(result != 0 and not ra.called and not rc.called and "Teardown incomplete" in out.getvalue()
          and "Teardown complete." not in out.getvalue(), "failed Docker removal fails teardown and keeps agents and clients")
    with tempfile.TemporaryDirectory() as box:
        root, outside = Path(box) / "clients", Path(box) / "outside.toml"
        (codex := root / "codex").mkdir(parents=True)
        gen = '# Generated by ailocal. Do not edit.\nmodel = "old"\n'
        (codex / "plan.config.toml").write_text(gen)
        (codex / "review.config.toml").write_text('model = "operator"\n')
        outside.write_text(gen)
        (codex / "cyber.config.toml").symlink_to(outside)
        with po(runtime, "_confirm", return_value=True), po(runtime, "compose", return_value=NS(returncode=0)) as compose, \
                po(runtime, "_docker") as docker, po(I, "remove_login_agents"), \
                po(I.policy, "deployed_client_root", return_value=root), quiet():
            result = runtime.cmd_teardown([])
            I.retire_generated_clients()
        check(result == 0 and compose.call_args.args == ("down", "--volumes", "--remove-orphans") and not docker.called
              and not (codex / "plan.config.toml").exists() and (codex / "review.config.toml").exists()
              and (codex / "cyber.config.toml").is_symlink() and outside.exists(),
              "teardown leaves networks to Compose and retires generated presets idempotently, keeping authored files and symlinks")


def provenance():
    S.section("config provenance")
    with tempfile.TemporaryDirectory() as box:
        box = Path(box)
        cfg, state = box / "config", box / "state"
        I.provision(RESOURCES, cfg, state)
        check(sorted(p.name for p in cfg.iterdir()) == ["profiles"] and (cfg / "profiles/64gb.toml").is_file()
              and (cfg / "profiles/clients.toml").is_file() and (state / I.MANIFEST_NAME).is_file(),
              "a fresh install writes profiles/ only, and records a manifest")
        check(I.distribution_source() == RESOURCES and raises(SystemExit, I.provision, RESOURCES, RESOURCES, state),
              "the source is the package's resources; installing it over itself is refused")
        src, cfg, state = box / "src", box / "c2", box / "s2"
        (src / "profiles").mkdir(parents=True)
        for n in "abcd":
            (src / f"profiles/{n}.toml").write_text(f"{n} = 1\n")
        I.provision(src, cfg, state)
        edited, keep = cfg / "profiles/a.toml", "a = 'operator'\n"
        edited.write_text(keep)
        for n in (1, 2):  # the manifest records what shipped, so the edit survives repeatedly
            rep = I.provision(src, cfg, state)
            check("profiles/a.toml" in rep["preserved"] and edited.read_text() == keep, f"an edit survives upgrade #{n}")
        rep = I.provision(src, cfg, state, reset=True)
        bk = rep.get("backup")
        check(rep["preserved"] == [] and edited.read_text() == "a = 1\n" and bk is not None
              and (bk / "profiles/a.toml").read_text() == keep and not (bk / "profiles/b.toml").exists(),
              "--reset-config takes the default after backing up only the edited file")
        check(I.provision(src, cfg, state, reset=True).get("backup") is None, "a reset with nothing edited makes no backup")
        (cfg / "profiles/b.toml").unlink()
        rep = I.provision(src, cfg, state)
        check("profiles/b.toml" in rep["absent"] and not (cfg / "profiles/b.toml").exists(), "a deleted default is not resurrected")
        check(I.provision(src, cfg, state, reset=True) and (cfg / "profiles/b.toml").is_file(), "reset restores a deleted default")
        (cfg / "profiles/d.toml").write_text("d = 'operator'\n")
        I.provision(src, cfg, state)
        for n in "cd":
            (src / f"profiles/{n}.toml").unlink()
        rep = I.provision(src, cfg, state)
        check(not (cfg / "profiles/c.toml").exists() and (cfg / "profiles/d.toml").read_text() == "d = 'operator'\n"
              and "profiles/c.toml" in rep["retired"], "a retired file is removed only while it matches what shipped")
        (cfg / "profiles/x.toml").write_text("never shipped\n")
        edited.write_text(keep)
        (state / I.MANIFEST_NAME).write_text("{ not json")
        I.provision(src, cfg, state)
        check(edited.read_text() == keep and (cfg / "profiles/x.toml").is_file(),
              "a corrupt manifest licenses no overwrite; unshipped files are left alone")


def model_upgrade():
    S.section("--upgrade-models and tier selection")
    with tempfile.TemporaryDirectory() as box:
        config = Path(box) / "config"
        shutil.copytree(RESOURCES / "profiles", config / "profiles")
        prof = lambda t: config / f"profiles/{t}.toml"  # noqa: E731
        path, ctx = prof("32gb"), P.resolve_role("32gb", "architecture", RESOURCES)["context_input"]
        text = path.read_text()
        for old, new in ((f"context_input = {ctx}", "context_input = 65536"), ("[completion]\n", "[completion]\nenabled = false\n"),
                         ("temperature = 0.7", "temperature = 0.2"), ("disk_gb = 21", "disk_gb = 16")):
            text = text.replace(old, new)
        path.write_text("# operator comment\n" + text)
        before, routing = path.read_bytes(), prof("clients").read_bytes()
        authored = P.load_profile("32gb", config)
        total = max(c["context_input"] + c["max_output"] for r, c in authored.items()
                    if r in P.ROLES and c["active"] == authored["architecture"]["active"])
        res = I.upgrade_model_defaults(RESOURCES, config)
        after, shipped = P.load_profile("32gb", config), P.load_profile("32gb", RESOURCES)
        arch = after["architecture"]
        check("32gb" in res["updated"] and (res["backup"] / "32gb.toml").read_bytes() == before, "the original policy is backed up")
        check(arch["max_output"] == 16384 and arch["context_input"] + arch["max_output"] == total
              and after["implementation"]["temperature"] == 0.7 and after["completion"].get("enabled", True)
              and after["disk_gb"] == shipped["disk_gb"],
              "the shared runner keeps the largest authored window; shipped sampling, FIM and footprint are adopted")
        check(path.read_text().startswith("# operator comment\n") and prof("clients").read_bytes() == routing
              and not I.upgrade_model_defaults(RESOURCES, config)["updated"],
              "comments and client routing are preserved; a repeat is a no-op")
        for t in ("16gb", "32gb"):
            prof(t).write_text(prof(t).read_text().replace("temperature = 0.7", "temperature = 0.3"))
        originals, real, calls = {t: prof(t).read_bytes() for t in ("16gb", "32gb")}, I.os.replace, []

        def flaky(a, b):
            calls.append(b)
            if len(calls) == 2:
                raise OSError(28, "No space left on device")
            real(a, b)
        with po(I.os, "replace", side_effect=flaky):
            failed = raises(OSError, I.upgrade_model_defaults, RESOURCES, config)
        check(failed and all(prof(t).read_bytes() == b for t, b in originals.items())
              and not list((config / "profiles").glob("*.upgrade-tmp")), "a failed write restores every replaced profile")
    for gb, want in ((8, None), (16, "16gb"), (31, "16gb"), (32, "32gb"), (63, "32gb"), (127, "64gb"), (128, "128gb")):
        check(I.tier_for_memory(gb) == want, f"{gb} GB selects {want}", f"got {I.tier_for_memory(gb)}")


def parity():
    S.section("install parity")
    from ailocal.checks import install_parity as IP
    with tempfile.TemporaryDirectory() as td:
        stale = Path(td) / "ailocal"
        shutil.copytree(REPO / "src/ailocal", stale, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        with po(IP, "installed_root", return_value=stale):
            check(IP.compare(REPO)[0], "a matching installation passes")
            (stale / "extra.py").touch()
            check(not IP.compare(REPO)[0], "an added file is detected (paths are hashed)")
            (stale / "generation.py").write_text((stale / "generation.py").read_text() + "# drift\n")
            ok, why = IP.compare(REPO)
            check(not ok and "STALE" in why.upper() and IP.REMEDIATION == "pipx install --force . && ailocal start",
                  "a stale install is refused; the remedy reinstalls and restarts", why)
        with po(IP, "installed_root", return_value=None):
            check(not IP.compare(REPO)[0], "no installation fails closed")
    check(REPO / "src/ailocal" not in P.deployed_client_root().parents, "generated config drift is a separate concern")


def fingerprint() -> dict:
    """Duplication-sensitive state of the env-selected roots."""
    clients, state, fp = P.deployed_client_root(), P.state_root(), {}
    for p in (state / "litellm/config.yaml", state / "litellm/capabilities.json", clients / "codex/model_catalog.json",
              clients / "claude/settings.json", clients / "codex/config.toml"):
        text = re.sub(r'"generated_at":\s*"[^"]*"', "", p.read_text()) if p.is_file() else "absent"
        fp[str(p)] = hashlib.sha256(text.encode()).hexdigest()
    toml = clients / "codex/config.toml"
    fp["codex_mcp"] = len(re.findall(r"^\[mcp_servers\.", toml.read_text(), re.M)) if toml.is_file() else 0
    cj = clients / "claude/.claude.json"
    fp["claude_mcp_names"] = sorted(json.loads(cj.read_text()).get("mcpServers", {})) if cj.is_file() else []
    if os.environ.get("ZDOTDIR"):  # never read the operator's own ~/.zshrc
        rc = Path(os.environ["ZDOTDIR"]) / ".zshrc"
        fp["zshrc"] = rc.read_text().count("ailocal") if rc.is_file() else 0
    return fp


def idempotence():
    S.section("idempotence: ailocal clients claude codex, twice")
    cmd = [os.environ.get("AILOCAL_PY", sys.executable), "-m", "ailocal.cli", "clients", "claude", "codex"]
    first = run(cmd, timeout=300)
    if check(first.returncode == 0, "the first run succeeds", (first.stdout + first.stderr)[-400:]):
        a = fingerprint()
        second = run(cmd, timeout=300)
        diff = {k: v for k, v in fingerprint().items() if a.get(k) != v}
        check(second.returncode == 0 and not diff, "a second run changes nothing", repr(diff)[:400])


def main() -> int:
    for section in (model_sources, native_gguf, model_pulls, install_flow, launchd, activation, ollama_env,
                    teardown, provenance, model_upgrade, parity, idempotence):
        try:
            section()
        except Exception as exc:  # noqa: BLE001
            S.error(section.__name__, exc)
    return S.report()


if __name__ == "__main__":
    sys.exit(main())
