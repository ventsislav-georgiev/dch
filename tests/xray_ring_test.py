#!/usr/bin/env python3
"""The X-ray shell ring (#119) through a real master.

Covers what tests/ring_unit.c cannot reach from inside vt.c: the master
protocol's read op (MSG_RING_READ: range, the 1 MiB cap, session_id), the
primary-screen read (DCH_READ_F_PRIMARY, #140; nothing while alt or paused),
subscribers and their edge-triggered wake (a clear wakes one too; a wake at a
full subscriber's out cap drops it safely; a stale one loses its slot), the clear message, the xray_ring
config (0 = off), the client-input flag on a C mark from real keystrokes,
`dch --shell-init` in real zsh and bash, the --restart handover (the ring and
session_id survive, the resume blob carries no ring bytes, no shm object is
left open; a failed handover rotates to an empty ring with a new seq base and
session_id; a pre-1.43 client gets no handover and the core limit back), the trace canary (S3: no ring text in DCH_DEBUG traces) and the
master's core-dump limit. And a `--xray --json` answer never says complete
while rows that scrolled between its ring read and its screen read are
missing (#173).

Run:  python3 tests/xray_ring_test.py       (uses ./dch; run.sh isolates it)
"""
import json, os, re, resource, shutil, socket, struct, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import xray_ring as X  # noqa: E402

DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
TAG = "%d" % os.getpid()
fails = [0]


def check(msg, cond, detail=""):
    print("  %s %s" % ("ok  " if cond else "FAIL", msg))
    if not cond:
        fails[0] += 1
        if detail:
            print("       " + str(detail)[:600])


def dch(*a, env=None, **kw):
    return subprocess.run([DCH] + list(a), capture_output=True, text=True,
                          timeout=30, env=env, **kw)


def spawn(name, *cmd, env=None):
    r = dch("--spawn", name, "--size", "80x24", *cmd, env=env)
    if r.returncode != 0:
        raise SystemExit("spawn %s failed: %s" % (name, r.stderr))


def send(name, text):
    dch("--send", name, text)


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


def run(name, cmd, marker):
    """Type cmd, then echo a marker; wait until the marker is on screen."""
    send(name, cmd + "; echo " + marker + "-$((1+1))\n")
    return wait_for(lambda: marker + "-2" in screen(name))


def master_pid(name):
    """The pid whose argv is `dch --master-of <sock> ...`: /proc on Linux,
    ps elsewhere; 0 when there is none (or no way to look)."""
    sock = X.sock_path(name)
    if os.path.isdir("/proc/self/fd"):
        for d in os.listdir("/proc"):
            try:
                a = open("/proc/%s/cmdline" % d, "rb").read().split(b"\0")
            except (OSError, ValueError):
                continue
            if a[1:3] == [b"--master-of", sock.encode()]:
                return int(d)
        return 0
    ps = shutil.which("ps") or "/bin/ps"
    if not os.access(ps, os.X_OK):
        return 0
    out = subprocess.run([ps, "-axo", "pid=,args="], capture_output=True,
                         text=True).stdout
    for line in out.splitlines():
        f = line.split()
        if f[2:4] == ["--master-of", sock]:
            return int(f[0])
    return 0


def shm_objects(pid):
    """The pid's open shm/memfd objects as text ("" = none), or None when
    this platform offers no way to list them (the caller says skip)."""
    if os.path.isdir("/proc/%d/fd" % pid):
        links = []
        for fd in os.listdir("/proc/%d/fd" % pid):
            try:
                links.append(os.readlink("/proc/%d/fd/%s" % (pid, fd)))
            except OSError:
                pass
        return "\n".join(l for l in links
                         if "memfd:" in l or l.startswith("/dev/shm/"))
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    if not os.access(lsof, os.X_OK):
        return None
    lo = subprocess.run([lsof, "-p", str(pid)], capture_output=True,
                        text=True).stdout
    return "\n".join(l for l in lo.splitlines()
                     if "PSXSHM" in l or "dch-ring" in l)


def kill(name):
    dch("-k", name)


def restart_raw(name, exe, caps=True):
    """MSG_RESTART onto exe -> (type, status) of the reply. caps=True first
    sends MSG_RESTART_CAPS as a current dch does; caps=False is a pre-1.43
    client, which sends the path only (#121)."""
    s = X.connect(name)
    try:
        if caps:
            s.sendall(struct.pack("<BHB", X.MSG_RESTART_CAPS, 1, X.F_RING))
            X.recv_frame(s)
        s.sendall(struct.pack("<BH", X.MSG_RESTART, len(exe)) + exe.encode())
        t, st, _ = X.recv_frame(s)
        return t, st
    finally:
        s.close()


