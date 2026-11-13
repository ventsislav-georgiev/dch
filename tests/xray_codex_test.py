#!/usr/bin/env python3
"""`dch --xray` over a live Codex's rollout (#160, XR3c; spec 7.4, K9).

tests/xray_golden.py pins the answers per Codex format version
(codex-0151, codex-0167); this drives what a golden cannot, through real
masters running tests/fake_codex.c as `codex`, all under a temp HOME:

  - the mapping only through dch_codex_snapshot_id's one-UUID rule: no
    snapshot, two ids for the session (mutant 35) or a relative CODEX_HOME
    (mutant 36, refused before any read) is corpus none, never the newest
    rollout, and --ref says no corpus
  - the open seam (8.1): one open per query, the rollout's own inode, the
    lookup's trace; no rollout text in a trace line
  - a malformed line and a truncated last line: the answer still stands
  - --ref and --clear (the ring is never cleared under a Codex); --chapter
    over the rollout (#169)
  - --scope project|all (#169): a Codex <n> reads its rollout, then the
    other sessions' Claude and Codex files; a shell or a Claude <n> reads
    the live Codex sessions' rollouts too; one seam open each, never a ring
  - --follow: growth (a chapter and a ref delta), shrink (reset shrunk),
    rotation (a new thread: reset rotated, the new id's snapshot), a rollout
    that appears after the follower started, and a shell's follower as a
    Codex starts and exits in it (the ring pauses for a Codex, 7.3)
  - nothing written, and nothing in the Codex dir ever run

Run:  python3 tests/xray_codex_test.py          (uses ./dch; run.sh isolates)
"""
import json, os, re, shutil, subprocess, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import xray_ring as X  # noqa: E402

DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
TID = "019a2222-3333-7444-8555-666677778801"
TID2 = "019a2222-3333-7444-8555-666677778802"
TID3 = "019a2222-3333-7444-8555-666677778803"
TIDA = "019a2222-3333-7444-8555-666677778804"
TIDB = "019a2222-3333-7444-8555-666677778805"
TIDC = "019a2222-3333-7444-8555-666677778806"
CLSID = "11111111-2222-4333-8444-555555555590"
fails = [0]


def check(msg, cond, detail=""):
    print("  %s %s" % ("ok  " if cond else "FAIL", msg))
    if not cond:
        fails[0] += 1
        if detail:
            print("       " + str(detail)[:1200])


T = os.path.realpath(tempfile.mkdtemp(prefix="xx"))
HOME = os.path.join(T, "home")
CODEX = os.path.join(HOME, ".codex")
SNAPS = os.path.join(CODEX, "shell_snapshots")
DAY = os.path.join(CODEX, "sessions", "2026", "11", "10")
for _d in (HOME, SNAPS, DAY):
    os.makedirs(_d, mode=0o700)
ENV = {k: v for k, v in os.environ.items()
       if not k.startswith(("DCH_", "CLAUDE", "CODEX", "Malloc"))}
ENV.update(XDG_RUNTIME_DIR=T, XDG_STATE_HOME=os.path.join(T, "state"),
           HOME=HOME, DCH_CONFIG=os.path.join(T, "config"),
           DCH_LOGIN_ENV="0", DCH_NO_REMOTE="1", DCH_RECORD_MS="1000")
os.environ.update(XDG_RUNTIME_DIR=T)     # xray_ring.sock_path
os.environ.pop("DCH_SOCKET_DIR", None)
FAKE = os.path.join(T, "bin", "codex")
# a hook a Codex would run: dch never does (the brief: nothing in the Codex
# dir is executed)
RAN = os.path.join(T, "hook-ran")


def dch(*a, env=None):
    return subprocess.run([DCH] + list(a), capture_output=True, text=True,
                          timeout=30, env=env or ENV)


def xray(*a, env=None):
    r = dch("--xray", *a, env=env)
    try:
        return r.returncode, json.loads(r.stdout), r.stderr
    except ValueError:
        return r.returncode, None, r.stderr


