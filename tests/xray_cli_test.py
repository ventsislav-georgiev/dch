#!/usr/bin/env python3
"""`dch --xray <n> --json` and `--clear` through real masters (#120, XR1b).

tests/xray_golden.py pins the answers over fixture rings and transcripts;
this drives what a fixture cannot: a real shell's OSC 133 block typed with
--send (the client-input flag), --clear through MSG_RING_CLEAR with nothing
reaching the pty, a live Claude (tests/fake_claude.py as `claude`) and its
transcript, the open seam's counting test (spec 8.1: one open per Claude
query, none for a shell, a clear or a refused lookup; nothing written), and
the INFO-2 name rule on every create path dch runs locally.

Run:  python3 tests/xray_cli_test.py          (uses ./dch; run.sh isolates)
"""
import json, os, re, shutil, stat, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import xray_ring as X  # noqa: E402

DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
ID = "m0123456789abcdef"
fails = [0]


def check(msg, cond, detail=""):
    print("  %s %s" % ("ok  " if cond else "FAIL", msg))
    if not cond:
        fails[0] += 1
        if detail:
            print("       " + str(detail)[:800])


T = tempfile.mkdtemp(prefix="xc")
HOME = os.path.join(T, "home")
os.makedirs(HOME)
ENV = {k: v for k, v in os.environ.items()
       if not k.startswith(("DCH_SESSION", "DCH_SOCKET_DIR", "DCH_DEBUG",
                            "CLAUDE", "Malloc"))}
ENV.update(XDG_RUNTIME_DIR=T, XDG_STATE_HOME=os.path.join(T, "state"),
           HOME=HOME, DCH_CONFIG=os.path.join(T, "config"),
           DCH_LOGIN_ENV="0", DCH_NO_REMOTE="1")
os.environ.update(XDG_RUNTIME_DIR=T)     # xray_ring.sock_path
os.environ.pop("DCH_SOCKET_DIR", None)


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


def screen(name):
    return dch("--read", name).stdout


def sock(name):
    return os.path.join(T, "dch-%d" % os.getuid(), name + ".sock")


# ---- a real shell block ---------------------------------------------------

def test_shell():
    n = "xc-sh"
    flag = os.path.join(T, "pty-got")
    prog = ("printf '\\033]133;A\\007$ \\033]133;B\\007'; read cmd; "
            "printf '\\033]133;C\\007'; echo \"ran:$cmd\"; "
            "printf '\\033]133;D;4\\007'; seq 1 40; "
            "printf '\\033]133;A\\007$ '; read x; echo \"$x\" > %s; "
            "sleep 60" % flag)
    r = dch("--spawn", n, "--size", "80x24", "sh", "-c", prog)
    check("shell: spawned", r.returncode == 0, r.stderr)
    try:
        wait_for(lambda: "$" in screen(n), 5)
        dch("--send", n, "hello  world\n")
        wait_for(lambda: "40" in screen(n), 5)
        rc, o, err = xray(n, "--json")
        h, recs = X.read_all(n, 0)
        prompts = [r["seq"] for r in recs if r["kind"] == 1]
        check("shell: one schema-1 object, corpus shell, live",
              rc == 0 and o and o["schema"] == 1 and o["corpus"] == "shell"
              and o["live"] and o["available"] and o["refs"] == [], (rc, err))
        if not o:
            return
        check("shell: session_id is the ring's", o["session_id"] ==
              h["session_id"] and re.fullmatch(r"m[0-9a-f]{16}",
                                               o["session_id"]), o)
        ch = o["chapters"]
        check("shell: the typed block is a chapter (title collapsed, D 4 "
              "is an error)", len(ch) == 1 and ch[0]["title"] ==
              "hello world" and ch[0]["error"] and ch[0]["kind"] ==
              "command", ch)
        check("shell: anchored on its prompt record's seq",
              ch and prompts and ch[0]["anchor"] == "s%d" % prompts[0] and
              ch[0]["offset"] == prompts[0], (ch, prompts))
        check("shell: lines = ring rows plus the screen's",
              o["lines"] >= 40 and o["complete"] and not o["mentions_only"],
              o)
        rc, c, err = xray(n, "--clear", "--json")
        check("clear: {schema, session, session_id, corpus, ring_cleared}",
              rc == 0 and c == {"schema": 1, "session": n, "session_id":
                                o["session_id"], "corpus": "shell",
                                "ring_cleared": True}, (rc, c, err))
        time.sleep(0.5)
        check("clear: nothing reached the pty", not os.path.exists(flag))
        rc, o2, _ = xray(n, "--json")
        check("clear: the ring is empty after it", rc == 0 and
              o2["chapters"] == [] and o2["session_id"] == o["session_id"],
              o2)
        # T9h: the visible rows go too (the mirror's screen and scrollback)
        rec = dch("--read", n, "--recent", "1000").stdout
        check("clear: the screen and scrollback are cleared (--read "
              "--recent agrees)", "ran:hello" not in rec and
              "hello world" not in rec, rec[-300:])
        rc, c, _ = xray(o["session_id"], "--clear", "--json")
        check("clear: an exact live session_id resolves", rc == 0 and
              c and c["session"] == n, c)
        rc, o3, err = xray(o["session_id"], "--json")
        check("json: a session_id is only a name to --json (exit 1)",
              rc == 1 and err.strip() == "dch: no such session", err)
    finally:
        dch("-k", n)


