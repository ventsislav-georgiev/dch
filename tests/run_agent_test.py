"""`dch --run` into a Claude Code session goes through --say's gate (#112).

Drives tests/fake_claude.py as `claude` first on PATH, with HOME and the
runtime dir temporary. The fake's `fold` knob folds an Enter read together
with typed text into it, as Claude Code does with a burst: the plain burst
leaves the text unsent, the gate (paste, then Enter once it shows) submits.
Covers: submitted through the gate; a slash command typed raw and submitted;
eaten Enters re-pressed; every Enter eaten (not_submitted), text that never
lands (not_typed), a draft and a dialog (nothing typed): exit 1 with a
`dch: <n>: <reason> — <why>` line; a plain sh session still gets the burst.
On dch-lite: --run into the fake keeps the burst, exit 0.

Run:  python3 tests/run_agent_test.py        (uses ./dch)
"""
import json, os, shutil, stat, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
TAG = str(os.getpid())


def run(*args, timeout=120, env=None):
    return subprocess.run([DCH] + list(args), capture_output=True, text=True,
                          timeout=timeout, env=env)


def until(fn, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    tmp = tempfile.mkdtemp(prefix="dchr")
    home = os.path.join(tmp, "home")
    fc = os.path.join(tmp, "fc")
    bindir = os.path.join(tmp, "bin")
    for d in (home, fc, bindir):
        os.mkdir(d)
    os.makedirs(os.path.join(home, ".claude", "sessions"), mode=0o700)
    p = os.path.join(bindir, "claude")
    shutil.copy(os.path.join(HERE, "fake_claude.py"), p)
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
    for k in list(os.environ):
        if k.startswith("CLAUDE") or k in ("DCH_SESSION", "DCH_SOCKET_DIR",
                                           "DCH_NO_DETECT"):
            del os.environ[k]
    os.environ.update(HOME=home, XDG_RUNTIME_DIR=tmp,
                      XDG_STATE_HOME=os.path.join(tmp, "state"),
                      FAKE_CLAUDE_DIR=fc,
                      PATH=bindir + os.pathsep + os.environ["PATH"])
    os.chdir(tmp)
    names, pids = [], set()
    fails = 0

    def check(msg, cond, detail=None):
        nonlocal fails
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails += 1
            if detail:
                print("       %s" % detail())

    def sess(base, *cmd, **ctl):
        n = "%s%s" % (base, TAG)
        names.append(n)
        with open(os.path.join(fc, n + ".ctl"), "w") as f:
            json.dump(ctl, f)
        subprocess.run([DCH, "--spawn", n, "--size", "100x30"] + list(cmd),
                       capture_output=True, check=True)
        return n

    def events(n):
        try:
            with open(os.path.join(fc, n + ".log")) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def typed(n):
        return [e["text"] for e in events(n) if e["ev"] == "line"]

    def up(n):
        s = until(lambda: [e for e in events(n) if e["ev"] == "start"], 15)
        if s:
            pids.add(s[0]["pid"])
            rec = os.path.join(home, ".claude", "sessions",
                               "%d.json" % s[0]["pid"])
            until(lambda: os.path.exists(rec), 15)
            until(lambda: lite or "auto mode on" in run("--read", n).stdout)
        return s

    lite = False
    try:
        probe = sess("probe", "sh")
        lite = run("--read", probe).returncode == 3
        run("-k", probe)
        names.remove(probe)

        sh = sess("rsh", "sh")
        r = run("--run", sh, "echo", "ran$((40+2))")
        check("plain sh: burst, exit 0, the command ran",
              r.returncode == 0 and r.stderr == "" and
              until(lambda: "ran42" in run("--read", sh).stdout
                    if not lite else True))

        if lite:
            c = sess("rlite", "claude")
            up(c)
            r = run("--run", c, "hello")
            check("lite: claude keeps the burst, exit 0",
                  r.returncode == 0 and until(lambda: typed(c) == ["hello"]))
            return finish(fails, tmp, names, pids)

        # --- the gate: what the burst would leave unsent is submitted -----
        a = sess("ra", "claude", fold=True)
        up(a)
        r = run("--run", a, "do", "the", "thing")
        check("claude: gated, submitted (not folded), exit 0",
              r.returncode == 0 and r.stderr == "" and
              typed(a) == ["do the thing"] and
              not any(e["ev"] == "enter_folded" for e in events(a)))
        check("claude: the plain burst would have folded the Enter",
              run("--send", a, "x\r").returncode == 0 and
              until(lambda: any(e["ev"] == "enter_folded"
                                for e in events(a))))
        run("-k", a)
        names.remove(a)

        sl = sess("rsl", "claude", fold=True)
        up(sl)
        r = run("--run", sl, "/compact")
        check("slash command: typed raw, submitted, exit 0",
              r.returncode == 0 and typed(sl) == ["/compact"])
        # The burst is two pushes ("own", then "\r"): the fake folds the
        # Enter only when both land in one read, which a loaded box splits.
        # Either way it is the burst; the gate would have pasted.
        pastes = sum(e["ev"] == "paste" for e in events(sl))
        r = run("--run", sl, "own", env=dict(os.environ, DCH_SESSION=sl))
        took = until(lambda: any(e["ev"] == "enter_folded"
                                 for e in events(sl)) or "own" in typed(sl))
        check("own session ($DCH_SESSION): plain burst, exit 0",
              r.returncode == 0 and r.stderr == "" and took and
              sum(e["ev"] == "paste" for e in events(sl)) == pastes,
              lambda: "rc=%r stderr=%r pastes_before=%d events=%r" % (
                  r.returncode, r.stderr, pastes, events(sl)))

        fast = dict(os.environ, DCH_AGENT_REENTER_MS="1000",
                    DCH_AGENT_LAND_MS="1000")
        ea = sess("rea", "claude", eat_enters=2)
        up(ea)
        r = run("--run", ea, "eaten", env=fast)
        check("eaten Enters: re-pressed until it took, exit 0",
              r.returncode == 0 and typed(ea) == ["eaten"] and
              sum(e["ev"] == "enter_eaten" for e in events(ea)) == 2)

        # --- failures: exit 1, one `dch: <n>: <reason> — <why>` line ------
        nx = sess("rnx", "claude", eat_enters=99)
        lag = sess("rlag", "claude", lag_ms=4000)
        drf = sess("rdrf", "claude", draft="half typed")
        dlg = sess("rdlg", "claude", dialog=True)
        for n in (nx, lag, drf, dlg):
            up(n)
        for n, code in ((nx, "not_submitted"), (lag, "not_typed"),
                        (drf, "draft"), (dlg, "prompt")):
            r = run("--run", n, "never", "sent", env=fast)
            check("%s: exit 1, `dch: <n>: %s — ...`, nothing submitted"
                  % (code, code),
                  r.returncode == 1 and r.stdout == "" and
                  r.stderr.startswith("dch: %s: %s — " % (n, code)) and
                  r.stderr.count("\n") == 1 and typed(n) == [])
        check("dialog: no Enter answered it",
              not any(e["ev"] == "enter_dialog" for e in events(dlg)))
        check("draft: left as it was",
              "half typed" in run("--read", drf).stdout and
              "never" not in run("--read", drf).stdout)
        return finish(fails, tmp, names, pids)
    except Exception:
        import traceback
        traceback.print_exc()
        finish(fails, tmp, names, pids)
        return 1


def finish(fails, tmp, names, pids):
    for n in names:
        subprocess.run([DCH, "-k", n], capture_output=True)
    time.sleep(0.3)
    for p in pids:          # safety net: only the fakes this test started
        try:
            os.kill(p, 0)
            os.kill(p, 9)
        except OSError:
            pass
    shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print("FAIL: %d --run gate check(s) failed" % fails)
        return 1
    print("PASS: --run gate")
    return 0


if __name__ == "__main__":
    sys.exit(main())
