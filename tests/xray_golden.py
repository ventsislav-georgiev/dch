#!/usr/bin/env python3
"""X-ray engine goldens (#120, XR1b; spec 9.2): tests/xray/fixtures/.

Each case is a directory:
  case.json        {"schema":1,"about","session","argv":[[...],...],
                    "display_only":[]}
  expected/<k>.json  what argv entry k (1-based) answers: the --json object
                   as dch prints it (anchors' inode masked as f*:<offset>),
                   or {"schema":1,"exit":N,"stderr":"<first line>"} for a
                   nonzero exit
  expected/<k>.ndjson  a --ref stream (3.4, #135): its lines with
                   elapsed_ms masked to 0 and progress lines left out (they
                   are timing; checked to sit between first and done)
  claude/<uuid>.jsonl  transcripts, placed at
                   <config>/projects/<setup "project_dir">/<uuid>.jsonl,
                   or at setup "project_dirs"[<uuid>] (#155: another
                   project dir; "@WORK:<name>@" is live shell <name>'s
                   pane cwd's key); an argv element f*:<offset> (a
                   --chapter anchor, #156) is f<the last copied
                   transcript's inode>:<offset>
  shell/<name>.ring.ndjson  a live shell session <name>: ring records,
                   served by a test-only fake master (below)
  codex/<uuid>.jsonl  Codex rollouts (7.4, #160), placed at
                   <home>/.codex/sessions/<setup codex.day>/rollout-...-
                   <uuid>.jsonl; a real dch master runs tests/fake_codex.c
                   as `codex` in session <setup codex.session>, and the
                   first rollout's id is its thread (a shell snapshot that
                   carries the session, as Codex writes one)
  setup.json       what the case needs around the corpus: History records,
                   the fake masters' ring status, config lines. A live
                   shell's pane runs in <tmp>/work/<name> (MSG_CMD answers
                   a real child there, 24x80), unless "cmd":"unsup" (a
                   master that does not answer MSG_CMD). Its primary screen
                   (MSG_READ with DCH_READ_F_PRIMARY, #140) is "screen"
                   (default "screen row\n"); "primary":"alt" answers
                   DCH_ST_ALT (the alt screen is active), "primary":"old"
                   DCH_ST_ERR (a master before #140). "isolated": true
                   fails the case if any argv reads that master's ring or
                   screen (#155: no scope reads another session's ring,
                   S17). Ignored by Prosper.

The temp root is masked as /T in the output, and its project key as -T (7.1
maps every '/' to '-'), so a golden carries the real pane-cwd key shape.

The fake master lives here, in tests/, never in dch: it answers the control
ops a query sends (MSG_RING_READ, MSG_READ, MSG_RING_CLEAR, MSG_CMD) from
the .ring.ndjson records, so a golden pins the client's reading of a ring
whatever the capture does. tests/ring_unit.c's round trip pins the file
format (run.sh loads and dumps every fixture ring byte-identically).

MANIFEST lists the sha256 of every file under fixtures/, sorted by path;
this test recomputes it (spec 9.2: a hand edit or a stale copy fails).

Run:  python3 tests/xray_golden.py            (uses ./dch)
      python3 tests/xray_golden.py --update   (rewrites expected/ and
                                               MANIFEST: review the diff)
      python3 tests/xray_golden.py <case>...  (only those cases)
"""
import hashlib, json, os, re, shutil, socket, struct, subprocess, sys
import tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "xray", "fixtures")
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
UPDATE = "--update" in sys.argv
fails = [0]