def wait_for(fn, secs=10.0):
    end = time.time() + secs
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def ts(s):
    return "2026-11-10T10:%02d:%02d.000Z" % divmod(s, 60)


def line(sec, typ, payload):
    return json.dumps({"timestamp": ts(sec), "type": typ, "payload": payload},
                      separators=(",", ":")) + "\n"


def user(sec, text):
    return line(sec, "response_item", {"type": "message", "role": "user",
                                       "content": [{"type": "input_text",
                                                    "text": text}]})


def rollout(tid, cwd, body, day=DAY):
    os.makedirs(day, exist_ok=True)
    p = os.path.join(day, "rollout-2026-11-10T10-00-00-%s.jsonl" % tid)
    with open(p, "w") as f:
        f.write(line(0, "session_meta", {"id": tid, "cwd": cwd,
                                         "cli_version": "0.167.0"}) + body)
    return p


def snapshot(tid, name, marker, mode=0o600):
    p = os.path.join(SNAPS, tid + ".1.sh")
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        f.write("# Snapshot file\nexport DCH_SESSION=%s\n%s" % (
            name, "export DCH_NATIVE_BRIDGE_ID=%s\n" % marker if marker
            else ""))
    os.chmod(tmp, mode)
    os.rename(tmp, p)
    return p


def spawn(n, cwd, **extra):
    """A session whose command is the fake codex: (pid, marker)."""
    log = os.path.join(T, n + ".log")
    os.makedirs(cwd, exist_ok=True)
    r = dch("--spawn", n, "--size", "80x24", "--cwd", cwd, FAKE,
            env=dict(ENV, FAKE_CODEX_LOG=log, **extra))
    check("%s: spawned" % n, r.returncode == 0, r.stderr)
    got = wait_for(lambda: os.path.exists(log) and
                   open(log).read().endswith("\n") and
                   open(log).read().split(), 10)
    check("%s: the fake codex runs" % n, bool(got))
    return (int(got[0]), got[1] if got[1] != "-" else None) if got \
        else (0, None)


def is_master(pid):
    """pid runs `dch --master-of ...`: /proc on Linux, ps elsewhere."""
    try:
        a = open("/proc/%d/cmdline" % pid, "rb").read()
    except OSError:
        a = subprocess.run(["ps", "-o", "args=", "-p", str(pid)],
                           capture_output=True).stdout
    return b"--master-of" in a


def files(root):
    """Less a live master's history record mid-write (master.c rec_write:
    <name>.json.<its pid>.tmp, then a rename, every DCH_RECORD_MS; #283):
    the pid is a `--master-of` process, so never the X-ray's own write."""
    out = {}
    for dp, dn, fn in os.walk(root):
        for f in dn + fn:
            p = os.path.join(dp, f)
            m = re.search(r"\.json\.(\d+)\.tmp$", f)
            if m and is_master(int(m.group(1))):
                continue
            st = os.lstat(p)
            out[p] = (st.st_size, st.st_mtime_ns)
    return out


def key(cwd):
    return "".join(c if c.isascii() and c.isalnum() else "-" for c in cwd)


BODY = (line(1, "response_item", {"type": "message", "role": "developer",
                                   "content": [{"type": "input_text",
                                                "text": "dev text #900"}]}) +
        line(1, "response_item", {"type": "message", "role": "user",
                                  "content": [{"type": "input_text", "text":
                                               "<environment_context>#901"
                                               "</environment_context>"}]}) +
        user(2, "fix the parser, see #41") +
        line(3, "response_item", {"type": "custom_tool_call", "name": "exec",
                                  "call_id": "c1",
                                  "input": "git show 4e5f6a7b8c"}) +
        line(4, "response_item", {"type": "message", "role": "assistant",
                                  "content": [{"type": "output_text",
                                               "text": "done, see #41"}]}))


# ---- one live Codex: the mapping, the seam, --ref, --clear ------------------

