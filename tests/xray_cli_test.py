#!/usr/bin/env python3
"""`dch --xray <n> --json` and `--clear` through real masters (#120, XR1b).

tests/xray_golden.py pins the answers over fixture rings and transcripts;
this drives what a fixture cannot: a real shell's OSC 133 block typed with
--send (the client-input flag), --clear through MSG_RING_CLEAR with nothing
reaching the pty, a live Claude (tests/fake_claude.py as `claude`) and its
transcript, the open seam's counting test (spec 8.1: one open per Claude
query, none for a shell, a clear or a refused lookup; nothing written), and
the INFO-2 name rule on every create path dch runs locally, and refs (#135):
the index freed with its query (K3), --ref's budget stop, nothing written,
SIGTERM and EPIPE as a cancel, and the 16 MB budget at the index's caps,
and the primary screen (#140): read with the ring, never the alt buffer, and
--chapter (#156): a live and a History Claude chapter and a live shell's,
the value rules, stale anchors, the 6000/3000 caps and the 1 MiB cap.

Run:  python3 tests/xray_cli_test.py          (uses ./dch; run.sh isolates)
"""
import json, os, re, shutil, socket, stat, struct, subprocess, sys
import tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import xray_ring as X  # noqa: E402

DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
ID = "m0123456789abcdef"
UP = "M0123456789ABCDEF"
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


# ---- MED-1 (QA 1.44): no ring, no clear ----------------------------------

def test_clear_off():
    """xray_ring 0: --clear answers corpus none, ring_cleared false, and
    the mirror's screen and scrollback stay, from dch or from a raw op."""
    cfg = ENV["DCH_CONFIG"]
    n = "xc-off"
    with open(cfg, "w") as f:
        f.write("xray_ring = 0\n")
    try:
        r = dch("--spawn", n, "--size", "80x24", "sh", "-c",
                "seq 1 200; echo OFF''TAIL; sleep 60")
        check("clear off: spawned under xray_ring 0", r.returncode == 0,
              r.stderr)
        wait_for(lambda: "OFFTAIL" in screen(n), 10)
        rec0 = dch("--read", n, "--recent", "1000").stdout
        rc, c, err = xray(n, "--clear", "--json")
        check("clear off: corpus none, ring_cleared false",
              rc == 0 and c and c["corpus"] == "none" and
              c["ring_cleared"] is False, (rc, c, err))
        rec = dch("--read", n, "--recent", "1000").stdout
        check("clear off: the screen and scrollback survive (--read "
              "--recent)", "OFFTAIL" in screen(n) and "\n7\n" in rec and
              rec == rec0, (rec0[-120:], rec[-120:]))
        check("clear off: a raw MSG_RING_CLEAR is ACKed and clears nothing",
              X.clear(n) and dch("--read", n, "--recent", "1000").stdout ==
              rec0)
    finally:
        dch("-k", n)
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
            if self.mode == "rst":
                return               # gone before reading a byte
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
    # nonzero exit carries one stderr line (#153: the session is gone, exit
    # 1; test_hangup pins each close point)
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
    # #142: a master that closes before reading anything. The close races
    # the client's write; the write's failure and the read's EOF (#153)
    # both exit 1 with exactly "dch: no such session".
    e = Evil("xc-evil", "rst")
    try:
        bad, fails = [], 0
        for k in range(200):
            r = dch("--xray", "xc-evil", "--json")
            if r.returncode < 0 or (r.returncode and (
                    r.stderr != "dch: no such session\n")):
                bad.append((r.returncode, r.stderr))
            fails += r.returncode > 0
            if fails >= 5 or bad:
                break
        check("evil: a close before any read: a failed request write "
              "exits nonzero with one stderr line (%d hit)" % fails,
              not bad, bad[:3])
    finally:
        e.close()


MSG_READ, MSG_ACK, MSG_CMD, MSG_RING_CLEAR = 6, 11, 15, 21


class Hang(Evil):
    """#153: a master that hangs up at one point of the X-ray request.
    "pre": every connection, once a request's header is in, before any reply
    byte. On a record read (a header-only read and MSG_CMD are answered):
    "hdr" after the ring header, "rec" inside a READ_DATA frame, "next" on
    the second reply's request; --clear's MSG_RING_CLEAR before its ack.
    "scr": the ring answers in full, the screen read (MSG_READ) hangs up."""

    def conn(self, c):
        def recv(n):
            b = b""
            while len(b) < n:
                k = c.recv(n - len(b))
                if not k:
                    raise OSError
                b += k
            return b

        def hdr(sub, tail):
            h = struct.pack("<BQQQII", sub, 0, tail, 0, 1, 7) + \
                self.sid.encode()
            return struct.pack("<BH", MSG_RING_HDR, len(h)) + h

        rec = struct.pack("<HBBI", 5, 3, 0, 0) + b"hello"
        data = struct.pack("<BH", MSG_READ_DATA, len(rec)) + rec
        end = struct.pack("<BH", MSG_READ_END, 0)
        p, reads = self.mode, 0
        try:
            while True:
                t, n = struct.unpack("<BH", recv(3))
                if p == "pre":
                    return
                body = recv(n) if t in (MSG_READ, MSG_CMD,
                                        MSG_RING_READ) else b""
                if t == MSG_CMD:
                    c.sendall(struct.pack("<BH", MSG_READ_END, 5))  # UNSUP
                elif t == MSG_READ:
                    if p == "scr":
                        return
                    c.sendall(struct.pack("<BH", MSG_READ_DATA, 3) + b"row" +
                              end)
                elif t == MSG_RING_CLEAR:
                    if p != "scr":
                        return
                    c.sendall(struct.pack("<BH", MSG_ACK, 0))
                elif t != MSG_RING_READ:
                    return
                else:
                    flags = body[12] if len(body) > 12 else 0
                    sub = 0x10 if flags & 1 else 0
                    reads += not flags & 2
                    if flags & 2 or p == "scr":
                        c.sendall(hdr(sub, 1) + (b"" if flags & 2 else data) +
                                  end)
                    elif p == "hdr":
                        return c.sendall(hdr(sub, 1 << 40))
                    elif p == "rec":
                        return c.sendall(hdr(sub, 1 << 40) + data[:9])
                    elif reads > 1:
                        return
                    else:
                        c.sendall(hdr(sub, 1 << 40) + data + end)
        except OSError:
            pass
        finally:
            c.close()


