#!/usr/bin/env python3
"""The X-ray shell ring (#119) through a real master.

Covers what tests/ring_unit.c cannot reach from inside vt.c: the master
protocol's read op (MSG_RING_READ: range, the 1 MiB cap, session_id),
subscribers and their edge-triggered wake (a clear wakes one too; a wake at a
full subscriber's out cap drops it safely), the clear message, the xray_ring
config (0 = off), the client-input flag on a C mark from real keystrokes,
`dch --shell-init` in real zsh and bash, the --restart handover (the ring and
session_id survive, the resume blob carries no ring bytes, no shm object is
left open; a failed handover rotates to an empty ring with a new seq base and
session_id), the trace canary (S3: no ring text in DCH_DEBUG traces) and the
master's core-dump limit.

Run:  python3 tests/xray_ring_test.py       (uses ./dch; run.sh isolates it)
"""
import os, re, shutil, socket, struct, subprocess, sys, tempfile, time

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
        run(n, "seq 1 100; printf '\\033[3J'", "c2")
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
                f.write("PS1='bp$ '\n" + init)
            env = dict(os.environ)
            cmd = [path, "--noprofile", "--rcfile", rcf, "-i"]
        n = "xr-%s%s" % (sh, TAG)
        spawn(n, *cmd, env=env)
        try:
            wait_for(lambda: "p" in screen(n), 5)
            send(n, "false\n")
            time.sleep(0.5)
            send(n, "seq 1 60\n")
            wait_for(lambda: "\n60\n" in screen(n) or
                     screen(n).rstrip().endswith("60"), 5)
            send(n, "seq 1 60\n")
            time.sleep(1.0)
            h, recs = X.read_all(n, 0)
            m = marks(recs)
            seqm = [t for t, f in m]
            i = seqm.index("D;1") if "D;1" in seqm else -1
            check("%s: A, B, C (input flag), D;1 for `false`" % sh,
                  i >= 3 and seqm[i - 3:i] == ["A", "B", "C"] and
                  m[i - 1][1] & X.F_INPUT and m[i][1] & X.F_EXIT, m[:12])
            kinds = [(r["kind"], r["text"].decode()) for r in recs
                     if r["kind"] in (1, 2)]
            check("%s: the prompt and the typed command are told apart" % sh,
                  any(k == 2 and t.startswith("false") for k, t in kinds) and
                  any(k == 1 and t.startswith(("zp", "bp")) for k, t in kinds),
                  kinds[:6])
        finally:
            kill(n)


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
    spawn(n, "sh", env=env)
    try:
        run(n, "echo %s; seq 1 100" % canary, "r1")
        h0, r0 = X.read_all(n, 0)
        check("restart: the canary is in the ring",
              canary.encode() in b"".join(r["text"] for r in r0))
        blobcopy = os.path.join(tmp, "blob")
        wrap = os.path.join(tmp, "wrap.sh")
        with open(wrap, "w") as f:
            f.write('#!/bin/sh\ncp "$DCH_RESUME" "%s"\nexec "%s" "$@"\n'
                    % (blobcopy, DCH))
        os.chmod(wrap, 0o755)
        s = X.connect(n)
        try:
            s.sendall(bytes([13, len(wrap) & 255, len(wrap) >> 8]) +
                      wrap.encode())
            t, st, _ = X.recv_frame(s)
            check("restart: ACK from the new image", t == X.MSG_ACK and
                  st == 0, (t, st))
        finally:
            s.close()
        blob = open(blobcopy, "rb").read()
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
        s = X.connect(n)
        try:
            s.sendall(bytes([13, len(bad) & 255, len(bad) >> 8]) +
                      bad.encode())
            t, st, _ = X.recv_frame(s)
        finally:
            s.close()
        h3, r3 = X.read_all(n, h2["tail"])
        check("10: a failed handover: empty ring, new seq base, new id",
              t == X.MSG_ACK and st == 0 and
              h3["session_id"] != h0["session_id"] and
              h3["status_any"] & X.S_RANGE and
              not (h0["head"] <= h3["head"] <= h2["tail"]) and
              h3["tail"] - h3["head"] < 30, h3)
        X.read_all(n, 0)
        tr = open(trace, "rb").read()
        check("S3 trace canary: traces carry lengths and seqs, no text",
              b"xray read from=" in tr and b"handover" in tr and
              canary.encode() not in tr)
        check("S5: the master lowers its own core limit (trace)",
              b"core limit: soft 0" in tr)
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
    tmp = tempfile.mkdtemp(prefix="xr", dir="/tmp")
    try:
        test_read_op()
        test_subscribers()
        test_clear_and_off()
        test_wake_at_cap(tmp)
        test_shell_init(tmp)
        test_forged_block()
        test_restart(tmp)
        test_help_clean()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if fails[0] else 0


if __name__ == "__main__":
    sys.exit(main())
