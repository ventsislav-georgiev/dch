#!/usr/bin/env python3
"""`dch --xray <n> --json` and `--clear` on a real shell: an OSC 133 block typed with
--send, --clear through MSG_RING_CLEAR, the ring, evil and hangup masters, the
16 MiB RSS budget (3.8), shell caps and the shell --ref / --chapter rows.

Split from xray_cli_test.py; helpers in tests/xray_cli_lib.py.
Run:  python3 tests/xray_shell_test.py          (uses ./dch; run.sh isolates)
"""
import json, os, random, re, shutil, socket, stat, struct, subprocess, sys
import tempfile, threading, time

from xray_cli_lib import *  # noqa: E402,F401,F403
from xray_cli_lib import X  # noqa: E402,F401


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
        wait_for(lambda: "0000007" in screen(n), 5)
        dch("--send", n, "seq 1 40\n")
        wait_for(lambda: "40" in screen(n), 5)
        wait_for(lambda: len((xray(n, "--json")[1] or {})
                             .get("chapters", [])) >= 2, 5)
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


# ---- #149: a shell --ref's output caps and its tick (QA 1.50.0 MED-3) -----

def typed_blocks(n, nblocks, body, done):
    """A live shell of nblocks typed OSC 133 blocks, each printing body (sh
    commands); one typed line per block, each sent only once that block's
    B has come out of an attached mirror (the master parses a B before it
    forwards it), so each has input between B and C (7.3). One mirror types
    them all: 1300 dch --send starts outran the file's 10 min under ASan, and
    unpaced parallel sends forged blocks (1086 of 1300)."""
    import fcntl, pty, select, signal, termios
    prog = ("i=0; while [ $i -lt %d ]; do printf '\\033]133;A\\007$ "
            "\\033]133;B\\007'; read cmd; printf '\\033]133;C\\007'; %s"
            "printf '\\033]133;D;0\\007'; i=$((i+1)); done; echo %s''DONE; "
            "sleep 300") % (nblocks, body, done)
    r = dch("--spawn", n, "--size", "200x24", "sh", "-c", prog)
    check("%s: spawned" % n, r.returncode == 0, r.stderr)
    wait_for(lambda: "$" in screen(n), 5)	# block 0's B is in
    ws = struct.pack("HHHH", 24, 200, 0, 0)
    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, ws)
        os.execve(DCH, [DCH, "-f", "-a", n], ENV)
    os.set_inheritable(fd, False)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, ws)
    mark, tail, bs, ok = b"\033]133;B\007", b"", 0, True
    try:
        end = time.time() + 10	# the attach's redraw: its input path is up
        while b"$" not in tail and time.time() < end:
            if select.select([fd], [], [], 0.1)[0]:
                tail = (tail + os.read(fd, 65536))[-64:]
        tail = b""
        for i in range(nblocks):
            os.write(fd, b"x\n")
            if i == nblocks - 1:	# the last line through before the detach
                mark, bs = (done + "DONE").encode(), i
            end = time.time() + 30
            while bs <= i and time.time() < end:	# block i+1's B
                if select.select([fd], [], [], 0.2)[0]:
                    s = tail + os.read(fd, 65536)
                    bs += s.count(mark)
                    tail = s[1 - len(mark):]
            if bs <= i:
                ok = False
                break
    finally:
        os.kill(pid, signal.SIGUSR1)	# detach
        os.close(fd)
        os.waitpid(pid, 0)
    return ok and wait_for(lambda: done + "DONE" in screen(n), 60)


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
        # the blocks still on screen are not in the ring yet: about 1300
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


def main():
    try:
        test_shell()
        test_record()
        test_heavy()
        test_evicted()
        test_clear_off()
        test_evil()
        test_hangup()
        test_chapter_shell()
        test_shell_caps()
        test_ref_shell_caps()
        test_ref_shell_tick()
    finally:
        shutil.rmtree(T, ignore_errors=True)
    if fails[0]:
        print("%d failed" % fails[0])
        sys.exit(1)


if __name__ == "__main__":
    main()
