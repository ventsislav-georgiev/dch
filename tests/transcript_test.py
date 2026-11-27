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
in place, and no wait for an empty one left by a dead Claude (#253),
nor once its pid is reused (#274); a
record whose procStart does not match; a
sessionId that is no UUID; CLAUDE_CONFIG_DIR honoured; null once the
program exits (its record removed, or left behind under a held session).
"shells" (#481): the Claude's children with fd 1 on its own
.../<sessionId>/tasks/<id>.output, newest first, the eval'd command
unquoted and cut on a UTF-8 boundary; another session's or other stdout
not listed; the key absent with no Claude. Each shell's owner (#482) from
fixture transcripts: main's (agent null) found by a binary search in a
file past one read window, a subagent's (agentId, its meta description),
descriptions cut on a UTF-8 boundary, and agent/description/background
left out for a shell named only by a malformed or unfinished line.
#487: C1 controls as spaces (a byte each); a sidechain line in the main
file without "agent"; a subagent file older than the shell, or whose
complete last line has no newline yet, not taken; FIFOs as a subagent file
or meta skipped at once; 32 unnamed shells x 250 x 1.2 MB subagent files
under 2 s (the per-call read budget). #711: each shell's "output" and
"pgid"; --stop-shell ends a shell and its child, not the Claude or a
sibling shell; no_shell, not_claude, no_session, --json, usage exit 2.
#712 "subagents": running (also one silent 30 min), done (tool_result,
or meta finished:true alone), failed (is_error, with finished:true too),
a background agent done or killed by its <task-notification>; ended
before the 10-minute window, a day silent, no meta: left out. "row" off a
fake "<glyph> <agentType>  <activity>" panel: the running ones by launch
order, a lingering ended run aligned by type; a type mismatch, two
offsets or a same-ms same-type tie give null; key order.

Run:  python3 tests/transcript_test.py        (uses ./dch)
"""
import calendar
import json, shlex, os, shutil, signal, stat, subprocess, sys, tempfile, time

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

    def spawn(name, *cmd, env=None):
        names.append(name)
        run("--spawn", name, *cmd, env=env)
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
        check("non-claude session: no shells key",
              field(b, "shells") == MISSING)
        check("a Claude with no shells: shells []", field(a, "shells") == [])

        # --- "shells" (#481): its Bash tool shells, from the kernel -------
        # The "Claude" here starts each shell Claude Code's way: fd 1 on
        # <tmp>/claude-<uid>/<project>/<sessionId>/tasks/<id>.output
        h = "tt-sh"
        ctl = os.path.join(tmp, "shctl")
        os.mkdir(ctl)
        hp = spawn(h, sys.executable, "-c",
                   "import json,os,subprocess as sp,sys,time\n"
                   "c,seen=sys.argv[1],set()\n"
                   "while True:\n"
                   "    for f in sorted(os.listdir(c)):\n"
                   "        if f.endswith('.json') and f not in seen:\n"
                   "            seen.add(f); j=json.load(open(c+'/'+f))\n"
                   "            p=sp.Popen(j['argv'],stdout=open(j['out'],'ab'),"
                   "stdin=sp.DEVNULL,stderr=sp.DEVNULL,"
                   "start_new_session=bool(j.get('new')))\n"
                   "            open(c+'/'+f[:-5]+'.pid','w').write(str(p.pid))\n"
                   "    time.sleep(0.05)\n", ctl)
        sh_sid = "48148148-1481-4814-8148-148148148148"
        record(hp, sh_sid)
        shpids = []

        def shell(key, cmd, sid=sh_sid, out=None, new=False):
            d = os.path.join(tmp, "claude-%d" % os.getuid(), "-tmp-proj", sid,
                             "tasks")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(ctl, key + ".tmp"), "w") as f:
                json.dump({"out": out or os.path.join(d, key + ".output"),
                           "new": new,
                           "argv": ["/bin/sh", "-c", "true && eval " + cmd +
                                    " < /dev/null && pwd -P >| /dev/null"]},
                          f)
            os.rename(os.path.join(ctl, key + ".tmp"),
                      os.path.join(ctl, key + ".json"))
            pid = until(lambda: open(os.path.join(ctl, key + ".pid")).read()
                        if os.path.exists(os.path.join(ctl, key + ".pid"))
                        else None)
            shpids.append(int(pid))
            # its own child (sleep) holds the same fd 1: listed once
            until(lambda: subprocess.run(["/usr/bin/pgrep", "-P", pid],
                                         capture_output=True).stdout)
            return int(pid)

        def outp(key):
            return os.path.join(tmp, "claude-%d" % os.getuid(), "-tmp-proj",
                                sh_sid, "tasks", key + ".output")

        def ox(key, pid):
            return {"output": outp(key), "pgid": os.getpgid(pid)}

        def alive(pid):
            try:
                os.kill(pid, 0)
            except OSError:
                return False
            st = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)],
                                capture_output=True, text=True).stdout
            return not st.startswith("Z")

        def started(pid):
            return calendar.timegm(time.strptime(proc_start(pid),
                                                 "%a %b %d %H:%M:%S %Y"))
        try:
            # bash's quoting of ' in the eval argument ('\'')
            p1 = shell("bsh1one", "'sleep 120 ; echo '\\''hi'\\'''")
            want1 = {"id": "bsh1one", "pid": p1, "started": started(p1),
                     "command": "sleep 120 ; echo 'hi'",
                     **ox("bsh1one", p1)}
            got = field(h, "shells")
            check("one shell: its id, pid, start, command, output and pgid (%s)" % got,
                  got == [want1])
            time.sleep(1.1)	# a later start second: the order is by start
            # zsh's ('"'"')
            p2 = shell("bsh2two", "'sleep 120; echo '\"'\"'x y'\"'\"''")
            want2 = {"id": "bsh2two", "pid": p2, "started": started(p2),
                     "command": "sleep 120; echo 'x y'",
                     **ox("bsh2two", p2)}
            got = field(h, "shells")
            check("two shells: newest first (%s)" % got, got == [want2, want1])
            shell("bsh3oth", "'sleep 120; true'",
                  sid="99999999-9999-9999-9999-999999999999")
            shell("bsh4else", "'sleep 120; true'",
                  out=os.path.join(tmp, "elsewhere.output"))
            check("another session's tasks file, or stdout elsewhere: not "
                  "listed", field(h, "shells") == [want2, want1])
            # a 2-byte é across the 200-byte cut; a tab as a space
            lead = "sleep 120;\techo " + "a" * 183
            time.sleep(1.1)
            p5 = shell("bsh5utf", "'" + lead + "ézz'")
            got = field(h, "shells")
            c5 = got[0].get("command") if isinstance(got, list) and got \
                and got[0].get("pid") == p5 else None
            check("command cut at 200 bytes between UTF-8 scalars, control "
                  "bytes as spaces (%r)" % c5,
                  c5 == lead.replace("\t", " ") and
                  len(c5.encode()) == 199)

            # --- who started each shell (#482), from the transcripts ------
            # Fixtures only in the shapes Claude Code writes (compact JSON,
            # one object a line): the main conversation's background Bash
            # call 3 MB into a 5 MB file (past any one window: the binary
            # search by "timestamp" finds it), a subagent's in its own file
            # with a meta, and a shell no transcript names.
            p6 = shell("bsh6main", "'sleep 120'")
            p7 = shell("bsh7sub", "'sleep 120'")
            p8 = shell("bsh8none", "'sleep 120'")
            t6 = started(p6)

            def iso(t):
                return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))

            def jl(o):
                return json.dumps(o, separators=(",", ":"),
                                  ensure_ascii=False) + "\n"

            def call(tu, t, desc, agent=None):
                side = {"agentId": agent, "isSidechain": True} if agent \
                    else {"isSidechain": False}
                inp = {"command": "sleep 120", "run_in_background": True}
                if desc is not None:
                    inp["description"] = desc
                return jl(dict(side, type="assistant", timestamp=iso(t - 1),
                               message={"role": "assistant", "content": [
                                   {"type": "tool_use", "id": tu,
                                    "name": "Bash", "input": inp}]}))

            def result(tu, sid_, t, agent=None):
                side = {"agentId": agent, "isSidechain": True} if agent \
                    else {"isSidechain": False}
                return jl(dict(side, type="user", timestamp=iso(t),
                               message={"role": "user", "content": [
                                   {"type": "tool_result", "tool_use_id": tu,
                                    "content": "running"}]},
                               toolUseResult={"backgroundTaskId": sid_,
                                              "interrupted": False,
                                              "isImage": False,
                                              "noOutputExpected": False,
                                              "stderr": "", "stdout": ""}))

            def filler(t, n):
                return jl({"type": "assistant", "timestamp": iso(t),
                           "message": {"content": [{"type": "text",
                                                    "text": "x" * 1000}]}}) * n

            stp = transcript(sh_sid)
            with open(stp, "w") as f:
                f.write(filler(t6 - 3600, 3000))
                f.write(call("toolu_m6", t6, "Run the main build"))
                f.write(result("toolu_m6", "bsh6main", t6))
                f.write(filler(t6 + 30, 2000))
            check("main transcript past one window (%d bytes)"
                  % os.path.getsize(stp), os.path.getsize(stp) > 4 << 20)
            sub = os.path.join(os.path.dirname(stp), sh_sid, "subagents")
            os.makedirs(sub)
            d7 = "d" * 199 + "é tail"	# the 2-byte é across the 200-byte cut
            with open(os.path.join(sub, "agent-aSUB1.meta.json"), "w") as f:
                json.dump({"agentType": "Explore",
                           "description": "Explore\tthe tests",
                           "finished": False, "isBuiltIn": True,
                           "toolUseId": "toolu_agent"}, f)
            with open(os.path.join(sub, "agent-aSUB1.jsonl"), "w") as f:
                f.write(call("toolu_s7", t6, d7, "aSUB1"))
                f.write(result("toolu_s7", "bsh7sub", t6, "aSUB1"))
                # bsh8none only in a malformed line and an unfinished last one
                f.write('{"type":"user","toolUseResult":{"backgroundTaskId":'
                        '"bsh8none"},oops}\n')
                f.write('{"type":"user","toolUseResult":{"backgroundTaskId":'
                        '"bsh8none","stdout":"')
            with open(os.path.join(sub, "agent-aOLD2.jsonl"), "w") as f:
                f.write(filler(t6 - 3600, 3))	# a finished agent's
            got = {s.get("id"): s for s in (field(h, "shells") or [])
                   if isinstance(s, dict)}
            check("main's background shell: agent null, its description, "
                  "background (%s)" % got.get("bsh6main"),
                  got.get("bsh6main") == {
                      "id": "bsh6main", "pid": p6, "started": t6,
                      "command": "sleep 120", **ox("bsh6main", p6),
                      "agent": None, "description": "Run the main build",
                      "background": True})
            check("a subagent's shell: its agentId and description, the "
                  "call's description cut at 200 bytes between UTF-8 "
                  "scalars (%s)" % got.get("bsh7sub"),
                  got.get("bsh7sub") == {
                      "id": "bsh7sub", "pid": p7, "started": started(p7),
                      "command": "sleep 120", **ox("bsh7sub", p7),
                      "agent": "aSUB1",
                      "agent_description": "Explore the tests",
                      "description": "d" * 199, "background": True})
            check("a shell in no complete line: no agent, description or "
                  "background (%s)" % got.get("bsh8none"),
                  got.get("bsh8none") == {
                      "id": "bsh8none", "pid": p8, "started": started(p8),
                      "command": "sleep 120", **ox("bsh8none", p8)})
            keys = []
            out = run("--ls-json").stdout
            for r in json.loads(out, object_pairs_hook=lambda kv: kv):
                for s in dict(r).get("shells", []):
                    if dict(s).get("id") == "bsh7sub":
                        keys = [k for k, _ in s]
            check("keys in order after command (%s)" % keys, keys == [
                "id", "pid", "started", "command", "output", "pgid", "agent",
                "agent_description", "description", "background"])

            # --- #487: C1 controls, edge rows, FIFOs, a bounded cost ------
            # C1 controls (U+0085, U+009B) as spaces, one per byte as X-ray
            p9 = shell("bsh9c1", "'echo \u0085x\u009by; sleep 120'")
            p10 = shell("bsh10side", "'sleep 120'")
            p11 = shell("bsh11old", "'sleep 120'")
            p12 = shell("bsh12part", "'sleep 120'")
            p13 = shell("bsh13fifo", "'sleep 120'")
            p14 = shell("bsh14meta", "'sleep 120'")
            t9, t10, t11, t12, t14 = (started(p) for p in
                                      (p9, p10, p11, p12, p14))
            with open(stp, "w") as f:	# the new results before the tail
                f.write(filler(t6 - 3600, 3000))
                f.write(call("toolu_m6", t6, "Run the main build"))
                f.write(result("toolu_m6", "bsh6main", t6))
                f.write(call("toolu_c9", t9, "c\u0085d\u009be"))
                f.write(result("toolu_c9", "bsh9c1", t9))
                # a sidechain line in the main file: no agent key (mside)
                f.write(call("toolu_s10", t10, "Side", "aSIDE"))
                f.write(result("toolu_s10", "bsh10side", t10, "aSIDE"))
                f.write(filler(t14 + 30, 2000))
            # its result in a file last written before it started (mmtime)
            old = os.path.join(sub, "agent-aOLD3.jsonl")
            with open(old, "w") as f:
                f.write(call("toolu_o11", t11, "Old", "aOLD3"))
                f.write(result("toolu_o11", "bsh11old", t11, "aOLD3"))
            os.utime(old, (t11 - 1, t11 - 1))
            # a complete JSON last line with no newline yet (mpartial)
            with open(os.path.join(sub, "agent-aPART.jsonl"), "w") as f:
                f.write(call("toolu_p12", t12, "Part", "aPART"))
                f.write(result("toolu_p12", "bsh12part", t12,
                               "aPART").rstrip("\n"))
            # FIFOs as a subagent file and as a meta (only the user can)
            with open(os.path.join(sub, "agent-aMF.jsonl"), "w") as f:
                f.write(call("toolu_f14", t14, "Meta fifo", "aMF"))
                f.write(result("toolu_f14", "bsh14meta", t14, "aMF"))
            fifos = [os.path.join(sub, "agent-aFIFO.jsonl"),
                     os.path.join(sub, "agent-aMF.meta.json")]
            for q in fifos:
                os.mkfifo(q)
            t0 = time.time()
            try:
                out = subprocess.run([DCH, "--ls-json"], capture_output=True,
                                     text=True, timeout=10).stdout
            except subprocess.TimeoutExpired:
                out = "[]"
            took = time.time() - t0
            for q in fifos:	# a hung base binary must not hang the rest
                os.unlink(q)
            got = {}
            for r in json.loads(out or "[]"):
                if r.get("name") == h:
                    got = {s["id"]: s for s in r.get("shells", [])}
            base = {s: {"id": s, "pid": p, "started": started(p),
                        "command": "sleep 120", **ox(s, p)} for s, p in
                    (("bsh10side", p10), ("bsh11old", p11),
                     ("bsh12part", p12), ("bsh13fifo", p13),
                     ("bsh14meta", p14))}
            check("FIFOs named agent-<id>.jsonl and .meta.json: --ls-json "
                  "returns at once (%.2f s)" % took, took < 5)
            check("C1 controls as spaces, one per byte, in command and "
                  "description (%s)" % got.get("bsh9c1"),
                  got.get("bsh9c1") == {
                      "id": "bsh9c1", "pid": p9, "started": t9,
                      "command": "echo   x  y; sleep 120",
                      **ox("bsh9c1", p9), "agent": None,
                      "description": "c  d  e", "background": True})
            check("a sidechain line in the main file: no agent key (%s)"
                  % got.get("bsh10side"),
                  got.get("bsh10side") == dict(base["bsh10side"],
                                               description="Side",
                                               background=True))
            check("a subagent file older than the shell: not searched (%s)"
                  % got.get("bsh11old"),
                  got.get("bsh11old") == base["bsh11old"])
            check("a complete last line with no newline yet: not taken (%s)"
                  % got.get("bsh12part"),
                  got.get("bsh12part") == base["bsh12part"])
            check("a FIFO subagent file: no keys (%s)" % got.get("bsh13fifo"),
                  got.get("bsh13fifo") == base["bsh13fifo"])
            check("a FIFO meta: agent and description, no agent_description "
                  "(%s)" % got.get("bsh14meta"),
                  got.get("bsh14meta") == dict(base["bsh14meta"],
                                               agent="aMF",
                                               description="Meta fifo",
                                               background=True))

            # the read budget by bytes (#491), no timing: a window reads
            # ~1.07 MB of a 1.2 MB filler, so 16 MB is gone after 16 of them.
            # Subagent files are read in readdir order: bbud's result file is
            # made the last one (20 fillers before it), bctl's the 5th.
            pb = shell("bbud", "'sleep 120'")
            pc = shell("bctl", "'sleep 120'")
            tb, tc = started(pb), started(pc)
            src = os.path.join(tmp, "bud.jsonl")
            with open(src, "w") as f:
                f.write(filler(max(tb, tc) + 5, 1200))
            names = ["agent-aBUD%02d.jsonl" % k for k in range(22)]
            for nm in names:
                shutil.copyfile(src, os.path.join(sub, nm))
            order = [n for n in os.listdir(sub) if n in names]
            for nm, sid_, t_ in ((order[4], "bctl", tc),
                                 (order[-1], "bbud", tb)):
                with open(os.path.join(sub, nm), "r+") as f:	# same inode
                    f.truncate(0)
                    f.write(result("toolu_" + sid_, sid_, t_, nm[6:-6]))
            check("budget fixture: result files at readdir positions 4, 21",
                  [n for n in os.listdir(sub) if n in names] == order)
            got = {s.get("id"): s for s in (field(h, "shells") or [])
                   if isinstance(s, dict)}
            check("5th subagent file: shell found (%s)" % got.get("bctl"),
                  got.get("bctl", {}).get("agent") == order[4][6:-6])
            check("result past 16 MB of subagent reads: id, pid, started, "
                  "command only (%s)" % got.get("bbud"),
                  got.get("bbud") == {"id": "bbud", "pid": pb, "started": tb,
                                      "command": "sleep 120",
                                      **ox("bbud", pb)})
            for nm in names:
                os.unlink(os.path.join(sub, nm))

            # cost bounded per call (#487): 32 shells no transcript names x
            # 250 subagent files of 1.2 MB, every line since they started
            # (hard links: one file on disk). 1.102.0 read ~1 MB a pair.
            big = [shell("bsz%02d" % k, "'sleep 120'") for k in range(32)]
            src = os.path.join(tmp, "big.jsonl")
            with open(src, "w") as f:
                f.write(filler(started(big[-1]), 1200))
            for k in range(250):
                os.link(src, os.path.join(sub, "agent-aBIG%03d.jsonl" % k))
            run("--ls-json")	# warm the page cache
            t0 = time.time()
            out = run("--ls-json").stdout
            took = time.time() - t0
            got = {}
            for r in json.loads(out or "[]"):
                if r.get("name") == h:
                    got = {s["id"]: s for s in r.get("shells", [])}
            # ASan (the nightly) runs slower; 1.102.0 took 8 s unsanitized
            lim = 6 if "ASAN_OPTIONS" in os.environ else 2
            check("32 unnamed shells x 250 subagent files: --ls-json in "
                  "%.2f s (< %d s), main's shell still found" % (took, lim),
                  took < lim and got.get("bsh6main", {}).get("agent",
                                                           MISSING) is None
                  and len(got) >= 38)
            # --- --stop-shell (#711): the shell's tree, nothing else -----
            p20 = shell("bsh20stop", "'sleep 120; true'")
            k20 = [int(k) for k in subprocess.run(
                ["/usr/bin/pgrep", "-P", str(p20)], capture_output=True,
                text=True).stdout.split()]
            r = run("--stop-shell", h, "bsh20stop")
            check("--stop-shell: exit 0, '<s>: stopped <id>' (%r)" % r.stdout,
                  r.returncode == 0 and r.stdout == h + ": stopped bsh20stop\n")
            check("--stop-shell: the shell and its sleep are gone, the Claude "
                  "and a sibling shell live",
                  k20 and until(lambda: not any(alive(q) for q in [p20] + k20),
                               5) and alive(hp) and alive(p1) and
                  "bsh20stop" not in {x.get("id") for x in
                                     field(h, "shells") or []})
            r = run("--stop-shell", h, "nosuch", "--json")
            try:
                j = json.loads(r.stdout)
            except ValueError:
                j = None
            check("--stop-shell --json unknown id: one line, no_shell, exit 1 "
                  "(%r)" % r.stdout, r.returncode == 1 and j == {
                      "name": h, "id": "nosuch", "result": "failed",
                      "reason": "no_shell"} and r.stdout.count("\n") == 1)
            r = run("--stop-shell", h, "nosuch")
            check("--stop-shell unknown id: '<s>: failed — no_shell'",
                  r.returncode == 1 and r.stdout == h +
                  ": failed — no_shell\n")
            check("--stop-shell: no Claude, no session",
                  json.loads(run("--stop-shell", b, "x", "--json").stdout
                             or "{}").get("reason") == "not_claude" and
                  json.loads(run("--stop-shell", "tt-nope", "x", "--json")
                             .stdout or "{}").get("reason") == "no_session")
            check("--stop-shell usage: exit 2 (missing id, extra arg, flag)",
                  run("--stop-shell", h).returncode == 2 and
                  run("--stop-shell", h, "a", "b").returncode == 2 and
                  run("--stop-shell", h, "a", "-x").returncode == 2)
            check("--stop-shell usage: -x a and 4 positionals are exit 2 (#718)",
                  run("--stop-shell", h, "-x", "a").returncode == 2 and
                  run("--stop-shell", h, "a", "b", "c", "d").returncode == 2)
            # run inside the shell it stops: not signalled itself, prints (#718)
            ff = os.path.join(tmp, "self.json")
            ps = shell("bsh21self", shlex.quote(
                "trap '' TERM; (trap '' TERM; sleep 120.0718) & sleep 1; "
                "%s --stop-shell %s bsh21self --json > %s; sleep 120.0718"
                % (shlex.quote(DCH), h, shlex.quote(ff))))
            kk = [int(k) for k in subprocess.run(
                ["/usr/bin/pgrep", "-P", str(ps)], capture_output=True,
                text=True).stdout.split()]
            until(lambda: os.path.exists(ff) and os.path.getsize(ff), 10)
            try:
                j = json.load(open(ff))
            except (OSError, ValueError):
                j = None
            check("--stop-shell run inside the shell it stops still prints "
                  "its result (%r)" % j, j == {
                      "name": h, "id": "bsh21self", "result": "ok",
                      "reason": ""})
            check("  ... and that shell is gone", until(
                lambda: not any(alive(q) for q in [ps] + kk), 5))
            # a shell with its own fd 1 moved for the while (bash's `{ …; } >
            # f`, dash's `cmd > f`): only its earlier child shows the
            # .output, yet the shell is listed and stopped (#718)
            pm = shell("bsh21moved", shlex.quote(
                "sleep 120.0718 & { sleep 120.0718; } > /dev/null"))
            km = [int(k) for k in subprocess.run(
                ["/usr/bin/pgrep", "-P", str(pm)], capture_output=True,
                text=True).stdout.split()]
            check("a shell running '{ ...; } > f' is listed as itself, not "
                  "its child (#718)", until(lambda: [
                      x.get("pid") for x in field(h, "shells") or []
                      if x.get("id") == "bsh21moved"] == [pm], 5))
            r = run("--stop-shell", h, "bsh21moved", "--json")
            check("  ... and --stop-shell ends it and its children (%r)"
                  % r.stdout, json.loads(r.stdout or "{}").get("result") ==
                  "ok" and until(lambda: not any(alive(q)
                                                 for q in [pm] + km), 5))
            # a TERM-ignoring shell that keeps starting sleeps: none left (#718)
            pl = shell("bsh22loop", shlex.quote(
                "trap '' TERM; while :; do sleep 100.0718 & sleep 0.2; done"),
                       new=True)
            pg = os.getpgid(pl)
            r = run("--stop-shell", h, "bsh22loop", "--json")
            left = subprocess.run(["/usr/bin/pgrep", "-g", str(pg)],
                                  capture_output=True, text=True).stdout.split()
            left = [q for q in left if alive(int(q))]
            for q in left:
                os.kill(int(q), 9)
            check("--stop-shell: nothing the shell started meanwhile "
                  "survives, ok (%r, left %r)" % (r.stdout, left),
                  '"result":"ok"' in r.stdout and not left)
            os.unlink(os.path.join(sessdir, "%d.json" % hp))
            check("its record gone: no shells key", field(h, "shells") ==
                  MISSING)
        finally:
            run("-k", h)
            for p in shpids:	# each shell and its sleep
                kids = subprocess.run(["/usr/bin/pgrep", "-P", str(p)],
                                      capture_output=True, text=True).stdout
                for q in [p] + [int(k) for k in kids.split()]:
                    try:
                        os.kill(q, signal.SIGKILL)
                    except OSError:
                        pass

        # --- "subagents" (#712): from <sid>/subagents/ and the main ------
        # transcript; "row" off a fake agent panel in Claude Code's format,
        # "<glyph> <agentType>  <activity>", reprinted per case from a file
        sa = "tt-sa"
        sapf = os.path.join(tmp, "sa-panel")
        sap = spawn(sa, "/bin/sh", "-c",
                    "while :; do if [ -f %s ]; then printf '\\033[2J\\033[H'; "
                    "cat %s; rm -f %s; fi; sleep 0.2; done" % (sapf, sapf, sapf))
        sa_sid = "71271271-2712-7127-1271-271271271271"
        now = int(time.time())

        def iso2(t):
            return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))

        def jl2(o):
            return json.dumps(o, separators=(",", ":")) + "\n"

        def tres(tu, t, err=False, bg=False):
            c = {"tool_use_id": tu, "type": "tool_result",
                 "content": [{"type": "text", "text": "ok"}]}
            if err:
                c["is_error"] = True
            return jl2({"type": "user", "timestamp": iso2(t),
                        "message": {"role": "user", "content": [c]},
                        "toolUseResult": {"status": "async_launched",
                                          "isAsync": True} if bg
                        else {"status": "completed"}})

        def note(aid, tu, t, status):
            return jl2({"type": "queue-operation", "operation": "enqueue",
                        "timestamp": iso2(t), "sessionId": sa_sid,
                        "content": "<task-notification>\n<task-id>%s"
                        "</task-id>\n<tool-use-id>%s</tool-use-id>\n"
                        "<status>%s</status>\n<summary>s</summary>\n"
                        "</task-notification>" % (aid, tu, status)})

        satp = transcript(sa_sid)
        sad = os.path.join(os.path.dirname(satp), sa_sid, "subagents")
        os.makedirs(sad)

        def agent(aid, typ, desc, start, mt, meta=True, fin=None):
            # fin: the meta says "finished":true, rewritten at that time
            if meta:
                mp = os.path.join(sad, "agent-%s.meta.json" % aid)
                m = {"agentType": typ, "description": desc,
                     "isBuiltIn": True, "spawnDepth": 1,
                     "toolUseId": "toolu_" + aid}
                if fin:
                    m["finished"] = True
                with open(mp, "w") as f:
                    json.dump(m, f)
                if fin:
                    os.utime(mp, (fin, fin))
            p = os.path.join(sad, "agent-%s.jsonl" % aid)
            with open(p, "w") as f:
                f.write(jl2({"type": "user", "isSidechain": True,
                             "agentId": aid,
                             "timestamp": start if isinstance(start, str)
                             else iso2(start)}) if start else "{}\n")
            os.utime(p, (mt, mt))

        lite = run("--read", sa).returncode == 3

        def panel(*types):
            with open(sapf + ".tmp", "w") as f:
                f.write("⏺ main\n" + "".join(
                    "◯ %s  Waiting …\n" % t for t in types))
            os.rename(sapf + ".tmp", sapf)
            until(lambda: lite or field(sa, "agents") == len(types))
            got = field(sa, "subagents")
            return {s["id"]: s for s in got} if isinstance(got, list) \
                else {}, got

        def rowsof(g, *ids):
            return [g.get(i, {}).get("row", "-") for i in ids]

        def nul(*r):
            return [None] * len(r) if lite else list(r)

        agent("aSLOW", "slow-worker", "Long build", now - 2000, now - 1800)
        agent("aKILL", "Explore", "Killed job", now - 210, now - 25)
        agent("aBG", "Explore", "Background job", now - 200, now - 25)
        agent("aDONE", "general-purpose", "Fix the flaky tests", now - 120,
              now - 40)
        agent("aFIN", "Plan", "Plan it", now - 100, now - 50, fin=now - 50)
        agent("aFERR", "Plan", "Plan badly", now - 90, now - 45,
              fin=now - 45)
        agent("aRUN", "Explore", "Read the docs", now - 60, now)
        agent("aERR", "Explore", "Break it", None, now - 40)
        agent("aOLD", "Explore", "Read the docs", now - 4000, now - 3600)
        agent("aOLDFIN", "Explore", "Old", now - 4000, now - 3600,
              fin=now - 3600)
        agent("aSTALE", "Explore", "Day old", now - 90000, now - 90000)
        agent("aNOMETA", "Explore", "x", now - 50, now, meta=False)
        with open(satp, "w") as f:
            f.write(tres("toolu_aOLD", now - 3590))
            f.write(tres("toolu_aBG", now - 200, bg=True))
            f.write(tres("toolu_aKILL", now - 210, bg=True))
            f.write(tres("toolu_aDONE", now - 30))
            f.write(tres("toolu_aERR", now - 30, err=True))
            f.write(tres("toolu_aFERR", now - 44, err=True))
            f.write(note("aBG", "toolu_aBG", now - 20, "completed"))
            f.write(note("aKILL", "toolu_aKILL", now - 20, "killed"))
        record(sap, sa_sid)
        # a lingering ended run: every listed one, in launch order, by type
        g, got = panel("slow-worker", "Explore", "Explore", "general-purpose",
                       "Plan", "Plan", "Explore")
        ids = [s.get("id") for s in got] if isinstance(got, list) else got
        check("subagents: running ones (one silent 30 min) and those ended "
              "in 10 min, newest first; ended earlier, a day silent, no "
              "meta left out (%s)" % ids,
              ids == ["aERR", "aRUN", "aFERR", "aFIN", "aDONE", "aBG",
                      "aKILL", "aSLOW"])

        def want(aid, typ, desc, start, state, row):
            return {"id": aid, "type": typ, "description": desc,
                    "started": start, "state": state,
                    "row": None if lite else row}
        check("running: no end line, no finished (%s)" % g.get("aRUN"),
              g.get("aRUN") == want("aRUN", "Explore", "Read the docs",
                                    now - 60, "running", 6))
        check("running: its file 30 min old, still listed (%s)"
              % g.get("aSLOW"),
              g.get("aSLOW") == want("aSLOW", "slow-worker", "Long build",
                                     now - 2000, "running", 0))
        check("done: its tool_result (%s)" % g.get("aDONE"),
              g.get("aDONE") == want("aDONE", "general-purpose",
                                     "Fix the flaky tests", now - 120,
                                     "done", 3))
        check("done: meta finished:true with no end line (%s)"
              % g.get("aFIN"),
              g.get("aFIN") == want("aFIN", "Plan", "Plan it", now - 100,
                                    "done", 4))
        check("failed: meta finished:true and an is_error end (%s)"
              % g.get("aFERR"),
              g.get("aFERR") == want("aFERR", "Plan", "Plan badly", now - 90,
                                     "failed", 5))
        fs = g.get("aERR", {}).get("started")
        check("failed: is_error; no first-line timestamp: started from the "
              "file; past the panel: row null (%s)" % g.get("aERR"),
              isinstance(fs, int) and abs(fs - now) < 60 and
              dict(g.get("aERR", {}), started=0) ==
              want("aERR", "Explore", "Break it", 0, "failed", None))
        check("background: done by its <task-notification>, the launch "
              "result no end (%s)" % g.get("aBG"),
              g.get("aBG") == want("aBG", "Explore", "Background job",
                                   now - 200, "done", 2))
        check("background killed: failed (%s)" % g.get("aKILL"),
              g.get("aKILL") == want("aKILL", "Explore", "Killed job",
                                     now - 210, "failed", 1))
        g, _ = panel("slow-worker", "Explore")
        r = rowsof(g, "aSLOW", "aRUN", "aDONE", "aERR")
        check("panel of the running ones: their rows by launch order, the "
              "rest null (%s)" % r, r == nul(0, 1, None, None))
        g, _ = panel("Explore", "slow-worker", "Plan")
        r = rowsof(g, "aSLOW", "aRUN", "aFIN", "aFERR")
        check("panel types match no run: rows null (%s)" % r,
              r == [None] * 4)
        keys = []
        for rr in json.loads(run("--ls-json").stdout,
                             object_pairs_hook=lambda kv: kv):
            if dict(rr).get("name") == sa:
                rk = [k for k, _ in rr]
                sub = dict(rr).get("subagents") or [[]]
                keys = rk[rk.index("shells"):rk.index("shells") + 2] + \
                    [k for k, _ in sub[0]]
        check("keys: subagents right after shells; id, type, description, "
              "started, state, row (%s)" % keys,
              keys == ["shells", "subagents", "id", "type", "description",
                       "started", "state", "row"])
        for fn in os.listdir(sad):
            os.unlink(os.path.join(sad, fn))
        open(satp, "w").close()
        # two of one type started in the same ms: no order between them
        agent("aT1", "Explore", "t1", "2026-01-01T00:00:00.123Z", now)
        agent("aT2", "Explore", "t2", "2026-01-01T00:00:00.123Z", now)
        g, _ = panel("Explore", "Explore")
        r = rowsof(g, "aT1", "aT2")
        check("same type, same start ms: rows null (%s)" % r,
              r == [None, None])
        for fn in os.listdir(sad):
            os.unlink(os.path.join(sad, fn))
        for i in range(4):
            agent("aA%d" % i, "Explore", "a", now - 100 + i, now - 30,
                  fin=now - 30)
        g, _ = panel("Explore", "Explore", "Explore")
        r = rowsof(g, "aA0", "aA1", "aA2", "aA3")
        check("ended ones: the panel fits at two offsets: rows null (%s)"
              % r, r == [None] * 4 and g.get("aA0", {}).get("state") ==
              "done")
        check("a non-Claude row: no subagents key",
              field(b, "subagents") == MISSING)
        os.unlink(os.path.join(sessdir, "%d.json" % sap))
        check("its record gone: no subagents key",
              field(sa, "subagents") == MISSING)

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
        # #274: one's record kept empty past the re-reads: still two, no
        # id (base: the other's); a reused pid's stale empty one is no
        # Claude: the other joins
        if kids:
            kp = os.path.join(sessdir, "%s.json" % kids[0])
            open(kp, "w").close()
            check("one of two claudes' record empty: still null",
                  field(c, "claude_session_id") is None)
            old = time.time() - 600
            os.utime(kp, (old, old))
            check("...its record a stale one instead: the other joins",
                  field(c, "claude_session_id") ==
                  "%08d-0000-0000-0000-000000000000" % 1)
            record(int(kids[0]), "%08d-0000-0000-0000-000000000000" % 0)
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
        # its master probes every 300ms: the history record's agent (#274)
        sn = spawn(n, "/bin/sh", "-c",
                   "HOME=%s '%s' -c 'import os,time; os.fork() or "
                   "time.sleep(120); time.sleep(120)' & wait"
                   % (nhome, sys.executable),
                   env=dict(os.environ, DCH_RECORD_MS="300"))
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
        # #274 (QA 1.60.0 LOW-X13): the ancestor's record kept empty past
        # the re-reads (a stalled rewrite) is its Claude, id unknown: no
        # id, never the one below's (base: s5 in all three)
        hrec = os.path.join(tmp, "state", "dch", "sessions",
                            os.path.join(tmp, "dch-%d" % os.getuid())
                            .replace("/", "-"), n + ".json")

        def hsid():
            try:
                return json.load(open(hrec))["agent"]["session_id"]
            except (OSError, ValueError, KeyError, TypeError):
                return None
        until(lambda: hsid() == s4)
        transcript(s5, top=os.path.join(nhome, ".claude", "projects"))
        open(os.path.join(sessdir, "%d.json" % sn), "w").close()
        check("ancestor's record empty: --ls-json gives no id, not the "
              "one below's", field(n, "claude_session_id") is None)
        check("...--xray --json joins none either", xray_sid(n) is None)
        time.sleep(1.5)		# 5 record ticks
        check("...the history record keeps the id it had (%s)"
              % (hsid() or "")[:8], hsid() == s4)
        record(sn, s4)

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
        # ... nor once its pid is reused: the live process started after
        # the record's last write is not its Claude (#274, QA 1.60.0
        # LOW-X14; base: 20 re-reads, +100ms)
        dead = subprocess.Popen(["/usr/bin/true"])
        dead.wait()
        reused = subprocess.Popen(["/bin/sleep", "60"])
        stale = [os.path.join(sessdir, "%d.json" % dead.pid),
                 os.path.join(sessdir, "stale.json"),
                 os.path.join(sessdir, "%d.json" % reused.pid)]

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
        old = time.time() - 600
        os.utime(stale[2], (old, old))
        open(trace, "w").close()
        slow = fastest()
        waited = traced("read again")
        for f in stale:
            os.unlink(f)
        reused.kill()
        reused.wait()
        check("empty dead-pid, non-pid and reused-pid records: no re-read "
              "(%d)" % waited, waited == 0)
        # the wait is 100ms of sleeps; ASan's noise is as large, so there the
        # "read again" count above (0 vs 20 on the base) is the proof
        check("... and no wait: fastest of 10 %.0fms vs %.0fms clean"
              % (slow * 1000, clean * 1000),
              bool(no_budget) or slow - clean < 0.06)

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
