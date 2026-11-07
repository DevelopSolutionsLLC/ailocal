#!/usr/bin/env python3
"""Install: pinned model sources, launchd services, teardown, Docker Desktop settings and
config ownership. Writes only temp roots and never reaches this machine's Docker or Ollama.
"""
import contextlib
import hashlib
import io
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import RESOURCES, Suite  # noqa: E402
from ailocal import docker_desktop as D, install as I, model_sources as M, policy as P  # noqa: E402

# No test may reach this machine's Docker Desktop: a real restore restarts it.
_NOWHERE = Path(tempfile.mkdtemp(prefix="ailocal-dd-"))
D.STORE, D.BACKEND, D.RECEIPT = _NOWHERE / "store.json", _NOWHERE / "backend.sock", _NOWHERE / "receipt.json"

S = Suite("INSTALL")
check, po = S.check, patch.object
OK = NS(returncode=0, stdout="", stderr="")
quiet = lambda: contextlib.redirect_stdout(io.StringIO())  # noqa: E731
body = lambda request: json.loads(request.data)  # noqa: E731


def q(fn, *a, **k):
    with quiet():
        return fn(*a, **k)


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
            po(M.policy, "ollama_binary", return_value="ollama"), po(M.urllib.request, "urlopen", side_effect=hub):
        root = M.fetch(src)
        fetched = {p.name for p in root.iterdir()}
        meta["sha"] = "b" * 40
        moved = raises(ValueError, M.fetch, src)
        meta["sha"] = src["revision"]
        shutil.rmtree(root)
        files["model.safetensors"] = b"corrupt-weights"
        corrupt = (raises(ValueError, M.fetch, src) and not (root / "model.safetensors").exists()
                   and not list(root.glob("*.partial")))
    check(fetched == {"config.json", "model.safetensors"} and moved and corrupt,
          "fetch takes only the pinned revision's verified assets: no code or traversal paths, "
          "a moved revision and corrupt weights are refused", f"fetched={fetched} moved={moved} corrupt={corrupt}")


def native_gguf():
    S.section("native GGUF aliases bind the reviewed pins (#87)")
    native = {"model": "hf.co/fixture/model:weights.gguf", "sha256": "d" * 64, "renderer": "qwen3.5", "parser": "qwen3.5"}
    paired = {**native, "draft_model": "hf.co/fixture/model:head.gguf", "draft_sha256": "f" * 64}
    visual = {**paired, "projector_model": "hf.co/fixture/model:mmproj.gguf", "projector_sha256": "c" * 64}
    blobs = M.policy.ollama_model_store() / "blobs"
    target, head, wrong, proj = (blobs / ("sha256-" + c * 64) for c in "dfec")
    show = lambda mf: io.BytesIO(json.dumps({"modelfile": mf, "renderer": "qwen3.5", "parser": "qwen3.5"}).encode())  # noqa: E731

    def daemon(modelfile):
        def answer(request, **k):
            if request.full_url.endswith("/api/show"):
                return show(modelfile(body(request)["model"]))
            return io.BytesIO(b'{"status":"success"}')
        return po(M.urllib.request, "urlopen", side_effect=answer)
    # The publisher's pull carries its own projector (`wrong`); the reviewed one is pulled as its own
    # tag, whose manifest lists it as model and projector, and the alias shows FROM, DRAFT, FROM.
    trio = lambda alias: lambda m: (f"FROM {proj}\nFROM {proj}\n" if m == visual["projector_model"]  # noqa: E731
                                    else f"FROM {head}\n" if m == visual["draft_model"]
                                    else f"FROM {target}\nFROM {wrong}\n" if m == visual["model"] else alias)
    single = lambda m: f"FROM {target}\n"  # noqa: E731
    pair = lambda m: f"FROM {head}\n" if m == paired["draft_model"] else f"FROM {target}\nDRAFT {head}\n"  # noqa: E731
    cases = {"reviewed target, head and projector": ("fixture:ok", visual, trio(f"FROM {target}\nDRAFT {head}\nFROM {proj}\n")),
             "different weight digest": ("fixture:w", {**native, "sha256": "e" * 64}, single),
             "wrong FROM hiding the target in TEMPLATE": ("fixture:t", native,
                                                          lambda m: f'FROM {wrong}\nTEMPLATE """\nFROM {target}\n"""\n'),
             "mismatched draft digest": ("fixture:d", {**paired, "draft_sha256": "a" * 64}, pair),
             "create that omits DRAFT": ("fixture:o", {**paired, "draft_sha256": "d" * 64}, single),
             "mismatched projector digest": ("fixture:p", {**visual, "projector_sha256": "a" * 64},
                                             trio(f"FROM {target}\nDRAFT {head}\nFROM {proj}\n")),
             "publisher's projector instead of the reviewed one": ("fixture:u", visual,
                                                                  trio(f"FROM {target}\nDRAFT {head}\nFROM {wrong}\n"))}
    got = {}
    with tempfile.TemporaryDirectory() as box, po(M.policy, "state_root", return_value=Path(box)), \
            po(M.policy, "ollama_binary", return_value="ollama"), po(M, "verify_load", return_value=True), \
            po(M.subprocess, "run", return_value=NS(returncode=0)), po(M, "_digest", return_value="sha256:n"):
        for label, (tag, recipe, modelfile) in cases.items():
            with daemon(modelfile), quiet():
                got[label] = bool(M.install(tag, recipe)) and bool(M.current(tag, recipe))
    want = {label: label.startswith("reviewed") for label in cases}
    check(got == want, "only an alias whose weights, head and projector match the reviewed digests installs",
          repr({k: v for k, v in got.items() if v != want[k]}))