MSG_READ, MSG_READ_DATA, MSG_READ_END, MSG_ACK = 6, 8, 9, 11
MSG_CMD, MSG_RING_READ, MSG_RING_HDR, MSG_RING_CLEAR = 15, 18, 19, 21
MSG_CMD_STAT = 17
MSG_WINCH, MSG_REDRAW, MSG_ATTACH, MSG_DETACH = 4, 5, 1, 2
ST_OK, ST_ERR, ST_NOVT, ST_UNSUP, ST_ALT = 0, 1, 2, 5, 7
READ_F_PRIMARY = 0x08
S_RANGE = 0x01
PKT_MAX = 4096
KINDS = {"prompt": 1, "input": 2, "output": 3, "mark": 4, "gap": 5}


def check(msg, cond, detail=""):
    print("  %s %s" % ("ok  " if cond else "FAIL", msg))
    if not cond:
        fails[0] += 1
        if detail:
            print("       " + str(detail)[:1500])


# ---- the fake master ------------------------------------------------------

class FakeMaster:
    """One session's control socket over a fixed list of ring records."""

    def __init__(self, path, records, status=0, sid="m0000000000000000",
                 screen="screen row\n", cwd=None, primary=None):
        self.recs = records          # [(seq, kind, flags, t, bytes)]
        self.status, self.sid, self.screen = status, sid, screen
        self.primary = primary
        # the pane: a real child whose cwd the client reads, or None
        self.child = (subprocess.Popen(["sleep", "600"], cwd=cwd)
                      if cwd else None)
        self.epoch = 0
        self.ops = []
        self.base = records[0][0] if records else 1 << 40
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(16)
        self.path = path
        threading.Thread(target=self.serve, daemon=True).start()

    def close(self):
        if self.child:
            self.child.kill()        # ours: by its exact pid
            self.child.wait()
        self.sock.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.conn, args=(c,), daemon=True).start()

    @staticmethod
    def recv(c, n):
        b = b""
        while len(b) < n:
            k = c.recv(n - len(b))
            if not k:
                raise EOFError
            b += k
        return b

    def conn(self, c):
        try:
            while True:
                t, n = struct.unpack("<BH", self.recv(c, 3))
                plen = (8 if t in (MSG_WINCH, MSG_REDRAW) else
                        0 if t in (MSG_ATTACH, MSG_DETACH) else n)
                p = self.recv(c, plen) if plen else b""
                self.ops.append(t)
                c.sendall(self.answer(t, p))
        except (EOFError, OSError):
            pass
        finally:
            c.close()

    def answer(self, t, p):
        if t == MSG_RING_READ:
            return self.ring_read(p)
        if t == MSG_RING_CLEAR:
            self.recs = []
            self.epoch += 1
            return struct.pack("<BH", MSG_ACK, ST_OK)
        if t == MSG_READ:
            if self.screen is None:
                return struct.pack("<BH", MSG_READ_END, ST_NOVT)
            if len(p) > 4 and p[4] & READ_F_PRIMARY and self.primary:
                return struct.pack("<BH", MSG_READ_END, ST_ALT
                                   if self.primary == "alt" else ST_ERR)
            b = self.screen.encode()
            return (struct.pack("<BH", MSG_READ_DATA, len(b)) + b +
                    struct.pack("<BH", MSG_READ_END, ST_OK))
        if t == MSG_CMD and self.child:
            b = b"\0".join(x.encode() for x in (
                str(self.child.pid), "running", "-1", "0", "1", "sleep")) + b"\0"
            stat = (struct.pack("<BH", MSG_CMD_STAT, 6) +
                    struct.pack("<HHH", 24, 80, 0)) if p[:1] == b"\1" else b""
            return (stat + struct.pack("<BH", MSG_READ_DATA, len(b)) + b +
                    struct.pack("<BH", MSG_READ_END, ST_OK))
        return struct.pack("<BH", MSG_READ_END, ST_UNSUP)   # MSG_CMD too

    def ring_read(self, p):
        seq, maxb = struct.unpack_from("<QI", p)
        head = self.recs[0][0] if self.recs else self.base
        tail = self.recs[-1][0] + 1 if self.recs else self.base
        st = self.status
        if self.status & 0x08:                       # S_OFF: no ring
            out, first = [], tail
        else:
            if seq < head or seq > tail:
                st |= S_RANGE
                seq = head
            out, size = [], 0
            for r in self.recs:
                if r[0] < seq:
                    continue
                b = struct.pack("<HBBI", len(r[4]), r[1], r[2], r[3]) + r[4]
                if out and size + len(b) > maxb:
                    break
                out.append(b)
                size += len(b)
            first = seq if out else tail
        hdr = (struct.pack("<BQQQII", st, head, tail, first, len(out),
                           self.epoch) + self.sid.encode()[:17])
        data = b"".join(out)
        f = struct.pack("<BH", MSG_RING_HDR, len(hdr)) + hdr
        for k in range(0, len(data), PKT_MAX):
            ch = data[k:k + PKT_MAX]
            f += struct.pack("<BH", MSG_READ_DATA, len(ch)) + ch
        return f + struct.pack("<BH", MSG_READ_END, ST_OK)


