"""--ls-json "claude_session_id" and "transcript_path" per row (#079).

Both come from Claude Code's own record <config>/sessions/<pid>.json of the
one live interactive Claude under the session's child (procStart verified),
read fresh on every call; the transcript is the one regular
<config>/projects/*/<sessionId>.jsonl. Anything less certain is null.

Drives tests/fake_claude.py as one session's command, and hand-written
records for plain `sleep` children elsewhere, with HOME, the runtime dir
and the state dir all temporary. Covers: id and path for a session running
one Claude; the id following a rewritten record (/clear, resume); a missing
.jsonl; two .jsonl under different project dirs; a session with no Claude;
two Claudes in one session, the --xray --json answer's "claudes" count
saying so, 0 for a client that cannot see their records (#256); a Claude
with Claudes below it (the ancestor
joins, --xray --json agreeing, #253); a record read while it is rewritten
in place, and no wait for an empty one left by a dead Claude (#253); a
record whose procStart does not match; a
sessionId that is no UUID; CLAUDE_CONFIG_DIR honoured; null once the
program exits (its record removed, or left behind under a held session).

Run:  python3 tests/transcript_test.py        (uses ./dch)
"""
import json, os, shutil, signal, stat, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
MISSING = "<no such field>"


def run(*args, env=None):
    return subprocess.run([DCH] + list(args), capture_output=True, text=True,
                          timeout=60, env=env)