def install_flow():
    S.section("cmd_install")
    from ailocal import clients, generation, runtime
    from ailocal.checks import run as checks_run
    outcomes = {}
    for verdict in (0, 1):
        out = io.StringIO()
        with contextlib.ExitStack() as st:
            for n in ("require_prerequisites", "configure_ollama_services", "_write_env", "_print_plan",
                      "distribution_source", "_retire_model_warmup"):
                st.enter_context(po(I, n))
            for obj, n, v in ((I.policy, "required_runtime_patch", None), (I.policy, "ollama_patch_selection", ""),
                              (I, "_memory_gb", 64), (I, "select_tier", "64gb"), (I, "pull_models", 0),
                              (generation, "main", 0), (runtime, "cmd_start", 0), (clients, "present", False),
                              (checks_run, "main", verdict), (runtime, "compose", None), (clients, "report_missing", None),
                              (I, "provision", {"installed": [], "preserved": [], "absent": [], "retired": []}),
                              (I, "migrate_profile_roles", {"migrated": [], "backup": None, "conflicts": {}, "dropped": {},
                                                            "kept_fast": [], "thinking_off": []})):
                st.enter_context(po(obj, n, return_value=v))
            st.enter_context(patch("ailocal.ollama_patch.prepare", return_value=Path("/fixture/ollama")))
            st.enter_context(patch("ailocal.environment.set_ollama_patch"))
            st.enter_context(contextlib.redirect_stdout(out))
            outcomes[verdict] = (I.cmd_install(["--yes"]), "proxy is ready" in out.getvalue())
    check(outcomes == {0: (0, True), 1: (1, False)},
          "install returns the validation verdict and claims readiness only when it passes", repr(outcomes))