def test_read_op():
    n = "xr-read" + TAG
    spawn(n, "sh")
    try:
        run(n, "seq 1 220000", "done1")
        h, recs = X.read_all(n, 0)
        outs = X.texts(recs)
        check("read: a stale seq answers range (S10)",
              h["status_any"] & X.S_RANGE, h)
        check("read: session_id is m + 16 hex",
              re.fullmatch(r"m[0-9a-f]{16}", h["session_id"]), h)
        check("read: the 2 MiB ring evicted its head; seqs are 48-bit",
              h["head"] > 0 and h["tail"] < 1 << 49 and
              h["tail"] - h["head"] == len(recs), (h, len(recs)))
        nums = [int(t) for t in outs if t.isdigit()]
        check("read: seq 220000 rows land in order, none missing",
              nums == list(range(nums[0], nums[0] + len(nums))) and
              nums[-1] >= 220000 - 24, (nums[:3], nums[-3:]))
        check("read: no gap record", not [r for r in recs if r["kind"] == 5])
        s = X.connect(n)
        try:
            h1, r1 = X.read(s, h["head"], 0xFFFFFFFF)
            size = sum(8 + len(r["text"]) for r in r1)
            check("read: one reply carries at most 1 MiB (S10)",
                  0 < size <= 1 << 20 and size > (1 << 20) - 70000,
                  size)
            h2, r2 = X.read(s, h["head"], 10)
            check("read: a tiny max is raised to one whole record",
                  len(r2) >= 1 and not h2["status"] & X.S_RANGE, h2)
            h3, r3 = X.read(s, h["tail"])
            check("read: at the tail, nothing and no range",
                  r3 == [] and not h3["status"] & X.S_RANGE, h3)
            h4, _ = X.read(s, h["tail"] + 1000)
            check("read: a seq past the tail answers range",
                  h4["status"] & X.S_RANGE, h4)
        finally:
            s.close()
    finally:
        kill(n)


def recv_wakes(s, secs):
    """MSG_RING_WAKE frames that arrive within secs."""
    n = 0
    s.settimeout(secs)
    try:
        while True:
            t, ln, _ = X.recv_frame(s)
            if t != X.MSG_RING_WAKE:
                raise AssertionError("frame %d" % t)
            n += 1
    except socket.timeout:
        pass
    s.settimeout(10)
    return n


def test_subscribers():
    n = "xr-sub" + TAG
    spawn(n, "sh")
    socks = []
    try:
        run(n, "seq 1 100", "warm")
        for i in range(5):
            s = X.connect(n)
            socks.append(s)
            h, _ = X.read(s, 0, sub=True)
            if i < 4:
                check("subscribe %d of 4: accepted" % (i + 1),
                      h["status"] & X.S_SUB, h)
        check("a 5th subscriber is refused (S10)",
              h["status"] & X.S_SUBFULL and not h["status"] & X.S_SUB, h)
        s = socks[0]
        h, _ = X.read(s, 0, sub=True)
        send(n, "seq 1 50\n")
        time.sleep(1.0)
        send(n, "seq 1 50\n")
        w = recv_wakes(s, 1.5)
        check("wake: one per unread change, edge-triggered", w == 1, w)
        X.read(s, h["tail"])
        send(n, "seq 1 50\n")
        check("wake: a read re-arms it", recv_wakes(s, 1.5) == 1)
        socks.pop(1).close()
        time.sleep(0.2)
        s5 = X.connect(n)
        socks.append(s5)
        h, _ = X.read(s5, 0, sub=True)
        check("a closed subscriber frees its slot", h["status"] & X.S_SUB, h)
    finally:
        for s in socks:
            s.close()
        kill(n)


def screen_read(name, payload):
    """One MSG_READ with this payload on a connection of its own ->
    (MSG_READ_END status, text)."""
    s = X.connect(name)
    try:
        s.sendall(struct.pack("<BH", 6, len(payload)) + payload)
        data = b""
        while True:
            t, n, p = X.recv_frame(s)
            if t == X.MSG_READ_DATA:
                data += p
            elif t == X.MSG_READ_END:
                return n, data.decode("utf-8", "replace")
    finally:
        s.close()


def test_primary():
    """#140: DCH_READ_F_PRIMARY answers the visible primary rows with soft
    wraps joined, DCH_ST_ALT and nothing while the alt screen is active, and
    DCH_ST_ERR with another flag, ANSI or recent-N; the rows come back when
    alt exits. A DCH_RING_F_SCREEN subscriber is woken by a screen change that
    moves no ring row (the switch into alt and out of it, not a redraw on
    alt), a plain one is not."""
    n = "xr-pri" + TAG
    spawn(n, "sh")
    socks = []
    joined = "ab" * 50 + "END"
    try:
        wait_for(lambda: screen(n).strip())
        send(n, "printf 'ab%.0s' $(seq 1 50); echo END\n")
        # abEND is the output's end only: the typed line has "echo END"
        wait_for(lambda: "abEND" in screen(n))
        st, txt = screen_read(n, bytes([0, 0, 0, 0, 0x08]))
        check("primary: status OK, the wrapped row joined into one line",
              st == 0 and joined in txt.split("\n"), (st, txt))
        st, txt = screen_read(n, bytes([0, 0, 0, 0]))
        check("primary: a plain read still breaks it at the edge",
              st == 0 and "END" in txt and joined not in txt, (st, txt))
        for what, p in (("ANSI", [1, 0, 0, 0, 0x08]),
                        ("recent-N", [0, 1, 0, 0, 0x08]),
                        ("with F_CURSOR", [0, 0, 0, 0, 0x09]),
                        ("with F_META", [0, 0, 0, 0, 0x0a])):
            st, txt = screen_read(n, bytes(p))
            check("primary: %s is refused (DCH_ST_ERR)" % what,
                  st == 1 and txt == "", (st, txt))
        scr, plain = X.connect(n), X.connect(n)
        socks += [scr, plain]
        X.read(scr, 0, sub=True, flags=X.F_SCREEN)
        h0, _ = X.read(plain, 0, sub=True)
        # \101 is A: the typed line never reads ALT
        send(n, "printf '\\033[?1049h\\101LT\\n'; read x; "
             "printf '\\033[?1049l'\n")
        wait_for(lambda: "ALT" in screen(n))
        check("primary: a screen subscriber is woken by the alt switch",
              recv_wakes(scr, 1.5) == 1)
        check("primary: a plain subscriber is not (no ring row moved)",
              recv_wakes(plain, 0.5) == 0)
        h1, _ = X.read(plain, 0)
        check("primary: ... and the ring did not move", h1["tail"] ==
              h0["tail"], (h0, h1))
        st, txt = screen_read(n, bytes([0, 0, 0, 0, 0x08]))
        check("primary: while alt is active, DCH_ST_ALT and no data",
              st == 7 and txt == "", (st, txt))
        st, txt = screen_read(n, bytes([0, 0, 0, 0]))
        check("primary: a plain visible read is the alt screen, as before",
              st == 0 and "ALT" in txt, (st, txt))
        X.read(scr, 0)                  # re-arms its wake
        send(n, "abc")
        wait_for(lambda: "abc" in screen(n))
        check("primary: a redraw on the alt screen wakes no screen subscriber",
              recv_wakes(scr, 0.5) == 0)
        send(n, "\n")
        wait_for(lambda: "ALT" not in screen(n))
        check("primary: leaving alt wakes it", recv_wakes(scr, 1.5) == 1)
        st, txt = screen_read(n, bytes([0, 0, 0, 0, 0x08]))
        check("primary: alt exited, the primary rows are back",
              st == 0 and joined in txt.split("\n") and "ALT" not in txt,
              (st, txt))
    finally:
        for s in socks:
            s.close()
        kill(n)


