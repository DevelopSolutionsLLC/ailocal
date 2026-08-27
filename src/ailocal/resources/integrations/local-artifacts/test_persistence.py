#!/usr/bin/env python3
"""Project root resolution and automatic canonical-source persistence."""
import importlib.util, json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PASS = FAIL = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  PASS  {name}")
    else: FAIL += 1; print(f"  FAIL  {name}  {detail}")

def load(env, port):
    e = dict(os.environ)
    for k in ("LOCAL_ARTIFACTS_ROOT", "CLAUDE_PROJECT_DIR"):
        e.pop(k, None)
    e.update(env)
    e.update(LOCAL_ARTIFACTS_PORT=str(port), LOCAL_ARTIFACTS_AUTO_OPEN="0")
    old = dict(os.environ)
    os.environ.clear(); os.environ.update(e)
    spec = importlib.util.spec_from_file_location(f"srv{port}", str(HERE / "server.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    os.environ.clear(); os.environ.update(old)
    return m

print("=== 2: project root resolution ===")
proj = Path(tempfile.mkdtemp(prefix="proj-"))
m1 = load({"CLAUDE_PROJECT_DIR": str(proj)}, 7861)
check("CLAUDE_PROJECT_DIR is authoritative", m1.APPROVED_ROOT == proj.resolve(),
      f"{m1.APPROVED_ROOT}")
check(".artifacts sits under the project root", m1.ARTIFACT_DIR == proj.resolve() / ".artifacts")

override = Path(tempfile.mkdtemp(prefix="ovr-"))
m2 = load({"CLAUDE_PROJECT_DIR": str(proj), "LOCAL_ARTIFACTS_ROOT": str(override)}, 7862)
check("explicit override beats CLAUDE_PROJECT_DIR", m2.APPROVED_ROOT == override.resolve())

cwd = Path(tempfile.mkdtemp(prefix="cwd-"))
os.chdir(cwd)
m3 = load({}, 7863)
check("falls back to cwd when the env var is absent", m3.APPROVED_ROOT == cwd.resolve(),
      f"{m3.APPROVED_ROOT}")
m4 = load({"CLAUDE_PROJECT_DIR": "/nonexistent/nope"}, 7864)
check("a bogus CLAUDE_PROJECT_DIR falls back rather than trusting it",
      m4.APPROVED_ROOT == cwd.resolve(), f"{m4.APPROVED_ROOT}")
os.chdir(HERE)

print("\n=== 3+4: automatic persistence of the canonical source ===")
srv = load({"CLAUDE_PROJECT_DIR": str(proj)}, 7865)
srv.start_http_thread(); srv._http_ready.wait(5)

arch = {"title": "Routing", "nodes": [{"id": "a", "label": "A", "kind": "client"},
        {"id": "b", "label": "B", "kind": "service"}],
        "edges": [{"from": "a", "to": "b", "kind": "request"}]}
ok, msg = srv.publish(title="ailocal architecture", content=json.dumps(arch),
                      fmt="architecture")
check("architecture publishes", ok, msg[:120])
src = proj / ".artifacts" / "ailocal-architecture.architecture.json"
check("source written automatically, no file_path needed", src.exists(), str(src))
check("result reports artifact_id", "artifact_id:" in msg and "ailocal-architecture" in msg)
check("result reports source_path", "source_path:" in msg and str(src) in msg)
check("result reports preview_url", "preview_url:" in msg and "127.0.0.1" in msg)
saved = json.loads(src.read_text())
check("persists the SEMANTIC SPEC, not the rendered SVG",
      saved.get("nodes") and "<svg" not in src.read_text())

ok, msg = srv.publish(title="Flow", content="flowchart LR\n A-->B", fmt="mermaid")
check("mermaid source persisted as .mmd", (proj / ".artifacts" / "flow.mmd").exists())
ok, msg = srv.publish(title="Notes", content="# hi", fmt="markdown")
check("markdown persisted as .md", (proj / ".artifacts" / "notes.md").exists())
ok, msg = srv.publish(title="Dash", content="<h1>x</h1>", fmt="html")
check("html persisted as .html", (proj / ".artifacts" / "dash.html").exists())

print("\n=== 3: artifact identity and updates ===")
arch2 = dict(arch, nodes=arch["nodes"] + [{"id": "c", "label": "C", "kind": "model"}])
arch2["edges"] = arch["edges"] + [{"from": "b", "to": "c", "kind": "inference"}]
ok, msg = srv.publish(title="ailocal architecture v2", content=json.dumps(arch2),
                      fmt="architecture", artifact_id="ailocal-architecture")
check("update by artifact_id reuses the same source path", src.exists() and
      "ailocal-architecture.architecture.json" in msg, msg[:160])
check("content actually changed", len(json.loads(src.read_text())["nodes"]) == 3)
check("no duplicate file created",
      not (proj / ".artifacts" / "ailocal-architecture-2.architecture.json").exists())

# Deterministic identity: the same title resolves to the same file in a FRESH
# process too, so regenerating a diagram next session updates it rather than
# leaving ailocal-architecture-2, -3, -4 behind.
srv2 = load({"CLAUDE_PROJECT_DIR": str(proj)}, 7866)
ok, msg = srv2.publish(title="ailocal architecture", content=json.dumps(arch),
                       fmt="architecture")
check("a fresh process resolves the same title to the same source file",
      "ailocal-architecture.architecture.json" in msg, msg[:160])
check("no -2 duplicate accumulates across sessions",
      not (proj / ".artifacts" / "ailocal-architecture-2.architecture.json").exists())
check("distinct artifact_id keeps distinct artifacts apart",
      srv2.publish(title="ailocal architecture", content=json.dumps(arch),
                   fmt="architecture", artifact_id="second-diagram")[0]
      and (proj / ".artifacts" / "second-diagram.architecture.json").exists())

print("\n=== slug safety ===")
check("path separators cannot escape", srv.slugify("../../etc/passwd") == "etc-passwd",
      srv.slugify("../../etc/passwd"))
check("empty title still yields an id", srv.slugify("") == "artifact")
check("unicode title reduces safely", "/" not in srv.slugify("架构 diagram!"))
check("long title capped", len(srv.slugify("x" * 300)) <= 60)

print("\n=== 12: source survives process exit ===")
probe = subprocess.run([sys.executable, "-c",
    "import sys,json;from pathlib import Path;"
    "p=Path(sys.argv[1])/'.artifacts'/'ailocal-architecture.architecture.json';"
    "print(p.exists(), len(json.loads(p.read_text())['nodes']))", str(proj)],
    capture_output=True, text=True)
check("a separate process sees the persisted source", probe.stdout.strip().startswith("True"),
      probe.stdout.strip())

print("\n=== confinement still enforced ===")
outside = Path(tempfile.mkdtemp(prefix="out-"))
(outside / "x.md").write_text("# no")
ok, msg = srv.publish(title="T", file_path=str(outside / "x.md"))
check("file outside the project root refused", not ok and "outside the approved root" in msg)
link = proj / "escape.md"
if not link.exists(): link.symlink_to(outside / "x.md")
ok, msg = srv.publish(title="T", file_path=str(link))
check("symlink escaping the project root refused", not ok and "outside the approved root" in msg)
(proj / "ok.md").write_text("# yes")
ok, msg = srv.publish(title="T", file_path=str(proj / "ok.md"))
check("file inside the project root still publishes", ok, msg[:120])

for d in (proj, override, cwd, outside):
    shutil.rmtree(d, ignore_errors=True)
print(f"\n{'='*46}\n  PASS {PASS}   FAIL {FAIL}\n{'='*46}")
sys.exit(1 if FAIL else 0)