def launchd():
    S.section("launchd services")
    from ailocal import runtime
    wrong = {}
    with la(), po(I, "_prepare_model_store"), po(I, "_migrate_model_store"), \
            po(I, "_write_ollama_setenv_agent"), quiet():
        for holder, agent, current in (("", "", False), ("7", "7", False), ("7", "-", False), ("", "", True)):
            calls = []
            with po(I, "_ollama_is_current", return_value=current), po(I, "_port_owner", return_value=(holder, agent)), \
                    po(I, "_activate_ollama") as act, po(I, "_run", side_effect=lambda *a, **k: calls.append(a) or OK):
                failed = raises(RuntimeError, I.configure_ollama_services, binary="fixture-ollama")
            foreign = bool(holder) and holder != agent
            if not (failed == foreign and act.called == (not current and not foreign)
                    and not any(a[0] in ("osascript", "pkill") or a[:2] == ("launchctl", "disable") for a in calls)):
                wrong[(holder, agent, current)] = (failed, act.called, calls)
    check(not wrong, "Ollama is managed through launchd only: a foreign listener on its port is refused and "
          "the app is never quit or disabled", repr(wrong)[:400])

    with la() as box, po(I, "_bootout"):
        (target := box / "com.ailocal.ollama.plist").write_bytes(b"previous")
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
              "failed activation reloads the previous agent and reports launchd state without its environment")

    # #103: AILOCAL_BIND may move the proxy off loopback; nothing else follows it.
    compose = (RESOURCES / "deploy/litellm/compose.yaml").read_text()
    check('"${AILOCAL_BIND:-127.0.0.1}:${AILOCAL_LITELLM_PORT:-4000}:4000"' in compose
          and "127.0.0.1:${AILOCAL_SEARXNG_PORT:-8080}:8080" in (RESOURCES / "deploy/searxng/compose.yaml").read_text()
          and I.OLLAMA_ENV["OLLAMA_HOST"] == "127.0.0.1:11434",
          "only the proxy's mapping follows AILOCAL_BIND; SearXNG and Ollama stay on loopback (#103)")

    with po(runtime, "_confirm", return_value=False), po(runtime, "compose") as compose_call, \
            po(I, "remove_login_agents") as agents, quiet():
        declined = runtime.cmd_teardown([]), runtime.cmd_stop(["--volumes"])
    check(declined == (1, 1) and not compose_call.called and not agents.called,
          "a declined or unanswered teardown or volume wipe exits 1 and changes nothing", repr(declined))


def docker_settings():
    S.section("ailocal docker apply|restore against a scratch settings file (#105)")
    ledger = D.recommended()
    with tempfile.TemporaryDirectory() as box:
        box = Path(box)
        store, receipt = box / "settings-store.json", box / "LaunchAgents" / "restore.json"
        original = {"AnalyticsEnabled": True, "ExtensionsEnabled": True, "PortBindingBehavior": "default-port-binding",
                    "MemoryMiB": 8192}
        for _, key, want, _ in ledger:
            original.setdefault(key, want)
        store.write_text(json.dumps(original))

        def backend(timeout=5):  # what Docker would report: the store
            data, out = json.loads(store.read_text()), {}
            for path, key, _, _ in ledger:
                node = out
                for part in path[:-1]:
                    node = node.setdefault(part, {})
                node[path[-1]] = {"value": data.get(key)}
            return out

        restarts = []
        with po(D, "STORE", store), po(D, "RECEIPT", receipt), po(D, "effective", backend), \
                po(D, "_desktop", lambda action: restarts.append(action) or True), \
                po(D, "_wait", lambda up, seconds=180: True):
            rc_apply = q(D.main, ["apply", "--yes"])
            after = json.loads(store.read_text())
            drifted = D.drift(backend())
            record = json.loads(receipt.read_text()) if receipt.exists() else {}
            private = receipt.exists() and oct(receipt.stat().st_mode & 0o777) == "0o600"
            rc_restore = q(D.main, ["restore"])
            final = json.loads(store.read_text())
    recorded = {"AnalyticsEnabled": True, "ExtensionsEnabled": True, "PortBindingBehavior": "default-port-binding"}
    check(rc_apply == 0 and not drifted and after["MemoryMiB"] == 8192 and restarts[:2] == ["stop", "start"]
          and record.get("settings") == recorded and private
          and rc_restore == 0 and {k: final[k] for k in recorded} == recorded and not receipt.exists(),
          "apply records the operator's originals privately and changes only the ledger's keys; "
          "restore puts the originals back", f"apply={rc_apply} restore={rc_restore} record={record}")