def test_primary_paused(tmp):
    """#140 review: while capture is paused for an agent (Claude paints its
    TUI on the primary screen), DCH_READ_F_PRIMARY answers DCH_ST_ALT and no
    rows, decided by the master, so a shell answer racing the agent's start
    never takes the TUI as output; a plain visible read still renders it."""
    n = "xr-pp" + TAG
    bindir, fdir = os.path.join(tmp, "ppbin"), os.path.join(tmp, "ppfake")
    for d in (bindir, fdir):
        os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    env = dict(os.environ, FAKE_CLAUDE_DIR=fdir, DCH_RECORD_MS="300",
               PATH=bindir + ":" + os.environ.get("PATH", "/usr/bin:/bin"))

    def status():
        try:
            s = X.connect(n)
            try:
                return X.read(s, 0, 1)[0].get("status", 0)
            finally:
                s.close()
        except (OSError, EOFError):
            return 0
    spawn(n, "sh", env=env)
    try:
        wait_for(lambda: screen(n).strip())
        dch("--send", n, "claude\r", env=env)
        paused = wait_for(lambda: status() & X.S_PAUSED, 25)
        drawn = wait_for(lambda: "Claude Code" in screen(n))
        check("primary paused: an agent pauses capture, its TUI drawn",
              paused and drawn, screen(n))
        st, txt = screen_read(n, bytes([0, 0, 0, 0, 0x08]))
        check("primary paused: F_PRIMARY gives DCH_ST_ALT and no data",
              st == 7 and txt == "", (st, txt))
        st, txt = screen_read(n, bytes([0, 0, 0, 0]))
        check("primary paused: a plain visible read still renders the TUI",
              st == 0 and "Claude Code" in txt, (st, txt))
        dch("--send", n, "/exit\r", env=env)
    finally:
        kill(n)

def test_sub_evict():
    """QA 1.43 NIT-2 (#121): with the slots full, a new subscriber takes the
    slot of the one that has left a wake unanswered past 3 s; a younger
    wake, or a caught-up subscriber, keeps its slot."""
    n = "xr-evict" + TAG
    spawn(n, "sh")
    socks = []
    try:
        run(n, "seq 1 10", "warm")
        for i in range(4):
            socks.append(X.connect(n))
            X.read(socks[i], 0, sub=True)
        send(n, "seq 1 50\n")
        time.sleep(1.0)
        for s in socks[1:]:
            h, _ = X.read(s, 0, sub=True)       # answer the wake
        new = X.connect(n)
        socks.append(new)
        h, _ = X.read(new, 0, sub=True)
        check("evict: a wake unanswered for 1 s keeps its slot",
              h["status"] & X.S_SUBFULL and not h["status"] & X.S_SUB, h)
        time.sleep(2.6)
        h, _ = X.read(new, 0, sub=True)
        check("evict: past 3 s the stale subscriber's slot goes to the new "
              "one", h["status"] & X.S_SUB, h)
        h, _ = X.read(socks[0], 0)
        check("evict: the stale one reads on, unsubscribed",
              "end" in h and not h["status"] & X.S_SUB, h)
        h, _ = X.read(socks[1], 0)
        check("evict: a caught-up subscriber keeps its slot",
              h["status"] & X.S_SUB, h)
    finally:
        for s in socks:
            s.close()
        kill(n)