def test_live():
    n = "xx-cx"
    proj = os.path.join(T, "cxproj")
    pid, marker = spawn(n, proj)
    trace = os.path.join(T, "trace-live")
    open(trace, "w").close()
    dbg = dict(ENV, DCH_DEBUG=trace)
    try:
        rc, o, err = xray(n, "--json", env=dbg)
        check("no snapshot yet: corpus none, session_id empty, live",
              rc == 0 and o and o["corpus"] == "none" and
              o["session_id"] == "" and o["live"] and not o["available"],
              (rc, o, err))
        check("no snapshot: no lookup and no open (no newest-file guess)",
              "xray codex lookup" not in open(trace).read() and
              "xray seam:" not in open(trace).read(), open(trace).read())
        rollout(TID2, proj, user(2, "another thread's #77"))
        r = dch("--xray", n, "--ref", "#41", "--scope", "session", "--json")
        check("no snapshot: --ref is no corpus (exit 1)",
              r.returncode == 1 and "no x-ray corpus" in r.stderr, r.stderr)

        p = rollout(TID, proj, BODY)
        snapshot(TID, n, marker)
        open(trace, "w").close()
        rc, o, err = xray(n, "--json", env=dbg)
        tr = open(trace).read()
        check("mapped: corpus codex, mentions only, session_id the thread",
              rc == 0 and o and o["corpus"] == "codex" and
              o["mentions_only"] is True and o["available"] and
              o["session_id"] == TID and o["live"], (rc, o, err))
        if not o:
            return
        check("mapped: the user turn is the chapter (Codex's context and "
              "developer text are not)",
              [c["title"] for c in o["chapters"]] ==
              ["fix the parser, see #41"], o["chapters"])
        keys = {r["key"]: r for r in o["refs"]}
        check("mapped: mentions from the turn, the tool input and the "
              "answer; none from Codex's own parts; never defined",
              set(keys) == {"issue:#41", "commit:4e5f6a7"} and
              keys["issue:#41"]["count"] == 2 and
              keys["issue:#41"]["underline"] is True and
              keys["commit:4e5f6a7"]["underline"] is False and
              not any(r["defined"] for r in o["refs"]), o["refs"])
        check("mapped: project_label the Codex's cwd, project its key",
              o["project_label"] == proj and o["project"] == key(proj), o)
        ino = "%x" % os.stat(p).st_ino
        check("seam: one open, the rollout's own inode, none refused",
              tr.count("xray seam: open") == 1 and
              "xray seam: open ino=%s" % ino in tr and
              "xray seam: refused" not in tr, tr)
        check("seam: the lookup found one hit within its day dirs",
              re.search(r"xray codex lookup: 1 hit, [0-9]+ day dirs", tr),
              tr)
        check("seam: no rollout text in a trace line",
              "parser" not in tr and "#41" not in tr, tr)

        r = dch("--xray", n, "--ref", "#41", "--scope", "session", "--json")
        ls = [json.loads(x) for x in r.stdout.splitlines()]
        check("--ref: first names the thread, a mention, done",
              r.returncode == 0 and ls and
              ls[0]["first"]["session_id"] == TID and
              ls[0]["first"]["project"] == key(proj) and
              any("mention" in x for x in ls) and ls[-1].get("done") is True,
              r.stdout + r.stderr)
        # the default scope (project, #169): the rollout first, then the
        # store's other Claude and Codex files (none here)
        r = dch("--xray", n, "--ref", "#41", "--json")
        ls = [json.loads(x) for x in r.stdout.splitlines()]
        check("--ref, the default scope: scope project, the rollout first",
              r.returncode == 0 and ls and
              ls[0]["first"]["session_id"] == TID and
              ls[0]["first"]["scope"] == "project" and
              [x["tally"]["session"] for x in ls if "tally" in x] == [n] and
              ls[-1].get("done") is True, r.stdout + r.stderr)
        # --chapter (#169): the rollout's chapter reader, one seam open
        open(trace, "w").close()
        r = dch("--xray", n, "--chapter", o["chapters"][0]["anchor"],
                "--json", env=dbg)
        c = json.loads(r.stdout) if r.returncode == 0 else None
        tr = open(trace).read()
        check("--chapter on a live Codex: the turn, its call, the answer",
              c and c["session"] == n and c["session_id"] == TID and
              c["anchor"] == o["chapters"][0]["anchor"] and
              c["kind"] == "prompt" and c["truncated"] is False and
              c["entries"] == [
                  {"role": "user", "tool": None,
                   "text": "fix the parser, see #41", "error": False},
                  {"role": "tool", "tool": "exec",
                   "text": "git show 4e5f6a7b8c", "error": False},
                  {"role": "assistant", "tool": None,
                   "text": "done, see #41", "error": False}],
              (r.stdout, r.stderr))
        check("--chapter on a live Codex: one seam open, the rollout's",
              tr.count("xray seam: open") == 1 and
              "xray seam: open ino=%s" % ino in tr, tr)
        r = dch("--xray", n, "--chapter", "f%s:1" % ino, "--json")
        check("--chapter on a live Codex: an offset no chapter starts at is "
              "chapter gone (exit 1)", r.returncode == 1 and
              r.stdout == "" and r.stderr == "dch: chapter gone\n",
              (r.stdout, r.stderr))

        # a malformed line, then a truncated last line (no newline)
        with open(p, "a") as f:
            f.write("not json #55\n")
            f.write(user(5, "second ask #43"))
            f.write('{"timestamp":"%s","type":"response_item","payload":'
                    '{"type":"message","role":"user","content":[{"type":'
                    '"input_text","text":"half #56' % ts(6))
        rc, o2, err = xray(n, "--json")
        check("malformed and truncated: the answer stands, the bad line "
              "and the cut one add nothing",
              rc == 0 and o2 and [c["title"] for c in o2["chapters"]] ==
              ["fix the parser, see #41", "second ask #43"] and
              "issue:#55" not in {r["key"] for r in o2["refs"]} and
              "issue:#56" not in {r["key"] for r in o2["refs"]},
              (rc, o2, err))
        rollout(TID, proj, BODY)

        rc, c, err = xray(n, "--clear", "--json")
        check("--clear: corpus codex, its thread, the ring never cleared",
              (rc == 0 and c == {"schema": 1, "session": n,
                                 "session_id": TID, "corpus": "codex",
                                 "ring_cleared": False}) or
              (rc == 1 and "no x-ray corpus" in err), (rc, c, err))

        # the one-UUID rule (mutant 35): a second id for the session
        s2 = snapshot(TID2, n, marker)
        open(trace, "w").close()
        rc, o3, err = xray(n, "--json", env=dbg)
        check("two ids for the session: corpus none, session_id empty, "
              "nothing opened (not the newest rollout)",
              rc == 0 and o3 and o3["corpus"] == "none" and
              o3["session_id"] == "" and
              "xray seam:" not in open(trace).read(), (o3, err))
        r = dch("--xray", n, "--ref", "#77", "--scope", "session", "--json")
        check("two ids: --ref is no corpus", r.returncode == 1 and
              "no x-ray corpus" in r.stderr, r.stderr)
        r = dch("--xray", n, "--chapter", o["chapters"][0]["anchor"],
                "--json")
        check("two ids: --chapter is no corpus, never the ring",
              r.returncode == 1 and r.stderr == "dch: no x-ray corpus\n",
              (r.stdout, r.stderr))
        os.unlink(s2)

        # a group-writable snapshot is not trusted (strict)
        os.chmod(os.path.join(SNAPS, TID + ".1.sh"), 0o622)
        rc, o4, _ = xray(n, "--json")
        check("a group-writable snapshot maps nothing",
              rc == 0 and o4 and o4["corpus"] == "none", o4)
        os.chmod(os.path.join(SNAPS, TID + ".1.sh"), 0o600)

        # nothing written by any form; nothing in the Codex dir run
        before = files(T)
        xray(n, "--json")
        dch("--xray", n, "--ref", "#41", "--scope", "session", "--json")
        # the master's agent bridge (bridge.c) publishes this Codex's peer
        # record in ~/.claude/sessions on its own clock: not an --xray write
        peers = os.path.join(HOME, ".claude", "sessions") + os.sep
        new = sorted(p for p in set(files(T)) - set(before)
                     if not p.startswith(peers))
        check("nothing written by --json or --ref", not new, new)
    finally:
        dch("-k", n)