# ---- a live Claude and the open seam --------------------------------------

def key(cwd):
    return "".join(c if c.isascii() and c.isalnum() else "-" for c in cwd)


def files(root):
    out = {}
    for dp, dn, fn in os.walk(root):
        for f in fn:
            p = os.path.join(dp, f)
            st = os.lstat(p)
            out[p] = (st.st_size, st.st_mtime_ns)
    return out


def test_claude():
    n = "xc-cl"
    bindir = os.path.join(T, "bin")
    fdir = os.path.join(T, "fake")
    proj = os.path.join(T, "proj")
    for d in (bindir, fdir, proj):
        os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    trace = os.path.join(T, "trace")
    env = dict(ENV, PATH=bindir + ":" + ENV.get("PATH", "/usr/bin:/bin"),
               FAKE_CLAUDE_DIR=fdir)
    r = subprocess.run([DCH, "--spawn", n, "--size", "80x24", "--cwd", proj,
                        "claude"], capture_output=True, text=True, timeout=30,
                       env=env)
    check("claude: spawned", r.returncode == 0, r.stderr)
    try:
        sdir = os.path.join(HOME, ".claude", "sessions")
        rec = wait_for(lambda: [f for f in os.listdir(sdir)
                                if f.endswith(".json")]
                       if os.path.isdir(sdir) else None, 10)
        sid = json.load(open(os.path.join(sdir, rec[0])))["sessionId"]
        pdir = os.path.join(HOME, ".claude", "projects", key(proj))
        os.makedirs(pdir, exist_ok=True)
        tp = os.path.join(pdir, sid + ".jsonl")
        with open(tp, "w") as f:
            for i, t in enumerate(("first ask", "second ask")):
                f.write(json.dumps({"type": "user", "cwd": proj,
                                    "timestamp": "2026-11-10T10:0%d:00Z" % i,
                                    "message": {"content": t}}) + "\n")
                f.write(json.dumps({"type": "assistant", "message": {
                    "content": [{"type": "text", "text": "ok"}]}}) + "\n")
        dbg = dict(ENV, DCH_DEBUG=trace)
        before = files(T)
        rc, o, err = wait_for(lambda: (lambda x: x if x[1] and
                                       x[1]["corpus"] == "claude" else None)
                              (xray(n, "--json", env=dbg)), 10) or (1, None,
                                                                    "")
        check("claude: a live session's transcript chapters",
              rc == 0 and o and o["live"] and o["session_id"] == sid and
              [c["title"] for c in o["chapters"]] == ["first ask",
                                                      "second ask"] and
              o["project"] == key(proj) and o["project_label"] == proj,
              (rc, o, err))
        st = os.stat(tp)
        check("claude: anchors are f<ino hex>:<offset>",
              o and o["chapters"][0]["anchor"] == "f%x:0" % st.st_ino, o)
        rc, o2, _ = xray(sid, "--json")
        check("claude: the agent UUID answers the same session",
              rc == 0 and o2 and o2["session"] == n and
              o2["chapters"] == o["chapters"], o2)
        rc, c, err = xray(n, "--clear", "--json")
        check("clear: an agent session has no ring (ring_cleared false)",
              rc == 0 and c == {"schema": 1, "session": n, "session_id": sid,
                                "corpus": "claude", "ring_cleared": False},
              (c, err))

        # the seam's counting test
        open(trace, "w").close()
        xray(n, "--json", env=dbg)
        tr = open(trace).read()
        check("seam: one Claude query, exactly one open, none refused",
              tr.count("xray seam: open") == 1 and
              "xray seam: refused" not in tr, tr[-600:])
        check("seam: no transcript text in a trace line",
              "first ask" not in tr and "second ask" not in tr)
        open(trace, "w").close()
        xray(n, "--clear", "--json", env=dbg)
        check("seam: --clear opens nothing",
              "xray seam:" not in open(trace).read())
        # two <sid>.jsonl under two project dirs: an ambiguous lookup
        other = os.path.join(HOME, ".claude", "projects", "-elsewhere")
        os.makedirs(other)
        shutil.copy(tp, os.path.join(other, sid + ".jsonl"))
        open(trace, "w").close()
        rc, o3, _ = xray(n, "--json", env=dbg)
        tr = open(trace).read()
        check("seam: two hits open nothing (corpus none, session_id kept)",
              rc == 0 and o3["corpus"] == "none" and
              o3["session_id"] == sid and "xray seam:" not in tr and
              "xray lookup: 2 hits" in tr, (o3, tr[-400:]))
        shutil.rmtree(other)
        # a symlink in the transcript's place: lstat'ed, never opened
        os.rename(tp, tp + ".real")
        os.symlink(tp + ".real", tp)
        open(trace, "w").close()
        rc, o4, _ = xray(n, "--json", env=dbg)
        tr = open(trace).read()
        check("seam: a symlinked transcript is not followed",
              rc == 0 and o4["corpus"] == "none" and "xray seam: open" not in
              tr, (o4, tr[-400:]))
        os.unlink(tp)
        os.rename(tp + ".real", tp)
        after = files(T)
        new = sorted(p for p in set(after) - set(before)
                     if p != trace and "/fake/" not in p and
                     "/-elsewhere/" not in p and "/state/" not in p and
                     # the master's own sidecars (.act on fake claude output)
                     not p.startswith(sock(n) + "."))
        check("nothing written: no file appears outside the trace", not new,
              new)
        check("nothing written: the transcript is untouched",
              after.get(tp, (0, 0))[0] == before[tp][0])
    finally:
        dch("-k", n, env=env)


