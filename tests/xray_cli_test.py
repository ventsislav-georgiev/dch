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
import json, os, re, shutil, socket, stat, struct, subprocess, sys
import tempfile, threading, time

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
    cwd = os.path.join(T, "sh proj")
    os.makedirs(cwd, exist_ok=True)
    r = dch("--spawn", n, "--size", "80x24", "--cwd", cwd, "sh", "-c", prog)
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
        real = os.path.realpath(cwd)
        check("shell: project_label is the shell's cwd, project its key "
              "(7.1)", o["project_label"] == real and
              o["project"] == key(real), (o["project"], o["project_label"]))
        rc, c, err = xray(n, "--clear", "--json")
        check("clear: {schema, session, session_id, corpus, ring_cleared}",
              rc == 0 and c == {"schema": 1, "session": n, "session_id":
                                o["session_id"], "corpus": "shell",
                                "ring_cleared": True}, (rc, c, err))
        rc, o2, _ = xray(n, "--json")
        check("clear: the ring is empty after it", rc == 0 and
              o2["chapters"] == [] and o2["session_id"] == o["session_id"],
              o2)
        # T9h: the visible rows go too (the mirror's screen and scrollback)
        rec = dch("--read", n, "--recent", "1000").stdout
        check("clear: the screen and scrollback are cleared (--read "
              "--recent agrees)", "ran:hello" not in rec and
              "hello world" not in rec, rec[-300:])
        # the shell's pending `read x` takes the next line: had the clear's
        # ESC[H ESC[2J ESC[3J reached the pty, they would lead it
        dch("--send", n, "MARKER\n")
        got = wait_for(lambda: os.path.exists(flag) and open(flag).read(), 5)
        check("clear: nothing reached the pty (the next line read is "
              "exactly the marker)", got == "MARKER\n", repr(got))
        rc, c, _ = xray(o["session_id"], "--clear", "--json")
        check("clear: an exact live session_id resolves", rc == 0 and
              c and c["session"] == n, c)
        rc, o3, err = xray(o["session_id"], "--json")
        check("json: a session_id is only a name to --json (exit 1)",
              rc == 1 and err.strip() == "dch: no such session", err)
    finally:
        dch("-k", n)


# ---- a record name is History only (3.1) ---------------------------------

def test_record():
    """A plain shell's record is <name>.json; its next cold start rewrites
    it. The Ended pane holding that name never reads the new ring."""
    n = "xc-box"
    r = dch("--spawn", n, "sh", "-c", "echo OLD; sleep 60")
    check("record: spawned", r.returncode == 0, r.stderr)
    wait_for(lambda: "OLD" in screen(n), 5)
    dch("-k", n)
    wait_for(lambda: not os.path.exists(sock(n)), 5)
    rc, o, err = xray(n + ".json", "--json")
    check("record: the ended record answers corpus none, its record name",
          rc == 0 and o and o["corpus"] == "none" and o["reason"] ==
          "ended" and o["session_id"] == n + ".json" and not o["live"],
          (rc, o, err))
    r = dch("--spawn", n, "sh", "-c", "echo NEW; sleep 60")
    try:
        wait_for(lambda: "NEW" in screen(n), 5)
        h0, _ = X.read_all(n, 0)
        rc, o, err = xray(n + ".json", "--json")
        check("record: once the name runs again, <name>.json answers from "
              "History only: corpus none, reason ended, its record name, the "
              "shell label (3.1, 7.5, T11), never the new ring",
              rc == 0 and o and o["corpus"] == "none" and o["reason"] ==
              "ended" and o["session_id"] == n + ".json" and not o["live"] and
              o["chapters"] == [] and o["project_label"] and o["lines"] is None,
              (rc, o, err))
        rc, o, err = xray(n + ".json", "--clear", "--json")
        check("record: --clear by that name exits 1 no such session",
              rc == 1 and err.strip() == "dch: no such session", (rc, err))
        h, _ = X.read_all(n, 0)
        check("record: the new ring is untouched (never cleared: same "
              "epoch, same session_id)", h["epoch"] == h0["epoch"] and
              h["session_id"] == h0["session_id"], (h0, h))
    finally:
        dch("-k", n)


