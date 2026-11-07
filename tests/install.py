#!/usr/bin/env python3
"""Install: pinned native weights, config ownership, Ollama activation, loopback binding and
teardown consent. Writes only temp roots and never reaches this machine's Docker or Ollama.
"""
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import RESOURCES, Suite  # noqa: E402
from ailocal import docker_desktop as D, install as I, model_sources as M  # noqa: E402

# No test may reach this machine's Docker Desktop: a real restore restarts it.
_NOWHERE = Path(tempfile.mkdtemp(prefix="ailocal-dd-"))
D.STORE, D.BACKEND, D.RECEIPT = _NOWHERE / "store.json", _NOWHERE / "backend.sock", _NOWHERE / "receipt.json"

S = Suite("INSTALL")
check, po = S.check, patch.object
quiet = lambda: contextlib.redirect_stdout(io.StringIO())  # noqa: E731


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
                return show(modelfile(json.loads(request.data)["model"]))
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


def activation():
    S.section("Ollama activation")
    with tempfile.TemporaryDirectory() as box, po(I, "LA_DIR", Path(box)), po(I, "LOG_DIR", Path(box) / "logs"), \
            po(I, "_bootout"):
        (target := Path(box) / "com.ailocal.ollama.plist").write_bytes(b"previous")
        job = "Warning: client version is fixture-patched\nstate = exited\nlast exit code = 78\nTOKEN = private-value\n"
        with po(I, "_write_agent", side_effect=lambda *a, **k: target.write_bytes(b"candidate")), \
                po(I, "_wait", return_value=False), po(I, "_run", return_value=NS(returncode=0, stdout=job)) as r:
            try:
                I._activate_ollama("fixture-ollama")
                err = ""
            except RuntimeError as e:
                err = str(e)
        restored = target.read_bytes() == b"previous"
    check("last exit code = 78" in err and "private-value" not in err and restored
          and any(c.args[:2] == ("launchctl", "bootstrap") for c in r.call_args_list),
          "a failed activation reloads the previous agent and reports launchd state without its environment",
          f"restored={restored} error={err}")


def loopback():
    S.section("listening addresses (#103)")
    # AILOCAL_BIND may move the proxy, which authenticates; nothing else may follow it.
    check('"${AILOCAL_BIND:-127.0.0.1}:${AILOCAL_LITELLM_PORT:-4000}:4000"'
          in (RESOURCES / "deploy/litellm/compose.yaml").read_text()
          and "127.0.0.1:${AILOCAL_SEARXNG_PORT:-8080}:8080" in (RESOURCES / "deploy/searxng/compose.yaml").read_text()
          and I.OLLAMA_ENV["OLLAMA_HOST"] == "127.0.0.1:11434",
          "the proxy defaults to loopback and only it follows AILOCAL_BIND; SearXNG and Ollama stay on loopback")


def teardown():
    S.section("destructive commands need consent")
    from ailocal import clients, runtime
    # Every removal is patched, so a regression that proceeds is recorded instead of touching this machine.
    with po(runtime, "_confirm", return_value=False), po(runtime, "compose") as compose_call, \
            po(I, "remove_login_agents") as agents, po(clients, "remove_client_writes", return_value=[]) as writes, \
            quiet():
        declined = runtime.cmd_teardown([]), runtime.cmd_stop(["--volumes"])
    check(declined == (1, 1) and not (compose_call.called or agents.called or writes.called),
          "a declined teardown or volume wipe exits 1 and changes nothing", repr(declined))


def main() -> int:
    for section in (native_gguf, provenance, activation, loopback, teardown):
        try:
            section()
        except Exception as exc:  # noqa: BLE001
            S.error(section.__name__, exc)
    return S.report()


if __name__ == "__main__":
    sys.exit(main())