def test_relative_home():
    """Mutant 36: a relative CODEX_HOME is refused before any read."""
    n = "xx-rel"
    proj = os.path.join(T, "relproj")
    os.makedirs(os.path.join(proj, "rel", "shell_snapshots"))
    pid, marker = spawn(n, proj, CODEX_HOME="rel")
    # the relative dir holds a valid snapshot and rollout
    snap = os.path.join(proj, "rel", "shell_snapshots", TID + ".1.sh")
    with open(snap, "w") as f:
        f.write("export DCH_SESSION=%s\n%s" % (
            n, "export DCH_NATIVE_BRIDGE_ID=%s\n" % marker if marker else ""))
    os.chmod(snap, 0o600)
    rollout(TID, proj, BODY, os.path.join(proj, "rel", "sessions", "2026",
                                          "11", "10"))
    trace = os.path.join(T, "trace-rel")
    open(trace, "w").close()
    try:
        r = subprocess.run([DCH, "--xray", n, "--json"], capture_output=True,
                           text=True, timeout=30, cwd=proj,
                           env=dict(ENV, DCH_DEBUG=trace))
        o = json.loads(r.stdout) if r.returncode == 0 else None
        check("a relative CODEX_HOME: corpus none, no lookup, no open",
              o and o["corpus"] == "none" and o["session_id"] == "" and
              "xray codex lookup" not in open(trace).read() and
              "xray seam:" not in open(trace).read(), (r.stdout, r.stderr))
    finally:
        dch("-k", n)