# ---- 3.8: a 16M ring under 16 MB ------------------------------------------

# ASan's shadow memory and quarantine inflate RSS past any budget for dch
# itself; scripts/sanitize.sh exports ASAN_OPTIONS.
SANITIZED = "ASAN_OPTIONS" in os.environ
HELPER = os.environ.get("DCH_MAXRSS")   # tests/maxrss.c, built by run.sh


def maxrss(argv, env):
    """(rc, stdout, max RSS in bytes) of one run, through tests/maxrss.c: a
    child Python spawns starts at Python's RSS high-water mark on Linux."""
    if not HELPER:
        return -1, "", 1 << 62
    r = subprocess.run([HELPER] + argv, capture_output=True, text=True,
                       timeout=90, env=env)
    m = re.search(r"^maxrss (\d+)$", r.stderr, re.M)
    return r.returncode, r.stdout, int(m.group(1)) if m else 1 << 62


def rss_ok(what, rss):
    """The 16 MiB budget (3.8); skipped, visibly, under sanitizers."""
    if SANITIZED:
        print("  skip %s: %.1f MB, no RSS budget under ASan" % (what, rss / 1e6))
        return
    check("%s: under 16 MB (3.8): %.1f MB" % (what, rss / 1e6),
          rss < 16 << 20, "no DCH_MAXRSS helper" if not HELPER else rss)


def test_heavy():
    n = "xc-flood"
    cfg = ENV["DCH_CONFIG"]
    with open(cfg, "w") as f:
        f.write("xray_ring = 16M\n")
    try:
        r = dch("--spawn", n, "--size", "80x24", "sh", "-c",
                "yes x | head -n 2500000; echo FLOOD''DONE; sleep 120")
        check("heavy: spawned", r.returncode == 0, r.stderr)
        done = wait_for(lambda: "FLOODDONE" in screen(n), 120)
        check("heavy: the flood finished", done)
        c = X.connect(n)
        h, _ = X.read(c, 0, 1 << 16)
        c.close()
        rows = h["tail"] - h["head"]
        rc, out, rss = maxrss([DCH, "--xray", n, "--json"], ENV)
        try:
            o = json.loads(out)
        except ValueError:
            o = None
        check("heavy: a 16M ring (%d records) answers" % rows,
              rc == 0 and o and o["corpus"] == "shell" and rows > 1000000,
              (rc, out[:200]))
        rss_ok("heavy: the querying process", rss)
        check("heavy: evicted rows are complete:false, why range (3.2)",
              o and o["complete"] is False and o["why"] == "range" and
              o["chapters_evicted"] is False, o and
              {k: o[k] for k in ("complete", "why", "lines")})
    finally:
        dch("-k", n)
        os.unlink(cfg)


# ---- 3.2: evicted rows are the master's fact (DCH_RING_S_EVICTED) --------