def test_clear_and_off():
    n = "xr-clr" + TAG
    spawn(n, "sh")
    try:
        run(n, "seq 1 200", "c1")
        h0, recs = X.read_all(n, 0)
        check("before clear: rows captured", len(recs) > 100, len(recs))
        sub = X.connect(n)
        X.read(sub, h0["tail"], sub=True)
        check("clear message: ACK", X.clear(n))
        check("clear message: a subscriber wakes (no pty output needed)",
              recv_wakes(sub, 1.5) == 1)
        sub.close()
        h1, recs = X.read_all(n, 0)
        check("clear message: the ring is empty, epoch moves, seqs go on",
              not recs and h1["epoch"] == h0["epoch"] + 1 and
              h1["head"] == h1["tail"] == h0["tail"], (h0, h1))
        # Rows and the ED 3 in separate commands: the ring is empty and the
        # history too, so rows pushed and erased inside one feed slice (one
        # pty read; a slow master on Linux) never enter the ring and there
        # is nothing to clear: no epoch bump (CI run 42900970189).
        run(n, "seq 1 100", "c2")
        h2, recs = X.read_all(n, 0)
        check("before ED 3: rows captured", len(recs) > 50, (h1, h2))
        run(n, "printf '\\033[3J'", "c3")
        h2, recs = X.read_all(n, 0)
        check("ED 3 from the shell empties it", h2["epoch"] == h1["epoch"] + 1,
              h2)
    finally:
        kill(n)
    cfg = os.path.join(tempfile.mkdtemp(dir=os.environ.get("TMPDIR", "/tmp")),
                       "config")
    with open(cfg, "w") as f:
        f.write("xray_ring = 0\n")
    n = "xr-off" + TAG
    spawn(n, "sh", env=dict(os.environ, DCH_CONFIG=cfg))
    try:
        run(n, "seq 1 200", "o1")
        h, recs = X.read_all(n, 0)
        check("xray_ring = 0: the ring is off", h["status"] & X.S_OFF and
              not recs, h)
    finally:
        kill(n)
        shutil.rmtree(os.path.dirname(cfg), ignore_errors=True)


def test_wake_at_cap(tmp):
    """A subscriber that never reads, its out queue one 3-byte frame short
    of the 4 MiB cap, armed for a wake: pty output plus a read on that same
    connection. The wake cannot fit, so the master drops the subscriber, and
    must do it outside that client's packet handling (round 1 freed it under
    do_ring_read: a use-after-free, the session's shell gone with it). The
    frame count to the cap is measured first, then probed at k-1 .. k-6."""
    n = "xr-cap" + TAG
    trace = os.path.join(tmp, "trace-cap")
    env = dict(os.environ, DCH_DEBUG=trace)
    spawn(n, "sh", "-c", "yes %s | head -2000; exec cat" % ("y" * 60),
          env=env)

    def rd(sub=0):
        return struct.pack("<BHQIB", X.MSG_RING_READ, 13, 0, 65543, sub)
    q = X.connect(n)

    def sync():                 # the master has handled what came before
        q.sendall(struct.pack("<BH", 15, 0))
        while X.recv_frame(q)[0] != X.MSG_READ_END:
            pass
    tf = []

    def drops():                # subscribers dropped full since last call
        if not tf:
            tf.append(open(trace, "rb"))
        return len(re.findall(rb"remove client fd=\d+ attached=0 "
                              rb"outlen=4\d{6}", tf[0].read()))

    def conn():                 # subscribe, then bulk-fill to near BUSY
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
        s.settimeout(10)
        s.connect(X.sock_path(n))
        s.sendall(rd(1) + rd() * 70)
        sync()
        return s

    def done(s):
        s.close()
        time.sleep(0.1)
        sync()
        drops()
    try:
        wait_for(lambda: len(X.read_all(n, 0)[1]) > 1900)
        drops()
        s = conn()
        s.sendall(rd() * 800)
        sync()
        k = 800
        while not drops() and k < 30000:
            s.sendall(rd())
            k += 1
            sync()
        done(s)
        hits, alive = 0, True
        for back in range(1, 7):
            s = conn()
            s.sendall(rd() * (k - back))
            sync()
            if drops():
                done(s)
                continue
            try:
                s.sendall(struct.pack("<BH", 0, 120) + b"x\n" * 60 + rd())
            except OSError:
                pass
            time.sleep(0.3)
            try:
                sync()
            except (OSError, EOFError):
                alive = False       # the master died under the wake
                break
            hits += drops()
            done(s)
        check("a wake at the out cap drops its subscriber, master lives",
              k < 30000 and alive and hits >= 1, (k, hits, alive))
    finally:
        q.close()
        kill(n)