def provenance():
    S.section("config ownership")
    with tempfile.TemporaryDirectory() as box:
        box = Path(box)
        src, cfg, state = box / "src", box / "config", box / "state"
        (src / "profiles").mkdir(parents=True)
        for n in "abcd":
            (src / f"profiles/{n}.toml").write_text(f"{n} = 1\n")
        I.provision(src, cfg, state)
        edited, keep = cfg / "profiles/a.toml", "a = 'operator'\n"
        edited.write_text(keep)
        survived = all(edited.read_text() == keep and "profiles/a.toml" in I.provision(src, cfg, state)["preserved"]
                       for _ in range(2))  # the manifest records what shipped, so the edit survives repeatedly
        (cfg / "profiles/d.toml").write_text("d = 'operator'\n")
        for n in "cd":
            (src / f"profiles/{n}.toml").unlink()
        I.provision(src, cfg, state)
        retired = not (cfg / "profiles/c.toml").exists() and (cfg / "profiles/d.toml").read_text() == "d = 'operator'\n"
        (cfg / "profiles/x.toml").write_text("never shipped\n")
        (state / I.MANIFEST_NAME).write_text("{ not json")
        I.provision(src, cfg, state)
        corrupt = edited.read_text() == keep and (cfg / "profiles/x.toml").is_file()
    check(survived and retired and corrupt,
          "provisioning replaces or removes a config file only while it matches what shipped; "
          "a corrupt manifest licenses no overwrite", f"survived={survived} retired={retired} corrupt={corrupt}")


def old_profile(tier: str) -> str:
    """A 0.15-format profile built from the shipped one: [primary] becomes the three retired
    roles with their old windows, plus a [fast] section on every tier."""
    text = (RESOURCES / f"profiles/{tier}.toml").read_text()
    has_fast = re.search(r"(?m)^\[fast\]$", text) is not None
    start = text.index("\n[primary]") + 1
    end = min(i for i in (text.find("\n[fast]\n", start), text.find("\n[completion]", start)) if i > 0)
    primary = text[start:end]
    ci, mo = (int(re.search(rf"(?m)^{k} = (\d+)$", primary).group(1)) for k in ("context_input", "max_output"))
    # 0.15's implementation and fast kept the primary's total with an 8192 reserve.
    split = f"context_input = {ci + mo - 8192}\nmax_output = 8192"
    fast = primary.replace("[primary]", "[fast]").replace(f"context_input = {ci}\nmax_output = {mo}", split)
    impl = primary.replace("[primary]", "[implementation]").replace(f"context_input = {ci}\nmax_output = {mo}", split)
    arch = primary.replace("[primary]", "[architecture]").replace("reasoning = false", "reasoning = true", 1)
    old = text[:start] + arch + "\n" + impl + "\n" + primary.replace("[primary]", "[review]") + "\n" + text[end:]
    if not has_fast:
        old = old.replace("\n[completion]", "\n" + fast + "\n[completion]", 1)
    return old


def role_migration():
    S.section("profile role migration (#89)")
    with tempfile.TemporaryDirectory() as box:
        config = Path(box) / "config"
        (config / "profiles").mkdir(parents=True)
        shutil.copy(RESOURCES / "profiles/clients.toml", config / "profiles/clients.toml")
        prof = lambda t: config / f"profiles/{t}.toml"  # noqa: E731
        for t in P.TIERS:
            prof(t).write_text(old_profile(t))
        # 64gb: the operator edited [review] alone -- two operator values, no rule to choose.
        text = prof("64gb").read_text()
        i = text.index("\n[review]\n") + 1
        j = text.index("\n[", i)
        prof("64gb").write_text(text[:i] + re.sub(r"(?m)^temperature = .*$", "temperature = 0.3", text[i:j], count=1)
                                + text[j:])
        originals = {t: prof(t).read_bytes() for t in P.TIERS}
        res = I.migrate_profile_roles(RESOURCES, config)
        arch = P.load_profile("64gb", RESOURCES)["primary"]["temperature"]
        check(res["migrated"] == [] and res["backup"] is None
              and res["conflicts"] == {"64gb": [("temperature", {"architecture": arch, "review": 0.3})]}
              and all(prof(t).read_bytes() == b for t, b in originals.items()),
              "a retired role carrying its own operator edit is reported and nothing is written for any tier",
              repr({k: v for k, v in res.items() if k != "backup"}))


def main() -> int:
    for section in (model_sources, native_gguf, install_flow, launchd, docker_settings, provenance, role_migration):
        try:
            section()
        except Exception as exc:  # noqa: BLE001
            S.error(section.__name__, exc)
    return S.report()


if __name__ == "__main__":
    sys.exit(main())
