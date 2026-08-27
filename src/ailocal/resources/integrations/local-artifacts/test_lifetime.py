#!/usr/bin/env python3
"""Preview-server lifetime: the artifact must outlive the session that drew it.

The defect this suite pins was measured, not guessed. The HTTP listener used to
be a daemon thread inside the MCP stdio process. Claude Code terminates that
process when the session ends (MCP spec: close stdin, then SIGTERM, then
SIGKILL), so every `preview_url` in a transcript started refusing connections
the moment its session ended -- while the artifact itself sat on disk, intact
and unreachable. Reproduced from live state: content written at 13:56, nothing
listening on 7891, `Connection refused`.

So these tests spawn REAL processes and kill them. An in-process server would
prove nothing here: the whole failure was about process boundaries.
"""
import json, os, shutil, subprocess, sys, tempfile, time, urllib.error, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("LIFETIME_TEST_PORT", "7872"))
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  PASS  {name}")
    else:
        FAIL += 1; print(f"  FAIL  {name}  {detail}")


def get(path="/", timeout=3):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                 headers={"Host": f"127.0.0.1:{PORT}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return type(e).__name__, str(e)


def listener_pids():
    r = subprocess.run(["lsof", "-nP", f"-iTCP:{PORT}", "-sTCP:LISTEN"],
                       capture_output=True, text=True)
    return sorted({l.split()[1] for l in r.stdout.splitlines()[1:]})


# One publish, in its own process, which then EXITS -- exactly what a finishing
# Claude Code session does to its MCP server.
PUBLISHER = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location('s', sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
ok, msg = m.publish(title=sys.argv[2], content=sys.argv[3])
print(ok)
sys.stderr.write(msg)
"""

ROOT = Path(tempfile.mkdtemp(prefix="lt-root-"))
STATE = Path(tempfile.mkdtemp(prefix="lt-state-"))
ENV = dict(os.environ, LOCAL_ARTIFACTS_PORT=str(PORT), LOCAL_ARTIFACTS_AUTO_OPEN="0",
           LOCAL_ARTIFACTS_ROOT=str(ROOT), XDG_STATE_HOME=str(STATE),
           LOCAL_ARTIFACTS_IDLE_EXIT="0")   # no reaper except where tested


def publish(title, content, env=None):
    """Publish from a short-lived process and return (returncode, stdout)."""
    r = subprocess.run([sys.executable, "-c", PUBLISHER, str(HERE / "server.py"),
                        title, content],
                       env=env or ENV, cwd=str(HERE), capture_output=True,
                       text=True, timeout=60)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def cleanup():
    for pid in listener_pids():
        subprocess.run(["kill", pid], capture_output=True)
    shutil.rmtree(ROOT, ignore_errors=True)
    shutil.rmtree(STATE, ignore_errors=True)


try:
    for pid in listener_pids():          # a previous run must not mask a failure
        subprocess.run(["kill", pid], capture_output=True)
    time.sleep(0.3)

    print("=== A: the artifact outlives the session that published it ===")
    rc, out, err = publish("Session One", "<h1>drawn by session one</h1>")
    check("publish from a short-lived process succeeds", out == "True", err[:200])
    owners = listener_pids()
    check("a preview server is listening", len(owners) == 1, str(owners))
    code, body = get("/")
    check("viewer served while the publisher is already gone", code == 200, str(code))
    check("it is THIS session's artifact", "Session One" in body, body[:120])
    check("the listener is NOT the publisher (it outlived it)",
          owners and owners[0] not in (str(os.getpid()),), str(owners))

    print("\n=== B: a second session reuses the SAME server ===")
    before = listener_pids()
    rc, out, err = publish("Session Two", "<h1>drawn by session two</h1>")
    check("second session publishes successfully", out == "True", err[:200])
    after = listener_pids()
    check("no second server was started", after == before, f"{before} -> {after}")
    deadline = time.time() + 6          # the watcher polls once a second
    body = ""
    while time.time() < deadline:
        code, body = get("/")
        if "Session Two" in body:
            break
        time.sleep(0.3)
    check("cross-process publish reaches the shared server", "Session Two" in body,
          body[:160])

    print("\n=== C: many sessions, still one server ===")
    for i in range(3):
        publish(f"Concurrent {i}", f"<h1>{i}</h1>")
    check("three more sessions added no servers", listener_pids() == before,
          str(listener_pids()))

    print("\n=== D: no write endpoint was reintroduced ===")
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/publish", method="POST",
        data=json.dumps({"title": "PWNED", "content": "<h1>x</h1>"}).encode(),
        headers={"Content-Type": "text/plain", "Origin": "https://evil.example"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except Exception:
        code = "conn-error"
    check("POST /publish is still not a success", code != 200, f"got {code}")
    _, body = get("/")
    check("and nothing was injected", "PWNED" not in body)

    for pid in listener_pids():
        subprocess.run(["kill", pid], capture_output=True)
    time.sleep(0.5)

    print("\n=== E: process ownership ===")
    # start_new_session=True is the whole reason the viewer survives. Killing the
    # publisher's entire PROCESS GROUP is what proves it: a viewer in that group
    # would die here, and this is precisely what Claude Code does at session end.
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import importlib.util,sys,time;"
         "spec=importlib.util.spec_from_file_location('s',sys.argv[1]);"
         "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
         "print(m.publish(title='Group Kill', content='<h1>g</h1>')[0], flush=True);"
         "time.sleep(120)",
         str(HERE / "server.py")],
        env=ENV, cwd=str(HERE), stdout=subprocess.PIPE, text=True,
        start_new_session=True)          # give it its own group to kill
    holder.stdout.readline()
    owner_before = listener_pids()
    check("viewer is up before the group kill", len(owner_before) == 1, str(owner_before))
    os.killpg(os.getpgid(holder.pid), 15)
    holder.wait(timeout=10)
    time.sleep(0.5)
    check("killing the publisher's whole process group spares the viewer",
          listener_pids() == owner_before, str(listener_pids()))
    check("and it still serves", get("/")[0] == 200)

    # Truly concurrent publishers, started together with no server running: the
    # bind race must produce exactly one survivor, not one viewer per publisher.
    for pid in listener_pids():
        subprocess.run(["kill", pid], capture_output=True)
    time.sleep(0.5)
    racers = [subprocess.Popen([sys.executable, "-c", PUBLISHER,
                                str(HERE / "server.py"), f"Racer {i}", f"<h1>{i}</h1>"],
                               env=ENV, cwd=str(HERE), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
              for i in range(4)]
    outs = [r.communicate(timeout=60) for r in racers]
    check("every racing publisher succeeded", all(o[0].strip() == "True" for o in outs),
          str([o[0].strip() for o in outs]))
    check("the startup race left exactly one viewer", len(listener_pids()) == 1,
          str(listener_pids()))

    # A foreign occupant must never be mistaken for ours.
    for pid in listener_pids():
        subprocess.run(["kill", pid], capture_output=True)
    time.sleep(0.5)
    foreign = subprocess.Popen(
        [sys.executable, "-c",
         "import http.server,socketserver,sys;"
         "h=http.server.SimpleHTTPRequestHandler;"
         f"socketserver.TCPServer.allow_reuse_address=True;"
         f"socketserver.TCPServer(('127.0.0.1',{PORT}),h).serve_forever()"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    rc, out, err = publish("Foreign", "<h1>nope</h1>")
    check("foreign occupant is not mistaken for our viewer", out == "False", out)
    check("and the failure names the port and the override",
          str(PORT) in err and "LOCAL_ARTIFACTS_PORT" in err, err[:200])
    foreign.terminate(); foreign.wait(timeout=10)
    time.sleep(0.5)

    print("\n=== F: an idle server reaps itself ===")
    idle_env = dict(ENV, LOCAL_ARTIFACTS_IDLE_EXIT="2")
    rc, out, err = publish("Ephemeral", "<h1>short lived</h1>", env=idle_env)
    check("publish under a short idle timeout succeeds", out == "True", err[:200])
    check("server came up", len(listener_pids()) == 1, str(listener_pids()))
    deadline = time.time() + 25
    while time.time() < deadline and listener_pids():
        time.sleep(0.5)
    check("idle server exited on its own (no zombie)", listener_pids() == [],
          str(listener_pids()))

    print("\n=== G: reaping never breaks the next publish ===")
    # The full cycle the idle timeout has to survive:
    #   publish -> served -> idle exit -> publish again -> served again.
    code, _ = get("/")
    check("nothing is listening once it reaped", code != 200, str(code))
    rc, out, err = publish("Revived", "<h1>back from the dead</h1>")
    check("the next publish transparently starts a server", out == "True", err[:200])
    check("it returns a usable preview_url",
          "preview_url:" in err and f"127.0.0.1:{PORT}" in err, err[:200])
    revived = listener_pids()
    check("a genuinely NEW viewer process is running", len(revived) == 1, str(revived))
    code, body = get("/")
    check("and serves the new artifact", code == 200 and "Revived" in body, str(code))
    check("the artifact source is on disk regardless",
          any(f.name.endswith(".html") for f in (ROOT / ".artifacts").glob("*")),
          str(list((ROOT / ".artifacts").glob("*"))[:5]))

    print("\n=== H: the viewer dying mid-publish is recovered or reported ===")
    # This window -- server up, then gone before it ingests -- is real (the viewer
    # is shared, so another session's reaper can land in it) but not reachable by
    # timing from outside. Inject the failure instead: the control flow under test
    # is publish()'s, and it is the same flow a real death would drive.
    import importlib.util
    for k, v in ENV.items():
        os.environ[k] = v
    spec = importlib.util.spec_from_file_location("srv_h", str(HERE / "server.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

    real_await = m._await_ingest
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        return False if calls["n"] == 1 else real_await(*a, **kw)

    m._await_ingest = flaky
    ok, msg = m.publish(title="Recovered", content="<h1>recovered</h1>")
    check("a transient ingest failure is recovered, not reported as fine",
          ok and "Artifact published." in msg and "NOT viewable" not in msg, msg[:160])
    check("recovery actually retried the ingest", calls["n"] >= 2, str(calls["n"]))
    check("and the viewer really serves it", "Recovered" in get("/")[1])

    m._await_ingest = lambda *a, **kw: False        # never ingests
    ok, msg = m.publish(title="Honest", content="<h1>honest</h1>")
    check("a persistent failure is reported, not dressed up as success",
          "NOT viewable" in msg and "not responding" in msg, msg[:200])
    check("and it still tells you where the source landed",
          "source_path:" in msg and ".artifacts" in msg, msg[:240])
    m._await_ingest = real_await

finally:
    cleanup()

print(f"\n{'='*46}\n  PASS {PASS}   FAIL {FAIL}\n{'='*46}")
sys.exit(1 if FAIL else 0)