def test_seam(tmp):
    """#173 (QA 1.54.0 LOW-2): rows that scroll into the ring between its
    read and the screen read are on neither. A shell prints o/r#<i mod 1000>
    in bursts while `--xray --json` runs in a loop; each row's ref is counted
    once, so rows 0..b all there give counts non-increasing in the residue,
    at most 1 apart. Asserted on the invariant only: no answer is both
    complete and holed (partial is allowed). M stays under the pull room
    (500 in XR_ROOMS, xray.h): past it the oldest refs are evicted and come
    back with a fresh count (#262), which reads as a hole. Every 4000 rows
    the shell holds until the test has a complete answer (#283): a slow
    sanitized build re-reads past XR_SEAM_TRIES while rows scroll, so it
    only ever answered complete:false; the answers read while rows scroll
    are still counted, and checked, apart."""
    M = 400
    cfg = os.path.join(tmp, "seam.config")
    with open(cfg, "w") as f:
        f.write("xray_ring = 8M\n")     # never evicts within the run
    hold = os.path.join(tmp, "seam.hold")
    n = "xr-seam" + TAG
    spawn(n, "sh", env=dict(os.environ, DCH_CONFIG=cfg))
    reads = comp = moving = moving_comp = held = 0
    bad = []
    try:
        wait_for(lambda: screen(n).strip())
        send(n, 'i=0; while :; do echo "o/r#$((i%%%d))"; i=$((i+1)); '
             '[ $((i%%50)) = 0 ] && sleep 0.002; [ $((i%%4000)) = 0 ] && '
             '{ : > %s; while [ -e %s ]; do sleep 0.01; done; }; done\n'
             % (M, hold, hold))
        wait_for(lambda: "o/r#" in screen(n))
        end = time.time() + 12
        while time.time() < end and reads < 400:
            still = os.path.exists(hold)    # the shell holds: no new rows
            r = dch("--xray", n, "--json")
            if r.returncode != 0:
                continue
            a = json.loads(r.stdout)
            reads += 1
            moving += not still
            held += still
            if still and (a["complete"] or held >= 5):
                os.unlink(hold)             # scroll again
                held = 0
            if not a["complete"]:
                continue
            comp += 1
            moving_comp += not still
            c = [0] * M
            for e in a["refs"]:
                m = re.fullmatch(r"o/r#(\d+)", e["text"])
                if m:
                    c[int(m.group(1))] = e["count"]
            if not (all(c[k] >= c[k + 1] for k in range(M - 1)) and
                    max(c) - min(c) <= 1):
                bad.append([k for k in range(M - 1) if c[k] < c[k + 1]][:3])
    finally:
        kill(n)
    check("seam: complete answers were read (%d of %d)" % (comp, reads),
          comp > 0)
    # a sanitized build only answers complete:false while rows scroll (#283)
    slow = bool(os.environ.get("DCH_TEST_NO_BUDGET"))
    check("seam: answers were read while rows scrolled (%d, %d of them "
          "complete)" % (moving, moving_comp),
          moving > 0 and (slow or moving_comp > 0))
    check("seam: no complete answer misses rows (%d of %d did)"
          % (len(bad), comp), not bad, bad[:5])


def marks(recs):
    return [(r["text"].decode(), r["flags"]) for r in recs if r["kind"] == 4]


