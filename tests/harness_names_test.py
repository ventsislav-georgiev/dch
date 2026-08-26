"""Harness (Claude Code) session-name resolution for the picker / --ls-json.

Claude Code writes ~/.claude/sessions/<pid>.json for every live process; the
process's DCH_SESSION env names the dch session it runs inside. dch joins the
two and shows the harness name in the picker and as "harness" in --ls-json.

Cases:
  - named harness session      -> harness name resolved
  - "nameSource":"derived"     -> skipped (auto-titles beat no one)
  - dead pid in the json       -> skipped
  - resolution overhead        -> budget (mean over N runs vs empty HOME)

Run:  python3 tests/harness_names_test.py        (uses ./dch)
"""
import json, os, shutil, signal, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))

OVERHEAD_BUDGET_MS = 25.0  # generous for loaded CI; locally well under 1 ms
N = 30


def spawn_env_proc(session, watch):
    """Long-lived stand-in for a claude process carrying DCH_SESSION.

    Must be a NON-platform binary: macOS redacts the env of platform
    binaries (/bin/sleep etc.) from other processes' KERN_PROCARGS2, which
    is exactly the read dch does. Our own dch binary (--wait, blocking)
    is guaranteed present and readable."""
    env = dict(os.environ)
    env["DCH_SESSION"] = session
    return subprocess.Popen(
        [DCH, "--wait", watch, "--state", "done", "--timeout", "60000"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write_session_json(sessdir, pid, name, derived=False):
    j = {"pid": pid, "sessionId": "x", "cwd": "/", "name": name}
    if derived:
        j["nameSource"] = "derived"
    with open(os.path.join(sessdir, "%d.json" % pid), "w") as f:
        json.dump(j, f, separators=(",", ":"))


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
        for name in ("hn-named", "hn-derived"):
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

        sl = ls_json(home)
        check("named harness session resolved",
              sl.get("hn-named", {}).get("harness") == "my-claude")
        check("derived harness name skipped",
              sl.get("hn-derived", {}).get("harness") == "")
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