# ---- --scope project|all across Codex and Claude files (#169) ---------------

def history_claude(name, cwd, sid, text, updated):
    """An ended Claude session: its transcript and its History record."""
    cfg = os.path.join(HOME, ".claude")
    pd = os.path.join(cfg, "projects", key(cwd))
    os.makedirs(pd, exist_ok=True)
    with open(os.path.join(pd, sid + ".jsonl"), "w") as f:
        f.write(json.dumps({"type": "user", "message": {
            "role": "user", "content": text}, "cwd": cwd,
            "timestamp": ts(1)}) + "\n")
    sock = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions", sock.replace("/", "-"))
    os.makedirs(hdir, mode=0o700, exist_ok=True)
    p = os.path.join(hdir, name + ".json")
    with open(p, "w") as f:
        f.write(json.dumps({
            "name": name, "cwd": cwd, "end": "exited", "argv": ["claude"],
            "started": updated - 60, "updated": updated, "ended": updated,
            "agent": {"session_id": sid, "cwd": cwd, "config_dir": cfg},
            "sock_dir": sock}) + "\n")
    os.chmod(p, 0o600)


def tallies(r):
    ls = [json.loads(x) for x in r.stdout.splitlines()] \
        if r.returncode == 0 else []
    return ls, [x["tally"]["session"] for x in ls if "tally" in x]