def load_ring(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            out.append((r["seq"], KINDS[r["kind"]], r["flags"], r["t"],
                        r["text"].encode("utf-8", "surrogateescape")))
    return out


# ---- one case ---------------------------------------------------------------

ANCHOR = re.compile(r'"anchor":"f[0-9a-f]+:')
CHAPTER = re.compile(r'"chapter":"f[0-9a-f]+:')
ELAPSED = re.compile(r'"elapsed_ms":\d+')


def key(path):
    return "".join(c if c.isascii() and c.isalnum() else "-" for c in path)


def mask(s, root):
    s = ANCHOR.sub('"anchor":"f*:', s)
    s = CHAPTER.sub('"chapter":"f*:', s)
    s = ELAPSED.sub('"elapsed_ms":0', s)
    return s.replace(root, "/T").replace(key(root), "-T")


def stream(got):
    """A --ref stream: progress lines out, after checking where they sit."""
    lines = got.splitlines(True)
    objs = [json.loads(l) for l in lines]
    ok = (len(objs) >= 2 and "first" in objs[0] and objs[-1].get("done") is
          True and all(o.get("schema") == 1 for o in objs))
    keep = [l for l, o in zip(lines, objs) if "progress" not in o]
    return "".join(keep), ok


def codex_live(t, home, env, cx, xdir, spawned):
    """A live session running tests/fake_codex.c as `codex`, its rollouts
    under <home>/.codex and its thread's shell snapshot (7.4)."""
    day = cx.get("day", "2026/11/10")
    sess = os.path.join(home, ".codex", "sessions", day)
    snaps = os.path.join(home, ".codex", "shell_snapshots")
    os.makedirs(sess, mode=0o700)
    os.makedirs(snaps, mode=0o700)
    tids = sorted(f[:-len(".jsonl")] for f in os.listdir(xdir))
    for tid in tids:
        shutil.copy(os.path.join(xdir, tid + ".jsonl"), os.path.join(
            sess, "rollout-%sT10-00-00-%s.jsonl" % (day.replace("/", "-"),
                                                    tid)))
    bindir = os.path.join(t, "bin")
    os.makedirs(bindir)
    fake = os.path.join(bindir, "codex")
    b = subprocess.run(["cc", "-o", fake, os.path.join(HERE, "fake_codex.c")],
                       capture_output=True, text=True, timeout=60)
    check("codex: the fake codex builds", b.returncode == 0, b.stderr)
    n = cx.get("session", "cx")
    cwd = os.path.join(t, "work", n)
    os.makedirs(cwd)
    log = os.path.join(t, "codex.log")
    e = dict(env, FAKE_CODEX_LOG=log)
    r = subprocess.run([DCH, "--spawn", n, "--size", "80x24", "--cwd", cwd,
                        fake], capture_output=True, text=True, timeout=30,
                       env=e)
    check("codex: session %s spawned" % n, r.returncode == 0, r.stderr)
    spawned.append((n, e))
    line = None
    for _ in range(200):
        if os.path.exists(log) and open(log).read().endswith("\n"):
            line = open(log).read().split()
            break
        time.sleep(0.05)
    check("codex: the fake codex started", line is not None)
    marker = line[1] if line and line[1] != "-" else None
    p = os.path.join(snaps, tids[0] + ".1.sh")
    with open(p, "w") as f:
        f.write("# Snapshot file\nexport DCH_SESSION=%s\n%s" % (
            n, "export DCH_NATIVE_BRIDGE_ID=%s\n" % marker if marker
            else ""))
    os.chmod(p, 0o600)


def run_case(name):
    d = os.path.join(FIX, name)
    case = json.load(open(os.path.join(d, "case.json")))
    setup_p = os.path.join(d, "setup.json")
    setup = json.load(open(setup_p)) if os.path.exists(setup_p) else {}
    check("%s: case.json carries schema 1" % name, case.get("schema") == 1)
    t = tempfile.mkdtemp(prefix="xg")
    masters = []
    spawned = []
    try:
        home, state = os.path.join(t, "home"), os.path.join(t, "state")
        sockdir = os.path.join(t, "dch-%d" % os.getuid())
        cfg = os.path.join(home, ".claude")
        for p in (home, state, sockdir):
            os.makedirs(p, mode=0o700)
        # transcripts
        cdir = os.path.join(d, "claude")
        if os.path.isdir(cdir):
            dirs = setup.get("project_dirs", {})
            for f in sorted(os.listdir(cdir)):
                pd = dirs.get(f[:-len(".jsonl")],
                              setup.get("project_dir", "-home-demo-proj"))
                if pd.startswith("@WORK:"):
                    pd = key(os.path.join(os.path.realpath(t), "work",
                                          pd[6:-1]))
                proj = os.path.join(cfg, "projects", pd)
                os.makedirs(proj, exist_ok=True)
                shutil.copy(os.path.join(cdir, f), os.path.join(proj, f))
                ino = os.stat(os.path.join(proj, f)).st_ino
        # History records
        hdir = os.path.join(state, "dch", "sessions",
                            sockdir.replace("/", "-"))
        os.makedirs(hdir, mode=0o700)
        for h in setup.get("history", []):
            rec = json.loads(json.dumps(h["record"]).replace("@CFG@", cfg))
            rec["sock_dir"] = sockdir
            fp = os.path.join(hdir, h["file"] + ".json")
            with open(fp, "w") as f:
                f.write(json.dumps(rec) + "\n")
            os.chmod(fp, 0o600)
        # live shells
        sdir = os.path.join(d, "shell")
        live = setup.get("live", {})
        if os.path.isdir(sdir):
            for f in sorted(os.listdir(sdir)):
                n = f[:-len(".ring.ndjson")]
                o = live.get(n, {})
                cwd = None
                if o.get("cmd") != "unsup":
                    cwd = os.path.join(t, "work", n)
                    os.makedirs(cwd)
                masters.append(FakeMaster(
                    os.path.join(sockdir, n + ".sock"),
                    load_ring(os.path.join(sdir, f)), o.get("status", 0),
                    o.get("session_id", "m0000000000000000"),
                    None if o.get("lite") else o.get("screen", "screen row\n"),
                    cwd, o.get("primary")))
                masters[-1].isolated = (n, o.get("isolated"))
        # config lines (xray_ring for a ring's cap)
        with open(os.path.join(t, "config"), "w") as f:
            f.write("".join(l + "\n" for l in setup.get("config", [])))
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("DCH_", "Malloc", "CLAUDE"))}
        env.update(HOME=home, XDG_RUNTIME_DIR=t, XDG_STATE_HOME=state,
                   DCH_CONFIG=os.path.join(t, "config"), DCH_LOGIN_ENV="0",
                   DCH_NO_REMOTE="1")
        if any(l.startswith("remote_hosts") for l in setup.get("config", [])):
            del env["DCH_NO_REMOTE"]     # named hosts only, on loopback
        xdir = os.path.join(d, "codex")
        if os.path.isdir(xdir):
            codex_live(t, home, env, setup.get("codex", {}), xdir, spawned)
        for k, argv in enumerate(case["argv"], 1):
            argv = ["f%x:" % ino + a[3:] if a.startswith("f*:") else a
                    for a in argv]
            r = subprocess.run([DCH] + argv, capture_output=True, text=True,
                               timeout=30, env=env)
            ref = "--ref" in argv and r.returncode == 0
            if r.returncode == 0:
                got = mask(r.stdout, os.path.realpath(t))
                if ref:
                    got, shape = stream(got)
            else:
                got = json.dumps({"schema": 1, "exit": r.returncode,
                                  "stderr": (r.stderr.splitlines() or [""])[0]},
                                 separators=(",", ":")) + "\n"
            ep = os.path.join(d, "expected",
                              "%d.%s" % (k, "ndjson" if ref else "json"))
            if UPDATE:
                os.makedirs(os.path.dirname(ep), exist_ok=True)
                with open(ep, "w") as f:
                    f.write(got)
            want = open(ep).read() if os.path.exists(ep) else None
            check("%s/%d: %s" % (name, k, " ".join(argv)), got == want,
                  "want %r\n       got  %r\n       stderr %r"
                  % (want, got, r.stderr))
            if ref:
                check("%s/%d: first, schema 1 on every line, done last" %
                      (name, k), shape, got)
                continue
            obj = json.loads(got) if got.strip() else {}
            check("%s/%d: one line, schema 1%s" % (
                      name, k, ", session_id" if r.returncode == 0 else ""),
                  got.count("\n") == 1 and obj.get("schema") == 1 and
                  (r.returncode != 0 or "session_id" in obj), got)
        for m in masters:
            if m.isolated[1]:
                check("%s: no argv read %s's ring or screen (S17)" %
                      (name, m.isolated[0]),
                      MSG_RING_READ not in m.ops and MSG_READ not in m.ops,
                      m.ops)
        n_exp = len(os.listdir(os.path.join(d, "expected")))
        check("%s: one expected file per argv entry" % name,
              n_exp == len(case["argv"]), n_exp)
    finally:
        for m in masters:
            m.close()
        for n, e in spawned:
            subprocess.run([DCH, "-k", n], capture_output=True, timeout=30,
                           env=e)
        shutil.rmtree(t, ignore_errors=True)