def test_shell_init(tmp):
    shells = []
    for sh, ok in (("zsh", ["-n"]), ("bash", ["-n"])):
        p = shutil.which(sh)
        if sh == "bash":
            # PS0 needs bash 4.4+: /bin/bash on macOS is 3.2
            for c in [p] + ["/run/current-system/sw/bin/bash",
                            "/opt/homebrew/bin/bash", "/usr/local/bin/bash"]:
                if c and os.access(c, os.X_OK):
                    v = subprocess.run([c, "-c", "echo $BASH_VERSION"],
                                       capture_output=True, text=True).stdout
                    if v[:3] >= "4.4" or v[0] > "4":
                        p = c
                        break
            else:
                p = None
        if p:
            shells.append((sh, p))
    rf = dch("--shell-init", "fish")
    check("--shell-init fish prints fish hooks",
          rf.returncode == 0 and "fish_postexec" in rf.stdout)
    r = dch("--shell-init", "tcsh")
    check("--shell-init <other>: usage, exit 2", r.returncode == 2)
    fish = shutil.which("fish")
    if fish:
        rc = subprocess.run([fish, "-n"], input=rf.stdout, text=True)
        check("fish hooks parse", rc.returncode == 0)
    else:
        print("  skip fish hooks in a real fish (no fish installed)")
    for sh, path in shells:
        init = dch("--shell-init", sh).stdout
        rc = subprocess.run([path, "-n"], input=init, text=True,
                            capture_output=True)
        check("--shell-init %s parses (%s -n)" % (sh, sh), rc.returncode == 0,
              rc.stderr)
        d = os.path.join(tmp, sh)
        os.makedirs(d, exist_ok=True)
        if sh == "zsh":
            with open(os.path.join(d, ".zshrc"), "w") as f:
                f.write("PS1='zp%% '\n" + init)
            env = dict(os.environ, ZDOTDIR=d)
            cmd = [path, "-i"]
        else:
            rcf = os.path.join(d, "rc")
            with open(rcf, "w") as f:
                # NIT-1 (#121): the hooks must hold under set -u
                f.write("set -u\nPS1='bp$ '\n" + init)
            env = dict(os.environ)
            cmd = [path, "--noprofile", "--rcfile", rcf, "-i"]
        n = "xr-%s%s" % (sh, TAG)
        spawn(n, *cmd, env=env)
        try:
            wait_for(lambda: "p" in screen(n), 5)
            # each command ENDS before the next is sent (its prompt back;
            # the echo of `seq 1 60` alone also ends in 60): a line typed
            # ahead is not the case checked here (#302)
            pr = "zp%" if sh == "zsh" else "bp$"
            prompt = "\n60\n" + pr
            send(n, "false\n")
            wait_for(lambda: screen(n).rstrip().endswith("false\n" + pr))
            send(n, "seq 1 60\n")
            wait_for(lambda: screen(n).rstrip().endswith(prompt))
            send(n, "seq 1 60\n")
            # the second's end: its rows scrolled the first's D into the
            # ring, and the prompt is back after its 60
            wait_for(lambda: "D;0" in [t for t, f in
                                       marks(X.read_all(n, 0)[1])] and
                     screen(n).rstrip().endswith(prompt))
            h, recs = X.read_all(n, 0)
            m = marks(recs)
            seqm = [t for t, f in m]
            i = seqm.index("D;1") if "D;1" in seqm else -1
            check("%s: A, B, C (input flag), D;1 for `false`" % sh,
                  i >= 3 and seqm[i - 3:i] == ["A", "B", "C"] and
                  m[i - 1][1] & X.F_INPUT and m[i][1] & X.F_EXIT, m[:12])
            # --search (#297) over the ring: the typed commands as user,
            # their rows as tool, each under its command's chapter, newest
            # first; the prompt is not searched
            r = dch("--xray", n, "--search", "SEQ 1 6", "--json")
            o = json.loads(r.stdout) if r.returncode == 0 else {}
            hs = o.get("hits", [])
            cs = [h["chapter"] for h in hs]
            check("%s: --search finds both typed commands, newest first, "
                  "each its own chapter" % sh,
                  o.get("corpus") == "shell" and
                  [(h["who"], h["tool"], h["snippet"]) for h in hs] ==
                  [("user", None, "seq 1 60")] * 2 and
                  all(c and c[0] == "s" for c in cs) and
                  int(cs[0][1:]) > int(cs[1][1:]) and
                  hs[0]["offset"] > hs[1]["offset"] and
                  o["more"] is False and o["partial"] is False, r.stdout +
                  r.stderr)
            r = dch("--xray", n, "--search", "58", "--json")
            o = json.loads(r.stdout) if r.returncode == 0 else {}
            hs = o.get("hits", [])
            # a row still on the screen, not yet in the ring, has no
            # chapter, as --ref's mentions there (xr_shell_screen)
            check("%s: --search finds each command's output row, the ring's "
                  "under its command's chapter" % sh,
                  [(h["who"], h["snippet"]) for h in hs] == [("tool", "58")] * 2
                  and hs[0]["chapter"] in (cs[0], None) and
                  hs[1]["chapter"] == cs[1] and
                  hs[0]["offset"] > hs[1]["offset"], r.stdout + r.stderr)
            r = dch("--xray", n, "--search", "zp%" if sh == "zsh" else "bp$",
                    "--json")
            hs = json.loads(r.stdout)["hits"] if r.returncode == 0 else None
            check("%s: the ring's prompt records are not searched (only the "
                  "screen's last row may hold the prompt)" % sh,
                  hs is not None and len(hs) <= 1 and
                  all(h["chapter"] is None for h in hs), r.stdout + r.stderr)
            if sh == "bash":
                bad = [r["text"] for r in recs if b"unbound" in r["text"]]
                check("bash under set -u: no unbound-variable error",
                      not bad and "unbound" not in screen(n), bad[:2])
            kinds = [(r["kind"], r["text"].decode()) for r in recs
                     if r["kind"] in (1, 2)]
            check("%s: the prompt and the typed command are told apart" % sh,
                  any(k == 2 and t.startswith("false") for k, t in kinds) and
                  any(k == 1 and t.startswith(("zp", "bp")) for k, t in kinds),
                  kinds[:6])
            typed_ahead(n, sh, pr)
        finally:
            kill(n)


def typed_ahead(n, sh, pr):
    """#302: two lines in one write, the second typed ahead (queued before
    its prompt's B, so no input reaches the pty inside its B..C): each gets
    its own chapter. Then a running program that prints a whole A,B,C block
    while a line is typed ahead: its block is no chapter (its text is not
    the typed line), and the typed line keeps its own."""
    send(n, "seq 1 71\nseq 1 82\n")
    wait_for(lambda: screen(n).rstrip().endswith("\n82\n" + pr))
    r = dch("--xray", n, "--json")
    o = json.loads(r.stdout) if r.returncode == 0 else {}
    t = [c["title"] for c in o.get("chapters", [])]
    check("%s: a line typed ahead gets its own chapter (#302)" % sh,
          t[-2:] == ["seq 1 71", "seq 1 82"], t)
    r = dch("--xray", n, "--search", "SEQ 1 ", "--json")
    o = json.loads(r.stdout) if r.returncode == 0 else {}
    hs = [(h["who"], h["snippet"], h["chapter"]) for h in o.get("hits", [])]
    cs = [c for w, s, c in hs[:2]]
    check("%s: --search: the typed-ahead line and the one before it, newest "
          "first, each under its own chapter (#302)" % sh,
          [(w, s) for w, s, c in hs[:2]] ==
          [("user", "seq 1 82"), ("user", "seq 1 71")] and
          all(c and c[0] == "s" for c in cs) and cs[0] != cs[1], hs[:4])
    # the forger prints its block after the line is typed ahead (it waits
    # for the echo of it), with no input inside the forged B..C
    send(n, "stty -echo; sleep 1; printf '\\033]133;A\\007$ \\033]133;B\\007"
         "evil\\r\\n\\033]133;C\\007out\\r\\n\\033]133;D;0\\007'; stty echo; "
         "seq 1 40\n")
    time.sleep(0.3)
    send(n, "seq 1 93\n")
    wait_for(lambda: screen(n).rstrip().endswith("\n93\n" + pr))
    r = dch("--xray", n, "--json")
    o = json.loads(r.stdout) if r.returncode == 0 else {}
    t = [c["title"] for c in o.get("chapters", [])]
    check("%s: a block printed by a running program while a line is typed "
          "ahead is no chapter; the typed line keeps its own (#302)" % sh,
          "evil" not in t and t[-1:] == ["seq 1 93"] and
          any(x.startswith("stty -echo") for x in t), t)