def test_evicted():
    cfg = ENV["DCH_CONFIG"]
    try:
        with open(cfg, "w") as f:
            f.write("xray_ring = 128K\n")
        n = "xc-ev"
        r = dch("--spawn", n, "--size", "80x24", "sh", "-c",
                "seq 1 40000; echo EV''DONE; sleep 60")
        check("evicted: spawned under xray_ring 128K", r.returncode == 0,
              r.stderr)
        m = "xc-fit"
        with open(cfg, "w") as f:
            f.write("xray_ring = 64K\n")
        r = dch("--spawn", m, "--size", "80x24", "sh", "-c",
                "seq 1 10; echo FIT''DONE; sleep 60")
        check("evicted: spawned under xray_ring 64K", r.returncode == 0,
              r.stderr)
        wait_for(lambda: "EVDONE" in screen(n), 20)
        wait_for(lambda: "FITDONE" in screen(m), 10)
        for what, body in (("the config raised to 16M", "xray_ring = 16M\n"),
                           ("the config gone (the 2M default)", None)):
            if body:
                with open(cfg, "w") as f:
                    f.write(body)
            elif os.path.exists(cfg):
                os.unlink(cfg)
            rc, o, err = xray(n, "--json")
            check("evicted: %s, the ring that evicted stays complete:false, "
                  "why range" % what, rc == 0 and o and o["complete"] is False
                  and o["why"] == "range", (rc, o and {k: o[k] for k in (
                      "complete", "why", "lines")}, err))
            rc, o, err = xray(m, "--json")
            check("evicted: %s, a 64K ring that evicted nothing is complete"
                  % what, rc == 0 and o and o["complete"] is True and
                  o["why"] is None and o["lines"] >= 10, (rc, o, err))
        rc, c, _ = xray(n, "--clear", "--json")
        rc, o, err = xray(n, "--json")
        check("evicted: a clear resets it (complete again)", rc == 0 and o and
              o["complete"] is True, (rc, o, err))
    finally:
        dch("-k", "xc-ev")
        dch("-k", "xc-fit")
        if os.path.exists(cfg):
            os.unlink(cfg)


# ---- a hostile master never holds the query (F2, F7) ----------------------

MSG_READ_DATA, MSG_READ_END, MSG_RING_READ, MSG_RING_HDR = 8, 9, 18, 19


class Evil:
    """A same-uid socket in the session dir that answers MSG_RING_READ
    against the protocol: `mode` floods READ_DATA past any reply bound,
    repeats old records (first < seq), trickles one record per reply under
    a far tail, or closes every connection at once; "hdr" answers an empty
    ring and keeps each request's flags byte."""

    def __init__(self, name, mode, sid=ID):
        self.path, self.mode, self.sid = sock(name), mode, sid
        self.reads, self.flags = 0, []
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.bind(self.path)
        self.s.listen(8)
        threading.Thread(target=self.serve, daemon=True).start()

    def close(self):
        self.s.close()
        os.unlink(self.path)

    def serve(self):
        while True:
            try:
                c, _ = self.s.accept()
            except OSError:
                return
            threading.Thread(target=self.conn, args=(c,), daemon=True).start()

    def conn(self, c):
        try:
            if self.mode == "close":
                c.recv(3)            # the request's header, then gone
                return
            while True:
                h = b""
                while len(h) < 3:
                    k = c.recv(3 - len(h))
                    if not k:
                        return
                    h += k
                t, n = struct.unpack("<BH", h)
                p = b""
                while len(p) < n:
                    p += c.recv(n - len(p))
                if t != MSG_RING_READ:
                    c.sendall(struct.pack("<BH", MSG_READ_END, 5))  # UNSUP
                    continue
                seq = struct.unpack_from("<Q", p)[0]
                self.reads += 1
                self.flags.append(p[12] if len(p) > 12 else 0)
                if self.mode == "hdr":
                    hdr = struct.pack("<BQQQII", 0, 5, 5, 5, 0, 7) + \
                        self.sid.encode()
                    c.sendall(struct.pack("<BH", MSG_RING_HDR, len(hdr)) + hdr +
                              struct.pack("<BH", MSG_READ_END, 0))
                    continue
                first = {"flood": seq, "repeat": 100, "trickle": seq}[self.mode]
                hdr = struct.pack("<BQQQII", 0, 0, 1 << 62, first, 1, 7) + \
                    self.sid.encode()
                c.sendall(struct.pack("<BH", MSG_RING_HDR, len(hdr)) + hdr)
                if self.mode == "flood":
                    # 32 MiB: past any reply bound, yet finite, so a client
                    # without the bound fails the RSS check, not the host
                    blk = struct.pack("<BH", MSG_READ_DATA, 4096) + b"x" * 4096
                    for _ in range(8192):
                        c.sendall(blk)
                    c.sendall(struct.pack("<BH", MSG_READ_END, 0))
                    continue
                rec = struct.pack("<HBBI", 0, 3, 0, 0)
                c.sendall(struct.pack("<BH", MSG_READ_DATA, len(rec)) + rec +
                          struct.pack("<BH", MSG_READ_END, 0))
        except OSError:
            pass
        finally:
            c.close()


