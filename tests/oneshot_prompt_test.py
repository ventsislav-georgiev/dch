"""claude's one-shot startup prompt is sent once, never again (#138).

`dch --spawn s -- claude --name x "first prompt"` starts Claude with a first
message. Every later start drops it, and keeps every option, one after
the prompt too: --restart's
agent pass (the picker's R), --revive (the picker's History resume), a revive
with no session id, and a revive that types claude into its shell. The
History record never stores it, and --history (text and --json) and
--ls-json never show it; a record an older dch wrote with one is stripped
when read.

Drives tests/fake_claude.py as `claude` with HOME, the runtime dir and the
state dir all temporary.

Run:  python3 tests/oneshot_prompt_test.py        (uses ./dch)
"""
import json, os, shutil, stat, subprocess, sys, tempfile, time, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
P1 = "first prompt for p1"
P2 = "a prompt with an option after it"
OLD = "an old record's prompt"
X = "1380dead-0000-4000-8000-000000000138"
Y = "1380beef-0000-4000-8000-000000000138"
Z = "1380f00d-0000-4000-8000-000000000138"


def run(*args, timeout=120):
    return subprocess.run([DCH] + list(args), capture_output=True, text=True,
                          timeout=timeout)


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
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dch138", dir="/tmp"))
    home, fc, bindir = (os.path.join(tmp, d) for d in ("home", "fc", "bin"))
    proj, state = os.path.join(tmp, "proj"), os.path.join(tmp, "state")
    for d in (home, fc, bindir, proj):
        os.makedirs(d)
    claude = os.path.join(bindir, "claude")
    shutil.copy(os.path.join(HERE, "fake_claude.py"), claude)
    os.chmod(claude, os.stat(claude).st_mode | stat.S_IXUSR)
    with open(os.path.join(fc, "version"), "w") as f:
        f.write("1.0.0")
    for k in list(os.environ):
        if k.startswith(("CLAUDE", "DCH_", "Malloc")):
            del os.environ[k]
    os.environ.update(HOME=home, XDG_RUNTIME_DIR=tmp, XDG_STATE_HOME=state,
                      FAKE_CLAUDE_DIR=fc, DCH_RECORD_MS="300",
                      PATH=bindir + os.pathsep + os.environ["PATH"],
                      DCH_AGENT_SETTLE_MS="500", DCH_AGENT_STEP_MS="10000",
                      DCH_AGENT_TIMEOUT_MS="30000", DCH_LOGIN_ENV="0",
                      DCH_NO_REMOTE="1", DCH_CONFIG=os.path.join(tmp, "cfg"))
    sockdir = os.path.join(tmp, "dch-%d" % os.getuid())
    recdir = os.path.join(state, "dch", "sessions", sockdir.replace("/", "-"))
    os.chdir(proj)
    fails = [0]

    def check(msg, cond):
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails[0] += 1

    def starts(n):
        try:
            with open(os.path.join(fc, n + ".log")) as f:
                return [e for e in map(json.loads, f) if e["ev"] == "start"]
        except (OSError, ValueError):
            return []

    def up(n, count=1, t=15.0):
        s = until(lambda: len(starts(n)) >= count and starts(n)[count - 1], t)
        if s and not lite:
            until(lambda: "auto mode on" in run("--read", n).stdout)
        return s

    def gone(n):
        """-k returns before the master has gone: wait for its socket, as a
        revive of a name still answering is refused."""
        until(lambda: not os.path.exists(os.path.join(sockdir, n + ".sock")),
              10)
        # The fake claude can outlive the socket a moment, and --revive
        # refuses while a claude with that session id still runs.
        until(lambda: not any(alive(e.get("pid")) for e in starts(n)), 10)

    def alive(pid):
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        return True

    def raw(n):
        try:
            with open(os.path.join(recdir, n + ".json")) as f:
                return f.read()
        except OSError:
            return ""

    def plant(n, argv, launch=None, sid=None):
        """A record an older dch wrote, prompt and all: a session that
        vanished (SIGKILLed master)."""
        now = int(time.time())
        agent = None if not sid else dict(
            kind="claude", session_id=sid, name="", cwd=proj,
            launch=launch or argv, config_dir="")
        r = dict(name=n, sock_dir=sockdir, alias="", cwd=proj, argv=argv,
                 started=now - 900, updated=now - 30, ended=0, end="",
                 agent=agent)
        os.makedirs(recdir, mode=0o700, exist_ok=True)
        p = os.path.join(recdir, n + ".json")
        fd = os.open(p + ".t", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        os.rename(p + ".t", p)

    lite = False
    try:
        r = subprocess.run([DCH, "--spawn", "probe", "sh"],
                           capture_output=True)
        lite = run("--read", "probe").returncode == 3
        run("-k", "probe")

        # --- the first start gets the prompt; dch never records it ---------
        subprocess.run([DCH, "--spawn", "p1", "--size", "100x30", "--",
                        "claude", "--name", "x", P1], check=True,
                       capture_output=True)
        s1 = up("p1")
        check("the first start gets the prompt",
              s1 and s1["argv"] == ["--name", "x", P1])
        until(lambda: '"session_id":"%s"' % s1["sid"] in raw("p1"), 15)
        rec = raw("p1")
        check("the live record has the agent, no prompt in argv or launch "
              "[%s]" % rec.strip(),
              s1["sid"] in rec and P1 not in rec and
              json.loads(rec)["argv"] == [claude, "--name", "x"])
        r = run("--ls-json")
        check("--ls-json does not show the prompt",
              r.returncode == 0 and "p1" in r.stdout and P1 not in r.stdout)

        # --- --restart's agent pass (the picker's R) ----------------------
        if not lite:
            r = run("--restart", "p1", "--include-agents", "-f",
                    timeout=120)
            s2 = up("p1", 2, 45.0)
            check("--restart -f relaunches as --resume <id> --name x, no "
                  "prompt [%r]" % (s2 and s2["argv"]),
                  s2 and s2["argv"] == ["--resume", s1["sid"], "--name",
                                        "x"])
            sid = s2["sid"] if s2 else s1["sid"]
        else:
            sid = s1["sid"]

        # --- killed, then --revive (the picker's History resume) ----------
        until(lambda: '"session_id":"%s"' % sid in raw("p1"), 15)
        n0 = len(starts("p1"))
        run("-k", "p1")
        until(lambda: '"end":"killed"' in raw("p1"), 10)
        gone("p1")
        h = run("--history", "--json")
        t = run("--history")
        check("--history --json and --history do not show the prompt",
              h.returncode == 0 and '"p1"' in h.stdout and
              P1 not in h.stdout and "p1" in t.stdout and P1 not in t.stdout)
        r = run("--revive", "p1", "--no-message")
        s3 = up("p1", n0 + 1)
        check("--revive resumes with --name x, no prompt [%r %r %r %r]"
              % (r.returncode, r.stdout, r.stderr, s3 and s3["argv"]),
              r.returncode == 0 and s3 and
              s3["argv"] == ["--resume", sid, "--name", "x"])
        run("-k", "p1")

        # --- an option after the prompt stays (claude parses it there) ----
        subprocess.run([DCH, "--spawn", "p2", "--size", "100x30", "--",
                        "claude", P2, "--model", "m2"], check=True,
                       capture_output=True)
        t1 = up("p2")
        check("p2's first start gets the prompt and --model m2",
              t1 and t1["argv"] == [P2, "--model", "m2"])
        until(lambda: '"session_id":"%s"' % t1["sid"] in raw("p2"), 15)
        rec = raw("p2")
        check("p2's live record keeps --model m2, drops the prompt [%s]"
              % rec.strip(),
              P2 not in rec and json.loads(rec)["argv"] ==
              [claude, "--model", "m2"] and
              json.loads(rec)["agent"]["launch"] == [claude, "--model", "m2"])
        run("-k", "p2")
        until(lambda: '"end":"killed"' in raw("p2"), 10)
        gone("p2")
        r = run("--revive", "p2", "--no-message")
        t2 = up("p2", 2)
        check("p2 --revive: --resume <id> --model m2, no prompt [%r %r %r]"
              % (r.returncode, r.stderr, t2 and t2["argv"]),
              r.returncode == 0 and t2 and
              t2["argv"] == ["--resume", t1["sid"], "--model", "m2"])
        run("-k", "p2")

        # --- a record an older dch wrote, with the prompt -----------------
        # #141: a `claude` that is not the program run (here a directory
        # passed to sh -c) is no Claude argv: nothing of it is a prompt
        cdir = os.path.join(tmp, "src", "claude")
        os.makedirs(cdir)
        nc = ["sh", "-c", 'echo "ARGS: $*"; sleep 600', "sh", cdir,
              "extra-arg"]
        subprocess.run([DCH, "--spawn", "nc", "--size", "100x30", "--"] + nc,
                       check=True, capture_output=True)
        until(lambda: raw("nc"), 10)
        rec = raw("nc")
        check("a non-Claude session's live record keeps extra-arg [%s]"
              % rec.strip(),
              rec and json.loads(rec)["argv"][1:] == nc[1:])
        run("-k", "nc")
        until(lambda: '"end":"killed"' in raw("nc"), 10)
        gone("nc")
        h = run("--history", "--json")
        try:
            hj = [o for o in json.loads(h.stdout) if o["name"] == "nc"]
        except ValueError:
            hj = []
        check("...and --history --json keeps it [%r]" % hj,
              len(hj) == 1 and hj[0]["argv"][1:] == nc[1:])
        r = run("--revive", "nc", "--no-message")
        want = "ARGS: %s extra-arg" % cdir
        if lite:
            # no screen to read: the revived record shows the argv it ran
            ok = until(lambda: '"end":""' in raw("nc") and
                       json.loads(raw("nc"))["argv"][1:] == nc[1:], 10)
        else:
            ok = until(lambda: want in run("--read", "nc").stdout, 10)
        check("...and --revive runs it whole (%s) [%r %r]"
              % (want, r.returncode, r.stderr),
              r.returncode == 0 and ok)
        run("-k", "nc")

        # #141: claude behind a wrapper (headroom wrap claude ...) is cut
        # once dch has seen Claude run in the session
        wrap = os.path.join(bindir, "wrap")
        with open(wrap, "w") as f:
            f.write('#!/bin/sh\n"$@"\n')
        os.chmod(wrap, 0o755)
        P3 = "fix it"
        subprocess.run([DCH, "--spawn", "wr", "--size", "100x30", "--",
                        "wrap", "claude", P3, "--model", "opus"], check=True,
                       capture_output=True)
        w1 = up("wr")
        check("the wrapped claude gets the prompt",
              w1 and w1["argv"] == [P3, "--model", "opus"])
        until(lambda: w1 and '"session_id":"%s"' % w1["sid"] in raw("wr"),
              15)
        rec = raw("wr")
        check("the wrapped session's live record, agent seen: no prompt [%s]"
              % rec.strip(),
              P3 not in rec and rec and
              json.loads(rec)["argv"] == [wrap, "claude", "--model", "opus"])
        run("-k", "wr")
        until(lambda: '"end":"killed"' in raw("wr"), 10)
        gone("wr")
        h = run("--history", "--json")
        try:
            hj = [o for o in json.loads(h.stdout) if o["name"] == "wr"]
        except ValueError:
            hj = []
        check("...nor in --history --json [%r]" % hj,
              len(hj) == 1 and P3 not in h.stdout and
              hj[0]["argv"] == [wrap, "claude", "--model", "opus"])
        r = run("--revive", "wr", "--no-message")
        w2 = up("wr", 2)
        check("...and --revive runs wrap claude --resume <id> --model opus "
              "[%r %r %r]" % (r.returncode, r.stderr, w2 and w2["argv"]),
              r.returncode == 0 and w1 and w2 and
              w2["argv"] == ["--resume", w1["sid"], "--model", "opus"])
        run("-k", "wr")

        plant("old", [claude, "--name", "y", "--model", "opus", "--", OLD],
              sid=X)
        h = run("--history", "--json")
        t = run("--history")
        try:
            hj = [o for o in json.loads(h.stdout) if o["name"] == "old"]
        except ValueError:
            hj = []
        check("an old record's prompt: not in --history --json (still "
              "valid JSON, argv and launch cut) nor in --history",
              len(hj) == 1 and OLD not in h.stdout and OLD not in t.stdout
              and hj[0]["argv"] == [claude, "--name", "y", "--model", "opus"]
              and hj[0]["agent"]["launch"] == hj[0]["argv"])
        r = run("--revive", "old", "--no-message")
        so = up("old")
        check("...and --revive drops it [%r]" % (so and so["argv"]),
              r.returncode == 0 and so and
              so["argv"] == ["--resume", X, "--name", "y", "--model",
                             "opus"])
        run("-k", "old")

        # --- an old record with options after its prompt ------------------
        plant("old2", [claude, OLD, "--model", "o2"],
              launch=[claude, "-n", "w", OLD, "--effort", "high"], sid=Z)
        h = run("--history", "--json")
        try:
            hj = [o for o in json.loads(h.stdout) if o["name"] == "old2"]
        except ValueError:
            hj = []
        check("an old record, options after the prompt: --history --json "
              "valid, the options kept, the prompt gone [%r]" % hj,
              len(hj) == 1 and OLD not in h.stdout and
              hj[0]["argv"] == [claude, "--model", "o2"] and
              hj[0]["agent"]["launch"] == [claude, "-n", "w", "--effort",
                                           "high"])
        r = run("--revive", "old2", "--no-message")
        so = up("old2")
        check("...and --revive keeps --model o2 [%r]" % (so and so["argv"]),
              r.returncode == 0 and so and
              so["argv"] == ["--resume", Z, "--model", "o2"])
        run("-k", "old2")

        # --- a revive with no session id: a fresh start, no prompt --------
        plant("fresh", [claude, "--permission-mode", "plan", OLD])
        r = run("--revive", "fresh", "--no-message")
        sf = up("fresh")
        check("a revive with no session id starts claude without the "
              "prompt [%r]" % (sf and sf["argv"]),
              r.returncode == 0 and sf and
              sf["argv"] == ["--permission-mode", "plan"])
        run("-k", "fresh")

        # --- claude typed into a shell: the typed line has no prompt ------
        plant("shl", ["/bin/sh"], launch=[claude, "-n", "z", OLD], sid=Y)
        r = run("--revive", "shl", "--no-message")
        ss = up("shl", 1, 30.0)
        check("a shell revive types claude --resume <id> -n z, no prompt "
              "[%r %r]" % (r.stdout, ss and ss["argv"]),
              r.returncode == 0 and ss and
              ss["argv"] == ["--resume", Y, "-n", "z"])
        run("-k", "shl")
        until(lambda: '"end":"killed"' in raw("shl"), 10)
        check("no record holds a prompt any more",
              all(OLD not in raw(n) and P1 not in raw(n) and
                  P2 not in raw(n)
                  for n in ("p1", "p2", "old", "old2", "fresh", "shl")))
    except Exception:
        traceback.print_exc()
        fails[0] += 1
    finally:
        for s in os.listdir(sockdir) if os.path.isdir(sockdir) else []:
            if s.endswith(".sock"):
                run("-k", s[:-5], timeout=30)
        shutil.rmtree(tmp, ignore_errors=True)
    print("%s: %d failed" % (os.path.basename(__file__), fails[0]))
    return 1 if fails[0] else 0


if __name__ == "__main__":
    sys.exit(main())
