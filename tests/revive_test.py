"""Session history and `dch --revive` (#042).

Every master keeps $XDG_STATE_HOME/dch/sessions/<socket dir slug>/<name>.json:
cwd, argv, how the session ended, and the Claude Code agent inside it. `--history` lists the
ended ones; `--revive <name> [--as <new>]` / `--revive --all` start them again
in their cwd with their argv, claude resumed with --resume <id> (as the
command, or typed into the shell it was started from) and told to carry on.

Covers: the record (modes, fields, refresh, alias), its end (exit N, signal
N, killed by -k, vanished after a SIGKILLed master), --history and --json
filtering, revive in the right cwd with the same argv, --resume
substitution, a shell-launched agent typed back in (with a cd), name
collisions (named: refused; --all: next free -N), a missing cwd (others
carry on), the caller's Claude env stripped, the message typed only once the
composer is up and re-Entered when an Enter does not take, the lite path,
pruning, two runtime dirs sharing one state dir (records apart, same names
not clobbered, --history/--all blind to the other dir), and what --all
takes (abnormal, seen in the last 3 days, ran 5+ minutes; the rest by
name). Drives tests/fake_claude.py as `claude` with HOME, the runtime
dir and the state dir all temporary.

Run:  python3 tests/revive_test.py        (uses ./dch)
"""
import json, os, shutil, signal, socket, stat, subprocess, sys, tempfile
import threading, time, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
RESUME = "resume all work. This session ended"
CALLER_ENV = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "caller-sid",
              "CLAUDE_PID": "1", "CLAUDE_CODE_MESSAGING_TOKEN": "x"}