def test_evil():
    os.makedirs(os.path.dirname(sock("x")), mode=0o700, exist_ok=True)
    for mode, what, reads in (
            ("flood", "32 MiB of READ_DATA in one reply", 1),
            ("repeat", "first < seq again (no progress: no third ask)", 2),
            ("trickle", "one record a reply under tail 2^62 (64 replies)", 65)):
        e = Evil("xc-evil", mode)
        try:
            t0 = time.time()
            rc, out, rss = maxrss([DCH, "--xray", "xc-evil", "--json"], ENV)
            check("evil: %s exits 3 in %.1fs, %d reads" % (
                      what, time.time() - t0, e.reads),
                  rc == 3 and time.time() - t0 < 20 and e.reads <= reads,
                  (rc, out[:200]))
            rss_ok("evil: %s" % mode, rss)
        finally:
            e.close()
    # F4: --clear <id> asks every live master for the header only
    e = Evil("xc-evil", "hdr", sid="mfedcba9876543210")
    try:
        dch("--xray", "mfedcba9876543210", "--clear", "--json")
        check("evil: a session_id --clear asks for the header only "
              "(DCH_RING_F_HDR)", e.flags and all(f & 2 for f in e.flags),
              e.flags)
    finally:
        e.close()
    # F7: a master that hangs up mid-request: never a signal, and a
    # nonzero exit carries one stderr line (a hang-up before any byte is a
    # master before #119: corpus none, exit 0)
    e = Evil("xc-evil", "close")
    try:
        bad = []
        for k in range(20):
            for args in (("--json",), ("--clear", "--json")):
                r = dch("--xray", "xc-evil", *args)
                if r.returncode < 0 or (r.returncode and (
                        len(r.stderr.splitlines()) != 1 or
                        not r.stderr.startswith("dch: "))) or \
                        (not r.returncode and not r.stdout.startswith("{")):
                    bad.append((args, r.returncode, r.stderr))
        check("evil: 40 hang-ups: never a signal, one stderr line on a "
              "nonzero exit", not bad, bad[:3])
    finally:
        e.close()


# ---- a live Claude and the open seam --------------------------------------

def key(cwd):
    return "".join(c if c.isascii() and c.isalnum() else "-" for c in cwd)


def files(root):
    out = {}
    for dp, dn, fn in os.walk(root):
        for f in dn + fn:
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
        tmpd = os.path.join(T, "qtmp")
        os.makedirs(tmpd)
        dbg = dict(ENV, DCH_DEBUG=trace, TMPDIR=tmpd)
        state = os.path.join(T, "state")
        before = files(T)
        state0 = files(state)
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
        rc, o2, _ = xray(sid, "--json", env=dbg)
        check("claude: the agent UUID answers the same session",
              rc == 0 and o2 and o2["session"] == n and
              o2["chapters"] == o["chapters"], o2)
        rc, c, err = xray(n, "--clear", "--json", env=dbg)
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
        state1 = files(state)
        check("nothing written: the state dir is as it was (names, sizes, "
              "mtimes)", state1 == state0, sorted(
                  set(state1.items()) ^ set(state0.items())))
        check("nothing written: TMPDIR stays empty (spec 9.5)",
              os.listdir(tmpd) == [], os.listdir(tmpd))
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
        for pad in ("  " + ID, ID + " ", "\t" + ID + "\n"):
            r = dch("-m", "xc-live", pad)
            check("name rule: -m with whitespace padding %r exits 2" % pad,
                  r.returncode == 2 and "looks like a session_id" in r.stderr,
                  (r.returncode, r.stderr))
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
        test_record()
        test_heavy()
        test_evicted()
        test_evil()
        test_claude()
    finally:
        shutil.rmtree(T, ignore_errors=True)
    if fails[0]:
        print("%d failed" % fails[0])
        sys.exit(1)


if __name__ == "__main__":
    main()
