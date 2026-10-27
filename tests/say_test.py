"""`dch --say`: one gated message to the Claude Code sessions (#045).

Drives tests/fake_claude.py as `claude` first on PATH, with HOME and the
runtime dir temporary so no real ~/.claude or session is touched. Covers:
sent (typed and submitted), busy, a dialog (nothing typed, no Enter), a
draft (left as it was), not_claude (a plain sh session), self
(DCH_SESSION), a Claude typed into a shell, explicit names vs --all,
no_session, an old master, the --json shape (pure NDJSON), the re-pressed
Enter when one is eaten, not_submitted when every Enter is eaten, exit
codes, usage errors, and concurrency: several slow sessions cost about the
slowest one, and a fast one reports first. On dch-lite: every Claude session
is skipped/no_screen, nothing typed, exit 0.

Run:  python3 tests/say_test.py        (uses ./dch)
"""
import json, os, shutil, socket, stat, subprocess, sys, tempfile
import threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
TAG = str(os.getpid())
KEYS = ["detail", "name", "reason", "result"]


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


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def lines(out):
    """Text mode: {name: rest of its line}, and the number of lines."""
    ls = [l for l in out.splitlines() if l]
    return dict(l.split(": ", 1) for l in ls), len(ls)


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    tmp = tempfile.mkdtemp(prefix="dchv")
    home = os.path.join(tmp, "home")
    fc = os.path.join(tmp, "fc")
    bindir = os.path.join(tmp, "bin")
    for d in (home, fc, bindir):
        os.mkdir(d)
    os.makedirs(os.path.join(home, ".claude", "sessions"), mode=0o700)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    p = os.path.join(bindir, "claude")
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
    for k in list(os.environ):
        if k.startswith("CLAUDE") or k in ("DCH_SESSION", "DCH_SOCKET_DIR",
                                           "DCH_NO_DETECT"):
            del os.environ[k]
    os.environ.update(HOME=home, XDG_RUNTIME_DIR=tmp,
                      XDG_STATE_HOME=os.path.join(tmp, "state"),
                      FAKE_CLAUDE_DIR=fc,
                      PATH=bindir + os.pathsep + os.environ["PATH"])
    sockdir = os.path.join(tmp, "dch-%d" % os.getuid())
    os.chdir(tmp)
    names, pids = [], set()
    fails = 0

    def check(msg, cond):
        nonlocal fails
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails += 1

    def setctl(n, **ctl):
        with open(os.path.join(fc, n + ".ctl.tmp"), "w") as f:
            json.dump(ctl, f)
        os.rename(os.path.join(fc, n + ".ctl.tmp"),
                  os.path.join(fc, n + ".ctl"))

    def sess(base, *cmd, **ctl):
        n = "%s%s" % (base, TAG)
        names.append(n)
        setctl(n, **ctl)
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
            until(lambda: lite or "auto mode on" in run("--read", n).stdout)
        return s

    def idle(n):
        """The fake's turn is over (it paints "Working" while busy)."""
        return until(lambda: "Working" not in run("--read", n).stdout, 15)

    def say(*args, env=None):
        return run("--say", *args, env=env)

    lite = False
    try:
        probe = sess("probe", "sh")
        lite = run("--read", probe).returncode == 3
        run("-k", probe)
        names.remove(probe)

        # --- usage: exit 1, nothing on stdout ----------------------------
        for args, why in (([], "nothing"), (["x"], "no --"),
                          (["--", "hi"], "no names, no --all"),
                          (["x", "--all", "--", "hi"], "names and --all"),
                          (["x", "--"], "empty text"),
                          (["x", "--", " ", ""], "blank text"),
                          (["x", "-q", "--", "hi"], "unknown flag")):
            r = say(*args)
            check("usage error (%s): exit 1, stdout empty" % why,
                  r.returncode == 1 and r.stdout == "" and r.stderr)

        if lite:
            c = sess("lc", "claude")
            s = sess("ls", "sh")
            up(c)
            r = say("--all", "--", "hello", "there")
            got, n = lines(r.stdout)
            check("lite: claude session skipped/no_screen, sh not_claude, "
                  "exit 0", r.returncode == 0 and n == 2 and
                  got.get(c) == "skipped — the composer is not on screen"
                  and got.get(s) == "skipped — no claude runs in the "
                  "session")
            r = say(c, "nosuch" + TAG, "--json", "--", "hi")
            objs = [json.loads(l) for l in r.stdout.splitlines()]
            check("lite: --json no_screen + no_session, exit 1",
                  r.returncode == 1 and
                  sorted((o["reason"], o["result"]) for o in objs) ==
                  [("no_screen", "skipped"), ("no_session", "failed")])
            check("lite: nothing typed", typed(c) == [])
            return finish(fails, tmp, names, pids)

        # --- --all: one result per session -------------------------------
        a = sess("sa", "claude")
        b = sess("sb", "claude")
        busy = sess("sbusy", "claude", busy=True)
        dlg = sess("sdlg", "claude", dialog=True)
        drf = sess("sdrf", "claude", draft="half typed")
        me = sess("sme", "claude")
        sh = sess("ssh", "sh")
        shc = sess("sshc", "sh")
        run("--run", shc, "claude")
        for n in (a, b, busy, dlg, drf, me, shc):
            up(n)
        r = say("--all", "--", "resume", "work,", "limits are back",
                env=dict(os.environ, DCH_SESSION=me))
        print("     | " + r.stdout.rstrip().replace("\n", "\n     | "))
        got, n = lines(r.stdout)
        msg = "resume work, limits are back"
        check("--all: exit 0 (skips are not failures)", r.returncode == 0)
        check("--all: exactly one line per session", n == 8 and
              set(got) == {a, b, busy, dlg, drf, me, sh, shc})
        check("sent: text arrives and is submitted",
              got.get(a) == "sent" and got.get(b) == "sent" and
              typed(a) == [msg] and typed(b) == [msg] and
              until(lambda: any(e["ev"] == "turn" and e["text"] == msg
                                for e in events(a))))
        check("busy: skipped, nothing typed",
              got.get(busy) == "skipped — claude is busy" and
              typed(busy) == [])
        check("dialog: skipped, nothing typed, no Enter on it",
              got.get(dlg) == "skipped — a prompt or dialog is waiting "
              "for you" and not any(e["ev"] in ("line", "enter_dialog")
                                    for e in events(dlg)))
        setctl(dlg)
        check("dialog: no text left behind in the composer",
              until(lambda: "Try" in run("--read", dlg).stdout) and
              "resume" not in run("--read", dlg).stdout)
        check("draft: skipped, draft untouched",
              got.get(drf) == "skipped — the composer holds an unsent "
              "draft" and typed(drf) == [] and
              "half typed" in run("--read", drf).stdout and
              "resume" not in run("--read", drf).stdout)
        check("self (DCH_SESSION): skipped, nothing typed",
              got.get(me) == "skipped — the session you are in" and
              typed(me) == [])
        check("plain sh: skipped/not_claude",
              got.get(sh) == "skipped — no claude runs in the session")
        check("claude typed into a shell: sent",
              got.get(shc) == "sent" and typed(shc) == [msg])

        # --- explicit names; --json ---------------------------------------
        idle(a)
        idle(b)
        r = say(b, "--", "only", "b")
        check("explicit name: only that session, one line, exit 0",
              r.returncode == 0 and r.stdout == "%s: sent\n" % b and
              typed(b) == [msg, "only b"] and typed(a) == [msg])
        idle(b)
        r = say(a, busy, "nosuch" + TAG, sh, "--json", "--", "json", "msg")
        objs = []
        try:
            objs = [json.loads(l) for l in r.stdout.splitlines()]
        except ValueError:
            pass
        check("--json: every stdout line is one JSON object, one per name",
              len(objs) == 4 == len(r.stdout.splitlines()) and
              all(isinstance(o, dict) and sorted(o) == KEYS for o in objs))
        by = {o.get("name"): o for o in objs if isinstance(o, dict)}
        check("--json: results and reasons",
              by.get(a) == {"name": a, "result": "ok", "reason": "",
                            "detail": ""} and
              by.get(busy, {}).get("result") == "skipped" and
              by[busy]["reason"] == "busy" and by[busy]["detail"] and
              by.get(sh, {}).get("reason") == "not_claude" and
              by.get("nosuch" + TAG, {}).get("result") == "failed" and
              by["nosuch" + TAG]["reason"] == "no_session")
        check("--json: a failure exits 1", r.returncode == 1)
        check("--json: the sent one got it", typed(a)[-1:] == ["json msg"])
        for n in (busy, dlg, drf, me, sh, shc):
            run("-k", n)
            names.remove(n)

        # --- old master: failed/old_master -------------------------------
        old = os.path.join(sockdir, "old%s.sock" % TAG)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(old)
        srv.listen(8)

        def oldmaster():
            # predates MSG_CMD: answers every verb with DCH_ST_UNSUP
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                conn.settimeout(2)
                try:
                    if conn.recv(4096):
                        conn.sendall(bytes([11, 5, 0]))
                except OSError:
                    pass
                conn.close()
        threading.Thread(target=oldmaster, daemon=True).start()
        r = say("old" + TAG, "--json", "--", "hi")
        srv.close()
        os.unlink(old)
        check("old master: failed/old_master, exit 1",
              r.returncode == 1 and
              json.loads(r.stdout or "{}").get("reason") == "old_master")

        # --- eaten Enters: pressed again; all eaten: not_submitted -------
        ea = sess("seat", "claude", eat_enters=2)
        nx = sess("snx", "claude", eat_enters=99)
        up(ea)
        up(nx)
        fast = dict(os.environ, DCH_AGENT_REENTER_MS="1000")
        r = say(ea, nx, "--", "eaten", env=fast)
        got, n = lines(r.stdout)
        check("eaten Enters: re-pressed until it took",
              got.get(ea) == "sent" and typed(ea) == ["eaten"] and
              sum(e["ev"] == "enter_eaten" for e in events(ea)) == 2)
        check("every Enter eaten: failed/not_submitted, exit 1",
              got.get(nx) == "failed — the message stayed in the composer "
              "through every Enter" and typed(nx) == [] and
              r.returncode == 1)
        for n in (ea, nx):
            run("-k", n)
            names.remove(n)

        # --- concurrency: about the slowest session, not the sum ---------
        # Each slow one: two eaten Enters, 3 s apart = 6 s+; five in a row
        # would take 30 s+.
        slow = [sess("sc%d" % k, "claude", eat_enters=2) for k in range(5)]
        quick = sess("sq", "claude")
        for n in slow + [quick]:
            up(n)
        t0 = time.time()
        r = say(*(slow + [quick]), "--", "together",
                env=dict(os.environ, DCH_AGENT_REENTER_MS="3000"))
        dt = time.time() - t0
        print("     | %.1fs: " % dt +
              r.stdout.rstrip().replace("\n", "\n     | "))
        got, n = lines(r.stdout)
        check("concurrent: all sent, exit 0", r.returncode == 0 and n == 6
              and all(got.get(s) == "sent" for s in slow + [quick]))
        check("concurrent: %.1fs, well under the 30s+ sum" % dt, dt < 20)
        check("concurrent: results print as each finishes (the quick one, "
              "named last, first)",
              r.stdout.splitlines()[0] == "%s: sent" % quick)
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
        if alive(p):
            try:
                os.kill(p, 9)
            except OSError:
                pass
    shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print("FAIL: %d --say check(s) failed" % fails)
        return 1
    print("PASS: --say")
    return 0


if __name__ == "__main__":
    sys.exit(main())