# ---- MANIFEST -----------------------------------------------------------------

def manifest():
    rows = []
    for root, _, files in os.walk(FIX):
        for f in files:
            p = os.path.relpath(os.path.join(root, f), FIX)
            if p == "MANIFEST":
                continue
            h = hashlib.sha256(open(os.path.join(root, f), "rb").read())
            rows.append((p, h.hexdigest()))
    return "".join("%s  %s\n" % (h, p) for p, h in sorted(rows))


def main():
    only = [a for a in sys.argv[1:] if not a.startswith("--")]
    cases = sorted(c for c in os.listdir(FIX)
                   if os.path.isdir(os.path.join(FIX, c)) and
                   (not only or c in only))
    for c in cases:
        run_case(c)
    mp = os.path.join(FIX, "MANIFEST")
    if UPDATE:
        with open(mp, "w") as f:
            f.write(manifest())
    check("MANIFEST: the sha256 of every fixture file, sorted by path",
          os.path.exists(mp) and open(mp).read() == manifest(),
          "run tests/xray_golden.py --update and review the diff")
    if not only:
        check("goldens: %d cases" % len(cases), len(cases) >= 10)
    if fails[0]:
        print("%d failed" % fails[0])
        sys.exit(1)


if __name__ == "__main__":
    main()