def until(fn, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def proc_start(pid):
    return subprocess.run(
        ["/bin/ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
        text=True, env={"LC_ALL": "C", "TZ": "UTC"}).stdout.strip()


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchv", dir=os.environ.get("DCH_TEST_TMP", "/tmp")))
    home, fc, bindir = (os.path.join(tmp, d) for d in ("home", "fc", "bin"))
    cfg = os.path.join(home, ".claude")
    sessdir, projects = os.path.join(cfg, "sessions"), os.path.join(cfg,
                                                                   "projects")
    for d in (fc, bindir, sessdir, projects):
        os.makedirs(d)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    p = os.path.join(bindir, "claude")
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
    no_budget = os.environ.get("DCH_TEST_NO_BUDGET")  # before the scrub
    for k in list(os.environ):
        if k.startswith("CLAUDE") or k.startswith("DCH_"):
            del os.environ[k]
    os.environ.update(HOME=home, XDG_RUNTIME_DIR=tmp,
                      XDG_STATE_HOME=os.path.join(tmp, "state"),
                      FAKE_CLAUDE_DIR=fc, DCH_LOGIN_ENV="0",
                      DCH_CONFIG=os.path.join(tmp, "config"),
                      PATH=bindir + os.pathsep + os.environ["PATH"])
    names = []
    fails = [0]

    def check(msg, cond):
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails[0] += 1

    def rows(env=None):
        out = run("--ls-json", env=env).stdout
        try:
            return {r["name"]: r for r in json.loads(out)}
        except ValueError:
            return {}

    def field(name, key, env=None):
        return rows(env).get(name, {}).get(key, MISSING)

    def spawn(name, *cmd):
        names.append(name)
        run("--spawn", name, *cmd)
        return until(lambda: child(name))

    def child(name):
        try:
            return json.loads(run("--cmd", name).stdout)["pid"]
        except (ValueError, KeyError, TypeError):
            return None

    def xray_sid(name):
        # the sid --xray --json joins, None for any corpus but a Claude's
        try:
            o = json.loads(run("--xray", name, "--json").stdout)
        except ValueError:
            return None
        return o.get("session_id") if o.get("corpus") == "claude" else None

    def xray_json(name, env=None):
        try:
            return json.loads(run("--xray", name, "--json", env=env).stdout)
        except ValueError:
            return {}

    def record(pid, sid, start=None, where=sessdir):
        rec = {"pid": pid, "sessionId": sid, "cwd": tmp,
               "procStart": proc_start(pid) if start is None else start,
               "version": "2.1.0", "kind": "interactive", "status": "idle",
               "statusUpdatedAt": 1}
        path = os.path.join(where, "%d.json" % pid)
        with open(path + ".tmp", "w") as f:
            json.dump(rec, f)
        os.rename(path + ".tmp", path)

    def transcript(sid, slug="-tmp-proj", top=projects):
        d = os.path.join(top, slug)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, sid + ".jsonl")
        open(path, "w").close()
        return path

    try:
        # --- one Claude as the session's command (the fake) --------------
        fake = "tt-fake"
        spawn(fake, "claude")
        sid = until(lambda: (field(fake, "claude_session_id") or None)
                    if field(fake, "claude_session_id") != MISSING else None)
        recs = [json.load(open(os.path.join(sessdir, f)))
                for f in os.listdir(sessdir)]
        check("one claude: claude_session_id is its record's sessionId",
              sid is not None and [r["sessionId"] for r in recs] == [sid])
        check("one claude, no .jsonl yet: transcript_path null",
              field(fake, "transcript_path") is None)
        tp = transcript(sid) if sid else None
        check("one claude: transcript_path is the one .jsonl (absolute)",
              tp and field(fake, "transcript_path") == tp
              and os.path.isabs(tp))
        tp2 = transcript(sid, "-other-proj") if sid else None
        check("two .jsonl under different project dirs: path null, id kept",
              field(fake, "transcript_path") is None
              and field(fake, "claude_session_id") == sid)
        if tp2:
            os.unlink(tp2)
        check("the second .jsonl gone: path back",
              tp and field(fake, "transcript_path") == tp)

        # exit: the fake removes its record; a hold keeps the row
        run("--respawn", fake)
        # --run into Claude goes through the composer gate (#112), which
        # never waits: let the fake draw its composer first
        until(lambda: "auto mode on" in run("--read", fake).stdout)
        run("--run", fake, "/exit")
        until(lambda: rows().get(fake, {}).get("state") is not None and
              field(fake, "claude_session_id") is None, 15)
        r = rows().get(fake, {})
        check("program exited (held row): both null",
              fake in rows() and r.get("claude_session_id", MISSING) is None
              and r.get("transcript_path", MISSING) is None)

        # --- hand-written records on a plain child ------------------------
        a = "tt-rec"
        pa = spawn(a, "/bin/sleep", "120")
        s1 = "11111111-2222-3333-4444-555555555555"
        s2 = "66666666-7777-8888-9999-aaaaaaaaaaaa"
        record(pa, s1)
        t1, t2 = transcript(s1), transcript(s2)
        check("record on the child: id and path",
              field(a, "claude_session_id") == s1
              and field(a, "transcript_path") == t1)
        record(pa, s2)	# /clear and resume rewrite the record's sessionId
        check("rotation: the next --ls-json follows the new sessionId",
              field(a, "claude_session_id") == s2
              and field(a, "transcript_path") == t2)
        record(pa, s1, start="Thu Jan  1 00:00:00 1970")
        check("procStart mismatch: both null",
              field(a, "claude_session_id") is None
              and field(a, "transcript_path") is None)
        record(pa, "../../../../etc/passwd")
        check("a sessionId that is no UUID: both null",
              field(a, "claude_session_id") is None
              and field(a, "transcript_path") is None)
        record(pa, s1)
        check("record restored: id back", field(a, "claude_session_id") == s1)

        # --- a session with no Claude -------------------------------------
        b = "tt-none"
        spawn(b, "/bin/sleep", "120")
        check("non-claude session: both null",
              field(b, "claude_session_id") is None
              and field(b, "transcript_path") is None)

        # --- two Claudes under one session --------------------------------
        c = "tt-two"
        # under a HOME without their records: a client follows a Claude's
        # own HOME (#062), and Linux lets it read any env of its uid
        sh = spawn(c, "/usr/bin/env", "HOME=" + tmp, "/bin/sh", "-c",
                   "/bin/sleep 120 & /bin/sleep 120 & wait")
        kids = until(lambda: (lambda k: k if len(k) == 2 else None)(
            subprocess.run(["/usr/bin/pgrep", "-P", str(sh)],
                           capture_output=True, text=True).stdout.split()))
        for i, k in enumerate(kids or []):
            record(int(k), "%08d-0000-0000-0000-000000000000" % i)
        check("two claudes in one session: both null",
              kids and field(c, "claude_session_id") is None
              and field(c, "transcript_path") is None)
        check("the other sessions keep theirs",
              field(a, "claude_session_id") == s1)
        check("two claudes: --xray --json joins no Claude either",
              xray_sid(c) is None)
        # #256: the answer says why: how many Claudes it found there
        o = xray_json(c)
        check("two claudes: --xray --json says corpus none, claudes 2",
              o.get("corpus") == "none" and o.get("claudes") == 2)
        # a client whose HOME holds no record finds none (the master did)
        o = xray_json(c, dict(os.environ, HOME=tmp))
        check("two claudes, records out of the client's sight: claudes 0",
              o.get("corpus") == "none" and o.get("claudes") == 0)
        check("a shell with no agent: no claudes field",
              "claudes" not in xray_json(b))

        # --- a Claude with another Claude below it (#253) -----------------
        # a subagent's test suite runs a fake Claude, under its own temp
        # HOME, inside the session's tree: the ancestor is still the answer
        n = "tt-nest"
        nhome = os.path.join(tmp, "nesthome")
        os.makedirs(os.path.join(nhome, ".claude", "sessions"))
        # not /bin/sh or /bin/sleep: macOS hides a platform binary's env,
        # and HOME there is what leads the scan to the nested records
        sn = spawn(n, "/bin/sh", "-c",
                   "HOME=%s '%s' -c 'import os,time; os.fork() or "
                   "time.sleep(120); time.sleep(120)' & wait"
                   % (nhome, sys.executable))
        sub = until(lambda: (lambda k: k if len(k) == 1 else None)(
            subprocess.run(["/usr/bin/pgrep", "-P", str(sn)],
                           capture_output=True, text=True).stdout.split()))
        leaf = sub and until(lambda: (lambda k: k if len(k) == 1 else None)(
            subprocess.run(["/usr/bin/pgrep", "-P", sub[0]],
                           capture_output=True, text=True).stdout.split()))
        s4 = "44444444-4444-4444-4444-444444444444"
        s5 = "55555555-5555-5555-5555-555555555555"
        transcript(s4)
        record(sn, s4)
        if leaf:
            record(int(leaf[0]), s5,
                   where=os.path.join(nhome, ".claude", "sessions"))
            record(int(sub[0]), "%s-0000-0000-0000-000000000000" % ("7" * 8),
                   where=os.path.join(nhome, ".claude", "sessions"))
        check("nested claudes: --ls-json joins the ancestor",
              leaf and field(n, "claude_session_id") == s4)
        check("nested claudes: --xray --json agrees",
              leaf and xray_sid(n) == s4)
        # the ancestor gone: two Claudes left, one under the other
        os.unlink(os.path.join(sessdir, "%d.json" % sn))
        check("ancestor's record gone: the next one down joins",
              field(n, "claude_session_id") == "7" * 8 +
              "-0000-0000-0000-000000000000")
        # the middle one gone instead: the ancestor two generations up
        record(sn, s4)
        if leaf:
            os.unlink(os.path.join(nhome, ".claude", "sessions",
                                   "%s.json" % sub[0]))
        check("nested through a non-Claude process: the ancestor joins",
              field(n, "claude_session_id") == s4
              and xray_sid(n) == s4)

        # the ancestor's record read last: its config dir is the root's
        # own, ours (read first) holds two sibling Claudes below it
        m = "tt-nest2"
        mhome = os.path.join(tmp, "nesthome2")
        os.makedirs(os.path.join(mhome, ".claude", "sessions"))
        sm = spawn(m, "/usr/bin/env", "HOME=" + mhome, sys.executable, "-c",
                   "import os,time\n"
                   "for _ in (1, 2):\n"
                   "    os.fork() or time.sleep(120)\n"
                   "time.sleep(120)\n")
        twins = until(lambda: (lambda k: k if len(k) == 2 else None)(
            subprocess.run(["/usr/bin/pgrep", "-P", str(sm)],
                           capture_output=True, text=True).stdout.split()))
        s6 = "66666666-0000-0000-0000-000000000006"
        record(sm, s6, where=os.path.join(mhome, ".claude", "sessions"))
        for i, k in enumerate(twins or []):
            record(int(k), "%08d-1111-1111-1111-111111111111" % i)
        check("siblings below a Claude read before it: the ancestor joins",
              twins and field(m, "claude_session_id") == s6)
        os.unlink(os.path.join(mhome, ".claude", "sessions", "%d.json" % sm))
        check("the same siblings without it: none",
              twins and field(m, "claude_session_id") is None)
        # done with: their four live records would cost every later
        # --ls-json four procStart reads (a forked ps each, a /proc walk
        # on Linux), over the budget's own records below (#261)
        run("-k", n)
        run("-k", m)

        # --- a record read while Claude rewrites it in place (#253) -------
        # Claude Code updates its record with fs.writeFile: O_TRUNC, then
        # the write, so a read in between sees an empty file. Empty 5ms of
        # every 30, not 3 of 10: on a slow runner a re-read stretched to the
        # writer's own period kept landing in its empty phase (#261). A
        # rewrite the writer took over 45ms to finish (a runner that did
        # not schedule it) outlasts the reader's 10 waits of 5ms: only a
        # sample such a stall overlaps may miss. Timed from before the
        # truncate to after the write, the whole empty span; and the writer
        # is stopped between rewrites, not killed, so a stall still under
        # way at the last sample is logged too (#270).
        rp = os.path.join(sessdir, "%d.json" % pa)
        body = open(rp).read()
        stalls = os.path.join(tmp, "stalls")
        w = subprocess.Popen([sys.executable, "-c",
            "import os,sys,time\n"
            "p,b,s=sys.argv[1],sys.argv[2].encode(),sys.argv[3]\n"
            "while not os.path.exists(s+'.stop'):\n"
            "    t=time.monotonic(); fd=os.open(p,os.O_WRONLY|os.O_TRUNC)\n"
            "    time.sleep(0.005)\n"
            "    os.write(fd,b); u=time.monotonic(); os.close(fd)\n"
            "    if u-t>0.045: open(s,'a').write('%f %f\\n'%(t,u))\n"
            "    time.sleep(0.025)\n", rp, body, stalls])
        trace = os.path.join(tmp, "trace")
        dbg = dict(os.environ, DCH_DEBUG=trace)
        got = []
        try:
            for _ in range(60):
                t0 = time.monotonic()
                g = field(a, "claude_session_id", dbg)
                got.append((g, t0, time.monotonic()))
        finally:
            open(stalls + ".stop", "w").close()
            try:
                w.wait(timeout=30)
            except subprocess.TimeoutExpired:
                w.kill()
                w.wait()
            record(pa, s1)	# killed mid-write, it may have left it empty
        try:
            st = [tuple(map(float, l.split())) for l in open(stalls)]
        except OSError:
            st = []
        miss = [(t0, t1) for g, t0, t1 in got if g != s1]
        stalled = [any(t < t1 and u > t0 for t, u in st) for t0, t1 in miss]
        check("record rewritten in place: no sample misses (%d of 60, %d "
              "beside a writer stalled past the re-reads)"
              % (len(miss), sum(stalled)), all(stalled))
        def traced(what):
            try:
                return open(trace).read().count(what)
            except OSError:
                return 0
        rereads = traced("empty, read again")
        check("a live pid's empty record is read again (%d times)" % rereads,
              rereads > 0)

        # --- an empty record that stays empty (#253) ----------------------
        # a Claude killed between its truncate and its write: no wait for
        # it, or every scan of the dir pays it for ever (10 waits of 5ms per
        # dir scan, two scans per --ls-json: 100ms; the bound leaves room
        # for load)
        dead = subprocess.Popen(["/usr/bin/true"])
        dead.wait()
        stale = [os.path.join(sessdir, "%d.json" % dead.pid),
                 os.path.join(sessdir, "stale.json")]

        def fastest():
            best = None
            for _ in range(10):
                t0 = time.perf_counter()
                rows(dbg)
                t = time.perf_counter() - t0
                best = t if best is None else min(best, t)
            return best
        clean = fastest()
        for f in stale:
            open(f, "w").close()
        open(trace, "w").close()
        slow = fastest()
        waited = traced("read again")
        for f in stale:
            os.unlink(f)
        check("empty dead-pid and non-pid records: no re-read (%d)" % waited,
              waited == 0)
        check("... and no wait: fastest of 10 %.0fms vs %.0fms clean"
              % (slow * 1000, clean * 1000), slow - clean < 0.06)

        # --- CLAUDE_CONFIG_DIR is honoured --------------------------------
        e = "tt-cfg"
        alt = os.path.join(tmp, "altcfg")
        os.makedirs(os.path.join(alt, "sessions"))
        pe = spawn(e, "/bin/sleep", "120")
        s3 = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
        record(pe, s3, where=os.path.join(alt, "sessions"))
        t3 = transcript(s3, top=os.path.join(alt, "projects"))
        env = dict(os.environ, CLAUDE_CONFIG_DIR=alt)
        check("CLAUDE_CONFIG_DIR: record and transcript under it",
              field(e, "claude_session_id", env) == s3
              and field(e, "transcript_path", env) == t3)

        # --- exit with the record left behind (a SIGKILLed Claude) --------
        run("--respawn", a)
        os.kill(pa, signal.SIGKILL)
        until(lambda: rows().get(a, {}).get("state") is not None and
              child(a) is None)
        check("program exited, its record left behind: both null",
              os.path.exists(os.path.join(sessdir, "%d.json" % pa))
              and a in rows()
              and field(a, "claude_session_id") is None
              and field(a, "transcript_path") is None)

        # --- budget: the scan stays cheap ---------------------------------
        if not no_budget:
            # fastest of 20: a sleep slows every call, load only some (#154)
            def took():
                t0 = time.perf_counter()
                rows()
                return time.perf_counter() - t0
            check("--ls-json with records: fastest of 20 calls < 100ms",
                  min(took() for _ in range(20)) < 0.1)
    finally:
        for n in names:
            run("-k", n)
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if fails[0] else 0


if __name__ == "__main__":
    sys.exit(main())