def test_scope():
    proj = os.path.join(T, "scproj")
    other = os.path.join(T, "scother")
    now = int(time.time())
    names = ("xx-a", "xx-b", "xx-c", "xx-s")
    try:
        for n, tid, cwd, age in (("xx-a", TIDA, proj, 150),
                                 ("xx-b", TIDB, proj, 100),
                                 ("xx-c", TIDC, other, 10)):
            _, marker = spawn(n, cwd)
            p = rollout(tid, cwd, user(1, "%s about #61" % n))
            os.utime(p, (now - age, now - age))
            snapshot(tid, n, marker)
        history_claude("xx-h", proj, CLSID, "claude about #61", now - 50)
        r = dch("--spawn", "xx-s", "--size", "80x24", "--cwd", proj, "sh")
        check("scope: shell spawned", r.returncode == 0, r.stderr)

        rc, o, err = xray("xx-a", "--json")
        check("scope: --json other_projects counts the other dir's Codex",
              rc == 0 and o and o["corpus"] == "codex" and
              o["other_projects"] == 1, (o, err))

        trace = os.path.join(T, "trace-scope")
        open(trace, "w").close()
        r = dch("--xray", "xx-a", "--ref", "#61", "--scope", "project",
                "--full", "--json", env=dict(ENV, DCH_DEBUG=trace))
        ls, ts_ = tallies(r)
        tr = open(trace).read()
        check("scope project, a Codex <n>: its rollout, then the project's "
              "Claude and Codex files newest first, not the other dir's, "
              "never the shell's ring",
              ts_ == ["xx-a", "xx-h", "xx-b"] and
              ls[0]["first"]["scope"] == "project" and
              ls[-1]["counts"]["project_sessions"] == 2 and
              ls[-1]["counts"]["project"] == 2 and
              ls[-1]["counts"]["all"] is None and
              ls[-1]["partial"] is False, r.stdout + r.stderr)
        check("scope project: other sessions' mentions in full mode, a "
              "Codex's among them",
              any(x.get("mention", {}).get("session") == "xx-b" for x in ls),
              r.stdout)
        check("scope project: one seam open per file, none refused",
              tr.count("xray seam: open") == 3 and
              "xray seam: refused" not in tr, tr)

        r = dch("--xray", "xx-a", "--ref", "#61", "--scope", "all", "--full",
                "--json")
        ls, ts_ = tallies(r)
        check("scope all, a Codex <n>: then the other dir's Codex",
              ts_ == ["xx-a", "xx-h", "xx-b", "xx-c"] and
              ls[-1]["counts"]["all_sessions"] == 1 and
              ls[-1]["counts"]["all"] == 1, r.stdout + r.stderr)

        r = dch("--xray", "xx-a", "--ref", "#61", "--json")
        ls, ts_ = tallies(r)
        others = [x for x in ls if "mention" in x and
                  x["mention"]["session"] != "xx-a"]
        check("light, a Codex <n>: its rollout first; another session's "
              "file gives counts only, and its first result stops the scan",
              r.returncode == 0 and ts_[:1] == ["xx-a"] and not others and
              (ls[-1]["why"] == "first" and len(ts_) == 2 or
               ls[-1]["why"] == "budget"), r.stdout + r.stderr)

        r = dch("--xray", "xx-h", "--ref", "#61", "--scope", "project",
                "--full", "--json")
        ls, ts_ = tallies(r)
        check("scope project, a Claude <n>: its transcript, then the live "
              "Codex sessions' rollouts", ts_ == ["xx-h", "xx-b", "xx-a"],
              r.stdout + r.stderr)

        rc, so, _ = xray("xx-s", "--json")
        if so and so["corpus"] == "none":
            print("  skip shell <n>: no ring corpus (%s)" % so["reason"])
        else:
            r = dch("--xray", "xx-s", "--ref", "#61", "--scope", "project",
                    "--full", "--json")
            ls, ts_ = tallies(r)
            check("scope project, a shell <n>: its ring, then the project's "
                  "Claude and Codex files", ts_ == ["xx-s", "xx-h", "xx-b",
                                                    "xx-a"],
                  r.stdout + r.stderr)

        before = files(T)
        dch("--xray", "xx-a", "--ref", "#61", "--scope", "all", "--full",
            "--json")
        dch("--xray", "xx-a", "--chapter", "f0:0", "--json")
        peers = os.path.join(HOME, ".claude", "sessions") + os.sep
        new = sorted(p for p in set(files(T)) - set(before)
                     if not p.startswith(peers))
        check("scope: nothing written", not new, new)
    finally:
        for n in names:
            dch("-k", n)


# ---- --follow --------------------------------------------------------------

class Follow:
    def __init__(self, n, env=None):
        self.p = subprocess.Popen([DCH, "--xray", n, "--follow", "--json"],
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, env=env or ENV)
        self.got = []
        self.th = threading.Thread(target=self.run, daemon=True)
        self.th.start()

    def run(self):
        for raw in self.p.stdout:
            try:
                self.got.append(json.loads(raw))
            except ValueError:
                self.got.append({"bad": raw.decode(errors="replace")})

    def objs(self, frm=0):
        return list(self.got[frm:])

    def wait(self, pred, secs=10.0, frm=0):
        return wait_for(lambda: [o for o in self.objs(frm) if pred(o)], secs)

    def stop(self):
        if self.p.poll() is None:
            self.p.terminate()          # ours: by its exact pid
        try:
            self.p.wait(5)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait(5)
        self.th.join(5)
        return self.p.returncode


