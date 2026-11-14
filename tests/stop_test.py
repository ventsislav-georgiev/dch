"""`dch --stop`: pause Claude, /exit, end the session as "stopped" (#046).

Drives tests/fake_claude.py as `claude` first on PATH, with HOME, the
runtime dir and the state dir all temporary. Covers: a Claude that is the
session's command stopped (pause typed, /exit, the record ends "stopped"
with the exit line's id) and --revive'd back into that id with the
stopped-wording message; the same round trip for a Claude typed into a
shell, never /exit'ed (the record's id, the hangup ends shell and claude
together: `claude; touch f`, `claude && touch f` never run the touch,
`exec claude` too), with a record refresh every 100 ms never losing the
agent; a session without Claude stopped at once; a Claude that
never goes idle (failed after DCH_AGENT_TIMEOUT_MS, still running, resume
message typed); one SIGTERM while pausing (interrupted, resume message);
self (DCH_SESSION); a pending respawn; --all over a mix with --json (pure
NDJSON: phases, then one final object per session); --revive --all
skipping a stopped record; unknown names, an old master, usage errors;
#058: a master SIGSTOPped after /exit (failed with the id, the marker kept,
the record ends stopped with it), a session that ended during /exit with its
record stopped (ok, #050), a second Ctrl-C at "exiting" (interrupted with
the id, the record still ends stopped). #062: a Claude under its own
CLAUDE_CONFIG_DIR, as the command and typed into the shell, while --stop and
the master run without it (found by its own env: paused, /exit'ed, the
record ends stopped with its id, --revive brings it back under that dir);
#065: the record's agent keeps that dir as config_dir ("" for ~/.claude),
and --revive resumes under it whether it was typed (`CLAUDE_CONFIG_DIR=…
claude`, `HOME=… claude`) or came from the session's env; a
Claude whose session record cannot be read (failed/claude_unreadable,
nothing typed, the session left running). #067: a `claude -p` / `claude
mcp serve` stops like a plain session, /exit ignored (exit_failed, still
running, the id kept), a crash on /exit (ok, the record's id),
DCH_NO_HISTORY (no --revive promised). On
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
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchv", dir=os.environ.get("DCH_TEST_TMP", "/tmp")))
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
                      DCH_AGENT_TIMEOUT_MS="60000",
                      # never the developer's login shell (#057)
                      DCH_LOGIN_ENV="0")
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

    def alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

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
        # a 1.23 master speaks --cmd but knows no .stopped: refused
        v = sess("ov", "sh")
        vf = os.path.join(sockdir, v + ".sock.ver")
        until(lambda: os.path.exists(vf))
        with open(vf + ".tmp", "w") as f:
            f.write("1.23.0\n")
        os.rename(vf + ".tmp", vf)
        r = stop(v, "--json")
        check("1.23 master: failed/old_master, exit 1, session still up",
              r.returncode == 1 and
              (finals(ndjson(r.stdout)).get(v) or {})
              .get("reason") == "old_master" and cmd(v) is not None)
        run("-k", v)
        until(lambda: cmd(v) is None)

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

        # --- #143: /exit raises Claude Code's "Background work is running"
        # prompt (a monitor the record does not show): dch answers 1 and
        # says what it stopped
        if not lite:
            bw = sess("stbw", "claude", bgwork=["monitor \u00b7 watch PRs"])
            b1 = up(bw)
            bsid = b1 and b1["sid"]
            agent_sid(bw, bsid)
            r = stop(bw)
            print("     | " + r.stdout.rstrip())
            check("exit prompt: answered 1 once, stopped with the id, what "
                  "it stopped said, exit 0",
                  r.returncode == 0 and r.stdout ==
                  "%s: stopped (claude %s; revive with dch --revive %s) (at "
                  "/exit claude stopped the background work it still ran: "
                  "monitor \u00b7 watch PRs)\n" % (bw, bsid, bw) and
                  [e["text"] for e in events(bw)
                   if e["ev"] == "exit_answer"] == ["1"] and
                  gone(bw) and rec(bw)["end"] == "stopped")

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
              a["launch"][1:] == ["--chrome"] and a["cwd"] == proj and
              a.get("config_dir") == "")
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
        out = list(proc.stdout)
        proc.wait(timeout=120)
        done.set()
        w.join()
        objs = ndjson("".join(out))
        print("     | " + "".join(out).rstrip().replace("\n", "\n     | "))
        check("shell claude --json: pure NDJSON, phases pausing, ending, "
              "then one final ok with the id, exit 0",
              objs is not None and proc.returncode == 0 and
              [o.get("phase") for o in objs[:-1]] == ["pausing", "ending"] and
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
        check("shell claude: pause typed, never /exit",
              typed(shc)[:1] and typed(shc)[0].startswith(PAUSE) and
              "/exit" not in typed(shc) and
              not any(e["ev"] == "exit" for e in events(shc)))
        check("no record ever written without the agent during --stop (%d)"
              % len(seen),
              seen and all((r or {}).get("agent") and
                           r["agent"]["session_id"] == sid3
                           for t, r in seen if r))
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

        # --- what followed claude on the shell's line never runs ----------
        for base, op, js in (("stsa", ";", True), ("stsb", " &&", False)):
            n = sess(base, "sh")
            mark = os.path.join(tmp, n + ".ran")
            time.sleep(0.3)
            run("--run", n, "claude%s touch %s" % (op, mark))
            s5 = up(n)
            sid5 = s5 and s5["sid"]
            agent_sid(n, sid5)
            r = stop(n, *(["--json"] if js else []))
            print("     | " + r.stdout.rstrip().replace("\n", "\n     | "))
            objs = ndjson(r.stdout) if js else None
            check("`claude%s touch`: --stop ok with the record's id, exit 0"
                  % op, r.returncode == 0 and sid5 and
                  (r.stdout == "%s: stopped (claude %s; revive with dch "
                   "--revive %s)\n" % (n, sid5, n) if not js else
                   objs is not None and
                   [o.get("phase") for o in objs[:-1]] ==
                   ["pausing", "ending"] and
                   objs[-1] == {"name": n, "result": "ok", "reason": "",
                                "detail": "", "session_id": sid5}))
            # Poll, not a fixed sleep: under ASan on a CI runner the
            # teardown can take longer. Once the shell is gone the touch
            # can no longer run, so checking the mark after is sound.
            def parts():
                r6 = rec(n) or {}
                return {"no_exit": "/exit" not in typed(n),
                        "end": r6.get("end"),
                        "sid": (r6.get("agent") or {}).get("session_id")
                        == sid5,
                        "gone": cmd(n) is None, "no_marker": not marker(n),
                        "no_touch": not os.path.exists(mark)}
            want = {"no_exit": True, "end": "stopped", "sid": True,
                    "gone": True, "no_marker": True, "no_touch": True}
            got = want if until(lambda: parts() == want) else parts()
            check("`claude%s touch`: never /exit'ed, record stopped with the "
                  "id, session gone, the touch never ran" % op, got == want)
            if got != want:
                print("     | " + json.dumps(got))
            names.remove(n)
            r = run("--revive", n)
            names.append(n)
            s6 = up(n, 1)
            check("`claude%s touch`: --revive resumes it with --resume <id>"
                  % op, r.returncode == 0 and s6 and s6["sid"] == sid5 and
                  s6["argv"][:2] == ["--resume", sid5] and
                  not os.path.exists(mark))
            run("-k", n)
            names.remove(n)
        n = sess("stse", "sh")
        time.sleep(0.3)
        run("--run", n, "exec claude")
        s7 = up(n)
        sid7 = s7 and s7["sid"]
        agent_sid(n, sid7)
        r = stop(n)
        check("`exec claude`: --stop ok with the record's id, never /exit'ed,"
              " record stopped with the id",
              r.returncode == 0 and sid7 and
              r.stdout == "%s: stopped (claude %s; revive with dch --revive "
              "%s)\n" % (n, sid7, n) and "/exit" not in typed(n) and
              gone(n) and rec(n)["end"] == "stopped" and
              (rec(n)["agent"] or {}).get("session_id") == sid7)

        # --- #062: claude under its own CLAUDE_CONFIG_DIR -----------------
        # --stop and the master run without it: the record is found by the
        # claude process's own env
        alt = os.path.join(tmp, "alt-claude")
        g = sess("stcfg", "/usr/bin/env", "CLAUDE_CONFIG_DIR=" + alt, "claude")
        s8 = up(g)
        sid8 = s8 and s8["sid"]
        check("other CLAUDE_CONFIG_DIR (command): the master's record has "
              "the agent", sid8 and agent_sid(g, sid8))
        r = stop(g)
        print("     | " + r.stdout.rstrip())
        check("other CLAUDE_CONFIG_DIR (command): stopped with the id, "
              "pause then /exit typed, record ends stopped with the id",
              r.returncode == 0 and sid8 and
              r.stdout == "%s: stopped (claude %s; revive with dch --revive "
              "%s)\n" % (g, sid8, g) and
              typed(g)[:1] and typed(g)[0].startswith(PAUSE) and
              typed(g)[-1] == "/exit" and gone(g) and
              rec(g)["end"] == "stopped" and
              (rec(g)["agent"] or {}).get("session_id") == sid8 and
              rec(g)["agent"].get("config_dir") == alt)
        r = run("--revive", g)
        names.append(g)
        s9 = up(g, 1)
        check("other CLAUDE_CONFIG_DIR (command): --revive resumes the id "
              "under that dir",
              r.returncode == 0 and s9 and s9["sid"] == sid8 and
              s9["cfg"] == alt)
        run("-k", g)
        names.remove(g)
        until(lambda: cmd(g) is None)
        g = sess("stcfgs", "sh")
        time.sleep(0.3)
        run("--run", g, "CLAUDE_CONFIG_DIR=%s claude" % alt)
        s8 = up(g)
        sid8 = s8 and s8["sid"]
        check("other CLAUDE_CONFIG_DIR (typed into the shell): the master's "
              "record has the agent", sid8 and agent_sid(g, sid8))
        r = stop(g)
        print("     | " + r.stdout.rstrip())
        gone(g)
        print("     | typed %r; record end %r agent %r" % (
            [t[:20] for t in typed(g)], (rec(g) or {}).get("end"),
            ((rec(g) or {}).get("agent") or {}).get("session_id")))
        check("other CLAUDE_CONFIG_DIR (typed into the shell): stopped with "
              "the id, pause typed, record ends stopped with the id",
              r.returncode == 0 and sid8 and
              r.stdout == "%s: stopped (claude %s; revive with dch --revive "
              "%s)\n" % (g, sid8, g) and
              typed(g)[:1] and typed(g)[0].startswith(PAUSE) and
              (rec(g) or {}).get("end") == "stopped" and
              (rec(g)["agent"] or {}).get("session_id") == sid8)

        # --- #065: --revive brings it back under that dir ------------------
        # The typed `CLAUDE_CONFIG_DIR=… claude` (and `HOME=… claude`) is in
        # no argv: the record's config_dir carries it, the revive types
        # `env CLAUDE_CONFIG_DIR=… claude --resume <id>`. A claude that got
        # it from the session's env (argv claude) gets it in the env again.
        def cfg_round(g, sid, want, what):
            a = (rec(g) or {}).get("agent") or {}
            check("%s: the stopped record keeps config_dir [%r]"
                  % (what, a.get("config_dir")),
                  a.get("session_id") == sid and a.get("config_dir") == want)
            r = run("--revive", g, "--no-message")
            names.append(g)
            s = up(g, 1)
            check("%s: --revive resumes the id under it [%r]"
                  % (what, s and (s["argv"], s["cfg"])),
                  r.returncode == 0 and s and
                  s["argv"] == ["--resume", sid] and s["cfg"] == want)
            run("-k", g)
            names.remove(g)
            until(lambda: cmd(g) is None)

        cfg_round(g, sid8, alt, "CLAUDE_CONFIG_DIR typed into the shell")
        h2 = os.path.join(tmp, "home2")
        g = sess("stcfgh", "sh")
        time.sleep(0.3)
        run("--run", g, "HOME=%s claude" % h2)
        s8 = up(g)
        sid8 = s8 and s8["sid"]
        agent_sid(g, sid8)
        r = stop(g)
        gone(g)
        check("HOME typed into the shell: stopped with the id",
              r.returncode == 0 and sid8 and sid8 in r.stdout)
        cfg_round(g, sid8, os.path.join(h2, ".claude"),
                  "HOME typed into the shell")
        g = "stcfge" + TAG
        names.append(g)
        setctl(g)
        subprocess.run([DCH, "--spawn", g, "--size", "100x30", "claude"],
                       capture_output=True, check=True,
                       env=dict(os.environ, CLAUDE_CONFIG_DIR=alt))
        s8 = up(g)
        sid8 = s8 and s8["sid"]
        agent_sid(g, sid8)
        r = stop(g)
        gone(g)
        check("CLAUDE_CONFIG_DIR in the session's env (argv claude): "
              "stopped with the id", r.returncode == 0 and sid8 and
              sid8 in r.stdout)
        cfg_round(g, sid8, alt, "CLAUDE_CONFIG_DIR in the session's env")

        # --- #062: a claude whose record cannot be read is left alone -----
        u = sess("stunr", "claude")
        s10 = up(u)
        until(lambda: (rec(u) or {}).get("agent"))
        if s10:
            os.unlink(os.path.join(home, ".claude", "sessions",
                                   "%d.json" % s10["pid"]))
        r = stop(u, "--json")
        f = finals(ndjson(r.stdout)).get(u, {})
        print("     | " + r.stdout.rstrip().replace("\n", "\n     | "))
        check("record unreadable: failed/claude_unreadable, exit 1, nothing "
              "typed",
              s10 and r.returncode == 1 and f.get("result") == "failed" and
              f.get("reason") == "claude_unreadable" and
              f.get("session_id") == "" and typed(u) == [])
        ci = cmd(u)
        check("record unreadable: session and claude still running, no "
              "marker, record not ended",
              ci and ci["state"] == "running" and
              not any(e["ev"] == "exit" for e in events(u)) and
              s10 and alive(s10["pid"]) and not marker(u) and
              (rec(u) or {}).get("end") == "")
        run("-k", u)
        names.remove(u)

        # --- #067: a non-interactive claude (-p, a subcommand) has no
        # composer to pause and no record to read: the session stops like
        # a plain one, not claude_unreadable --------------------------------
        batch = os.path.join(tmp, "batch")
        os.makedirs(batch)
        bcl = os.path.join(batch, "claude")
        with open(bcl, "w") as f:
            # up-<session> once it runs: no record, nothing on screen
            f.write("#!/usr/bin/env python3\nimport os, time\n"
                    "open(os.path.join(os.path.dirname(__file__), 'up-' + "
                    "os.environ['DCH_SESSION']), 'w').close()\n"
                    "time.sleep(120)\n")
        os.chmod(bcl, 0o755)

        def batch_up(n):
            return os.path.exists(os.path.join(batch, "up-" + n))

        for k, (what, cl) in enumerate((
                ("claude -p", [bcl, "-p", "hi"]),
                ("claude mcp serve, typed into a shell",
                 ["sh", "-c", "'%s' mcp serve; sleep 120" % bcl]))):
            b = sess("stb%d" % k, *cl)
            until(lambda: cmd(b) and batch_up(b))
            r = stop(b, "--json")
            f = finals(ndjson(r.stdout)).get(b, {})
            print("     | " + r.stdout.rstrip().replace("\n", "\n     | "))
            check("%s in the session: stopped like a plain session, exit 0, "
                  "record ends stopped" % what,
                  r.returncode == 0 and f.get("result") == "ok" and
                  gone(b) and rec(b)["end"] == "stopped")

        # --- #067 (QA 1.28.0 missing tests): /exit ignored, a crash on it -
        x = sess("stn", "claude", noexit=True)
        sx = up(x)
        sidx = sx and sx["sid"]
        agent_sid(x, sidx)
        r = stop(x, "--json", env=dict(os.environ, DCH_AGENT_STEP_MS="3000"))
        f = finals(ndjson(r.stdout)).get(x, {})
        print("     | " + r.stdout.rstrip().replace("\n", "\n     | "))
        check("/exit ignored: failed exit_failed with the id, resume message "
              "typed, exit 1",
              r.returncode == 1 and f.get("result") == "failed" and
              f.get("reason") == "exit_failed" and
              f.get("session_id") == sidx and
              "resume message typed" in f.get("detail", "") and
              typed(x)[-1].startswith(GAVE_UP))
        ci = cmd(x)
        check("/exit ignored: still running, disarmed, the marker dropped, "
              "the record still tracks the id",
              ci and ci["state"] == "running" and not marker(x) and
              (rec(x) or {}).get("end") == "" and
              ((rec(x) or {}).get("agent") or {}).get("session_id") == sidx)
        run("-k", x)
        names.remove(x)
        y = sess("stk", "claude", crash=True)
        sy = up(y)
        sidy = sy and sy["sid"]
        agent_sid(y, sidy)
        r = stop(y, "--json")
        f = finals(ndjson(r.stdout)).get(y, {})
        print("     | " + r.stdout.rstrip().replace("\n", "\n     | "))
        check("crash on /exit: ok with the record's id, exit 0",
              r.returncode == 0 and f.get("result") == "ok" and
              f.get("session_id") == sidy and
              "used the session record's id" in f.get("detail", ""))
        check("crash on /exit: the record ends stopped with the id",
              gone(y) and rec(y)["end"] == "stopped" and
              (rec(y)["agent"] or {}).get("session_id") == sidy)

        # --- #067 L1: no record kept (DCH_NO_HISTORY): no revive promised -
        for what, cl in (("sh", ["sh"]), ("claude", ["claude"])):
            n = "stl%s%s" % (what, TAG)
            names.append(n)
            subprocess.run([DCH, "--spawn", n, "--size", "100x30"] + cl,
                           capture_output=True, check=True,
                           env=dict(os.environ, DCH_NO_HISTORY="1"))
            sl = up(n) if what == "claude" else None
            until(lambda: cmd(n))
            r = stop(n)
            print("     | " + r.stdout.rstrip())
            check("no record kept (%s): stopped, exit 0, no --revive "
                  "promised%s" % (what, ", the id to resume by hand"
                                  if sl else ""),
                  r.returncode == 0 and r.stdout.startswith(n + ": stopped (")
                  and "revive with" not in r.stdout and
                  "no session record kept" in r.stdout and rec(n) is None and
                  (not sl or ("claude --resume %s" % sl["sid"]) in r.stdout))
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

        # --- #058 H1/M6: the master goes quiet (SIGSTOP) after /exit ------
        def spawn_held(base, hold, **ctl):
            n = "%s%s" % (base, TAG)
            names.append(n)
            setctl(n, **ctl)
            subprocess.run([DCH, "--spawn", n, "--size", "100x30", "claude"],
                           capture_output=True, check=True,
                           env=dict(os.environ, DCH_HOLD_MS=str(hold)))
            s = up(n)
            agent_sid(n, s and s["sid"])
            return n, s and s["sid"]

        def master_pid(n):
            out = subprocess.run(["/bin/ps", "-o", "ppid=", "-p",
                                  str(cmd(n)["pid"])], capture_output=True,
                                 text=True).stdout.strip()
            return int(out)

        q, sidq = spawn_held("stq", 3000, exit_ms=1500)
        mq = master_pid(q)
        proc = subprocess.Popen([DCH, "--stop", q], stdout=subprocess.PIPE,
                                text=True, env=dict(os.environ,
                                                    DCH_AGENT_STEP_MS="3000"))
        try:
            until(lambda: "/exit" in typed(q), 60)
            os.kill(mq, signal.SIGSTOP)     # before claude quits
            out = proc.communicate(timeout=60)[0]
            mk = marker(q)
        finally:
            os.kill(mq, signal.SIGCONT)
        print("     | " + out.rstrip())
        check("quiet master after /exit: failed, the text line carries the "
              "id, exit 1",
              proc.returncode == 1 and out.startswith(q + ": failed — ") and
              "the master stopped answering after /exit" in out and
              ("claude --resume %s" % sidq) in out)
        check("quiet master after /exit: the .stopped marker survives", mk)
        check("quiet master after /exit: once it runs again and the hold "
              "ends, the record ends stopped with claude's id",
              gone(q) and rec(q)["end"] == "stopped" and
              (rec(q)["agent"] or {}).get("session_id") == sidq)

        # --- #050: the session ended during /exit, record stopped: ok ------
        g, sidg = spawn_held("stg", 300, exit_ms=500)
        proc = subprocess.Popen([DCH, "--stop", g, "--json"],
                                stdout=subprocess.PIPE, text=True)
        try:
            until(lambda: "/exit" in typed(g), 60)
            # claude quits, the hold runs out unseen by --stop
            proc.send_signal(signal.SIGSTOP)
            ended = until(lambda: (rec(g) or {}).get("end"), 20)
        finally:
            proc.send_signal(signal.SIGCONT)
        out = proc.communicate(timeout=60)[0]
        f = finals(ndjson(out)).get(g, {})
        print("     | " + out.rstrip().replace("\n", "\n     | "))
        check("ended during /exit, record stopped with the id: ok with the "
              "id, exit 0",
              ended == "stopped" and proc.returncode == 0 and
              f.get("result") == "ok" and f.get("session_id") == sidg and
              (rec(g)["agent"] or {}).get("session_id") == sidg)
        names.remove(g)

        # --- #058 M5/M6: Ctrl-C twice as --stop reaches "exiting" ----------
        d, sidd = spawn_held("std", 2000)
        proc = subprocess.Popen([DCH, "--stop", d, "--json"],
                                stdout=subprocess.PIPE, text=True)
        head = []
        while True:
            l = proc.stdout.readline()
            head.append(l)
            if '"exiting"' in l or not l:
                break
        proc.send_signal(signal.SIGINT)
        time.sleep(0.01)
        proc.send_signal(signal.SIGINT)
        out = "".join(head) + proc.communicate(timeout=60)[0]
        f = finals(ndjson(out)).get(d, {})
        print("     | " + out.rstrip().replace("\n", "\n     | "))
        check("second Ctrl-C at exiting: a final interrupted line with the "
              "id, exit 1",
              proc.returncode == 1 and f.get("result") == "interrupted" and
              f.get("reason") == "signal" and f.get("session_id") == sidd and
              ("claude --resume %s" % sidd) in f.get("detail", ""))
        check("second Ctrl-C at exiting: /exit typed, the record ends stopped "
              "with the id",
              until(lambda: "/exit" in typed(d)) and gone(d) and
              rec(d)["end"] == "stopped" and
              (rec(d)["agent"] or {}).get("session_id") == sidd)
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