def test_hangup():
    """#153: a master that hangs up after the connect, before the answer is
    complete, is a session gone at every close point and in every form: one
    stderr line "dch: no such session", exit 1, no partial object; --ref
    keeps its `first` line and writes no done. The screen read's own
    connection is no hang-up of the query: silent, the ring's answer."""
    forms = (("json", ("--json",)), ("clear", ("--clear", "--json")),
             ("ref", ("--ref", "o/r#5", "--scope", "session", "--json")),
             ("follow", ("--follow", "--json")))
    for point in ("pre", "hdr", "rec", "next"):
        e = Hang("xc-hang", point)
        try:
            for form, args in forms:
                bad = []
                for _ in range(30):
                    r = dch("--xray", "xc-hang", *args)
                    lines = r.stdout.splitlines()
                    want = ['{"schema":1,"first":'] \
                        if form == "ref" and point != "pre" else []
                    if r.returncode != 1 or \
                            r.stderr != "dch: no such session\n" or \
                            [l[:20] for l in lines] != want:
                        bad.append((r.returncode, r.stderr, r.stdout[:120]))
                check("hang-up %s, %s: 30 runs exit 1, 'no such session'%s" % (
                          point, form, ", `first` and no done" if want else
                          ", no stdout"), not bad, bad[:2])
        finally:
            e.close()
    e = Hang("xc-hang", "scr")
    try:
        for form, args in forms[:3]:
            bad = []
            for _ in range(30):
                r = dch("--xray", "xc-hang", *args)
                last = r.stdout.splitlines()[-1:] or [""]
                if r.returncode or r.stderr or not (
                        last[0].startswith('{"schema":1,"done":') if
                        form == "ref" else last[0].startswith('{"schema":1,"s')):
                    bad.append((r.returncode, r.stderr, r.stdout[:120]))
            check("screen read hangs up, %s: 30 runs exit 0, silent, the "
                  "ring's answer" % form, not bad, bad[:2])
        p = subprocess.Popen([DCH, "--xray", "xc-hang", "--follow", "--json"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env=ENV)
        try:
            line = p.stdout.readline()
            check("screen read hangs up, follow: the snapshot, and it follows",
                  line.startswith('{"schema":1,"snapshot":') and
                  p.poll() is None, line[:120])
        finally:
            p.kill()
            p.communicate()
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
        # --chapter on a live Claude (3.5, #156): one seam open, no write
        open(trace, "w").close()
        rc, c, err, _ = chapter(n, o["chapters"][1]["anchor"], env=dbg)
        tr = open(trace).read()
        check("chapter: a live Claude's chapter, from the transcript",
              rc == 0 and c and c["session"] == n and
              c["session_id"] == sid and c["title"] == "second ask" and
              c["entries"] == [ent("user", "second ask"),
                               ent("assistant", "ok")], (rc, c, err))
        check("chapter: one seam open, no transcript text traced",
              tr.count("xray seam: open") == 1 and "second ask" not in tr,
              tr[-400:])
        wait_for(lambda: "Claude Code" in screen(n), 10)
        scr0 = screen(n)
        rec0 = dch("--read", n, "--recent", "1000").stdout
        rc, c, err = xray(n, "--clear", "--json", env=dbg)
        check("clear: an agent session has no ring (ring_cleared false)",
              rc == 0 and c == {"schema": 1, "session": n, "session_id": sid,
                                "corpus": "claude", "ring_cleared": False},
              (c, err))
        # MED-1 (QA 1.44): ring_cleared false means the mirror is untouched
        check("clear: an agent session's screen and scrollback survive "
              "(--read, --read --recent)", scr0.strip() and
              screen(n) == scr0 and dch("--read", n, "--recent",
                                        "1000").stdout == rec0,
              (scr0[-200:], screen(n)[-200:]))
        # an older or foreign client still sends the op: the master ignores
        # it while an agent runs (ACK, nothing cleared)
        check("clear: a raw MSG_RING_CLEAR to an agent session is ACKed and "
              "clears nothing", X.clear(n) and screen(n) == scr0,
              screen(n)[-200:])

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
        check("seam: two hits report the cwd's project key, not a hit's "
              "(QA 1.44 LOW-3)", o3 and o3["project"] ==
              key(o3["project_label"]) and o3["project_label"] ==
              os.path.realpath(proj), o3)
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

        # QA 1.44 LOW-4: there but unreadable is exit 3, not "gone"
        if os.geteuid() != 0:
            os.chmod(tp, 0)
            r = dch("--xray", n, "--json")
            os.chmod(tp, 0o600)
            check("claude: an unreadable transcript exits 3", r.returncode ==
                  3 and r.stderr == "dch: X-ray failed on this session\n",
                  (r.returncode, r.stderr))
        # QA 1.44 LOW-7: a symlinked project dir is not followed
        moved = os.path.join(T, "moved-proj")
        os.rename(pdir, moved)
        os.symlink(moved, pdir)
        rc, o5, _ = xray(n, "--json")
        check("claude: a symlinked project dir is not followed (corpus "
              "none)", rc == 0 and o5 and o5["corpus"] == "none", o5)
        os.unlink(pdir)
        os.rename(moved, pdir)
        rc, o5, _ = xray(n, "--json")
        check("claude: the real dir back, the transcript answers",
              rc == 0 and o5 and o5["corpus"] == "claude", o5)
        # MED-2 (QA 1.44): ids are kept as a bounded key, never whole
        def put(lines):
            with open(tp, "w") as f:
                f.write(json.dumps({"type": "user", "cwd": proj, "message":
                                    {"content": "start"}}) + "\n")
                for ln in lines:
                    f.write(json.dumps(ln) + "\n")
        big = lambda i: "%08d" % i + "x" * 1000000
        for what, mk in (
                ("tool_use ids", lambda i: {"type": "assistant", "message": {
                    "content": [{"type": "tool_use", "id": big(i),
                                 "name": "Bash", "input": {}}]}}),
                ("agentIds", lambda i: {"type": "assistant",
                                        "isSidechain": True,
                                        "agentId": big(i),
                                        "message": {"content": "s"}})):
            put(mk(i) for i in range(24))
            rc, out, rss = maxrss([DCH, "--xray", n, "--json"], env)
            try:
                o = json.loads(out)
            except ValueError:
                o = None
            check("claude: 24 x 1 MB %s answer" % what, rc == 0 and o and
                  o["corpus"] == "claude", (rc, out[:200]))
            rss_ok("claude: 24 x 1 MB %s" % what, rss)
        # QA 1.44 LOW-8: lines near every cap at once (6000 chapters, then
        # 20 lines just under 1 MiB of ~100k tokens and one paste each)
        z = ",".join(["0"] * 99960)
        room = 1048576 - len(z) - 300
        heavy = ('{"type":"user","x":[' + z + '],"message":{"content":'
                 '"<pasted_content id=\\"k\\">' + "a" * (room - 60) +
                 '</pasted_content id=\\"k\\">"}}')
        with open(tp, "w") as f:
            f.write((json.dumps({"type": "user", "message": {
                "content": "\u0001" * 130}}) + "\n" +
                '{"type":"assistant","message":{"content":"s"}}\n') * 6000)
            f.write((heavy + '\n{"type":"assistant","message":{"content":'
                     '"s"}}\n') * 20)
        rc, out, rss = maxrss([DCH, "--xray", n, "--json"], env)
        check("claude: near-cap lines answer", rc == 0 and
              out.startswith("{"), (rc, out[:200]))
        rss_ok("claude: near-cap lines (6000 chapters, 20 x 1 MiB pastes)",
               rss)
        # the long ids still pair: a quiet Grep's failed result shows no
        # error; an orphan result (no pairing) would mark the chapter
        put([{"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": big(1), "name": "Grep",
                 "input": {"pattern": "x"}}]}},
             {"type": "user", "message": {"content": [
                 {"type": "tool_result", "tool_use_id": big(1),
                  "is_error": True, "content": "x"}]}}])
        rc, o, err = xray(n, "--json")
        check("claude: a 1 MB tool_use id still pairs with its result",
              rc == 0 and o and [(c["title"], c["error"]) for c in
                                 o["chapters"]] == [("start", False)],
              (rc, o and o["chapters"], err))
        # MED-3 (QA 1.44): unclosed <pasted_content> openers are linear
        # (each line under the 1 MiB line cap)
        for what, op, cnt in (
                ("one id", lambda k: '<pasted_content id="a">', 40000),
                ("unique ids", lambda k: '<pasted_content id="a%d">' % k,
                 30000)):
            ask = {"type": "user", "message": {"content": "".join(
                op(k) for k in range(cnt))}}
            put([{"type": "assistant", "message": {"content": [
                {"type": "text", "text": "ok"}]}}, ask] * 3)
            t0 = time.time()
            rc, o, err = xray(n, "--json")
            dt = time.time() - t0
            check("claude: 3 lines of %dk unclosed openers (%s) in %.2fs "
                  "(< 2 s)" % (cnt // 1000, what, dt), rc == 0 and o and dt < 2 and
                  [c["title"] for c in o["chapters"][1:]] == [
                      "".join(op(k) for k in range(6))[:120]] * 3,
                  (rc, dt, o and o["chapters"][:2], err))
        # #155: a live Claude as <n>, --scope project: its own transcript
        # (live), then a History session of its project dir
        put([{"type": "user", "message": {"content": "see #941"}}])
        osid = "33333333-4444-4555-8666-777777777777"
        with open(os.path.join(pdir, osid + ".jsonl"), "w") as f:
            f.write(json.dumps({"type": "user", "cwd": proj, "message": {
                "content": "older #941"}}) + "\n")
        history("xc-cl-old", osid, os.path.join(HOME, ".claude"),
                os.path.realpath(proj))
        open(trace, "w").close()
        r = dch("--xray", n, "--ref", "#941", "--scope", "project", "--full",
                "--json", env=dbg)
        ls = ref_lines(r.stdout) or [{}]
        tl = [(l["tally"]["session"], l["tally"]["live"]) for l in ls
              if "tally" in l]
        check("scope: a live Claude <n>, then its project's History session",
              r.returncode == 0 and tl == [(n, True), ("xc-cl-old", False)]
              and ls[-1]["counts"]["project"] == 1 and
              open(trace).read().count("xray seam: open") == 2,
              (tl, r.stdout[-400:], r.stderr))
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
    for pad in (" " + ID, ID + " ", "\t" + ID):
        r = dch("--spawn", pad, "sh", "-c", "sleep 5")
        check("name rule: --spawn %r (padded) exits 2" % pad,
              r.returncode == 2 and "looks like a session_id" in r.stderr,
              (r.returncode, r.stderr))
        if r.returncode == 0:
            dch("-k", pad)
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
        # LOW-1 (QA 1.45.1): any case, since a case-insensitive file
        # system answers the id with the variant's socket
        for v in ("m0123456789abcdeF", UP):
            r = dch("-m", "xc-live", v)
            check("name rule: -m %s (a case variant) exits 2" % v,
                  r.returncode == 2 and "looks like a session_id" in r.stderr,
                  r.stderr)
            r = dch("--spawn", v, "sh", "-c", "sleep 5")
            check("name rule: --spawn %s (a case variant) exits 2" % v,
                  r.returncode == 2 and "looks like a session_id" in r.stderr,
                  r.stderr)
            if r.returncode == 0:
                dch("-k", v)
        r = dch("--spawn", "m0123456789abcdeg", "sh", "-c", "sleep 1")
        check("name rule: a non-hex letter is a name", r.returncode == 0,
              r.stderr)
    finally:
        dch("-k", "xc-live")
        dch("-k", "m0123456789abcdeg")

    # a session that already has such a name (an older dch let it) keeps
    # it, by its exact name only: its case variant (the id) is no name
    legacy = subprocess.Popen([DCH, "--master-of", sock(UP), "--", "sh",
                               "-c", "sleep 60"], env=ENV,
                              stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,
                              start_new_session=True)
    try:
        wait_for(lambda: os.path.exists(sock(UP)) and
                 dch("--status", UP).returncode == 0, 10)
        rc, o, err = xray(UP, "--json")
        check("legacy %s: --xray by its exact name answers" % UP,
              rc == 0 and o and o["session"] == UP, (rc, err))
        r = dch("-n", UP)
        check("legacy %s: -n still finds it (attaching needs a terminal)"
              % UP, r.returncode == 1 and "terminal" in r.stderr,
              (r.returncode, r.stderr))
        rc, o, err = xray(ID, "--json")
        check("legacy: --xray %s is not that session by name" % ID,
              rc == 1 and "no such session" in err, (rc, o, err))
        rc, o, err = xray(ID, "--clear", "--json")
        check("legacy: --xray %s --clear clears nothing of it" % ID,
              rc == 1 and "no such session" in err, (rc, o, err))
        r = dch("-n", ID)
        check("legacy: -n %s is refused, not attached to it" % ID,
              r.returncode == 2 and "looks like a session_id" in r.stderr,
              (r.returncode, r.stderr))
    finally:
        dch("-k", UP)
        try:
            legacy.wait(10)
        except subprocess.TimeoutExpired:
            legacy.kill()
            legacy.wait()


# ---- two Claudes under one shell: the master ignores the clear -----------

def test_clear_after_claude():
    """LOW-3 (QA 1.45.1): a Claude typed into a shell pauses the shell's
    ring on the master's next agent tick; once it exits, a --clear clears
    the ring at once, not only after the next tick (DCH_RECORD_MS apart)."""
    n = "xc-after"
    bindir = os.path.join(T, "bin")
    fdir = os.path.join(T, "fake")
    for d in (bindir, fdir):
        os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    env = dict(ENV, PATH=bindir + ":" + ENV.get("PATH", "/usr/bin:/bin"),
               FAKE_CLAUDE_DIR=fdir, DCH_RECORD_MS="10000")
    r = subprocess.run([DCH, "--spawn", n, "--size", "80x24", "sh"],
                       capture_output=True, text=True, timeout=30, env=env)
    check("after claude: shell spawned", r.returncode == 0, r.stderr)

    def status():
        try:
            s = X.connect(n)
            try:
                return X.read(s, 0, 1)[0].get("status", 0)
            finally:
                s.close()
        except (OSError, EOFError):
            return 0
    try:
        wait_for(lambda: "$" in screen(n) or "#" in screen(n), 5)
        dch("--send", n, "claude\r", env=env)
        paused = wait_for(lambda: status() & X.S_PAUSED, 25)
        t0 = time.time()
        log = os.path.join(fdir, n + ".log")
        dch("--send", n, "/exit\r", env=env)
        quit_ = wait_for(lambda: os.path.exists(log) and
                         '"exit"' in open(log).read(), 5)
        sdir = os.path.join(HOME, ".claude", "sessions")
        wait_for(lambda: not [f for f in os.listdir(sdir)
                              if f.endswith(".json")], 5)
        rc, c, err = xray(n, "--clear", "--json")
        secs = time.time() - t0
        check("after claude: --clear at once clears the shell's ring "
              "(%.1f s after the pause, the tick is 10 s)" % secs,
              paused and quit_ and rc == 0 and c and
              c["corpus"] == "shell" and c["ring_cleared"] is True and
              secs < 8, (paused, quit_, rc, c, err))
    finally:
        dch("-k", n)


def test_two_claudes():
    """claude_find answers 2: the master's probe says agent and ignores the
    clear, so the client must say ring_cleared false (review LOW-1)."""
    n = "xc-two"
    bindir = os.path.join(T, "bin2")
    fdir = os.path.join(T, "fake2")
    for d in (bindir, fdir):
        os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    env = dict(ENV, PATH=bindir + ":" + ENV.get("PATH", "/usr/bin:/bin"),
               FAKE_CLAUDE_DIR=fdir)
    sdir = os.path.join(HOME, ".claude", "sessions")
    had = set(os.listdir(sdir)) if os.path.isdir(sdir) else set()
    r = subprocess.run([DCH, "--spawn", n, "--size", "80x24", "sh", "-c",
                        "seq 1 300; exec 3<&0; claude <&3 & sleep 0.5; "
                        "claude"], capture_output=True, text=True,
                       timeout=30, env=env)
    check("two claudes: spawned", r.returncode == 0, r.stderr)
    try:
        if dch("--read", n).returncode == 3:
            print("  skip two claudes: dch-lite keeps no ring")
            return
        wait_for(lambda: len([f for f in os.listdir(sdir) if f not in had
                              and f.endswith(".json")]) >= 2
                 if os.path.isdir(sdir) else None, 10)
        _, r0 = X.read_all(n, 0)
        rc, c, err = xray(n, "--clear", "--json")
        check("two claudes: --clear says ring_cleared false",
              rc == 0 and c and c["ring_cleared"] is False, (rc, c, err))
        h, r1 = X.read_all(n, 0)
        # at least seq's rows that scrolled off the 24-row pane
        check("two claudes: the ring keeps its rows",
              len(r1) == len(r0) and len(r1) >= 276, (len(r0), len(r1)))
        # the read probes for the agent (QA 1.54.0 MED-2): paused, so the
        # client, which maps no one Claude here, answers no shell corpus
        _, o, _ = xray(n, "--json")
        check("two claudes: the ring reads paused, no shell answer",
              h["status"] & X.S_PAUSED and o and o["corpus"] == "none",
              (h, o))
    finally:
        dch("-k", n)


# ---- refs (#135, XR2a): the index's lifetime, --ref's budget and stop -----

RSID = "11111111-2222-4333-8444-5555555555a1"


def ref_lines(out):
    try:
        return [json.loads(l) for l in out.splitlines() if l.strip()]
    except ValueError:
        return None


def history(name, sid, cfg, cwd="/home/demo/proj"):
    sockdir = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    rec = {"name": name, "cwd": cwd, "end": "exited",
           "argv": ["claude"], "started": 1794299400, "updated": 1794300000,
           "ended": 1794300000, "sock_dir": sockdir,
           "agent": {"session_id": sid, "cwd": cwd,
                     "config_dir": cfg}}
    with open(os.path.join(hdir, name + ".json"), "w") as f:
        f.write(json.dumps(rec) + "\n")


def test_refs():
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, RSID + ".jsonl")
    canary = "Canary passage 7f3a"

    def put(lines):
        with open(tp, "w") as f:
            for o in lines:
                f.write(json.dumps(o) + "\n")

    def bash(id, cmd, out):
        return [{"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": id, "name": "Bash",
                     "input": {"command": cmd}}]}},
                {"type": "user", "toolUseResult": {"stdout": out},
                 "message": {"content": [{"type": "tool_result",
                                          "tool_use_id": id,
                                          "content": out}]}}]
    put([{"type": "user", "cwd": "/home/demo/proj",
          "timestamp": "2026-11-10T10:00:00Z",
          "message": {"content": "look at #831 and o/r#7"}}] +
        bash("b1", "gh issue view 831", "# #831 " + canary))
    history("xr-refs", RSID, cfg)
    trace = os.path.join(T, "trace-refs")
    dbg = dict(ENV, DCH_DEBUG=trace)

    # mutant 3 (K3): the index goes with its query
    open(trace, "w").close()
    rc, o, err = xray("xr-refs", "--json", env=dbg)
    tr = open(trace).read()
    m = re.search(r"xray refs: keys=(\d+) live=(\d+)", tr)
    check("refs: the index answers the transcript's keys",
          rc == 0 and o and [r["key"] for r in o["refs"]] ==
          ["issue:#831", "pull:o/r#7"] and o["refs"][0]["defined"], (o, err))
    check("refs: the index is freed with its query (K3: live=0)",
          m and int(m.group(1)) == 2 and m.group(2) == "0", tr[-400:])

    # nothing written by an index build and a full --ref (9.5)
    before = files(T)
    open(trace, "w").close()
    r = dch("--xray", "xr-refs", "--ref", "#831", "--scope", "session", "--full",
            "--json", env=dbg)
    ls = ref_lines(r.stdout) or []
    check("ref: a full --ref finds the definition",
          r.returncode == 0 and any(l.get("definition") for l in ls) and
          ls[-1].get("done") is True and ls[-1]["why"] is None,
          r.stdout[-600:] + r.stderr)
    tr = open(trace).read()
    check("ref: the trace holds no passage or transcript text",
          canary not in tr and "look at" not in tr, tr[-400:])
    new = sorted(set(files(T)) - set(before) - {trace})
    check("ref: nothing written by a --ref", not new, new)

    # NIT-7 (#150): a relative path that only outgrows the key against the
    # session's cwd is no ref, as main's check says for a short one (exit 2)
    history("xr-deep", RSID, cfg, "/" + "d" * 1000)
    r = dch("--xray", "xr-deep", "--ref", "src/" + "a" * 40 + ".c:3",
            "--scope", "session", "--json")
    check("ref: a relative path past the key cap against a deep cwd is "
          "not a ref (exit 2)",
          r.returncode == 2 and r.stderr == "dch: not a ref\n" and
          not r.stdout, (r.returncode, r.stderr, r.stdout[:200]))

    # a big transcript: light stops at its budget, full reads it all
    pad = "x" * 900
    with open(tp, "w") as f:
        f.write(json.dumps({"type": "user", "cwd": "/home/demo/proj",
                            "timestamp": "2026-11-10T10:00:00Z",
                            "message": {"content": "start #1"}}) + "\n")
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "PROJ-5 o/r#3 " + pad}]}}) + "\n"
        f.write(line * 40000)
        f.write(json.dumps({"type": "user", "message": {
            "content": "the end #424242"}}) + "\n")
    t0 = time.time()
    r = dch("--xray", "xr-refs", "--ref", "#424242", "--scope", "session",
            "--budget-ms", "50", "--json")
    ls = ref_lines(r.stdout) or [{}]
    d = ls[-1]
    check("ref: light past its budget stops, partial, why budget "
          "(%.2fs)" % (time.time() - t0),
          r.returncode == 0 and d.get("done") is True and d["partial"] and
          d["why"] == "budget" and d["bytes"] < os.path.getsize(tp) and
          not any("mention" in l for l in ls), r.stdout[-600:] + r.stderr)
    t0 = time.time()
    r = dch("--xray", "xr-refs", "--ref", "#424242", "--scope", "session",
            "--full", "--json")
    wall = time.time() - t0
    ls = ref_lines(r.stdout) or [{}]
    d = ls[-1]
    check("ref: full reads the whole transcript, one mention",
          r.returncode == 0 and d.get("done") is True and not d["partial"] and
          d["bytes"] == os.path.getsize(tp) and
          sum("mention" in l for l in ls) == 1, r.stdout[-600:] + r.stderr)
    prog = [l for l in ls if "progress" in l]
    check("ref: schema 1 on every line; a progress line per 250 ms "
          "(%d in %.2fs)" % (len(prog), wall),
          all(l.get("schema") == 1 for l in ls) and
          (prog or wall < 0.5), r.stdout[:600])
    os.unlink(tp)