def snap_of(tid=None, corpus=None):
    return lambda o: "snapshot" in o and \
        (tid is None or o["snapshot"]["session_id"] == tid) and \
        (corpus is None or o["snapshot"]["corpus"] == corpus)


def test_follow():
    n = "xx-fl"
    proj = os.path.join(T, "flproj")
    pid, marker = spawn(n, proj)
    p = rollout(TID, proj, BODY)
    s1 = snapshot(TID, n, marker)
    fl = None
    try:
        _, one, _ = xray(n, "--json")
        fl = Follow(n)
        s = fl.wait(snap_of())
        check("follow: the first line is the --json object as a snapshot",
              s and fl.objs()[0] == s[0] and s[0]["snapshot"] == one,
              (fl.objs(), one))

        k = len(fl.got)
        with open(p, "a") as f:
            f.write(line(5, "response_item", {"type": "function_call_output",
                                              "call_id": "c1",
                                              "output": "gh pr view 88"}))
            f.write(user(6, "next ask #44"))
        # the late output re-sends chapter 1 first (its response is gone)
        ch = fl.wait(lambda o: "chapter" in o and
                     o["chapter"]["title"] == "next ask #44", frm=k)
        rf = fl.wait(lambda o: "ref" in o and o["ref"]["key"] == "issue:#44",
                     frm=k)
        check("follow: growth gives a chapter and a ref delta",
              ch and ch[0]["chapter"]["title"] == "next ask #44" and rf and
              rf[0]["ref"]["defined"] is False, fl.objs(k))

        time.sleep(0.4)
        k = len(fl.got)
        os.truncate(p, len(line(0, "session_meta", {
            "id": TID, "cwd": proj, "cli_version": "0.167.0"})))
        s = fl.wait(snap_of(), frm=k)
        check("follow: a shrunk rollout sends reset shrunk, then a snapshot",
              s and fl.objs(k)[0] == {"schema": 1,
                                      "reset": {"why": "shrunk"}} and
              s[0]["snapshot"]["chapters"] == [], fl.objs(k))

        # rotation: a new thread (its rollout, then its snapshot in place
        # of the old one)
        time.sleep(0.4)
        k = len(fl.got)
        rollout(TID2, proj, user(1, "new thread #45"))
        snapshot(TID2, n, marker)
        os.unlink(s1)
        s = fl.wait(snap_of(TID2, "codex"), 10, frm=k)
        check("follow: a new thread sends reset rotated, then the new id's "
              "snapshot", s and fl.objs(k)[0] == {
                  "schema": 1, "reset": {"why": "rotated"}} and
              [c["title"] for c in s[-1]["snapshot"]["chapters"]] ==
              ["new thread #45"], fl.objs(k))

        # a thread whose rollout appears after the follower started, in a
        # day dir that is not there yet (today's, as Codex names it)
        time.sleep(0.4)
        k = len(fl.got)
        snapshot(TID3, n, marker)
        os.unlink(os.path.join(SNAPS, TID2 + ".1.sh"))
        s = fl.wait(snap_of(TID3), 10, frm=k)
        check("follow: a new thread with no rollout yet: corpus none, its "
              "id", s and s[-1]["snapshot"]["corpus"] == "none", fl.objs(k))
        k = len(fl.got)
        lt = time.localtime()
        today = os.path.join(CODEX, "sessions", "%04d" % lt.tm_year,
                             "%02d" % lt.tm_mon, "%02d" % lt.tm_mday)
        time.sleep(0.3)
        rollout(TID3, proj, user(1, "late rollout #46"), today)
        s = fl.wait(snap_of(TID3, "codex"), 10, frm=k)
        check("follow: the rollout's arrival is seen (reset rotated, its "
              "snapshot)", s and [c["title"] for c in
                                 s[-1]["snapshot"]["chapters"]] ==
              ["late rollout #46"], fl.objs(k))

        before = files(T)
        k = len(fl.got)
        with open(os.path.join(today, os.listdir(today)[0]), "a") as f:
            f.write(user(2, "quiet #47"))
        fl.wait(lambda o: "chapter" in o, frm=k)
        new = sorted(set(files(T)) - set(before))
        check("follow: nothing written", not new, new)

        dch("-k", n)
        try:
            rc = fl.p.wait(5)
        except subprocess.TimeoutExpired:
            rc = None
        check("follow: the session's end ends it, exit 0", rc == 0, rc)
    finally:
        if fl:
            fl.stop()
        dch("-k", n)