def test_forged_block():
    """A program that prints B..C itself, with no client input between, is
    an orphan (§11.5): its C mark has no input flag."""
    n = "xr-forge" + TAG
    prog = ("printf '\\033]133;A\\007$ \\033]133;B\\007cat notes.txt\\r\\n"
            "\\033]133;C\\007secret\\r\\n\\033]133;D;0\\007'; seq 1 60; "
            "sleep 30")
    spawn(n, "sh", "-c", prog)
    try:
        wait_for(lambda: "60" in screen(n), 5)
        h, recs = X.read_all(n, 0)
        m = marks(recs)
        c = [f for t, f in m if t.startswith("C")]
        check("forged B..C in one burst: C carries no input flag",
              c and not c[0] & X.F_INPUT, m)
    finally:
        kill(n)


def test_restart(tmp):
    n = "xr-rst" + TAG
    canary = "CANARY-%s-ringtext" % TAG
    trace = os.path.join(tmp, "trace")
    env = dict(os.environ, DCH_DEBUG=trace)
    # S5 (mutant 44): spawn with a raised soft core limit, so a master that
    # keeps it is seen by the --restart image it execs (wrap.sh records it)
    hard = resource.getrlimit(resource.RLIMIT_CORE)[1]
    soft = hard if hard != resource.RLIM_INFINITY else 1 << 30
    r = dch("--spawn", n, "--size", "80x24", "sh", env=env,
            preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE,
                                                  (soft, hard)))
    if r.returncode != 0:
        raise SystemExit("spawn %s failed: %s" % (n, r.stderr))
    corefile = os.path.join(tmp, "core-limit")
    try:
        run(n, "echo %s; seq 1 100" % canary, "r1")
        h0, r0 = X.read_all(n, 0)
        check("restart: the canary is in the ring",
              canary.encode() in b"".join(r["text"] for r in r0))
        blobcopy = os.path.join(tmp, "blob")
        wrap = os.path.join(tmp, "wrap.sh")
        with open(wrap, "w") as f:
            f.write('#!/bin/sh\ncp "$DCH_RESUME" "%s"\nulimit -c > "%s"\n'
                    'exec "%s" "$@"\n' % (blobcopy, corefile, DCH))
        os.chmod(wrap, 0o755)
        t, st = restart_raw(n, wrap)
        check("restart: ACK from the new image", t == X.MSG_ACK and
              st == 0, (t, st))
        blob = open(blobcopy, "rb").read()
        if soft:
            check("S5: the master's own soft core limit is 0 (its exec sees "
                  "0; it was spawned with %d)" % soft,
                  open(corefile).read().strip() == "0", open(corefile).read())
        else:
            print("  skip S5's raised limit (hard core limit 0 here)")
        check("10: the resume blob has no ring bytes (canary absent)",
              canary.encode() not in blob and b"ringfd " in blob and
              h0["session_id"].encode() in blob)
        h1, r1 = X.read_all(n, 0)
        check("10: --restart hands the ring over: same session_id, seqs, "
              "records", h1["session_id"] == h0["session_id"] and
              h1["head"] == h0["head"] and h1["tail"] == h0["tail"] and
              [r["text"] for r in r1] == [r["text"] for r in r0],
              (h0, h1))
        pid = master_pid(n)
        check("T10b: the new master's pid is found", pid > 0)
        objs = shm_objects(pid) if pid else None
        if pid and objs is None:
            print("  skip T10b: no /proc and no lsof to list the master's fds")
        elif pid:
            check("T10b: the new master holds no shm/memfd object",
                  objs == "", objs[-400:])
        run(n, "seq 500 600", "r2")
        h2, r2 = X.read_all(n, h1["tail"])
        check("10: capture goes on after the handover, seqs continue",
              r2 and r2[0]["seq"] == h1["tail"] and
              "500" in X.texts(r2), (h1, h2))
        # a failed handover: the blob loses its ringfd line
        bad = os.path.join(tmp, "bad.sh")
        with open(bad, "w") as f:
            f.write('#!/bin/sh\nsed -i.x "/^ringfd /d" "$DCH_RESUME"\n'
                    'exec "%s" "$@"\n' % DCH)
        os.chmod(bad, 0o755)
        t, st = restart_raw(n, bad)
        h3, r3 = X.read_all(n, h2["tail"])
        check("10: a failed handover: empty ring, new seq base, new id",
              t == X.MSG_ACK and st == 0 and
              h3["session_id"] != h0["session_id"] and
              h3["status_any"] & X.S_RANGE and
              not (h0["head"] <= h3["head"] <= h2["tail"]) and
              h3["tail"] - h3["head"] < 30, h3)
        # mutant 47's other half: the ring imports, the session_id does not
        # validate, so the imported seqs must go too (a rotate, not a keep)
        run(n, "seq 700 720", "r3")
        h3, r3 = X.read_all(n, 0)
        with open(bad, "w") as f:
            f.write('#!/bin/sh\nsed -i.x "s/^sid m/sid z/" "$DCH_RESUME"\n'
                    'exec "%s" "$@"\n' % DCH)
        t, st = restart_raw(n, bad)
        h4, r4 = X.read_all(n, h3["tail"])
        check("10: a bad handed-over session_id: the imported ring goes, "
              "new seq base, new id", t == X.MSG_ACK and st == 0 and
              r3 and h4["session_id"] != h3["session_id"] and
              not (h3["head"] <= h4["head"] <= h3["tail"]) and
              "720" not in X.texts(X.read_all(n, 0)[1]), (h3, h4))
        X.read_all(n, 0)
        tr = open(trace, "rb").read()
        check("S3 trace canary: traces carry lengths and seqs, no text",
              b"xray read from=" in tr and b"handover" in tr and
              canary.encode() not in tr)
        check("S5: the master lowers its own core limit (trace)",
              b"core limit: soft 0" in tr)
    finally:
        kill(n)


