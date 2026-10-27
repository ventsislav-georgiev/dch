"""`dch --stop`: pause Claude, /exit, end the session as "stopped" (#046).

Drives tests/fake_claude.py as `claude` first on PATH, with HOME, the
runtime dir and the state dir all temporary. Covers: a Claude that is the
session's command stopped (pause typed, /exit, the record ends "stopped"
with the exit line's id) and --revive'd back into that id with the
stopped-wording message; the same round trip for a Claude typed into a
shell, with a record refresh every 100 ms never losing the agent between
/exit and the end; a session without Claude stopped at once; a Claude that
never goes idle (failed after DCH_AGENT_TIMEOUT_MS, still running, resume
message typed); one SIGTERM while pausing (interrupted, resume message);
self (DCH_SESSION); a pending respawn; --all over a mix with --json (pure
NDJSON: phases, then one final object per session); --revive --all
skipping a stopped record; unknown names, an old master, usage errors. On
dch-lite: Claude sessions skipped/no_screen with nothing typed, the rest
still stop.

Run:  python3 tests/stop_test.py        (uses ./dch)
"""
import json, os, shutil, signal, socket, stat, subprocess, sys, tempfile
import threading, time, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
TAG = str(os.getpid())
PAUSE = "pause all work, this session is being stopped by dch --stop"
GAVE_UP = "resume all work. dch --stop gave up"
REVIVED = "resume all work. This session was stopped by dch --stop"
FINAL = ["detail", "name", "reason", "result", "session_id"]


def run(*args, timeout=120, env=None):
    return subprocess.run([DCH] + list(args), capture_output=True, text=True,
                          timeout=timeout, env=env)