def run(*args, timeout=90, env=None):
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
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchrv", dir="/tmp"))
    home, fc, bindir = (os.path.join(tmp, d) for d in ("home", "fc", "bin"))
    proj, state = os.path.join(tmp, "proj"), os.path.join(tmp, "state")
    for d in (home, fc, bindir, proj, os.path.join(proj, "sub")):
        os.makedirs(d)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    with open(os.path.join(bindir, "fakewrap"), "w") as f:
        f.write('#!/bin/sh\n[ "$1" = wrap ] && shift\n"$@"\nexit $?\n')
    for b in ("claude", "fakewrap"):
        p = os.path.join(bindir, b)
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
    with open(os.path.join(fc, "version"), "w") as f:
        f.write("1.0.0")
    for k in list(os.environ):
        if k.startswith("CLAUDE") or k in ("DCH_SESSION", "DCH_SOCKET_DIR",
                                           "DCH_NO_DETECT", "DCH_REVIVE_MSG"):
            del os.environ[k]
    os.environ.update(CALLER_ENV)
    os.environ.update(HOME=home, XDG_RUNTIME_DIR=tmp, XDG_STATE_HOME=state,
                      FAKE_CLAUDE_DIR=fc, DCH_RECORD_MS="300",
                      PATH=bindir + os.pathsep + os.environ["PATH"],
                      DCH_AGENT_STEP_MS="20000", DCH_AGENT_REENTER_MS="700")
    sockdir = os.path.join(tmp, "dch-%d" % os.getuid())
    # records are per socket dir: sessions/<socket dir, '/' as '-'>
    recroot = os.path.join(state, "dch", "sessions")
    recdir = os.path.join(recroot, sockdir.replace("/", "-"))
    rt2 = os.path.join(tmp, "rt2")     # a second runtime dir, same state dir
    sockdir2 = os.path.join(rt2, "dch-%d" % os.getuid())
    recdir2 = os.path.join(recroot, sockdir2.replace("/", "-"))
    os.makedirs(rt2)
    os.chdir(proj)
    names, fails = [], [0]
    held = []

    def check(msg, cond):
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails[0] += 1

    def env2():
        return dict(os.environ, XDG_RUNTIME_DIR=rt2)

    def spawn(n, *cmd, cwd=proj, env=None):
        names.append((n, env) if env else n)
        subprocess.run([DCH, "--spawn", n, "--size", "100x30"] + list(cmd),
                       capture_output=True, check=True, cwd=cwd, env=env)
        return n

    def rec(n, d=None):
        try:
            with open(os.path.join(d or recdir, n + ".json")) as f:
                return json.loads(f.read())
        except (OSError, ValueError):
            return None

    def rewrite(n, d=None, **fields):
        """Edit a record as stored (same compact form, 0600): how a test
        makes a session look older or longer-lived than it was."""
        r = rec(n, d)
        r.update(fields)
        p = os.path.join(d or recdir, n + ".json")
        fd = os.open(p + ".t", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
        os.rename(p + ".t", p)

    def ran(n, secs, d=None):
        """Backdate `started`, as if the session had run `secs`."""
        r = rec(n, d)
        rewrite(n, d, started=(r["ended"] or r["updated"]) - secs)

    def setctl(n, **ctl):
        with open(os.path.join(fc, n + ".ctl.tmp"), "w") as f:
            json.dump(ctl, f)
        os.rename(os.path.join(fc, n + ".ctl.tmp"),
                  os.path.join(fc, n + ".ctl"))

    def events(n):
        try:
            with open(os.path.join(fc, n + ".log")) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def starts(n):
        return [e for e in events(n) if e["ev"] == "start"]

    def ev(n, kind, pid):
        return [e for e in events(n) if e["ev"] == kind and e["pid"] == pid]

    def child(n):
        r = run("--cmd", n)
        return json.loads(r.stdout)["pid"] if r.returncode == 0 else 0

    def master(n):
        sock = os.path.join(sockdir, n + ".sock")
        out = subprocess.run(["ps", "-eo", "pid,command"],
                             capture_output=True, text=True).stdout
        for l in out.splitlines():
            if "--master-of" in l and l.rstrip().split(" -- ")[0].endswith(
                    " " + sock):
                return int(l.split()[0])
        return 0

    def vanish(n):
        """SIGKILL the session's master: no end gets written."""
        m = master(n)
        if m:
            os.kill(m, signal.SIGKILL)
        until(lambda: not master(n))

    def gone(n):
        return until(lambda: (rec(n) or {}).get("ended", 0) > 0)

    def history(env=None):
        r = run("--history", "--json", env=env)
        try:
            return {e["name"]: e for e in json.loads(r.stdout)}
        except ValueError:
            return None

    def ended_by_flag(n, code, env=None):
        flag = os.path.join(tmp, n + ".%d.go" % code)
        spawn(n, "sh", "-c", "while [ ! -e '%s' ]; do sleep 0.05; done; "
              "exit %d" % (flag, code), env=env)
        return flag

    try:
        # --- usage --------------------------------------------------------
        check("--revive with no name: usage error",
              run("--revive").returncode == 1)
        check("--as without --revive: usage error",
              run("--as", "x", "-ls").returncode == 1)
        check("--revive --all --as: usage error",
              run("--revive", "--all", "--as", "x").returncode == 1)
        check("--history with nothing ended: empty",
              run("--history").stdout == "" and history() == {})

        probe = spawn("probe", "sh")
        lite = run("--read", probe).returncode == 3
        run("-k", probe)

        # --- the record ---------------------------------------------------
        sh = spawn("rsh", "sh", "-c", "pwd > '%s/rsh.pwd'; exec sleep 60" %
                   tmp)
        r0 = until(lambda: rec(sh))
        check("record written at start, 0600 in a 0700 dir",
              r0 and stat.S_IMODE(os.stat(recdir).st_mode) == 0o700 and
              stat.S_IMODE(os.stat(os.path.join(recdir, sh + ".json"))
                           .st_mode) == 0o600)
        check("record fields: name, cwd, argv, live end, no agent",
              r0 and r0["name"] == sh and r0["cwd"] == proj and
              r0["argv"][:2] == ["sh", "-c"] and r0["end"] == "" and
              r0["ended"] == 0 and r0["agent"] is None and
              r0["started"] > 0)
        run("-m", sh, "My Alias")
        check("record refreshed: updated moves, alias picked up",
              until(lambda: (rec(sh) or {}).get("alias") == "My Alias" and
                    rec(sh)["updated"] >= r0["updated"]))
        check("record never stores the environment",
              "caller-sid" not in open(os.path.join(recdir, sh + ".json"))
              .read())

        flag3 = ended_by_flag("rex", 3)
        flag0 = ended_by_flag("rok", 0)
        until(lambda: rec("rex") and rec("rok"))
        open(flag3, "w").close()
        open(flag0, "w").close()
        check("child exit 3 -> end \"exit 3\"",
              gone("rex") and rec("rex")["end"] == "exit 3")
        check("child exit 0 -> end \"exit 0\"",
              gone("rok") and rec("rok")["end"] == "exit 0")
        os.unlink(flag3)        # a revived rex keeps running

        spawn("rsig", "sh", "-c", "pwd > '%s/rsig.pwd'; exec sleep 60" % tmp)
        until(lambda: rec("rsig") and child("rsig"))
        os.kill(child("rsig"), signal.SIGKILL)
        check("child killed by SIGKILL -> end \"signal 9\"",
              gone("rsig") and rec("rsig")["end"] == "signal 9")

        spawn("rkill", "sleep", "60")
        until(lambda: rec("rkill"))
        run("-k", "rkill")
        check("dch -k -> end \"killed\"",
              gone("rkill") and rec("rkill")["end"] == "killed")

        time.sleep(1.1)         # a later second, for the order check
        spawn("rvan", "sleep", "60")
        until(lambda: rec("rvan"))
        vanish("rvan")
        r = rec("rvan")
        check("SIGKILLed master leaves end \"\" (no exit write)",
              r and r["end"] == "" and r["ended"] == 0)

        h = history()
        check("--history --json: ended sessions only, the live one left out",
              h is not None and sh not in h and
              {"rex", "rok", "rsig", "rkill", "rvan"} <= set(h))
        check("--history --json: a SIGKILLed master reads \"vanished\"",
              h and h["rvan"]["end"] == "vanished")
        out = run("--history").stdout.splitlines()
        check("--history: newest first, one line each with end and cwd",
              out and out[0].startswith("rvan  ") and " vanished  " in out[0]
              and "  %s  $ sleep 60" % proj in out[0] and
              any(l.startswith("rkill  ") and " killed " in l for l in out))

        # --- two runtime dirs, one state dir: records stay apart --------
        f1 = ended_by_flag("twin", 4)
        f2 = ended_by_flag("twin", 5, env=env2())
        until(lambda: rec("twin") and rec("twin", recdir2))
        open(f1, "w").close()
        open(f2, "w").close()
        until(lambda: (rec("twin") or {}).get("ended") and
              (rec("twin", recdir2) or {}).get("ended"))
        os.unlink(f1)           # a revived twin keeps running
        os.unlink(f2)
        t1, t2 = rec("twin"), rec("twin", recdir2)
        check("same name in two socket dirs: two records, neither clobbered",
              t1 and t2 and t1["end"] == "exit 4" and t2["end"] == "exit 5"
              and t1["sock_dir"] == sockdir and t2["sock_dir"] == sockdir2)
        h2 = history(env2())
        check("--history sees only its own socket dir's records",
              h2 is not None and set(h2) == {"twin"} and
              h2["twin"]["end"] == "exit 5" and
              history()["twin"]["end"] == "exit 4")
        shutil.copy(os.path.join(recdir, "rex.json"),
                    os.path.join(recdir2, "rex.json"))
        check("a record whose sock_dir is another dir's is ignored",
              set(history(env2()) or {}) == {"twin"})
        r = run("--revive", "--all", env=env2())
        check("--all with only a short-lived session: nothing to revive, "
              "pointing at --history and revive by name",
              r.returncode == 0 and r.stdout.startswith("nothing to revive")
              and "dch --history" in r.stdout and "by name" in r.stdout)

        # --- named revive: same cwd, same argv ----------------------------
        os.chdir("/")
        pwdf = os.path.join(tmp, "rsig.pwd")
        os.unlink(pwdf)
        r = run("--revive", "rsig")
        check("--revive <name>: one line, exit 0",
              r.returncode == 0 and
              r.stdout == "rsig: revived as rsig in %s\n" % proj)
        check("revived in its cwd (not the caller's) with its argv",
              until(lambda: os.path.exists(pwdf)) and
              open(pwdf).read().strip() == proj and
              json.loads(run("--cmd", "rsig").stdout)["argv"] ==
              rec("rsig")["argv"])
        r = run("--revive", "rsig")
        check("named revive of a live name: refused with a clear message",
              r.returncode == 1 and "is live" in r.stderr and
              "--as <new>" in r.stderr)
        r = run("--revive", "rkill", "--as", "rkill2")
        names.append("rkill2")
        check("--revive <name> --as <new>: started under the new name, "
              "the old record retired",
              r.returncode == 0 and "rkill: revived as rkill2" in r.stdout
              and until(lambda: rec("rkill2")) and rec("rkill") is None)
        check("--revive <unknown>: exit 1",
              run("--revive", "nosuch").returncode == 1)

        # --- the agent, as the session's command --------------------------
        cl = spawn("rcl", "fakewrap", "wrap", "claude", "--chrome")
        s1 = until(lambda: starts(cl) and starts(cl)[0])
        check("caller's Claude env reaches a plain --spawn (control)",
              s1 and "CLAUDECODE" in s1["env"])
        a = until(lambda: ((rec(cl) or {}).get("agent") or {})
                  .get("session_id") == s1["sid"] and rec(cl)["agent"])
        check("record picks up the agent: kind, id, cwd, launch",
              a and a["kind"] == "claude" and a["cwd"] == proj and
              os.path.basename(a["launch"][0]) == "claude" and
              a["launch"][1:] == ["--chrome"])
        vanish(cl)

        # --- the agent, typed into a shell --------------------------------
        shc = spawn("rshc", "sh")
        time.sleep(0.3)
        run("--run", shc, "cd sub && fakewrap wrap claude --chrome")
        s2 = until(lambda: starts(shc) and starts(shc)[0])
        a = until(lambda: s2 and ((rec(shc) or {}).get("agent") or {})
                  .get("session_id") == s2["sid"] and rec(shc)["agent"])
        check("shell session: launch is the typed command, script as typed",
              a and a["launch"] == [os.path.join(bindir, "fakewrap"), "wrap",
                                    "claude", "--chrome"] and
              a["cwd"] == os.path.join(proj, "sub"))
        vanish(shc)

        # --- collisions and a missing cwd ---------------------------------
        col = spawn("rcol", "sleep", "60")
        until(lambda: rec(col) and child(col))
        os.kill(child(col), signal.SIGKILL)
        gone(col)
        lsock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        lsock.bind(os.path.join(sockdir, "rcol.sock"))
        lsock.listen(8)
        held.append(lsock)
        r = run("--revive", "rcol")
        check("ended record, name taken by a live session: named refused",
              r.returncode == 1 and "is live" in r.stderr)
        gdir = os.path.join(tmp, "gone")
        os.mkdir(gdir)
        spawn("rgone", "sleep", "60", cwd=gdir)
        until(lambda: rec("rgone") and child("rgone"))
        os.kill(child("rgone"), signal.SIGKILL)
        gone("rgone")
        os.rmdir(gdir)

        # --- --revive --all: what it takes --------------------------------
        # Sessions here live seconds; --all wants 5 minutes, so the ones it
        # should take are made to look it. rshort (seconds) and rold (four
        # days ago) are left out of --all but revive by name.
        spawn("rshort", "sleep", "60")
        spawn("rold", "sleep", "60")
        until(lambda: rec("rshort") and child("rshort") and rec("rold") and
              child("rold"))
        os.kill(child("rshort"), signal.SIGKILL)
        os.kill(child("rold"), signal.SIGKILL)
        gone("rshort")
        gone("rold")
        for n in ("rex", "rok", "rvan", "rcol", "rgone", cl, shc,
                  "twin"):
            ran(n, 600)
        ago = int(time.time()) - 4 * 86400
        rewrite("rold", started=ago - 3600, updated=ago, ended=ago)
        ran("twin", 600, recdir2)
        r = run("--revive", "--all", env=env2())
        names.append(("twin", env2()))
        check("--all in another socket dir revives only that dir's records",
              r.returncode == 0 and
              r.stdout == "twin: revived as twin in %s\n" % proj and
              json.loads(run("--cmd", "twin", env=env2()).stdout)
              ["argv"][:2] == ["sh", "-c"])
        names.append("twin")
        if not lite:
            setctl(cl, dialog=True, eat_enters=2)
            setctl(shc)

            def undialog():
                time.sleep(3.0)
                setctl(cl, eat_enters=2)
            threading.Thread(target=undialog, daemon=True).start()
        r = run("--revive", "--all", timeout=120)
        names.extend(["rex", "rvan", "rcol-2", cl, shc])
        print("     | " + r.stdout.rstrip().replace("\n", "\n     | "))
        lines = r.stdout.splitlines()
        check("--all: exit 1 (one failed), the rest carried on",
              r.returncode == 1)
        check("--all: skips a short-lived and a 4-day-old abnormal end",
              not any(l.startswith(("rshort:", "rold:")) for l in lines))
        r2 = run("--revive", "rshort")
        r3 = run("--revive", "rold")
        check("...both still revive by name",
              r2.returncode == 0 and r3.returncode == 0 and
              child("rshort") and child("rold"))
        check("--all: skips exit 0 and -k, takes exit N, signal, vanished",
              not any(l.startswith(("rok:", "rkill:")) for l in lines) and
              "rex: revived as rex in %s" % proj in lines and
              "rvan: revived as rvan in %s" % proj in lines)
        check("--all: a missing cwd fails alone, with the reason",
              any(l.startswith("rgone: NOT revived") and gdir in l
                  for l in lines))
        check("--all: a taken name goes to the next free -N, printed",
              "rcol: revived as rcol-2 in %s" % proj in lines and
              json.loads(run("--cmd", "rcol-2").stdout)["argv"] ==
              ["sleep", "60"])

        c2 = until(lambda: len(starts(cl)) > 1 and starts(cl)[1], 20)
        check("command agent: relaunched with --resume <id> after the token",
              json.loads(run("--cmd", cl).stdout)["argv"] ==
              ["fakewrap", "wrap", "claude", "--resume", s1["sid"],
               "--chrome"] and c2 and c2["sid"] == s1["sid"] and
              c2["cwd"] == proj)
        check("the caller's Claude identity is stripped from the revived env",
              c2 and not any(k in c2["env"] for k in CALLER_ENV))
        t2 = until(lambda: len(starts(shc)) > 1 and starts(shc)[1], 20)
        check("shell agent: shell revived, launch + --resume typed, with cd",
              t2 and t2["argv"] == ["--resume", s2["sid"], "--chrome"] and
              t2["cwd"] == os.path.join(proj, "sub") and
              json.loads(run("--cmd", shc).stdout)["argv"] == ["sh"])
        if lite:
            check("lite: agents resumed, the message skipped and said so",
                  all(any(l.startswith(n + ": revived as") and
                          "resumed " in l and "dch-lite" in l
                          for l in lines) for n in (cl, shc)) and
                  not ev(cl, "line", c2["pid"]))
        else:
            check("--all: one line per agent, message sent",
                  "%s: revived as %s in %s, claude resumed %s; resume "
                  "message sent" % (cl, cl, proj, s1["sid"]) in lines and
                  any(l.startswith(shc + ": revived as") and
                      l.endswith("resume message sent") for l in lines))
            tl = ev(cl, "turn", c2["pid"])
            check("message typed only once the dialog was gone (no Enter on "
                  "it), re-Entered past two Enters that did not take",
                  not ev(cl, "enter_dialog", c2["pid"]) and
                  len(ev(cl, "enter_eaten", c2["pid"])) == 2 and
                  len(tl) == 1 and tl[0]["text"].startswith(RESUME) and
                  "(vanished, last seen" in tl[0]["text"])
            check("shell agent got the message",
                  any(e["text"].startswith(RESUME)
                      for e in ev(shc, "turn", t2["pid"])))

            # --- the Enter never takes: bounded, reported -------------------
            nx = spawn("rnx", "claude")
            s3 = until(lambda: starts(nx) and starts(nx)[0])
            until(lambda: ((rec(nx) or {}).get("agent") or {})
                  .get("session_id") == s3["sid"])
            vanish(nx)
            setctl(nx, eat_enters=99)
            t0 = time.time()
            r = run("--revive", nx, timeout=120)
            s4 = until(lambda: len(starts(nx)) > 1 and starts(nx)[1])
            check("Enter that never takes: 1 + 5 presses, then reported",
                  r.returncode == 1 and
                  "it stayed in the composer through every Enter" in r.stdout
                  and s4 and len(ev(nx, "enter_eaten", s4["pid"])) == 6 and
                  not ev(nx, "turn", s4["pid"]) and time.time() - t0 < 30)

        check("a revived session writes its own record again",
              until(lambda: ((rec(cl) or {}).get("argv") or [])[3:5] ==
                    ["--resume", s1["sid"]]))

        # --- prune ----------------------------------------------------------
        lone = os.path.join(recroot, "-some-old-dir")
        os.mkdir(lone, 0o700)
        olds = [os.path.join(d, "ancient.json") for d in (recdir, lone)]
        t = time.time() - 31 * 86400
        for old in olds:
            with open(old, "w") as f:
                f.write(json.dumps({"name": "ancient"}))
            os.utime(old, (t, t))
        spawn("rprune", "sleep", "60")
        check("a cold start prunes records older than 30 days, in every "
              "socket dir's subdir, and drops the subdirs left empty",
              until(lambda: not any(map(os.path.exists, olds))) and
              until(lambda: not os.path.exists(lone)) and
              rec("rok") is not None and rec("twin", recdir2) is not None)
    except Exception:
        traceback.print_exc()
        fails[0] += 1
    return finish(fails[0], tmp, names, held)


def finish(fails, tmp, names, held):
    for s in held:
        s.close()
    for n in names:
        n, env = n if isinstance(n, tuple) else (n, None)
        subprocess.run([DCH, "-k", n], capture_output=True, env=env)
    time.sleep(0.3)
    # safety net: only the fakes this test started (their records name them)
    fc = os.path.join(tmp, "fc")
    for fn in os.listdir(fc) if os.path.isdir(fc) else []:
        if fn.endswith(".log"):
            for l in open(os.path.join(fc, fn)):
                try:
                    p = json.loads(l)["pid"]
                    os.kill(p, 0)
                    os.kill(p, signal.SIGKILL)
                except (OSError, ValueError, KeyError):
                    pass
    shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print("FAIL: %d revive check(s) failed" % fails)
        return 1
    print("PASS: session history and --revive")
    return 0


if __name__ == "__main__":
    sys.exit(main())