# ---- 3.8's caps on a live shell (review #135 H1, M1) ---------------------

def test_shell_caps():
    """A paired block of 32000 D1 headings (256 KB kept for definitions),
    then 32 lines of 64 KiB of shas: the index and a full --ref each answer
    under 1 s and 16 MB."""
    n = "xc-caps"
    cfg = ENV["DCH_CONFIG"]
    with open(cfg, "w") as f:
        f.write("xray_ring = 4M\n")
    prog = ("printf '\\033]133;A\\007$ \\033]133;B\\007'; read cmd; "
            "printf '\\033]133;C\\007'; "
            "awk 'BEGIN{for(i=0;i<32000;i++) printf \"# #12 x%d\\n\", i}'; "
            "printf '\\033]133;D;0\\007'; "
            "awk 'BEGIN{for(r=0;r<32;r++){s=\"\";for(i=0;i<8000;i++) "
            "s=s sprintf(\"a%06x \",i); print s}}'; "
            "echo CAPS''DONE; sleep 120")
    try:
        r = dch("--spawn", n, "--size", "80x24", "sh", "-c", prog)
        check("caps: spawned", r.returncode == 0, r.stderr)
        wait_for(lambda: "$" in screen(n), 5)
        dch("--send", n, "cat notes.md\n")
        done = wait_for(lambda: "CAPSDONE" in screen(n), 60)
        check("caps: the output finished", done)
        t0 = time.time()
        rc, out, rss = maxrss([DCH, "--xray", n, "--json"], ENV)
        wall = time.time() - t0
        try:
            o = json.loads(out)
        except ValueError:
            o = None
        h12 = [x for x in (o or {}).get("refs", []) if x["key"] == "issue:#12"]
        check("caps: 32000 headings in one block: 8 definitions (%.2fs)" %
              wall, rc == 0 and h12 and h12[0]["definitions"] == 8,
              (rc, out[:300]))
        check("caps: the index over 2 MiB of 64 KiB sha lines and 32000 "
              "headings under 1 s (%.2fs)" % wall, SANITIZED or wall < 1.0)
        rss_ok("caps: --json at the caps", rss)
        t0 = time.time()
        rc, out, rss = maxrss([DCH, "--xray", n, "--ref", "a000001",
                               "--scope", "session", "--full", "--json"], ENV)
        wall = time.time() - t0
        ls = ref_lines(out) or [{}]
        check("caps: a full --ref over the same ring under 1 s (%.2fs)" %
              wall, rc == 0 and ls[-1].get("done") is True and
              (SANITIZED or wall < 1.0), out[-300:])
        rss_ok("caps: --ref at the caps", rss)
    finally:
        dch("-k", n)
        os.unlink(cfg)


def test_claude_caps():
    """Retained memory at the index's caps (3.8, l.717): 5000 chapters of
    120-scalar titles, 2000 keys each with 8 definitions of 120-scalar
    titles, and 100 keys past the cap."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, RSID + ".jsonl")
    pad = "\u00e9" * 100
    with open(tp, "w") as f:
        for i in range(5000):
            f.write(json.dumps({"type": "user", "cwd": "/home/demo/proj",
                                "message": {"content": "prompt %d %s" %
                                            (i, pad)}}) + "\n")
            if i >= 2100:
                continue
            out = "\n".join("# PROJ-%d %s %d" % (i + 1, pad, j)
                            for j in range(8 if i < 2000 else 1))
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "b%d" % i, "name": "Bash",
                 "input": {"command": "gh issue view %d" % i}}]}}) + "\n")
            f.write(json.dumps({"type": "user", "toolUseResult": {
                "stdout": out}, "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "b%d" % i,
                     "content": out}]}}) + "\n")
    history("xr-caps", RSID, cfg)
    try:
        t0 = time.time()
        rc, out, rss = maxrss([DCH, "--xray", "xr-caps", "--json"], ENV)
        wall = time.time() - t0
        try:
            o = json.loads(out)
        except ValueError:
            o = None
        refs = (o or {}).get("refs", [])
        check("caps: 2000 keys of 8 definitions each, 100 dropped (%.2fs)" %
              wall, rc == 0 and len(refs) == 2000 and
              all(x["definitions"] == 8 for x in refs) and
              o["dropped_keys"] == 100, (rc, len(refs), out[-300:]))
        rss_ok("caps: --json with 5000 chapters and 2000 x 8 definitions",
               rss)
    finally:
        os.unlink(tp)


# ---- v3.2.2 Q5: D2 parses the whole Claude result, <= 256 KiB ---------------

def test_d2_whole():
    """A gh --json object past the 3000-scalar clip defines through D2 in
    the index and in --ref alike; one over 256 KiB defines nothing."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, RSID + ".jsonl")

    def gh(id, n, title, body, late=False, pad=""):
        o = {"title": title, "body": body, "number": n} if late else \
            {"number": n, "title": title, "body": body}
        out = json.dumps(o) + pad
        return out, [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": id, "name": "Bash", "input": {
                    "command": "gh pr view %d --json number,title,body" %
                    n}}]}},
            {"type": "user", "toolUseResult": {"stdout": out},
             "message": {"content": [{"type": "tool_result",
                                      "tool_use_id": id, "content": out}]}}]
    small, a = gh("d1", 12, "Fix login", "Body line.\n" + "y" * 10240)
    big, b = gh("d2", 13, "Too big", "z" * (300 << 10))
    # the id past the clip: its anchor still counts once (Q8)
    late, c = gh("d3", 15, "Late id", "w" * 10240, late=True)
    # small JSON, the whole result over 256 KiB: no D2 off its clip
    _, e = gh("d4", 14, "Padded", "p", pad=" " * (260 << 10))
    with open(tp, "w") as f:
        for o in [{"type": "user", "cwd": "/home/demo/proj",
                   "timestamp": "2026-11-10T10:00:00Z",
                   "message": {"content": "see #12 and #13 and #14"}}] + \
                a + b + c + e:
            f.write(json.dumps(o) + "\n")
    history("xr-d2", RSID, cfg)
    try:
        rc, o, err = xray("xr-d2", "--json")
        refs = {r["key"]: r for r in (o or {}).get("refs", [])}
        r12, r13 = refs.get("issue:#12", {}), refs.get("issue:#13", {})
        check("d2: a %d KiB object defines in the index, one of %d KiB "
              "does not" % (len(small) >> 10, len(big) >> 10),
              rc == 0 and r12.get("defined") is True and
              r12.get("definitions") == 1 and
              r12.get("title") == "Fix login" and
              r13.get("defined") is False and r13.get("definitions") == 0,
              (r12, r13, err))
        r14, r15 = refs.get("issue:#14", {}), refs.get("issue:#15", {})
        check("d2: a small object padded past 256 KiB does not define; an "
              "id past the clip defines and counts once",
              r14.get("defined") is False and r15.get("defined") is True and
              r15.get("title") == "Late id" and r15.get("count") == 1,
              (r14, r15))

        def defs(ref):
            r = dch("--xray", "xr-d2", "--ref", ref, "--scope", "session",
                    "--full", "--json")
            ls = ref_lines(r.stdout) or [{}]
            return (r.returncode, ls[-1].get("done"),
                    [l["definition"] for l in ls if "definition" in l])
        rc12, done12, d12 = defs("#12")
        rc13, done13, d13 = defs("#13")
        rc15, done15, d15 = defs("#15")
        r = dch("--xray", "xr-d2", "--ref", "#15", "--scope", "session",
                "--full", "--json")
        m15 = [l["mention"] for l in ref_lines(r.stdout) or [] if
               "mention" in l]
        check("d2: --ref agrees on an id past the clip: one D2, its "
              "anchor's line", rc15 == 0 and done15 and len(d15) == 1 and
              d15[0]["title"] == "Late id" and len(m15) == 1 and
              '"number": 15' in m15[0]["line"], (d15, m15))
        check("d2: --ref agrees: #12 has its D2, #13 none",
              rc12 == 0 and done12 and len(d12) == 1 and
              d12[0]["rule"] == "D2" and d12[0]["title"] == "Fix login" and
              d12[0]["passage"].startswith("Body line.") and
              rc13 == 0 and done13 and d13 == [], (d12, d13))
        rc, out, rss = maxrss([DCH, "--xray", "xr-d2", "--ref", "#13",
                               "--scope", "session", "--full", "--json"],
                              ENV)
        rss_ok("d2: --ref over a 300 KiB result", rss)
    finally:
        os.unlink(tp)


# ---- v3.2.2 Q14: EPIPE is exit 0, silent, for every form ---------------------

def to_fd(argv, how):
    """argv with stdout a pipe whose reader has left ("epipe"), or a
    read-only fd ("ebadf", a write error that is not EPIPE)."""
    if how == "epipe":
        r, w = os.pipe()
        os.close(r)
    else:
        w = os.open(os.devnull, os.O_RDONLY)
    try:
        p = subprocess.run(argv, stdout=w, stderr=subprocess.PIPE, env=ENV,
                           timeout=30)
    finally:
        os.close(w)
    return p.returncode, p.stderr.decode()


def test_write_fail():
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, RSID + ".jsonl")
    with open(tp, "w") as f:
        f.write(json.dumps({"type": "user", "cwd": "/home/demo/proj",
                            "message": {"content": "see #831"}}) + "\n")
    history("xr-wf", RSID, cfg)
    n = "xc-wf"
    try:
        r = dch("--spawn", n, "--size", "80x24", "sh", "-c",
                "echo WF''UP; sleep 60")
        check("write fail: spawned", r.returncode == 0 and
              wait_for(lambda: "WFUP" in screen(n), 10), r.stderr)
        forms = [("--json", ["xr-wf", "--json"]),
                 ("--ref", ["xr-wf", "--ref", "#831", "--scope", "session",
                            "--full", "--json"]),
                 ("--clear", [n, "--clear", "--json"]),
                 ("--chapter", ["xr-wf", "--chapter", "f%x:0" %
                                os.stat(tp).st_ino, "--json"])]
        for what, a in forms:
            rc, err = to_fd([DCH, "--xray"] + a, "epipe")
            check("write fail: %s into a closed pipe: exit 0, silent" % what,
                  rc == 0 and err == "", (rc, err))
            rc, err = to_fd([DCH, "--xray"] + a, "ebadf")
            check("write fail: %s on a read-only stdout: exit 3, the "
                  "failure line" % what, rc == 3 and
                  err == "dch: X-ray failed on this session\n", (rc, err))
    finally:
        dch("-k", n)
        os.unlink(tp)


# ---- 3.5: --chapter <anchor> --json (#156, XR3b) --------------------------

def chapter(n, anchor, env=None):
    r = dch("--xray", n, "--chapter", anchor, "--json", env=env)
    try:
        return r.returncode, json.loads(r.stdout), r.stderr, r.stdout
    except ValueError:
        return r.returncode, None, r.stderr, r.stdout


def ent(role, text, tool=None, error=False):
    return {"role": role, "tool": tool, "text": text, "error": error}