def test_follow_agent():
    """A shell's follower as a Codex starts in it and exits: the master's
    agent tick pauses the ring for the Codex as for a Claude (7.3), so the
    stream turns to the rollout and back."""
    n = "xx-sh"
    proj = os.path.join(T, "shproj")
    os.makedirs(proj)
    r = dch("--spawn", n, "--size", "80x24", "--cwd", proj, "sh")
    check("follow agent: shell spawned", r.returncode == 0, r.stderr)
    fl = None
    try:
        h, _ = X.read_all(n, 0)
        if h.get("status", 0) & X.S_OFF:
            print("  skip follow agent: no ring (a build without the mirror)")
            return
        fl = Follow(n)
        s0 = fl.wait(snap_of(corpus="shell"))
        check("follow agent: the first snapshot is corpus shell", s0,
              fl.objs())
        log = os.path.join(T, "sh-codex.log")
        k = len(fl.got)
        dch("--send", n, "FAKE_CODEX_LOG=%s %s\r" % (log, FAKE))
        got = wait_for(lambda: os.path.exists(log) and
                       open(log).read().endswith("\n") and
                       open(log).read().split(), 10)
        check("follow agent: codex started in the shell", bool(got))
        if not got:
            return
        cpid = int(got[0])
        rollout(TID, proj, user(1, "from the shell #48"))
        snapshot(TID, n, got[1] if got[1] != "-" else None)
        s = fl.wait(snap_of(TID, "codex"), 15, frm=k)
        check("follow agent: the Codex's start sends reset rotated, then a "
              "snapshot of corpus codex", s and {"schema": 1, "reset": {
                  "why": "rotated"}} in fl.objs(k), fl.objs(k))
        h, _ = X.read_all(n, 0)
        check("follow agent: the ring is paused while the Codex runs",
              h.get("status", 0) & X.S_PAUSED, h)
        k = len(fl.got)
        os.kill(cpid, 15)           # the fake codex we started: its pid
        s = fl.wait(snap_of(corpus="shell"), 10, frm=k)
        check("follow agent: its exit sends the shell's snapshot again", s,
              fl.objs(k))
    finally:
        dch("-k", n)
        if fl:
            wait_for(lambda: fl.p.poll() is not None, 5)
            fl.stop()


def main():
    os.makedirs(os.path.dirname(FAKE))
    b = subprocess.run(["cc", "-o", FAKE, os.path.join(HERE, "fake_codex.c")],
                       capture_output=True, text=True, timeout=60)
    check("the fake codex builds", b.returncode == 0, b.stderr)
    # a hook in the Codex dir that would leave a mark if anything ran it
    hook = os.path.join(CODEX, "notify.sh")
    with open(hook, "w") as f:
        f.write("#!/bin/sh\ntouch %s\n" % RAN)
    os.chmod(hook, 0o700)
    with open(os.path.join(CODEX, "config.toml"), "w") as f:
        f.write('notify = ["%s"]\n' % hook)
    try:
        test_live()
        test_relative_home()
        test_scope()
        test_follow()
        test_follow_agent()
        check("nothing in the Codex dir was ever run", not os.path.exists(RAN))
    finally:
        shutil.rmtree(T, ignore_errors=True)
    if fails[0]:
        print("%d failed" % fails[0])
        sys.exit(1)


if __name__ == "__main__":
    main()