def test_restart_legacy(tmp):
    """QA 1.43 LOW-1 (#121): --restart from a pre-1.43 client lands on a
    pre-1.43 image, which knows no ringfd/core lines. The master must hand
    that image neither: no ring fd left open across the exec, and the
    child's soft core limit back. A python stand-in for the old image
    records what it was handed, then execs the real dch."""
    n = "xr-old" + TAG
    hard = resource.getrlimit(resource.RLIMIT_CORE)[1]
    soft = hard if hard != resource.RLIM_INFINITY else 1 << 30
    env = dict(os.environ)
    if env.get("ASAN_OPTIONS"):
        # ASan's own disable_coredump would lower the inherited limit (to 1
        # under a piped core_pattern) before the master saves it
        env["ASAN_OPTIONS"] += ":disable_coredump=0"
    r = dch("--spawn", n, "--size", "80x24", "sh", env=env,
            preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE,
                                                  (soft, hard)))
    if r.returncode != 0:
        raise SystemExit("spawn %s failed: %s" % (n, r.stderr))
    rec = os.path.join(tmp, "old-image.json")
    old = os.path.join(tmp, "old-dch")
    with open(old, "w") as f:
        f.write("#!%s -I\n"
                "import fcntl, json, os, resource, sys\n"
                "def kept(fd):\n"
                "    try:\n"
                "        return not fcntl.fcntl(fd, fcntl.F_GETFD) & 1\n"
                "    except OSError:\n"
                "        return False\n"
                "fds = [fd for fd in range(3, 256) if kept(fd)]\n"
                "head = open(os.environ['DCH_RESUME'], 'rb').read()\n"
                "head = head.split(b'\\nsnap ')[0].decode('latin-1')\n"
                "json.dump({'fds': fds, 'head': head, 'core':\n"
                "           resource.getrlimit(resource.RLIMIT_CORE)[0]},\n"
                "          open(%r, 'w'))\n"
                "os.execv(%r, [%r] + sys.argv[1:])\n"
                % (sys.executable, rec, DCH, DCH))
    os.chmod(old, 0o755)
    try:
        run(n, "seq 1 100", "o1")
        h0, _ = X.read_all(n, 0)
        t, st = restart_raw(n, old, caps=False)
        check("old client: ACK from the image it landed on",
              t == X.MSG_ACK and st == 0, (t, st))
        got = json.load(open(rec))
        head = got["head"].splitlines()
        carried = set()
        for l in head:
            f = l.split()
            if f and f[0] in ("lfd", "pfd", "ack", "cli"):
                carried.add(int(f[1]))
        extra = sorted(set(got["fds"]) - carried)
        check("old client: the old image inherits no fd but the carried ones"
              " (no ring handover fd)", not extra and
              not [l for l in head if l.startswith("ringfd ")],
              (extra, got["fds"], sorted(carried)))
        check("old client: no core line in the blob",
              not [l for l in head if l.startswith("core ")], head)
        if soft:
            check("old client: the old image gets the child's soft core "
                  "limit back (%d), not 0" % soft, got["core"] == soft, got)
        h1, _ = X.read_all(n, 0)
        check("old client: the session serves on (a fresh ring)",
              h1.get("session_id") and
              h1["session_id"] != h0["session_id"], (h0, h1))
        # a current client on the same session: the handover still works
        run(n, "seq 1 30", "o2")
        h2, r2 = X.read_all(n, 0)
        r = dch("--restart", n)
        h3, r3 = X.read_all(n, 0)
        check("current client: --restart still hands the ring over",
              r.returncode == 0 and h3["session_id"] == h2["session_id"] and
              h3["tail"] == h2["tail"], (r.returncode, r.stderr, h2, h3))
    finally:
        kill(n)


def test_help_clean():
    r = dch("--help")
    out = r.stdout + r.stderr
    check("help never mentions the test-only ring dump",
          "ndjson" not in out and "ring_unit" not in out and
          "--shell-init" in out)


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    tmp = tempfile.mkdtemp(prefix="xr", dir=os.environ.get("DCH_TEST_TMP", "/tmp"))
    try:
        test_read_op()
        test_subscribers()
        test_sub_evict()
        test_primary()
        test_primary_paused(tmp)
        test_clear_and_off()
        test_wake_at_cap(tmp)
        test_shell_init(tmp)
        test_seam(tmp)
        test_forged_block()
        test_restart(tmp)
        test_restart_legacy(tmp)
        test_help_clean()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if fails[0] else 0


if __name__ == "__main__":
    sys.exit(main())