# ---- INFO-2: the name rule ------------------------------------------------

def test_names():
    def refused(r, msg):
        check("name rule: %s exits 2 'name looks like a session_id'" % msg,
              r.returncode == 2 and
              "dch: name looks like a session_id" in r.stderr, r.stderr)
        check("name rule: %s starts nothing" % msg,
              not os.path.exists(sock(ID)))

    refused(dch("-n", ID), "-n")
    refused(dch("--spawn", ID, "sh"), "--spawn")
    r = dch("--spawn", "xc-old", "sh", "-c", "exit 0")
    time.sleep(0.5)
    refused(dch("--revive", "xc-old", "--as", ID), "--revive --as")
    r = dch("--spawn", "xc-live", "sh", "-c", "sleep 30")
    try:
        r = dch("-m", "xc-live", ID)
        check("name rule: -m (rename) exits 2", r.returncode == 2 and
              "looks like a session_id" in r.stderr, r.stderr)
        check("name rule: the alias is not written",
              ID not in dch("-ls").stdout)
        r = dch("-m", "xc-live", "m0123456789abcdeF")
        check("name rule: only the exact shape (an uppercase digit is fine)",
              r.returncode == 0, r.stderr)
        r = dch("--spawn", "m0123456789abcdeg", "sh", "-c", "sleep 1")
        check("name rule: a non-hex letter is a name", r.returncode == 0,
              r.stderr)
    finally:
        dch("-k", "xc-live")
        dch("-k", "m0123456789abcdeg")


def main():
    try:
        test_names()
        test_shell()
        test_claude()
    finally:
        shutil.rmtree(T, ignore_errors=True)
    if fails[0]:
        print("%d failed" % fails[0])
        sys.exit(1)


if __name__ == "__main__":
    main()
