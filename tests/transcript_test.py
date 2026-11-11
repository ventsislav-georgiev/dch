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
two Claudes in one session; a record whose procStart does not match; a
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
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchv", dir="/tmp"))
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
        sh = spawn(c, "/bin/sh", "-c",
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