def until(fn, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def ndjson(out):
    """Every stdout line parsed, or None when one is not a JSON object."""
    try:
        objs = [json.loads(l) for l in out.splitlines()]
    except ValueError:
        return None
    return objs if all(isinstance(o, dict) for o in objs) else None


def finals(objs):
    return {o["name"]: o for o in objs or [] if "result" in o}


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchv", dir="/tmp"))
    home, fc, bindir = (os.path.join(tmp, d) for d in ("home", "fc", "bin"))
    proj, state = os.path.join(tmp, "proj"), os.path.join(tmp, "state")
    for d in (home, fc, bindir, os.path.join(proj, "sub")):
        os.makedirs(d)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    with open(os.path.join(bindir, "fakewrap"), "w") as f:
        f.write('#!/bin/sh\n[ "$1" = wrap ] && shift\n"$@"\nexit $?\n')
    for b in ("claude", "fakewrap"):
        p = os.path.join(bindir, b)
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
    for k in list(os.environ):
        if k.startswith("CLAUDE") or k.startswith("DCH_"):
            del os.environ[k]
    os.environ.update(HOME=home, XDG_RUNTIME_DIR=tmp, XDG_STATE_HOME=state,
                      FAKE_CLAUDE_DIR=fc, DCH_RECORD_MS="100",
                      PATH=bindir + os.pathsep + os.environ["PATH"],
                      DCH_AGENT_SETTLE_MS="500", DCH_AGENT_STEP_MS="20000",
                      DCH_AGENT_REENTER_MS="700",
                      DCH_AGENT_TIMEOUT_MS="60000")
    sockdir = os.path.join(tmp, "dch-%d" % os.getuid())
    recdir = os.path.join(state, "dch", "sessions",
                          sockdir.replace("/", "-"))
    os.chdir(proj)
    names, pids = [], set()
    fails = [0]

    def check(msg, cond):
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails[0] += 1

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

    def starts(n):
        return [e for e in events(n) if e["ev"] == "start"]

    def typed(n):
        return [e["text"] for e in events(n) if e["ev"] == "line"]

    def turns(n):
        return [e["text"] for e in events(n) if e["ev"] == "turn"]

    def up(n, k=0):
        """The k-th claude of n is painted; its pid joins the safety net."""
        s = until(lambda: len(starts(n)) > k and starts(n)[k], 20)
        if s:
            pids.add(s["pid"])
            until(lambda: lite or "auto mode on" in run("--read", n).stdout)
        return s

    def rec(n):
        try:
            with open(os.path.join(recdir, n + ".json")) as f:
                return json.loads(f.read())
        except (OSError, ValueError):
            return None

    def agent_sid(n, sid):
        return until(lambda: ((rec(n) or {}).get("agent") or {})
                     .get("session_id") == sid)

    def cmd(n):
        r = run("--cmd", n)
        return json.loads(r.stdout) if r.returncode == 0 else None

    def marker(n):
        return os.path.exists(os.path.join(sockdir, n + ".sock.stopped"))

    def ran(n, secs):
        """Backdate `started`, as if the session had run `secs`."""
        r = rec(n)
        r["started"] = (r["ended"] or r["updated"]) - secs
        p = os.path.join(recdir, n + ".json")
        fd = os.open(p + ".t", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
        os.rename(p + ".t", p)

    def stop(*args, env=None):
        return run("--stop", *args, env=env)

    def gone(n):
        names.remove(n)
        return until(lambda: (rec(n) or {}).get("ended", 0) > 0)

    lite = False
    try:
        probe = sess("probe", "sh")
        lite = run("--read", probe).returncode == 3
        run("-k", probe)
        names.remove(probe)

        # --- usage: exit 1, nothing on stdout ----------------------------
        for args, why in (([], "nothing"), (["--json"], "only --json"),
                          (["x", "--all"], "names and --all"),
                          (["x", "-q"], "unknown flag"), ([""], "empty name")):
            r = stop(*args)
            check("usage error (%s): exit 1, stdout empty" % why,
                  r.returncode == 1 and r.stdout == "" and r.stderr)

        # --- no claude: stopped at once ----------------------------------
        p = sess("stp", "sh")
        until(lambda: rec(p) and cmd(p))
        t0 = time.time()
        r = stop(p)
        dt = time.time() - t0
        check("no claude: stopped at once (%.1fs), one line, exit 0" % dt,
              r.returncode == 0 and dt < 10 and
              r.stdout == "%s: stopped (revive with dch --revive %s)\n"
              % (p, p))
        check("no claude: record final when --stop returns: end stopped, "
              "agent null, no marker left, session gone",
              (rec(p) or {}).get("end") == "stopped" and
              rec(p)["agent"] is None and not marker(p) and
              cmd(p) is None and
              not os.path.exists(os.path.join(sockdir, p + ".sock")))
        names.remove(p)
        r = run("--history")
        check("--history shows stopped",
              any(l.startswith(p + "  ") and "  stopped  " in l
                  for l in r.stdout.splitlines()))
        h = json.loads(run("--history", "--json").stdout)
        check("--history --json carries \"end\":\"stopped\"",
              any(e["name"] == p and e["end"] == "stopped" for e in h))

        # --- unknown name, old master ------------------------------------
        r = stop("nosuch" + TAG, "--json")
        check("unknown name: failed/no_session, exit 1",
              r.returncode == 1 and ndjson(r.stdout) == [
                  {"name": "nosuch" + TAG, "result": "failed",
                   "reason": "no_session", "detail": "no such session",
                   "session_id": ""}])
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
        r = stop("old" + TAG, "--json")
        srv.close()
        os.unlink(old)
        check("old master: failed/old_master, exit 1",
              r.returncode == 1 and
              (finals(ndjson(r.stdout)).get("old" + TAG) or {})
              .get("reason") == "old_master")

        if lite:
            c = sess("lc", "claude")
            shc = sess("lshc", "sh")
            s = sess("ls", "sh")
            up(c)
            run("--run", shc, "claude")
            up(shc)
            r = stop("--all", "--json")
            objs = ndjson(r.stdout)
            f = finals(objs)
            check("lite: --json pure NDJSON, one final per session, exit 0",
                  objs is not None and r.returncode == 0 and
                  set(f) == {c, shc, s} and
                  all(sorted(o) == FINAL for o in f.values()))
            check("lite: claude sessions skipped/no_screen, still running, "
                  "nothing typed",
                  all(f.get(n, {}).get("result") == "skipped" and
                      f[n]["reason"] == "no_screen" and cmd(n) and
                      typed(n) == [] for n in (c, shc)))
            check("lite: the session without claude stopped",
                  f.get(s, {}).get("result") == "ok" and
                  (gone(s) and rec(s)["end"] == "stopped"))
            return finish(fails[0], tmp, names, pids)

        # --- claude as the command: stop, then --revive ------------------
        c = sess("stc", "claude", "--chrome")
        s1 = up(c)
        sid = s1 and s1["sid"]
        agent_sid(c, sid)
        r = stop(c)
        print("     | " + r.stdout.rstrip())
        check("command claude: one line with the id, exit 0",
              r.returncode == 0 and
              r.stdout == "%s: stopped (claude %s; revive with dch --revive "
              "%s)\n" % (c, sid, c))
        check("command claude: pause typed first, then /exit",
              typed(c)[:1] and typed(c)[0].startswith(PAUSE) and
              typed(c)[-1] == "/exit" and
              any(e["ev"] == "exit" for e in events(c)))
        a = (rec(c) or {}).get("agent") or {}
        check("command claude: record ends stopped, agent kept with the id "
              "(claude quit cleanly first)",
              rec(c)["end"] == "stopped" and a.get("session_id") == sid and
              os.path.basename(a["launch"][0]) == "claude" and
              a["launch"][1:] == ["--chrome"] and a["cwd"] == proj)
        check("command claude: session gone, no marker left",
              cmd(c) is None and not marker(c))
        names.remove(c)
        r = run("--revive", c)
        names.append(c)
        s2 = up(c, 1)
        check("revive: claude --resume <the id>, after the token",
              r.returncode == 0 and s2 and s2["sid"] == sid and
              cmd(c)["argv"][1:] == ["--resume", sid, "--chrome"])
        check("revive: the stopped-wording message, typed once",
              until(lambda: [t for t in turns(c) if t.startswith(REVIVED)])
              and len([t for t in turns(c) if t.startswith(REVIVED)]) == 1
              and "you stopped before the pause" in
              [t for t in turns(c) if t.startswith(REVIVED)][0])

        # --- claude typed into a shell, --json, refreshes every 100 ms ---
        shc = sess("sts", "sh")
        time.sleep(0.3)
        run("--run", shc, "cd sub && fakewrap wrap claude --chrome")
        s3 = up(shc)
        sid3 = s3 and s3["sid"]
        agent_sid(shc, sid3)
        seen, done = [], threading.Event()

        def watch():
            last = None
            while not done.is_set():
                try:
                    st = os.stat(os.path.join(recdir, shc + ".json"))
                    key = (st.st_ino, st.st_mtime_ns)
                except OSError:
                    key = None
                if key and key != last:
                    last = key
                    seen.append((time.time(), rec(shc)))
                time.sleep(0.01)
        w = threading.Thread(target=watch, daemon=True)
        w.start()
        proc = subprocess.Popen([DCH, "--stop", shc, "--json"],
                                stdout=subprocess.PIPE, text=True)
        out, at = [], {}
        for l in proc.stdout:
            out.append(l)
            try:
                at[json.loads(l).get("phase")] = time.time()
            except ValueError:
                pass
        proc.wait(timeout=120)
        done.set()
        w.join()
        objs = ndjson("".join(out))
        print("     | " + "".join(out).rstrip().replace("\n", "\n     | "))
        check("shell claude --json: pure NDJSON, phases pausing, exiting, "
              "ending, then one final ok with the id, exit 0",
              objs is not None and proc.returncode == 0 and
              [o.get("phase") for o in objs[:-1]] ==
              ["pausing", "exiting", "ending"] and
              all(sorted(o) == ["name", "phase"] for o in objs[:-1]) and
              objs[-1] == {"name": shc, "result": "ok", "reason": "",
                           "detail": "", "session_id": sid3})
        a = (rec(shc) or {}).get("agent") or {}
        check("shell claude: record ends stopped, launch as typed, its cwd",
              rec(shc)["end"] == "stopped" and
              a.get("session_id") == sid3 and
              a["launch"] == [os.path.join(bindir, "fakewrap"), "wrap",
                              "claude", "--chrome"] and
              a["cwd"] == os.path.join(proj, "sub"))
        mid = [r for t, r in seen
               if at.get("exiting", 1e18) <= t <= at.get("ending", 0)]
        check("record refreshes between /exit and the end (%d) all keep the "
              "agent" % len(mid),
              mid and all((r or {}).get("agent") and
                          r["agent"]["session_id"] == sid3 for r in mid))
        check("no record ever written without the agent during --stop",
              all((r or {}).get("agent") for t, r in seen if r))
        names.remove(shc)
        r = run("--revive", shc)
        names.append(shc)
        s4 = up(shc, 1)
        check("revive: shell back, launch typed with cd and --resume <id>",
              r.returncode == 0 and s4 and s4["sid"] == sid3 and
              s4["argv"] == ["--resume", sid3, "--chrome"] and
              s4["cwd"] == os.path.join(proj, "sub") and
              cmd(shc)["argv"] == [shutil.which("sh")])
        check("revive: the stopped-wording message reached it",
              until(lambda: any(t.startswith(REVIVED) for t in turns(shc))))
        for n in (c, shc):
            run("-k", n)
            names.remove(n)

        # --- never idle: failed, still running, resume message -----------
        t = sess("stt", "claude", shell=True)
        up(t)
        r = stop(t, "--json", env=dict(os.environ,
                                       DCH_AGENT_TIMEOUT_MS="3000"))
        f = finals(ndjson(r.stdout)).get(t, {})
        print("     | " + r.stdout.rstrip().replace("\n", "\n     | "))
        check("never idle: failed/not_idle after the timeout, exit 1",
              r.returncode == 1 and f.get("result") == "failed" and
              f.get("reason") == "not_idle" and
              "not ready within 3s" in f.get("detail", "") and
              f.get("session_id") == "")
        check("never idle: pause then resume message typed, no /exit",
              typed(t)[:1] and typed(t)[0].startswith(PAUSE) and
              typed(t)[-1].startswith(GAVE_UP) and "/exit" not in typed(t))
        ci = cmd(t)
        check("never idle: session still running, not armed, no marker, "
              "record not ended",
              ci and ci["state"] == "running" and len(starts(t)) == 1 and
              not marker(t) and (rec(t) or {}).get("end") == "")
        run("-k", t)
        names.remove(t)

        # --- one SIGTERM while pausing: interrupted, resumed --------------
        i = sess("sti", "claude", shell=True)
        up(i)
        proc = subprocess.Popen([DCH, "--stop", i, "--json"],
                                stdout=subprocess.PIPE, text=True)
        first = proc.stdout.readline()
        until(lambda: typed(i))
        time.sleep(0.5)
        proc.send_signal(signal.SIGTERM)
        rest = proc.communicate(timeout=60)[0]
        objs = ndjson(first + rest)
        f = finals(objs).get(i, {})
        print("     | " + (first + rest).rstrip().replace("\n", "\n     | "))
        check("SIGTERM while pausing: interrupted/signal, exit 1",
              proc.returncode == 1 and objs is not None and
              json.loads(first) == {"name": i, "phase": "pausing"} and
              f.get("result") == "interrupted" and
              f.get("reason") == "signal" and
              "resume message typed" in f.get("detail", ""))
        ci = cmd(i)
        check("SIGTERM while pausing: resume typed, no /exit, still running",
              typed(i)[-1].startswith(GAVE_UP) and "/exit" not in typed(i)
              and ci and ci["state"] == "running" and not marker(i))
        run("-k", i)
        names.remove(i)

        # --- --all over a mix, --json -------------------------------------
        mc = sess("smc", "claude")
        ms = sess("sms", "sh")
        me = sess("sme", "claude")
        ma = sess("sma", "sleep", "60")
        for n in (mc, me):
            up(n)
        run("--respawn", ma, "--", "sleep", "61")
        until(lambda: (cmd(ma) or {}).get("state") == "armed")
        sidc = starts(mc)[0]["sid"]
        r = stop("--all", "--json", env=dict(os.environ, DCH_SESSION=me))
        objs = ndjson(r.stdout)
        print("     | " + r.stdout.rstrip().replace("\n", "\n     | "))
        f = finals(objs)
        check("--all --json: every line JSON, one final per session, "
              "exit 0", objs is not None and r.returncode == 0 and
              set(f) == {mc, ms, me, ma} and
              len([o for o in objs if "result" in o]) == 4 and
              all(sorted(o) == FINAL for o in f.values()))
        def seq(n):
            return [o.get("phase") or o["result"] for o in objs or []
                    if o["name"] == n]
        check("--all --json: each session's phases, then its final",
              seq(mc) == ["pausing", "exiting", "ending", "ok"] and
              seq(ms) == ["ending", "ok"] and seq(me) == ["skipped"] and
              seq(ma) == ["skipped"])
        check("--all: claude stopped with its id, sh stopped",
              f[mc]["result"] == "ok" and f[mc]["session_id"] == sidc and
              f[ms]["result"] == "ok" and f[ms]["session_id"] == "" and
              gone(mc) and rec(mc)["end"] == "stopped" and
              gone(ms) and rec(ms)["end"] == "stopped")
        check("--all: self skipped, nothing typed, still running",
              f[me]["result"] == "skipped" and f[me]["reason"] == "self" and
              typed(me) == [] and cmd(me))
        check("--all: a pending respawn skipped/respawn_pending, still armed",
              f[ma]["result"] == "skipped" and
              f[ma]["reason"] == "respawn_pending" and
              (cmd(ma) or {}).get("state") == "armed")
        r = stop(me, env=dict(os.environ, DCH_SESSION=me))
        check("self by name: skipped — the session you are in, exit 0",
              r.returncode == 0 and
              r.stdout == "%s: skipped — the session you are in\n" % me)

        # --- --revive --all skips stopped --------------------------------
        x = sess("stx", "sh", "-c", "sleep 0.3; exit 3")
        gone(x)
        for n in (x, mc, ms, p):
            ran(n, 600)
        r = run("--revive", "--all")
        names.append(x)
        print("     | " + r.stdout.rstrip().replace("\n", "\n     | "))
        check("--revive --all takes the exit 3, skips every stopped record",
              r.stdout.startswith(x + ": revived as") and
              not any(l.startswith((mc + ":", ms + ":", p + ":"))
                      for l in r.stdout.splitlines()) and
              cmd(mc) is None and cmd(ms) is None)
        return finish(fails[0], tmp, names, pids)
    except Exception:
        traceback.print_exc()
        finish(fails[0], tmp, names, pids)
        return 1


def finish(fails, tmp, names, pids):
    for n in names:
        subprocess.run([DCH, "-k", n], capture_output=True)
    time.sleep(0.3)
    for p in pids:          # safety net: only the fakes this test started
        try:
            os.kill(p, 0)
            os.kill(p, signal.SIGKILL)
        except OSError:
            pass
    shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print("FAIL: %d --stop check(s) failed" % fails)
        return 1
    print("PASS: --stop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