def test_chapter():
    """A History Claude chapter's entries, the value rules, mutual
    exclusion, stale anchors (K14.2, K14.3), the 6000/3000 caps and the
    1 MiB cap (K14.4), an ended shell (no corpus), nothing written."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, RSID + ".jsonl")

    def put(lines):
        with open(tp, "w") as f:
            for o in lines:
                f.write(json.dumps(o) + "\n")

    def use(id, name, inp):
        return {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": id, "name": name, "input": inp}]}}

    def res(id, out, err=False):
        return {"type": "user", "toolUseResult": {"stdout": out},
                "message": {"content": [{"type": "tool_result",
                                         "tool_use_id": id, "is_error": err,
                                         "content": out}]}}
    put([{"type": "user", "cwd": "/home/demo/proj",
          "timestamp": "2026-11-10T10:00:00Z",
          "message": {"content": "first ask"}},
         {"type": "assistant", "message": {"content": [
             {"type": "text", "text": "one"}]}},
         use("b1", "Bash", {"command": "ls -la"}), res("b1", "a\nb"),
         {"type": "user", "timestamp": "2026-11-10T10:01:00Z",
          "message": {"content": "second \u0007ask"}},
         use("e1", "Edit", {"file_path": "/home/demo/proj/x.c",
                            "old_string": "a", "new_string": "b"}),
         res("e1", "boom", True),
         {"type": "assistant", "message": {"content": [
             {"type": "text", "text": "done"}]}}])
    history("xr-ch", RSID, cfg)
    rc, o, err = xray("xr-ch", "--json")
    ch = (o or {}).get("chapters", [])
    check("chapter: the index has two chapters", rc == 0 and len(ch) == 2,
          (rc, o, err))
    if len(ch) != 2:
        return
    before = files(T)
    rc, c, err, out = chapter("xr-ch", ch[1]["anchor"])
    check("chapter: History Claude: one object, its keys in order",
          rc == 0 and c and list(c) == ["schema", "session", "session_id",
                                        "anchor", "title", "at", "kind",
                                        "entries", "truncated"] and
          c["schema"] == 1 and c["session"] == "xr-ch" and
          c["session_id"] == RSID and c["anchor"] == ch[1]["anchor"] and
          c["title"] == ch[1]["title"] and c["at"] == ch[1]["at"] and
          c["kind"] == ch[1]["kind"] and c["truncated"] is False and
          out.endswith("}\n") and out.count("\n") == 1, (rc, out, err))
    check("chapter: its entries: the prompt, the call, its error, the text",
          c and c["entries"] == [
              ent("user", "second \u0007ask"),
              ent("tool", "/home/demo/proj/x.c", "Edit"),
              ent("output", "boom", "Edit", True),
              ent("assistant", "done")], c and c["entries"])
    check("chapter: text escaped like --ls-json (K14.5: BEL as \\u0007)",
          "second \\u0007ask" in out, out[:300])
    rc, c, _, _ = chapter("xr-ch", ch[0]["anchor"])
    check("chapter: the first chapter: a Bash call is its command",
          rc == 0 and c and c["entries"] == [
              ent("user", "first ask"), ent("assistant", "one"),
              ent("tool", "ls -la", "Bash"), ent("output", "a\nb", "Bash")],
          c and c["entries"])
    new = sorted(set(files(T)) - set(before))
    check("chapter: nothing written", not new, new)

    # stale anchors (K14.2, K14.3, mutant 10): no seek on the anchor's say
    ino, off = ch[1]["anchor"][1:].split(":")
    for bad in ("f%s:%d" % (ino, int(off) + 1), "f%s:%d" % (ino, int(off) - 1),
                "f%s:0%s" % (ino, off), "f%x:%s" % (int(ino, 16) + 1, off),
                "F%s:%s" % (ino, off), "s1", "x", "f" + "1" * 127):
        rc, c, err, out = chapter("xr-ch", bad)
        check("chapter: %r exits 1 chapter gone" % bad[:40], rc == 1 and
              err == "dch: chapter gone\n" and out == "", (rc, err, out))
    put([{"type": "user", "message": {"content": "first ask"}}])
    rc, c, err, _ = chapter("xr-ch", ch[1]["anchor"])
    check("chapter: a rewritten transcript (new inode): chapter gone",
          rc == 1 and err == "dch: chapter gone\n", (rc, err))

    # the value rules, mutual exclusion, --json (3.1)
    for bad in ("", "a b", "a/b", "é", "a" * 129, "-", "a\n"):
        r = dch("--xray", "xr-ch", "--chapter", bad, "--json")
        ok = bad == "-"
        check("chapter: %r: %s" % (bad[:20], "an anchor (gone)" if ok else
                                   "exit 2 bad anchor"),
              (r.returncode, r.stderr) == ((1, "dch: chapter gone\n") if ok
                                           else (2, "dch: bad anchor\n")),
              (r.returncode, r.stderr))
    r = dch("--xray", "xr-ch", "--chapter", "a" * 128, "--json")
    check("chapter: 128 bytes is an anchor", r.returncode == 1 and
          r.stderr == "dch: chapter gone\n", (r.returncode, r.stderr))
    for a, msg in ((["--ref", "#1"], "--chapter and --ref"),
                   (["--clear"], "--chapter and --clear"),
                   (["--follow"], "--chapter and --follow")):
        r = dch("--xray", "xr-ch", "--chapter", "s1", "--json", *a)
        check("chapter: with %s: exit 2" % a[0], r.returncode == 2 and
              r.stderr == "dch: bad usage: %s\n" % msg and not r.stdout,
              (r.returncode, r.stderr))
    for a, e in ((["--chapter", "s1"], "dch: bad usage: --json is required "
                  "(the --xray view comes later)\n"),
                 (["--chapter"], "dch: bad usage: --chapter needs a value\n"),
                 (["--chapter", "s1", "--chapter", "s2", "--json"],
                  "dch: bad usage: --chapter twice\n"),
                 (["--chapter", "s1", "--scope", "session", "--json"],
                  "dch: bad usage: --scope, --full and --budget-ms come with "
                  "--ref\n")):
        r = dch("--xray", "xr-ch", *a)
        check("chapter: %s: exit 2" % " ".join(a)[:50], r.returncode == 2 and
              r.stderr == e, (r.returncode, r.stderr))
    r = dch("--xray", "nope-ch", "--chapter", "s1", "--json")
    check("chapter: no such session (exit 1)", r.returncode == 1 and
          r.stderr == "dch: no such session\n", (r.returncode, r.stderr))
    r = dch("--xray", "bad/name", "--chapter", "a b", "--json")
    check("chapter: a bad session is told before a bad anchor",
          r.returncode == 2 and r.stderr == "dch: bad session\n", r.stderr)

    # the scalar caps (v2.1 3.1: 6000 and 3000) and the 1 MiB cap (K14.4)
    put([{"type": "user", "cwd": "/home/demo/proj",
          "message": {"content": "é" * 7000}},
         use("b1", "Bash", {"command": "é" * 7000}),
         res("b1", "é" * 4000),
         {"type": "assistant", "message": {"content": [
             {"type": "text", "text": "é" * 7000}]}}])
    rc, o, _ = xray("xr-ch", "--json")
    rc, c, err, _ = chapter("xr-ch", o["chapters"][0]["anchor"])
    check("chapter: text clipped in scalars: 6000 (prompt, call, reply), "
          "3000 (output)", rc == 0 and c and [len(e["text"]) for e in
                                              c["entries"]] ==
          [6000, 6000, 3000, 6000] and
          all(set(e["text"]) == {"é"} for e in c["entries"]),
          (rc, err, c and [len(e["text"]) for e in c["entries"]]))
    out = "\u0001" * 3000    # 18000 escaped bytes an entry
    put([{"type": "user", "cwd": "/home/demo/proj",
          "message": {"content": "big"}}] +
        [x for i in range(100) for x in (use("c%d" % i, "Bash",
                                             {"command": "c%d" % i}),
                                         res("c%d" % i, out))] +
        [{"type": "user", "message": {"content": "next"}}])
    rc, o, _ = xray("xr-ch", "--json")
    r = dch("--xray", "xr-ch", "--chapter", o["chapters"][0]["anchor"],
            "--json")
    try:
        c = json.loads(r.stdout)
    except ValueError:
        c = None
    n = len(r.stdout.encode())
    check("chapter: 100 x 18 KB escaped outputs: cut at 1 MiB, truncated "
          "(%d bytes, %d entries)" % (n, len((c or {}).get("entries", []))),
          r.returncode == 0 and c and c["truncated"] is True and
          n <= 1 << 20 and n > (1 << 20) - 20000 and
          c["entries"][-1]["role"] in ("tool", "output") and
          all(e["text"] == out for e in c["entries"]
              if e["role"] == "output"), (r.returncode, r.stderr, n))

    # an ended shell has no corpus (3.5)
    r = dch("--spawn", "xc-chend", "sh", "-c", "echo UP; sleep 60")
    wait_for(lambda: "UP" in screen("xc-chend"), 5)
    dch("-k", "xc-chend")
    wait_for(lambda: not os.path.exists(sock("xc-chend")), 5)
    rc, c, err, out = chapter("xc-chend.json", "s1")
    check("chapter: an ended shell: exit 1 no x-ray corpus", rc == 1 and
          err == "dch: no x-ray corpus\n" and out == "", (rc, err))
    os.unlink(tp)
    rc, c, err, out = chapter("xr-ch", "s1")
    check("chapter: a History Claude with no transcript: no x-ray corpus",
          rc == 1 and err == "dch: no x-ray corpus\n", (rc, err))


def test_chapter_shell():
    """A live shell chapter: one tool entry (the typed command), one output
    entry (C..D, WRAP-joined, error from D's exit); a stale seq and a
    cleared ring are chapter gone."""
    n = "xc-chs"
    prog = ("while :; do printf '\\033]133;A\\007$ \\033]133;B\\007'; "
            "read -r cmd || exit; printf '\\033]133;C\\007'; eval \"$cmd\"; "
            "printf '\\033]133;D;%d\\007' $?; done")
    try:
        r = dch("--spawn", n, "--size", "80x24", "sh", "-c", prog)
        check("chapter shell: spawned", r.returncode == 0, r.stderr)
        wait_for(lambda: "$" in screen(n), 5)
        dch("--send", n, "echo ran; printf '%0100d\\n' 7; false\n")
        time.sleep(0.3)
        dch("--send", n, "seq 1 40\n")
        wait_for(lambda: "40" in screen(n), 5)
        time.sleep(0.3)
        rc, o, err = xray(n, "--json")
        ch = (o or {}).get("chapters", [])
        check("chapter shell: two chapters", rc == 0 and len(ch) == 2,
              (rc, o, err))
        if len(ch) != 2:
            return
        rc, c, err, out = chapter(n, ch[0]["anchor"])
        check("chapter shell: the typed command, then its rows joined "
              "across the wrap, error from D 1", rc == 0 and c and
              c["session"] == n and c["session_id"] == o["session_id"] and
              c["anchor"] == ch[0]["anchor"] and c["kind"] == "command" and
              c["truncated"] is False and c["entries"] == [
                  ent("tool", "echo ran; printf '%0100d\\n' 7; false"),
                  ent("output", "ran\n" + "0" * 99 + "7", error=True)],
              (rc, out, err))
        rc, c, err, out = chapter(n, "s999999")
        check("chapter shell: an unknown seq is chapter gone", rc == 1 and
              err == "dch: chapter gone\n", (rc, err))
        dch("--xray", n, "--clear", "--json")
        rc, c, err, out = chapter(n, ch[0]["anchor"])
        check("chapter shell: after a clear the anchor is gone", rc == 1 and
              err == "dch: chapter gone\n", (rc, err))
    finally:
        dch("-k", n)


# ---- 3.4: SIGTERM and EPIPE cancel a --ref (FROZEN l.464, l.635) ---------

def test_cancel():
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, RSID + ".jsonl")
    line = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "PROJ-5 o/r#3 " + "x" * 900}]}}) + "\n"
    with open(tp, "w") as f:
        f.write(json.dumps({"type": "user", "cwd": "/home/demo/proj",
                            "message": {"content": "start #1"}}) + "\n")
        for _ in range(16):
            f.write(line * 10000)
    history("xr-cancel", RSID, cfg)
    argv = [DCH, "--xray", "xr-cancel", "--ref", "#424242", "--scope",
            "session", "--full", "--json"]
    try:
        t0 = time.time()
        r = subprocess.run(argv, capture_output=True, text=True, env=ENV,
                           timeout=60)
        full = time.time() - t0
        ls = ref_lines(r.stdout) or [{}]
        check("cancel: the whole scan of %d MB ends with done (%.2fs)" %
              (os.path.getsize(tp) >> 20, full),
              r.returncode == 0 and ls[-1].get("done") is True, r.stderr)

        p = subprocess.Popen(argv, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=ENV)
        first = p.stdout.readline()
        running = p.poll() is None
        t0 = time.time()
        p.send_signal(15)
        p.wait(10)
        dt = time.time() - t0
        rest = p.stdout.read()
        p.stdout.close()
        p.stderr.close()
        check("cancel: SIGTERM mid-scan ends it at once (%.3fs, the whole "
              "scan %.2fs), no done" % (dt, full),
              running and b'"first"' in first and p.returncode == -15 and
              dt < 0.5 and b'"done"' not in rest, (p.returncode, rest[-300:]))

        p = subprocess.Popen(argv, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=ENV)
        first = p.stdout.readline()
        running = p.poll() is None
        t0 = time.time()
        p.stdout.close()
        p.wait(10)
        dt = time.time() - t0
        err = p.stderr.read()
        p.stderr.close()
        # dch learns of the close at its next write, at most 250 ms later
        # (the progress cadence): 500 ms wall from close to exit (v3.2.2
        # Q15). xray_unit pins the progress write's own EPIPE check.
        check("cancel: a closed pipe (EPIPE) ends it within 500 ms "
              "(%.3fs, the whole scan %.2fs), exit 0, nothing on "
              "stderr" % (dt, full),
              running and b'"first"' in first and p.returncode == 0 and
              err == b"" and dt < 0.5, (p.returncode, err))
    finally:
        os.unlink(tp)


# ---- --follow (#136, XR2b; spec 3.3, 9.5's tail row) ---------------------

class Follow:
    """`dch --xray <n> --follow --json`, its lines read off as they come,
    each with its arrival time."""

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
                o = json.loads(raw)
            except ValueError:
                o = {"bad": raw.decode(errors="replace")}
            self.got.append((time.time(), o))

    def objs(self, frm=0):
        return [o for _, o in self.got[frm:]]

    def kinds(self, frm=0):
        return [next((k for k in o if k != "schema"), "?")
                for o in self.objs(frm)]

    def wait(self, pred, secs=10.0, frm=0):
        return wait_for(lambda: [o for o in self.objs(frm) if pred(o)], secs)

    def stop(self):
        if self.p.poll() is None:
            self.p.terminate()
        try:
            self.p.wait(5)
        except subprocess.TimeoutExpired:
            self.p.kill()
            self.p.wait(5)
        self.th.join(5)
        return self.p.returncode


def is_ref(key, **kv):
    return lambda o: "ref" in o and o["ref"]["key"] == key and \
        all(o["ref"].get(k) == v for k, v in kv.items())


def is_reset(why):
    return lambda o: o.get("reset") == {"why": why}


def trace_from(path, off):
    with open(path) as f:
        f.seek(off)
        return f.read()


def test_follow_claude():
    n = "xc-fl"
    bindir = os.path.join(T, "bin")
    fdir = os.path.join(T, "fake")
    proj = os.path.join(T, "fproj")
    for d in (bindir, fdir, proj):
        os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    env = dict(ENV, PATH=bindir + ":" + ENV.get("PATH", "/usr/bin:/bin"),
               FAKE_CLAUDE_DIR=fdir)
    sdir = os.path.join(HOME, ".claude", "sessions")
    had = set(os.listdir(sdir)) if os.path.isdir(sdir) else set()
    r = subprocess.run([DCH, "--spawn", n, "--size", "80x24", "--cwd", proj,
                        "claude"], capture_output=True, text=True, timeout=30,
                       env=env)
    check("follow: claude spawned", r.returncode == 0, r.stderr)
    trace = os.path.join(T, "trace-follow")
    open(trace, "w").close()
    dbg = dict(ENV, DCH_DEBUG=trace)
    fl = None
    try:
        rec = wait_for(lambda: [f for f in os.listdir(sdir)
                                if f.endswith(".json") and f not in had]
                       if os.path.isdir(sdir) else None, 10)
        rp = os.path.join(sdir, rec[0])
        sid = json.load(open(rp))["sessionId"]
        pdir = os.path.join(HOME, ".claude", "projects", key(proj))
        os.makedirs(pdir, exist_ok=True)
        tp = os.path.join(pdir, sid + ".jsonl")

        def put(objs, mode="a"):
            with open(tp, mode) as f:
                f.write("".join(json.dumps(o) + "\n" for o in objs))

        put([{"type": "user", "cwd": proj, "message": {
                "content": "first ask #11"}},
             {"type": "assistant", "message": {"content": [
                 {"type": "text", "text": "ok"}]}}], "w")
        wait_for(lambda: (xray(n, "--json")[1] or {}).get("corpus") ==
                 "claude", 10)
        _, one, _ = xray(n, "--json")
        fl = Follow(n, dbg)
        snap = fl.wait(lambda o: "snapshot" in o)
        check("follow: the first line is a snapshot, the --json object",
              snap and fl.kinds()[0] == "snapshot" and
              snap[0]["snapshot"] == one, (fl.objs(), one))

        # mutant 34: idle, no timer and no wake (9.5: a counting seam)
        time.sleep(0.2)
        off = os.path.getsize(trace)
        time.sleep(1.0)
        idle = trace_from(trace, off)
        check("follow: idle, no deadline armed and no wake (no timer)",
              "xray follow: deadline" not in idle and
              "xray follow: woke" not in idle and fl.p.poll() is None, idle)

        # a tool_use, then its tool_result in a later read (QA A2)
        k0 = len(fl.got)
        put([{"type": "user", "message": {"content": "second ask #12"}},
             {"type": "assistant", "message": {"content": [
                 {"type": "tool_use", "id": "t1", "name": "Bash",
                  "input": {"command": "gh issue view 12"}}]}}])
        a = fl.wait(is_ref("issue:#12"), frm=k0)
        ch = fl.wait(lambda o: "chapter" in o, frm=k0)
        check("follow: appended lines give a chapter and a ref delta",
              a and not a[0]["ref"]["defined"] and ch and
              ch[0]["chapter"]["title"] == "second ask #12",
              fl.objs(k0))
        time.sleep(0.4)
        k1 = len(fl.got)
        put([{"type": "user", "toolUseResult": {"stdout": "# #12 Twelve"},
              "message": {"content": [{"type": "tool_result",
                                       "tool_use_id": "t1",
                                       "content": "# #12 Twelve"}]}}])
        d = fl.wait(is_ref("issue:#12", defined=True), frm=k1)
        check("follow: a tool_result in the read after its tool_use pairs "
              "(the tool_use ring survives)", d and
              d[0]["ref"]["title"] == "Twelve", fl.objs(k1))
        time.sleep(0.4)
        _, now, _ = xray(n, "--json")
        state = {"chapters": {}, "refs": {}}
        for o in fl.objs():
            if "snapshot" in o:
                state = {"chapters": {c["anchor"]: c for c in
                                      o["snapshot"]["chapters"]},
                         "refs": {x["key"]: x for x in
                                  o["snapshot"]["refs"]}}
            elif "chapter" in o:
                state["chapters"][o["chapter"]["anchor"]] = o["chapter"]
            elif "ref" in o:
                state["refs"][o["ref"]["key"]] = o["ref"]
        strip = lambda c: {k: v for k, v in c.items() if k != "i"}
        check("follow: the snapshot plus its deltas is the --json of now",
              now and sorted(state["refs"].values(), key=str) ==
              sorted(now["refs"], key=str) and
              [strip(c) for c in sorted(state["chapters"].values(),
                                        key=lambda c: c["offset"])] ==
              [strip(c) for c in now["chapters"]], (state, now))

        # coalescing: lines 30 ms apart, at most one batch per 250 ms (the
        # trace's batch times)
        k2 = len(fl.got)
        off = os.path.getsize(trace)
        for i in range(12):
            put([{"type": "assistant", "message": {"content": [
                {"type": "text", "text": "see #%d" % (200 + i)}]}}])
            time.sleep(0.03)
        fl.wait(is_ref("issue:#211"), frm=k2)
        ts = [float(l.split()[0]) for l in trace_from(trace, off).splitlines()
              if "xray follow: batch" in l]
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        nref = sum("ref" in o for o in fl.objs(k2))
        check("follow: deltas coalesce, batches >= 250 ms apart (%d refs in "
              "%d batches, gaps %s)" % (nref, len(ts),
                                        ["%.3f" % g for g in gaps]),
              # the count depends on how fast the lines land (a loaded
              # runner spreads them): the gap is the rule
              nref == 12 and 1 < len(ts) <= nref and
              all(g >= 0.245 for g in gaps), ts)

        # shrink: cut back to the first line
        time.sleep(0.4)
        k3 = len(fl.got)
        first = len(json.dumps({"type": "user", "cwd": proj, "message": {
            "content": "first ask #11"}})) + 1
        os.truncate(tp, first)
        s = fl.wait(lambda o: "snapshot" in o, frm=k3)
        check("follow: a shrunk transcript sends reset shrunk, then a "
              "snapshot", s and fl.kinds(k3)[:2] == ["reset", "snapshot"] and
              fl.objs(k3)[0] == {"schema": 1, "reset": {"why": "shrunk"}} and
              [c["title"] for c in s[0]["snapshot"]["chapters"]] ==
              ["first ask #11"], fl.objs(k3))

        # mutant 37: rotated (a /clear: a new id, a new file)
        time.sleep(0.4)
        k4 = len(fl.got)
        sid2 = "22222222-3333-4444-8555-66666666666b"
        r0 = json.load(open(rp))
        r0["sessionId"] = sid2
        with open(rp + ".tmp", "w") as f:
            json.dump(r0, f)
        os.rename(rp + ".tmp", rp)
        with open(os.path.join(pdir, sid2 + ".jsonl"), "w") as f:
            f.write(json.dumps({"type": "user", "cwd": proj, "message": {
                "content": "rotated ask #13"}}) + "\n")
        s = fl.wait(lambda o: "snapshot" in o, frm=k4)
        check("follow: a rotated transcript sends reset rotated, then the "
              "new session_id's snapshot", s and
              fl.objs(k4)[0] == {"schema": 1, "reset": {"why": "rotated"}} and
              s[0]["snapshot"]["session_id"] == sid2 and
              [c["title"] for c in s[0]["snapshot"]["chapters"]] ==
              ["rotated ask #13"], fl.objs(k4))
        _, one, _ = xray(n, "--json")
        check("follow: the rotated snapshot is the --json object",
              s and s[0]["snapshot"] == one, (s, one))

        # the same id at a new inode (the file replaced): rotated too
        time.sleep(0.4)
        k4 = len(fl.got)
        tp2 = os.path.join(pdir, sid2 + ".jsonl")
        with open(tp2 + ".new", "w") as f:
            f.write(json.dumps({"type": "user", "cwd": proj, "message": {
                "content": "replaced ask #15"}}) + "\n")
        os.rename(tp2 + ".new", tp2)
        s = fl.wait(lambda o: "snapshot" in o, frm=k4)
        check("follow: a transcript replaced under the same id (a new "
              "inode) sends reset rotated, then its snapshot", s and
              fl.objs(k4)[0] == {"schema": 1, "reset": {"why": "rotated"}} and
              [c["title"] for c in s[0]["snapshot"]["chapters"]] ==
              ["replaced ask #15"], fl.objs(k4))

        # QA 1.52.0 LOW-6: a /clear whose new transcript appears before the
        # record names it, the record then rewritten in place (no dir event)
        time.sleep(0.4)
        k4 = len(fl.got)
        sid3 = "33333333-4444-4555-8666-77777777777c"
        with open(os.path.join(pdir, sid3 + ".jsonl"), "w") as f:
            f.write(json.dumps({"type": "user", "cwd": proj, "message": {
                "content": "cleared ask #16"}}) + "\n")
        time.sleep(0.5)
        early = fl.objs(k4)
        r0 = json.load(open(rp))
        r0["sessionId"] = sid3
        with open(rp, "w") as f:
            f.write(json.dumps(r0))
        s = fl.wait(lambda o: "snapshot" in o, frm=k4)
        check("follow: a new transcript before its record, the record "
              "rewritten in place, sends reset rotated, then its snapshot",
              not early and s and
              fl.objs(k4)[0] == {"schema": 1, "reset": {"why": "rotated"}} and
              s[0]["snapshot"]["session_id"] == sid3 and
              [c["title"] for c in s[0]["snapshot"]["chapters"]] ==
              ["cleared ask #16"], (early, fl.objs(k4)))

        # the reader leaves: exit 0 at once, idle
        rd, wr = os.pipe()
        p = subprocess.Popen([DCH, "--xray", n, "--follow", "--json"],
                             stdout=wr, stderr=subprocess.PIPE, env=ENV)
        os.close(wr)
        line = b""
        while not line.endswith(b"\n"):
            b = os.read(rd, 65536)
            if not b:
                break
            line += b
        t0 = time.time()
        os.close(rd)
        try:
            rc = p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
            rc = p.wait()
        check("follow: the reader's close ends an idle follower, exit 0 "
              "(%.2fs)" % (time.time() - t0), rc == 0 and
              time.time() - t0 < 1.0 and not p.stderr.read(), rc)

        # nothing written by a follow (9.5)
        before = files(T)
        k5 = len(fl.got)
        put([{"type": "user", "message": {"content": "late #14"}}])
        fl.wait(is_ref("issue:#14"), frm=k5)
        tr = open(trace).read()
        new = sorted(set(files(T)) - set(before))
        check("follow: nothing written, no passage in the trace",
              not [p for p in new if p != trace] and
              "late #14" not in tr and "rotated ask" not in tr, new)

        # the session ends: exit 0
        dch("-k", n)
        try:
            rc = fl.p.wait(5)
        except subprocess.TimeoutExpired:
            rc = None
        check("follow: the session's end ends it, exit 0", rc == 0, rc)
        tr = open(trace).read()
        m = re.search(r"xray follow: end rc=(-?\d+) woke=(\d+) "
                      r"deadlines=(\d+)", tr)
        check("follow: the end trace counts its wakes and deadlines",
              m and m.group(1) == "0", tr[-400:])
    finally:
        if fl:
            fl.stop()
        dch("-k", n)


def test_follow_late():
    """QA 1.52.0: a project dir made after the follow started is watched
    (LOW-6); time blocked on a stalled reader is no build time (LOW-8)."""
    n = "xc-fl2"
    bindir = os.path.join(T, "bin")
    fdir = os.path.join(T, "fake")
    proj = os.path.join(T, "fproj-late")
    for d in (bindir, fdir, proj):
        os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    env = dict(ENV, PATH=bindir + ":" + ENV.get("PATH", "/usr/bin:/bin"),
               FAKE_CLAUDE_DIR=fdir)
    sdir = os.path.join(HOME, ".claude", "sessions")
    had = set(os.listdir(sdir)) if os.path.isdir(sdir) else set()
    r = subprocess.run([DCH, "--spawn", n, "--size", "80x24", "--cwd", proj,
                        "claude"], capture_output=True, text=True, timeout=30,
                       env=env)
    check("follow late: claude spawned", r.returncode == 0, r.stderr)
    trace = os.path.join(T, "trace-follow-late")
    open(trace, "w").close()
    dbg = dict(ENV, DCH_DEBUG=trace)
    fl = p = None
    try:
        rec = wait_for(lambda: [f for f in os.listdir(sdir)
                                if f.endswith(".json") and f not in had]
                       if os.path.isdir(sdir) else None, 10)
        sid = json.load(open(os.path.join(sdir, rec[0])))["sessionId"]
        # the dir dch watches: the key of Claude's cwd, as its record has it
        pdir = os.path.join(HOME, ".claude", "projects",
                            key(os.path.realpath(proj)))
        tp = os.path.join(pdir, sid + ".jsonl")

        def put(objs, mode="a"):
            with open(tp, mode) as f:
                f.write("".join(json.dumps(o) + "\n" for o in objs))

        # LOW-6: no project dir yet; it is made, its first file 0.5 s later
        fl = Follow(n, dbg)
        snap = fl.wait(lambda o: "snapshot" in o)
        check("follow late: no transcript, no project dir: a corpus none "
              "snapshot", snap and snap[0]["snapshot"]["corpus"] == "none" and
              not os.path.isdir(pdir), fl.objs())
        k0 = len(fl.got)
        os.makedirs(pdir)
        time.sleep(0.5)
        put([{"type": "user", "cwd": proj, "message": {
            "content": "late dir ask #21"}}], "w")
        s = fl.wait(lambda o: "snapshot" in o, frm=k0)
        check("follow late: a file in a project dir made after the follow "
              "started sends reset rotated, then its snapshot", s and
              fl.objs(k0)[0] == {"schema": 1, "reset": {"why": "rotated"}} and
              [c["title"] for c in s[0]["snapshot"]["chapters"]] ==
              ["late dir ask #21"], fl.objs(k0))
        fl.stop()

        # LOW-8: a batch stalled 3 s on a full pipe, then a small one: its
        # deadline is the 250 ms spacing, not twice the stall
        p = subprocess.Popen([DCH, "--xray", n, "--follow", "--json"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=dbg)
        first = p.stdout.readline()
        # 1200 ref lines (120 a line: under the 128 spans of a line)
        put([{"type": "assistant", "message": {"content": [
            {"type": "text", "text": " ".join("#%d" % (1000 + 120 * j + i)
                                              for i in range(120))}]}}
             for j in range(10)])
        time.sleep(3.0)
        got = []
        th = threading.Thread(target=lambda: got.extend(p.stdout),
                              daemon=True)
        th.start()
        wait_for(lambda: '"issue:#2199"'.encode() in b"".join(got), 15)
        wait_for(lambda: "xray follow: batch" in open(trace).read(), 5)
        off = os.path.getsize(trace)
        put([{"type": "assistant", "message": {"content": [
            {"type": "text", "text": "see #99"}]}}])
        wait_for(lambda: b'"issue:#99"' in b"".join(got), 15)
        dl = [int(m) for m in re.findall(r"xray follow: deadline (\d+) ms",
                                         trace_from(trace, off))]
        check("follow late: after a 3 s stall on the reader, the next "
              "deadline is the batch spacing (%s ms, < 1500)" % dl,
              b'"snapshot"' in first and len(got) > 1000 and
              b'"issue:#99"' in b"".join(got) and
              all(d < 1500 for d in dl), (len(got), dl))
    finally:
        if fl:
            fl.stop()
        if p and p.poll() is None:
            p.terminate()
            p.wait(5)
        dch("-k", n)


def test_follow_stale():
    """QA 1.52.0 LOW-7 (Prosper's ruling A): a shell follower that leaves a
    wake unanswered 3 s loses its slot to a fifth; when it reads again and no
    slot is free it exits 1 with `dch: busy` and writes nothing more."""
    import signal
    n = "xc-fst"
    with open(ENV["DCH_CONFIG"], "w") as f:
        f.write("xray_ring = 64K\n")
    prog = ("while :; do printf '\\033]133;A\\007$ \\033]133;B\\007'; "
            "read -r cmd || exit; printf '\\033]133;C\\007'; eval \"$cmd\"; "
            "printf '\\033]133;D;0\\007'; done")
    cwd = os.path.join(T, "fst proj")
    os.makedirs(cwd, exist_ok=True)
    fls, stopped = [], None
    try:
        r = dch("--spawn", n, "--size", "80x24", "--cwd", cwd, "sh", "-c",
                prog)
        check("follow stale: spawned", r.returncode == 0, r.stderr)
        _, o, _ = xray(n, "--json")
        if (o or {}).get("reason") == "lite":
            # dch-lite keeps no ring: no wake, so nothing to leave unanswered
            check("follow stale: lite, corpus none (no ring to subscribe)",
                  o["corpus"] == "none", o)
            return
        wait_for(lambda: "$" in screen(n), 5)
        dch("--send", n, "echo 'see o/r#51'\n")
        wait_for(lambda: "see o/r#51" in screen(n), 5)
        for _ in range(4):
            fls.append(Follow(n))
        ok = all(f.wait(lambda o: "snapshot" in o) for f in fls)
        time.sleep(0.5)
        stale = fls[3]
        k = len(stale.got)
        os.kill(stale.p.pid, signal.SIGSTOP)	# our own child: a paused reader
        stopped = stale
        dch("--send", n, "echo 'more o/r#52'\n")	# a wake it leaves unanswered
        time.sleep(3.5)
        fifth = Follow(n)
        fls.append(fifth)
        took = fifth.wait(lambda o: "snapshot" in o)
        os.kill(stale.p.pid, signal.SIGCONT)
        stopped = None
        try:
            rc = stale.p.wait(10)
        except subprocess.TimeoutExpired:
            rc = None
        stale.th.join(5)
        err = stale.p.stderr.read() if rc is not None else None
        check("follow stale: the stalled follower, its slot taken by a "
              "fifth, exits 1 with dch: busy and no further line",
              ok and took and rc == 1 and err == b"dch: busy\n" and
              len(stale.got) == k, (ok, bool(took), rc, err,
                                    stale.objs(k)))
        fls.remove(stale)
    finally:
        if stopped:
            os.kill(stopped.p.pid, signal.SIGCONT)
        for f in fls:
            f.stop()
        dch("-k", n)


def test_follow_agent(off=False):
    """A shell's follower across an agent's start and exit, on the default
    60 s record tick: while a follower is subscribed the master probes for
    an agent every second and wakes it on a change, ring or no ring. The
    start: reset rotated, then a snapshot of corpus claude. The exit, on an
    idle shell (no row pushed): reset rotated, then a snapshot of corpus
    shell (none with xray_ring 0). Each within 10 s: the probe is 1 s,
    the bound leaves room for a loaded runner."""
    n = "xc-fa" + ("o" if off else "")
    tag = "follow agent%s: " % (" (xray_ring 0)" if off else "")
    base = "none" if off else "shell"
    cfg = ENV["DCH_CONFIG"]
    bindir = os.path.join(T, "bin-fa")
    fdir = os.path.join(T, "fake-fa")
    proj = os.path.join(T, "faproj")
    for d in (bindir, fdir, proj):
        os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    env = dict(ENV, PATH=bindir + ":" + ENV.get("PATH", "/usr/bin:/bin"),
               FAKE_CLAUDE_DIR=fdir)
    env.pop("DCH_RECORD_MS", None)
    if off:
        with open(cfg, "w") as f:
            f.write("xray_ring = 0\n")
    sdir = os.path.join(HOME, ".claude", "sessions")
    had = set(os.listdir(sdir)) if os.path.isdir(sdir) else set()
    r = subprocess.run([DCH, "--spawn", n, "--size", "80x24", "--cwd", proj,
                        "sh"], capture_output=True, text=True, timeout=30,
                       env=env)
    check(tag + "shell spawned", r.returncode == 0, r.stderr)
    fl = None
    try:
        wait_for(lambda: "$" in screen(n) or "#" in screen(n), 5)
        fl = Follow(n)
        s0 = fl.wait(lambda o: "snapshot" in o)
        check(tag + "the first snapshot is corpus " + base,
              s0 and s0[0]["snapshot"]["corpus"] == base, fl.objs())

        k0 = len(fl.got)
        t0 = time.time()
        dch("--send", n, "claude\r", env=env)
        rec = wait_for(lambda: [f for f in os.listdir(sdir)
                                if f.endswith(".json") and f not in had]
                       if os.path.isdir(sdir) else None, 10)
        if rec:
            sid = json.load(open(os.path.join(sdir, rec[0])))["sessionId"]
            pdir = os.path.join(HOME, ".claude", "projects", key(proj))
            os.makedirs(pdir, exist_ok=True)
            with open(os.path.join(pdir, sid + ".jsonl"), "w") as f:
                f.write(json.dumps({"type": "user", "cwd": proj, "message": {
                    "content": "ask #11"}}) + "\n")

        def rotated_then(k0):
            ob = fl.objs(k0)
            i = next((j for j, o in enumerate(ob)
                      if is_reset("rotated")(o)), None)
            return ob[i + 1] if i is not None and i + 1 < len(ob) else None
        wait_for(lambda: rotated_then(k0), 15)
        secs = time.time() - t0
        nxt = rotated_then(k0)
        check(tag + "the agent's start sends reset rotated, then a "
              "snapshot of corpus claude (%.1f s)" % secs,
              nxt is not None and "snapshot" in nxt and
              nxt["snapshot"]["corpus"] == "claude" and secs < 10,
              fl.objs(k0))
        k1 = len(fl.got)
        t1 = time.time()
        dch("--send", n, "/exit\r", env=env)
        wait_for(lambda: rotated_then(k1), 15)
        secs = time.time() - t1
        nxt = rotated_then(k1)
        check(tag + "the agent's exit on an idle shell sends reset "
              "rotated, then a snapshot of corpus %s (%.1f s)" % (base, secs),
              nxt is not None and "snapshot" in nxt and
              nxt["snapshot"]["corpus"] == base and secs < 10,
              fl.objs(k1))
    finally:
        dch("-k", n)
        if off and os.path.exists(cfg):
            os.unlink(cfg)
        if fl:
            wait_for(lambda: fl.p.poll() is not None, 5)
            rc = fl.stop()
            check(tag + "the session's end exits 0", rc == 0,
                  (rc, fl.p.stderr.read() if fl.p.stderr else ""))

def test_screen_agent():
    """QA 1.54.0 MED-2: an agent's TUI, on the primary screen, never enters
    a shell answer, on the default 60 s record tick. The master probes for
    an agent before it answers a ring or a screen read. A follower: no ref
    from the TUI, then off corpus shell. A one-shot --ref with no follower:
    no count, both for a Claude the client maps itself and for one it
    cannot (a sessionId that is no UUID), where the master's pause alone
    keeps the TUI out."""
    bindir = os.path.join(T, "bin-sa")
    fdir = os.path.join(T, "fake-sa")
    for d in (bindir, fdir):
        os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    env = dict(ENV, PATH=bindir + ":" + ENV.get("PATH", "/usr/bin:/bin"),
               FAKE_CLAUDE_DIR=fdir)
    env.pop("DCH_RECORD_MS", None)

    def start(n):
        with open(os.path.join(fdir, n + ".ctl"), "w") as f:
            json.dump({"above": ["see q/z#909 in the agent TUI"]}, f)
        r = dch("--spawn", n, "--size", "80x24", "sh", env=env)
        check("screen agent: %s spawned" % n, r.returncode == 0, r.stderr)
        wait_for(lambda: "$" in screen(n) or "#" in screen(n), 5)

    fl = None
    names = ["xc-saf", "xc-sao", "xc-sau"]
    try:
        n = names[0]
        start(n)
        if dch("--read", n).returncode == 3:
            print("  skip screen agent: dch-lite reads no screen")
            return
        fl = Follow(n, env=env)
        s0 = fl.wait(lambda o: "snapshot" in o)
        check("screen agent: the follower starts on corpus shell",
              s0 and s0[0]["snapshot"]["corpus"] == "shell", fl.objs())
        k0 = len(fl.got)
        dch("--send", n, "claude\r", env=env)
        up = wait_for(lambda: "q/z#909" in screen(n), 10)
        # no transcript here: the agent's corpus is claude or none
        snap = fl.wait(lambda o: "snapshot" in o and
                       o["snapshot"]["corpus"] != "shell", 10, frm=k0)
        time.sleep(0.5)
        leak = [o for o in fl.objs(k0) if "q/z#909" in json.dumps(o)]
        check("screen agent: the follower emits nothing from the agent's TUI",
              up and not leak, (bool(up), leak))
        check("screen agent: ... and leaves corpus shell", snap,
              fl.objs(k0))

        for n, cmd, what in ((names[1], "claude\r", "a Claude the client "
                              "maps"),
                             (names[2], "claude --resume not-a-uuid\r",
                              "a Claude with no UUID sessionId")):
            start(n)
            dch("--send", n, cmd, env=env)
            up = wait_for(lambda: "q/z#909" in screen(n), 10)
            r = dch("--xray", n, "--ref", "q/z#909", "--scope", "session",
                    "--full", "--json", env=env)
            ls = ref_lines(r.stdout) or []
            leak = [l for l in ls if "mention" in l or
                    l.get("tally", {}).get("count")]
            check("screen agent: a one-shot --ref, %s: no mention, no count "
                  "(no corpus, or count 0)" % what, up and not leak and
                  (r.returncode == 0 or "no x-ray corpus" in r.stderr),
                  (bool(up), r.returncode, r.stdout[-600:], r.stderr))
    finally:
        for n in names:
            dch("-k", n)
        if fl:
            fl.stop()


def test_follow_history():
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, RSID + ".jsonl")
    with open(tp, "w") as f:
        f.write(json.dumps({"type": "user", "cwd": "/home/demo/proj",
                            "message": {"content": "ended #31"}}) + "\n")
    history("xr-fh", RSID, cfg)
    try:
        _, one, _ = xray("xr-fh", "--json")
        r = dch("--xray", "xr-fh", "--follow", "--json")
        ls = ref_lines(r.stdout) or []
        check("follow: a History session writes one snapshot, exit 0",
              r.returncode == 0 and len(ls) == 1 and
              ls[0] == {"schema": 1, "snapshot": one} and one["refs"],
              (r.returncode, r.stdout[-400:], r.stderr))
        r = dch("--xray", "xr-fh", "--follow", "--clear", "--json")
        check("follow: --follow with --clear is usage (exit 2)",
              r.returncode == 2 and "--follow and --clear" in r.stderr,
              r.stderr)
        r = dch("--xray", "xr-fh", "--follow", "--ref", "#31", "--json")
        check("follow: --follow with --ref is usage (exit 2)",
              r.returncode == 2 and "--follow and --ref" in r.stderr,
              r.stderr)
        r = dch("--xray", "xr-fh", "--follow", "--follow", "--json")
        check("follow: --follow twice is usage (exit 2)",
              r.returncode == 2 and "--follow twice" in r.stderr, r.stderr)
        r = dch("--xray", "nosuch", "--follow", "--json")
        check("follow: no such session, exit 1, nothing on stdout",
              r.returncode == 1 and not r.stdout and
              "no such session" in r.stderr, r.stderr)
    finally:
        os.unlink(tp)


def test_follow_cap():
    """Past the per-file range (3.3 item 1): the capped snapshot, then
    exactly one reset full and the whole file's. Sparse: the holes are
    over-long lines, cheap to read and to skip."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, RSID + ".jsonl")
    u = lambda t: (json.dumps({"type": "user", "cwd": "/home/demo/proj",
                               "message": {"content": t}}) + "\n").encode()
    with open(tp, "wb") as f:
        f.write(u("head #61"))
        f.seek(30 << 20)
        f.write(b"\n" + u("middle #62"))
        f.seek(300 << 20)
        f.write(b"\n" + u("tail #63"))
    n = "xc-fc"
    # a live Claude for the History record's transcript: the fake's id
    # pointed at RSID (a follow of a History session ends at once)
    bindir = os.path.join(T, "bin")
    fdir = os.path.join(T, "fake")
    for d in (bindir, fdir):
        os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    env = dict(ENV, PATH=bindir + ":" + ENV.get("PATH", "/usr/bin:/bin"),
               FAKE_CLAUDE_DIR=fdir)
    sdir = os.path.join(HOME, ".claude", "sessions")
    had = set(os.listdir(sdir)) if os.path.isdir(sdir) else set()
    r = subprocess.run([DCH, "--spawn", n, "--size", "80x24", "--cwd", T,
                        "claude"], capture_output=True, text=True, timeout=30,
                       env=env)
    fl = None
    try:
        rec = wait_for(lambda: [f for f in os.listdir(sdir)
                                if f.endswith(".json") and f not in had]
                       if os.path.isdir(sdir) else None, 10)
        rp = os.path.join(sdir, rec[0])
        r0 = json.load(open(rp))
        r0["sessionId"] = RSID
        with open(rp + ".tmp", "w") as f:
            json.dump(r0, f)
        os.rename(rp + ".tmp", rp)
        fl = Follow(n)
        s = fl.wait(lambda o: "snapshot" in o and len(
            [x for x in fl.objs() if "snapshot" in x]) >= 2, 30)
        time.sleep(1.0)
        ks = fl.kinds()
        keys = [[x["key"] for x in o["snapshot"]["refs"]]
                for o in fl.objs() if "snapshot" in o]
        check("follow: past the cap, the capped snapshot (no middle #62), "
              "then exactly one reset full and the whole file's",
              ks == ["snapshot", "reset", "snapshot"] and
              fl.objs()[1] == {"schema": 1, "reset": {"why": "full"}} and
              len(keys) == 2 and "issue:#62" not in keys[0] and
              "issue:#62" in keys[1] and
              not fl.objs()[0]["snapshot"]["complete"] and
              fl.objs()[2]["snapshot"]["complete"], (ks, keys))
    finally:
        if fl:
            fl.stop()
        dch("-k", n)
        os.unlink(tp)


def test_follow_shell():
    """A shell's --follow over a 64K ring: typed blocks give chapter and ref
    deltas, an eviction its evict line first, then counts down and
    underline off (mutant 42); a clear (--clear, ED 3, RIS) reset cleared,
    never evict (T9g); more than 256 keys evicted reset evicted; a fifth
    subscriber busy."""
    n = "xc-fs"
    cfg = ENV["DCH_CONFIG"]
    with open(cfg, "w") as f:
        f.write("xray_ring = 64K\n")
    prog = ("while :; do printf '\\033]133;A\\007$ \\033]133;B\\007'; "
            "read -r cmd || exit; printf '\\033]133;C\\007'; eval \"$cmd\"; "
            "printf '\\033]133;D;0\\007'; done")
    cwd = os.path.join(T, "fs proj")
    os.makedirs(cwd, exist_ok=True)
    trace = os.path.join(T, "trace-fs")
    open(trace, "w").close()
    dbg = dict(ENV, DCH_DEBUG=trace)
    fls = []
    try:
        r = dch("--spawn", n, "--size", "80x24", "--cwd", cwd, "sh", "-c",
                prog)
        check("follow shell: spawned under xray_ring 64K", r.returncode == 0,
              r.stderr)
        wait_for(lambda: "$" in screen(n), 5)
        for _ in range(2):
            dch("--send", n, "echo 'see o/r#41'; seq 1 30\n")
            time.sleep(0.3)
        wait_for(lambda: "30" in screen(n), 5)
        time.sleep(0.3)
        _, one, _ = xray(n, "--json")
        fl = Follow(n, dbg)
        fls.append(fl)
        snap = fl.wait(lambda o: "snapshot" in o)
        r41 = [x for x in (one or {}).get("refs", [])
               if x["key"] == "pull:o/r#41"]
        check("follow shell: the snapshot is the --json object",
              snap and snap[0]["snapshot"] == one and r41 and
              r41[0]["underline"], (snap, one))

        time.sleep(0.2)
        off = os.path.getsize(trace)
        time.sleep(1.0)
        idle = trace_from(trace, off)
        check("follow shell: idle, no deadline and no wake (no timer)",
              "xray follow: deadline" not in idle and
              "xray follow: woke" not in idle, idle)

        k0 = len(fl.got)
        dch("--send", n, "echo 'more o/r#42'; seq 1 30\n")
        a = fl.wait(is_ref("pull:o/r#42"), frm=k0)
        c = fl.wait(lambda o: "chapter" in o, frm=k0)
        check("follow shell: a typed block gives a chapter and a ref delta",
              a and c and c[0]["chapter"]["title"] == "echo 'more o/r#42'; seq 1 30",
              fl.objs(k0))
        time.sleep(0.4)
        _, now, _ = xray(n, "--json")
        cur = {x["key"]: x for x in snap[0]["snapshot"]["refs"]} if snap \
            else {}
        for o in fl.objs():
            if "ref" in o:
                cur[o["ref"]["key"]] = o["ref"]
        check("follow shell: the snapshot plus its deltas is the --json of "
              "now (refs)", now and sorted(cur.values(), key=str) ==
              sorted(now["refs"], key=str), (cur, now and now["refs"]))

        # mutant 42: the eviction step's evict line first, then underline off
        k1 = len(fl.got)
        dch("--send", n, "seq 1 20000\n")
        gone = fl.wait(is_ref("pull:o/r#41", count=0), 15, frm=k1)
        ob = fl.objs(k1)
        i = next((j for j, o in enumerate(ob) if is_ref("pull:o/r#41",
                                                         count=0)(o)), None)
        j = i
        while j is not None and j > 0 and ("ref" in ob[j - 1] or
                                           "chapter" in ob[j - 1]):
            j -= 1
        ev = ob[j - 1] if j else None
        check("follow shell: an eviction sends evict first, then the gone "
              "key as count 0, underline and defined off", gone and
              ev and "evict" in ev and gone[0]["ref"]["underline"] is False
              and gone[0]["ref"]["defined"] is False,
              [list(o)[1:] for o in ob][:40])
        chs = (one or {}).get("chapters", [])
        check("follow shell: evict's seq is past the evicted block's anchor",
              ev and "evict" in ev and chs and
              ev["evict"]["seq"] > chs[0]["offset"], (ev, chs))
        check("follow shell: no reset for a small eviction",
              not [o for o in ob if "reset" in o], ob[:20])
        wait_for(lambda: "20000" in screen(n), 10)

        # T9g: a clear sends reset cleared and a snapshot, never evict
        for what, how in (("--clear", None), ("ED 3", "printf '\\033[3J'\n"),
                          ("RIS", "printf '\\033c'\n")):
            time.sleep(0.4)
            k = len(fl.got)
            if how:
                dch("--send", n, "echo 'pre o/r#43'; seq 1 30\n")
                fl.wait(is_ref("pull:o/r#43"), frm=k)
                time.sleep(0.4)
                k = len(fl.got)
                dch("--send", n, how)
            else:
                rc, cl, err = xray(n, "--clear", "--json")
            s = fl.wait(lambda o: "snapshot" in o, frm=k)
            ob = fl.objs(k)
            check("follow shell: %s sends reset cleared, then a snapshot, "
                  "and no evict" % what, s and
                  ob[0] == {"schema": 1, "reset": {"why": "cleared"}} and
                  "snapshot" in ob[1] and
                  not [o for o in ob if "evict" in o] and
                  not [x for x in s[0]["snapshot"]["refs"]
                       if x["key"] in ("pull:o/r#41", "pull:o/r#43")],
                  [list(o)[1:] for o in ob][:10])

        # 4 subscribers per master: a fifth is busy, before any line
        for _ in range(3):
            fls.append(Follow(n))
        ok = all(f.wait(lambda o: "snapshot" in o) for f in fls)
        r = dch("--xray", n, "--follow", "--json")
        check("follow shell: a fifth subscriber gets busy, exit 1, nothing "
              "on stdout", ok and r.returncode == 1 and not r.stdout and
              r.stderr == "dch: busy\n", (ok, r.returncode, r.stdout[:200],
                                          r.stderr))

        # the session's end ends every follower, exit 0
        dch("-k", n)
        rcs = []
        for f in fls:
            try:
                rcs.append(f.p.wait(5))
            except subprocess.TimeoutExpired:
                rcs.append(None)
        check("follow shell: the session's end ends every follower, exit 0",
              rcs == [0] * len(fls), rcs)
        tr = open(trace).read()
        check("follow shell: the trace holds no passage",
              "see o/r#41" not in tr and "more o/r" not in tr, tr[-300:])
    finally:
        for f in fls:
            f.stop()
        dch("-k", n)

    # more than 256 keys in one eviction step: reset evicted. The keys sit
    # on three adjacent rows of 100 (a 1000-column pane; 128 mentions per
    # entry, 3.8), so one step evicts them all.
    n = "xc-fs2"
    fl = None
    try:
        r = dch("--spawn", n, "--size", "1000x24", "--cwd", cwd, "sh", "-c",
                prog)
        wait_for(lambda: "$" in screen(n), 5)
        fl = Follow(n)
        fl.wait(lambda o: "snapshot" in o)
        # typed as a loop: a canonical-mode line holds 1024 bytes
        dch("--send", n, "for i in $(seq 1000 1299); do printf 'o/r#%s ' "
            "$i; [ $((i % 100)) = 99 ] && echo; done; seq 1 30\n")
        fl.wait(is_ref("pull:o/r#1299"), 10)
        time.sleep(0.4)
        k = len(fl.got)
        dch("--send", n, "seq 1 20000\n")
        s = fl.wait(is_reset("evicted"), 15, frm=k)
        ob = fl.objs(k)
        i = next((j for j, o in enumerate(ob) if is_reset("evicted")(o)), 0)
        check("follow shell: an eviction of more than 256 keys sends evict, "
              "reset evicted, then a snapshot", s and i > 0 and
              "evict" in ob[i - 1] and i + 1 < len(ob) and
              "snapshot" in ob[i + 1] and
              not [x for x in ob[i + 1]["snapshot"]["refs"]
                   if x["key"] == "pull:o/r#1000"],
              [list(o)[1:] for o in ob][:12])
    finally:
        if fl:
            fl.stop()
        dch("-k", n)
        if os.path.exists(cfg):
            os.unlink(cfg)


# ---- #149: a shell --ref's output caps and its tick (QA 1.50.0 MED-3) -----

def typed_blocks(n, nblocks, body, done):
    """A live shell of nblocks typed OSC 133 blocks, each printing body (sh
    commands); one --send per block, so each has input between B and C."""
    prog = ("i=0; while [ $i -lt %d ]; do printf '\\033]133;A\\007$ "
            "\\033]133;B\\007'; read cmd; printf '\\033]133;C\\007'; %s"
            "printf '\\033]133;D;0\\007'; i=$((i+1)); done; echo %s''DONE; "
            "sleep 300") % (nblocks, body, done)
    r = dch("--spawn", n, "--size", "200x24", "sh", "-c", prog)
    check("%s: spawned" % n, r.returncode == 0, r.stderr)
    wait_for(lambda: "$" in screen(n), 5)
    for _ in range(nblocks):
        dch("--send", n, "x\n")
        time.sleep(0.005)
    return wait_for(lambda: done + "DONE" in screen(n), 60)


def test_ref_shell_caps():
    """MED-3's repro: 1300 typed blocks of three `# PROJ-9 <30 emoji> k`
    headings and six emoji lines in a 4M ring. A full --ref is first ...
    done, exit 0; definition lines once per title (LOW-2); mention lines
    past 200 counted in the tally's mentions_dropped (LOW-1)."""
    n = "xc-refcap"
    cfg = ENV["DCH_CONFIG"]
    with open(cfg, "w") as f:
        f.write("xray_ring = 4M\n")
    e = "\U0001F600" * 30
    body = "".join("echo '# PROJ-9 %s %d'; " % (e, k) for k in range(3)) + \
        "".join("echo '%s line %d'; " % (e, k) for k in range(6))
    try:
        check("refcap: 1300 blocks typed", typed_blocks(n, 1300, body, "RC"))
        rc, o, err = xray(n, "--json")
        p9 = [x for x in (o or {}).get("refs", []) if x["key"] == "ticket:PROJ-9"]
        # a block whose input raced its B is forged (7.3): about 1300
        count = p9[0]["count"] if p9 else 0
        check("refcap: the index counts ~1300 blocks, 3 definitions",
              rc == 0 and count > 1200 and p9[0]["definitions"] == 3,
              (rc, p9, err))
        r = dch("--xray", n, "--ref", "PROJ-9", "--scope", "session",
                "--full", "--json")
        ls = ref_lines(r.stdout) or [{}]
        defs = [l["definition"]["title"] for l in ls if "definition" in l]
        ment = sum("mention" in l for l in ls)
        tally = [l["tally"] for l in ls if "tally" in l]
        check("refcap: --ref --full exits 0 with first ... done (%d bytes)" %
              len(r.stdout.encode()), r.returncode == 0 and
              "first" in ls[0] and ls[-1].get("done") is True and
              len(r.stdout.encode()) <= 4 << 20, (r.returncode, r.stderr,
                                                  r.stdout[-300:]))
        check("refcap: definition lines once per title (3, LOW-2)",
              len(defs) == 3 and len(set(defs)) == 3, defs)
        check("refcap: 200 mention lines, the rest in mentions_dropped "
              "(LOW-1)", ment == 200 and len(tally) == 1 and
              tally[0]["count"] == count and
              tally[0]["mentions_dropped"] >= 3 * count - 200 and
              ls[-1]["counts"]["session"] == count, (ment, tally))
    finally:
        dch("-k", n)
        os.unlink(cfg)


def test_ref_shell_tick():
    """LOW-4: a shell --ref is ticked per record as a transcript is. A ring
    scans in well under a second, so each run is held with SIGSTOP right
    after `first`, mid-scan, to let the clock pass: a full one then writes
    progress, a light one stops at --budget-ms, a closed pipe ends one
    silently."""
    import signal
    n = "xc-reftick"
    cfg = ENV["DCH_CONFIG"]
    with open(cfg, "w") as f:
        f.write("xray_ring = 16M\n")
    prog = ("awk 'BEGIN{for(i=0;i<30000;i++){s=\"\";for(j=0;j<8;j++) "
            "s=s sprintf(\"https://github.com/o/r/pull/%d src/a%d.c:%d \", "
            "j+i%90, j, i%300); print s}}'; echo TICK''DONE; sleep 300")
    argv = [DCH, "--xray", n, "--ref", "#424242", "--scope", "session",
            "--json"]

    def held(extra, close=False):
        """argv + extra, stopped 300 ms once `first` is out: (rc, the
        lines, stderr)."""
        p = subprocess.Popen(argv + extra, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=ENV)
        first = p.stdout.readline()
        p.send_signal(signal.SIGSTOP)
        if close:
            p.stdout.close()
        time.sleep(0.3)
        p.send_signal(signal.SIGCONT)
        rest = b"" if close else p.stdout.read()
        p.wait(10)
        err = p.stderr.read()
        p.stderr.close()
        if not close:
            p.stdout.close()
        return p.returncode, ref_lines((first + rest).decode()) or [{}], err

    try:
        r = dch("--spawn", n, "--size", "200x24", "sh", "-c", prog)
        check("tick: spawned", r.returncode == 0, r.stderr)
        check("tick: the output finished",
              wait_for(lambda: "TICKDONE" in screen(n), 60))
        t0 = time.time()
        r = subprocess.run(argv + ["--full"], capture_output=True, text=True,
                           env=ENV, timeout=60)
        full = time.time() - t0
        ls = ref_lines(r.stdout) or [{}]
        d = ls[-1]
        check("tick: a full --ref reads the ring, first ... done (%.2fs)" %
              full, r.returncode == 0 and "first" in ls[0] and
              d.get("done") is True and not d["partial"] and
              d["bytes"] > 4 << 20, (r.returncode, r.stderr, r.stdout[-300:]))
        # the scan can end before SIGSTOP lands on a loaded machine: retry
        # until a run is caught mid-scan, strict on the one that is
        for k in range(5):
            rc, ls, err = held(["--full"])
            prog_ = [l["progress"] for l in ls if "progress" in l]
            if prog_:
                break
        check("tick: a full --ref held mid-scan writes progress, then done "
              "(%d tries)" % (k + 1),
              rc == 0 and "first" in ls[0] and ls[-1].get("done") is True and
              prog_ and prog_[0]["bytes"] < d["bytes"] and
              ls[-1]["bytes"] == d["bytes"], (rc, err, ls[-2:]))
        for k in range(5):
            rc, ls, err = held(["--budget-ms", "50"])
            b = ls[-1]
            if b.get("why") == "budget":
                break
        check("tick: a light --ref past --budget-ms 50 stops, why budget "
              "(%d tries)" % (k + 1),
              rc == 0 and b.get("done") is True and b["why"] == "budget" and
              b["partial"] and b["bytes"] < d["bytes"], (rc, err, ls[-1:]))
        rc, ls, err = held(["--full"], close=True)
        check("tick: a closed pipe ends a shell --ref, exit 0, silent",
              rc == 0 and err == b"" and "first" in ls[0], (rc, err))
    finally:
        dch("-k", n)
        os.unlink(cfg)


# ---- #140 (Q16): the current primary screen, never the alt buffer ---------

def test_screen():
    """A shell's answer is the ring plus the current primary screen: a ref
    printed but not yet scrolled is found by --json, --ref and --follow, and
    lines counts the rows read. While a program holds the alt screen, neither
    the alt buffer nor the primary screen is read: the ring alone, no error,
    no why (never "alt"); the follower drops the screen's ref with no ring
    row moved, and takes it back when alt exits."""
    n = "xc-scr"
    fl = None
    r = dch("--spawn", n, "--size", "80x24", "sh")
    check("screen: spawned", r.returncode == 0, r.stderr)

    def refs(o):
        return {x["key"]: x for x in (o or {}).get("refs", [])}

    def rows():
        return [l for l in screen(n).split("\n") if l.strip()]

    try:
        wait_for(lambda: screen(n).strip(), 5)
        # \043 is #: the typed line holds no ref, only the output does
        dch("--send", n, "printf 'see q/w\\043140 here\\n'\n")
        wait_for(lambda: "q/w#140" in screen(n), 5)
        _, recs = X.read_all(n, 0)
        rc, o, err = xray(n, "--json")
        check("screen: a ref on the primary screen, not yet in the ring, is "
              "found", rc == 0 and o and not recs and "pull:q/w#140" in
              refs(o) and refs(o)["pull:q/w#140"]["count"] == 1 and
              o["complete"] and o["why"] is None, (rc, o, recs, err))
        check("screen: lines counts the rows read (the screen's non-blank "
              "rows; the ring is empty)", o and o["lines"] == len(rows()),
              (o and o["lines"], rows()))
        r = dch("--xray", n, "--ref", "q/w#140", "--scope", "session",
                "--full", "--json")
        men = [json.loads(l).get("mention") for l in r.stdout.splitlines()]
        men = [m for m in men if m]
        check("screen: --ref --full gives its mention line", r.returncode == 0
              and len(men) == 1 and men[0]["line"] == "see q/w#140 here",
              r.stdout)
        fl = Follow(n)
        snap = fl.wait(lambda x: "snapshot" in x)
        check("screen: the follower's snapshot is the --json object",
              snap and snap[0]["snapshot"] == o, (snap, o))

        k = len(fl.got)
        dch("--send", n, "printf '\\033[?1049halt k/l\\043150\\n'; read x; "
            "printf '\\033[?1049l'\n")
        wait_for(lambda: "k/l#150" in screen(n), 5)
        gone = fl.wait(is_ref("pull:q/w#140", count=0), 5, frm=k)
        rc, a, err = xray(n, "--json")
        _, recs = X.read_all(n, 0)
        check("screen: alt active, neither the alt buffer nor the primary "
              "screen is read (the ring alone)", rc == 0 and a and not
              set(refs(a)) & {"pull:k/l#150", "pull:q/w#140"} and
              a["lines"] == len([x for x in recs if x["kind"] != 4]),
              (rc, a, err))
        check("screen: alt is no error: exit 0, nothing on stderr, "
              "available, complete, why null (never \"alt\")", rc == 0 and
              a and a["available"] and a["complete"] and a["why"] is None and
              err == "", (rc, a, err))
        check("screen: the follower drops the screen's ref on the alt switch "
              "(no ring row moved)", gone, fl.objs(k))
        r = dch("--xray", n, "--ref", "k/l#150", "--scope", "session",
                "--full", "--json")
        check("screen: --ref never reads the alt buffer", r.returncode == 0
              and '"count":0' in r.stdout and '{"mention"' not in
              r.stdout.replace('"schema":1,', ''), r.stdout)

        k = len(fl.got)
        dch("--send", n, "\n")
        wait_for(lambda: "k/l#150" not in screen(n), 5)
        back = fl.wait(is_ref("pull:q/w#140", count=1), 5, frm=k)
        time.sleep(0.4)
        rc, b, err = xray(n, "--json")
        check("screen: alt exited, the screen's rows rejoin --json",
              rc == 0 and b and "pull:q/w#140" in refs(b) and
              "pull:k/l#150" not in refs(b) and b["why"] is None and
              b["lines"] == len(rows()), (rc, b, rows()))
        check("screen: ... and the follower, with no new output",
              back, fl.objs(k))
        cur = refs(snap[0]["snapshot"]) if snap else {}
        for x in fl.objs():
            if "ref" in x:
                cur[x["ref"]["key"]] = x["ref"]
        cur = {kk: v for kk, v in cur.items() if v["count"]}
        check("screen: the snapshot plus its deltas is the --json of now",
              b and cur == refs(b), (cur, b and refs(b)))
    finally:
        if fl:
            fl.stop()
        dch("-k", n)


# ---- --ref across sessions (#155, XR3a): scope, order, the 3.8 caps -------

def test_scope():
    """A store of its own: <n>, Claude sessions in its project dir and in
    another, an unmapped transcript beside them. The seam counts exactly the
    mapped files a scope reads; 65 files scan 64; the bytes, time and
    per-file range caps stop with their why; light stops at its budget across
    files; nothing is written."""
    S = os.path.join(T, "scope")
    home = os.path.join(S, "home")
    cfg = os.path.join(home, ".claude")
    sockdir = os.path.join(S, "dch-%d" % os.getuid())
    hdir = os.path.join(S, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    for d in (home, hdir):
        os.makedirs(d)
    env = dict(ENV, XDG_RUNTIME_DIR=S, XDG_STATE_HOME=os.path.join(S, "state"),
               HOME=home)
    trace = os.path.join(T, "trace-scope")
    dbg = dict(env, DCH_DEBUG=trace)
    sid = lambda k: "22222222-3333-4444-8555-%012d" % k

    def put(k, cwd, lines, updated):
        pdir = os.path.join(cfg, "projects", key(cwd))
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, sid(k) + ".jsonl"), "w") as f:
            f.write("".join(json.dumps({"type": "user", "cwd": cwd,
                                        "message": {"content": t}}) + "\n"
                            for t in lines))
        if updated is None:
            return              # unmapped: a file with no record
        rec = {"name": "s%d" % k, "cwd": cwd, "end": "exited",
               "argv": ["claude"], "started": 1794299400,
               "updated": updated, "ended": updated, "sock_dir": sockdir,
               "agent": {"session_id": sid(k), "cwd": cwd,
                         "config_dir": cfg}}
        with open(os.path.join(hdir, "s%d.json" % k), "w") as f:
            f.write(json.dumps(rec) + "\n")

    def ref(*a, env=dbg):
        open(trace, "w").close()
        r = dch("--xray", "s0", "--ref", *a, "--json", env=env)
        ls = ref_lines(r.stdout) or [{}]
        tr = open(trace).read()
        return (r.returncode, ls, tr.count("xray seam: open"),
                [l["tally"]["session"] for l in ls if "tally" in l], r.stderr)

    P, O = "/home/demo/scope", "/home/demo/elsewhere"
    put(0, P, ["mine #931"], 1794300000)
    put(1, P, ["older #931"], 1794300010)
    put(2, P, ["newer #931", "again #931"], 1794300020)
    put(3, O, ["far #931"], 1794300030)
    put(4, P, ["unmapped #931"], None)
    rc, ls, opens, order, err = ref("#931", "--scope", "project", "--full")
    d = ls[-1]
    check("scope: project reads <n>, then its dir newest first; the seam "
          "opens exactly those 3 files",
          rc == 0 and order == ["s0", "s2", "s1"] and opens == 3 and
          d.get("counts") == {"session": 1, "project": 3,
                              "project_sessions": 2, "all": None,
                              "all_sessions": None} and not d["partial"],
          (rc, order, opens, d, err))
    rc, ls, opens, order, err = ref("#931", "--scope", "all", "--full")
    d = ls[-1]
    check("scope: all adds the other dir after; still never the unmapped "
          "file (4 opens)",
          rc == 0 and order == ["s0", "s2", "s1", "s3"] and opens == 4 and
          d["counts"]["all"] == 1 and d["counts"]["all_sessions"] == 1,
          (rc, order, opens, d, err))
    rc, ls, opens, order, err = ref("#931", "--full")
    check("scope: no --scope is project (a remote caller's default too: "
          "all only when passed)",
          rc == 0 and ls[0]["first"]["scope"] == "project" and
          order == ["s0", "s2", "s1"] and opens == 3, (order, opens, err))
    rc, ls, opens, order, err = ref("#931", "--scope", "session", "--full")
    check("scope: session stops after <n> (1 open)",
          rc == 0 and order == ["s0"] and opens == 1, (order, opens))
    # #176: --full's resolved line comes right after <n>'s own file, before
    # the other sessions are read, so a capped or timed-out wide read keeps
    # it (s0's cwd is absolute: /etc/hosts is outside_project, nothing read)
    for sc, rest in (("project", ["s2", "s1"]), ("all", ["s2", "s1", "s3"])):
        for caps, why in ((None, None), ("1", "files"), ("0:0:1", "time")):
            rc, ls, opens, order, err = ref(
                "/etc/hosts:1", "--scope", sc, "--full",
                env=dict(dbg, DCH_XRAY_CAPS=caps) if caps else dbg)
            seq = ["resolved" if "resolved" in l else
                   "done" if l.get("done") else l["tally"]["session"]
                   for l in ls if "resolved" in l or "done" in l or
                   "tally" in l]
            rv = [l["resolved"]["state"] for l in ls if "resolved" in l]
            d = ls[-1]
            check("resolved order, --scope %s, %s: right after s0's tally, "
                  "before the others; done last%s" % (
                      sc, "no cap" if not caps else "capped (%s)" % why,
                      ", partial" if caps else ""),
                  rc == 0 and rv == ["outside_project"] and
                  seq[:2] == ["s0", "resolved"] and seq[-1] == "done" and
                  (seq[2:-1] == rest if not caps else
                   why == "time" or seq[2:-1] == []) and
                  d.get("partial") == bool(caps) and d.get("why") == why,
                  (rc, seq, d, err))
    rc, o, err = xray("s0", "--json", env=env)
    check("scope: other_projects counts the mapped session in the other "
          "dir, from names only", rc == 0 and o and o["other_projects"] == 1,
          (o and o.get("other_projects"), err))

    # the per-file range of another session's file (3.8: head + newest),
    # lowered by DCH_XRAY_CAPS: a mention in the head and the tail found,
    # the one between not
    pad = ["x" * 900] * 40
    put(5, O, ["head #932"] + pad + ["middle #932"] + pad + ["tail #932"],
        1794300040)
    rc, ls, opens, order, err = ref("#932", "--scope", "all", "--full",
                                    env=dict(dbg, DCH_XRAY_CAPS=
                                             "0:0:0:4096:8192"))
    t5 = [l["tally"] for l in ls if "tally" in l and
          l["tally"]["session"] == "s5"]
    lines = [l["mention"]["line"] for l in ls if "mention" in l]
    check("caps: the per-file range finds the head and the tail, not the "
          "middle; tally partial, done why range",
          rc == 0 and t5 and t5[0]["count"] == 2 and t5[0]["partial"] and
          lines == ["head #932", "tail #932"] and ls[-1]["partial"] and
          ls[-1]["why"] == "range", (t5, lines, ls[-1], err))
    rc, ls, opens, order, err = ref("#932", "--scope", "all", "--full",
                                    env=dict(dbg, DCH_XRAY_CAPS="0:1000"))
    check("caps: the bytes cap stops the scan before the next file, why "
          "bytes", rc == 0 and ls[-1]["partial"] and
          ls[-1]["why"] == "bytes" and opens < 5 and
          ls[-1]["bytes"] < 2000, (opens, ls[-1], err))
    rc, ls, opens, order, err = ref("#932", "--scope", "all", "--full",
                                    env=dict(dbg, DCH_XRAY_CAPS="0:0:1"))
    check("caps: the wall-time cap stops it, why time",
          rc == 0 and ls[-1]["partial"] and ls[-1]["why"] == "time",
          (ls[-1], err))
    os.unlink(os.path.join(hdir, "s5.json"))

    # 65 files scan 64 (3.8, 9.5); light over 1 MB files stops at its budget
    big = "".join(json.dumps({"type": "user", "cwd": P, "message": {
        "content": "pad " + "y" * 1000}}) + "\n" for _ in range(1000))
    for k in range(10, 71):
        put(k, P, ["#931 in %d" % k], 1794300100 + k)
        with open(os.path.join(cfg, "projects", key(P), sid(k) + ".jsonl"),
                  "a") as f:
            f.write(big)
    before = files(S)
    rc, ls, opens, order, err = ref("#931", "--scope", "all", "--full")
    d = ls[-1]
    check("caps: 65 files scan 64, why files, the newest first",
          rc == 0 and opens == 64 and d["files"] == 64 and d["partial"] and
          d["why"] == "files" and order[:3] == ["s0", "s70", "s69"] and
          "s3" not in order, (opens, d, order[:4], err))
    t0 = time.time()
    # untraced: DCH_DEBUG's lines cost more than the 50 ms budget
    rc, ls, opens, order, err = ref("#933", "--scope", "all",
                                    "--budget-ms", "50", env=env)
    d = ls[-1]
    check("caps: light stops at its budget across files (%d files, %.2fs)"
          % (d.get("files", -1), time.time() - t0),
          rc == 0 and d.get("why") == "budget" and d["partial"] and
          d["files"] < 64, (d, err))
    rc, ls, opens, order, err = ref("#931", "--scope", "all")
    d = ls[-1]
    check("light: stops at the first other session's result, titles and "
          "counts only for it",
          rc == 0 and d["why"] == "first" and order == ["s0", "s70"] and
          not any("mention" in l and l["mention"]["session"] != "s0"
                  for l in ls), (order, d, err))
    after = files(S)
    check("scope: nothing written by a full and a light --scope all "
          "(names, sizes, mtimes)", after == before,
          sorted(set(after.items()) ^ set(before.items()))[:6])


def main():
    try:
        test_names()
        test_shell()
        test_record()
        test_heavy()
        test_evicted()
        test_clear_off()
        test_evil()
        test_hangup()
        test_claude()
        test_clear_after_claude()
        test_two_claudes()
        test_refs()
        test_d2_whole()
        test_write_fail()
        test_chapter()
        test_chapter_shell()
        test_cancel()
        test_claude_caps()
        test_shell_caps()
        test_follow_history()
        test_follow_claude()
        test_follow_late()
        test_follow_cap()
        test_follow_shell()
        test_follow_stale()
        test_follow_agent()
        test_follow_agent(off=True)
        test_screen_agent()
        test_screen()
        test_ref_shell_caps()
        test_ref_shell_tick()
        test_scope()
    finally:
        shutil.rmtree(T, ignore_errors=True)
    if fails[0]:
        print("%d failed" % fails[0])
        sys.exit(1)


if __name__ == "__main__":
    main()
