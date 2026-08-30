"""Harness (Claude Code and Codex) name resolution for picker / --ls-json.

Claude Code writes ~/.claude/sessions/<pid>.json for every live process; the
process's DCH_SESSION env names the dch session it runs inside. dch joins the
two and shows the harness name in the picker and as "harness" in --ls-json.

Cases:
  - named Claude/Codex session -> harness name resolved
  - "nameSource":"derived"     -> skipped (auto-titles beat no one)
  - dead, malformed, empty and wrong-session records -> skipped
  - resolution overhead        -> budget (mean over N runs vs empty HOME)

Run:  python3 tests/harness_names_test.py        (uses ./dch)
"""
import json, os, shutil, signal, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))

OVERHEAD_BUDGET_MS = 25.0  # generous for loaded CI; locally well under 1 ms
N = 30


def spawn_env_proc(session, watch, **extra_env):
    """Long-lived stand-in for a harness process carrying DCH_SESSION.

    Must be a NON-platform binary: macOS redacts the env of platform
    binaries (/bin/sleep etc.) from other processes' KERN_PROCARGS2, which
    is exactly the read dch does. Our own dch binary (--wait, blocking)
    is guaranteed present and readable."""
    env = dict(os.environ)
    env["DCH_SESSION"] = session
    env.update(extra_env)
    return subprocess.Popen(
        [DCH, "--wait", watch, "--state", "done", "--timeout", "60000"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write_session_json(sessdir, pid, name, derived=False):
    j = {"pid": pid, "sessionId": "x", "cwd": "/", "name": name}
    if derived:
        j["nameSource"] = "derived"
    with open(os.path.join(sessdir, "%d.json" % pid), "w") as f:
        json.dump(j, f, separators=(",", ":"))


def write_codex_index(home, records):
    path = os.path.join(home, ".codex", "session_index.jsonl")
    os.makedirs(os.path.dirname(path))
    with open(path, "w") as f:
        for record in records:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
        f.write('{"id":"codex-malformed","thread_name":\n')


def ls_json(home):
    env = dict(os.environ)
    env["HOME"] = home
    out = subprocess.run([DCH, "--ls-json"], env=env,
                         capture_output=True, text=True).stdout
    return {e["name"]: e for e in json.loads(out)}


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1

    tmp = tempfile.mkdtemp()
    os.environ["XDG_RUNTIME_DIR"] = tmp
    home = os.path.join(tmp, "home")
    sessdir = os.path.join(home, ".claude", "sessions")
    os.makedirs(sessdir)
    procs = []
    fails = 0

    def check(msg, cond):
        nonlocal fails
        if cond:
            print("  ok   %s" % msg)
        else:
            fails += 1
            print("  FAIL %s" % msg)

    try:
        # dch sessions to resolve against.
        for name in ("hn-named", "hn-derived", "hn-codex",
                     "hn-codex-empty", "hn-codex-wrong", "hn-codex-stale",
                     "hn-codex-malformed"):
            subprocess.run([DCH, "--spawn", name, "sleep", "60"], check=True)

        p1 = spawn_env_proc("hn-named", "hn-named")
        procs.append(p1)
        write_session_json(sessdir, p1.pid, "my-claude")

        p2 = spawn_env_proc("hn-derived", "hn-derived")
        procs.append(p2)
        write_session_json(sessdir, p2.pid, "hn-derived-7", derived=True)

        p3 = spawn_env_proc("hn-named", "hn-named")
        p3.terminate()
        p3.wait()
        # dead pid json pointing at the same session; must not crash or match
        write_session_json(sessdir, 99999999, "ghost")

        p4 = spawn_env_proc("hn-codex", "hn-codex",
                            CODEX_THREAD_ID="codex-live")
        p5 = spawn_env_proc("hn-codex-empty", "hn-codex-empty",
                            CODEX_THREAD_ID="codex-empty")
        p6 = spawn_env_proc("not-a-session", "hn-codex-wrong",
                            CODEX_THREAD_ID="codex-wrong")
        p7 = spawn_env_proc("hn-codex-malformed", "hn-codex-malformed",
                            CODEX_THREAD_ID="codex-malformed")
        procs.extend((p4, p5, p6, p7))
        write_codex_index(home, [
            {"id": "codex-live", "thread_name": "my-codex"},
            {"id": "codex-empty", "thread_name": ""},
            {"id": "codex-wrong", "thread_name": "wrong-session"},
            {"id": "codex-stale", "thread_name": "ghost-codex"},
        ])

        sl = ls_json(home)
        check("named harness session resolved",
              sl.get("hn-named", {}).get("harness") == "my-claude")
        check("derived harness name skipped",
              sl.get("hn-derived", {}).get("harness") == "")
        check("named Codex session resolved",
              sl.get("hn-codex", {}).get("harness") == "my-codex")
        check("empty Codex title skipped",
              sl.get("hn-codex-empty", {}).get("harness") == "")
        check("wrong-session Codex record skipped",
              sl.get("hn-codex-wrong", {}).get("harness") == "")
        check("stale Codex record skipped",
              sl.get("hn-codex-stale", {}).get("harness") == "")
        check("malformed Codex record skipped",
              sl.get("hn-codex-malformed", {}).get("harness") == "")
        check("alias field untouched",
              sl.get("hn-named", {}).get("alias") == "")

        # --- resolution overhead: --ls-json with jsons vs without ---
        empty_home = os.path.join(tmp, "home-empty")
        os.makedirs(empty_home)

        def mean_ms(h):
            t0 = time.perf_counter()
            for _ in range(N):
                ls_json(h)
            return (time.perf_counter() - t0) / N * 1000.0

        base = mean_ms(empty_home)
        withres = mean_ms(home)
        overhead = withres - base
        print("  info resolution overhead: %.3f ms/run "
              "(%.3f with, %.3f without)" % (overhead, withres, base))
        check("resolution overhead <= %.1f ms" % OVERHEAD_BUDGET_MS,
              overhead <= OVERHEAD_BUDGET_MS)
    finally:
        for p in procs:
            try:
                p.send_signal(signal.SIGKILL)
                p.wait()
            except OSError:
                pass
        os.system("'%s' -kl >/dev/null 2>&1" % DCH)
        shutil.rmtree(tmp, ignore_errors=True)

    if fails == 0:
        print("PASS: harness names resolved.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
