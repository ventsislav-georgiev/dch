#!/usr/bin/env python3
"""A stopped master with a full listen backlog (#265). On Linux a connect to
it waited for ever, so every verb that walks the socket dir hung; now it
waits CTL_CONNECT_MS and the session reads as live but stuck, never as dead.
macOS refuses that connect at once, as before.

Checks: a real master, SIGSTOPped, its backlog filled by connects it never
accepts. --ls-json, -ls, --history, -f --restart --all and --revive --all each
return within a bound and still list the healthy session beside it. --read
and --cmd of it fail at once. On Linux: the verbs name it as stuck; --spawn of its
name, --revive of it and --history --forget of it refuse and its socket and
record stay. Self-isolating (own XDG_RUNTIME_DIR).

Run:  python3 tests/stuck_master_test.py          (uses ./dch)
"""
import os, sys, json, glob, time, signal, socket, tempfile, subprocess
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))
LINUX = sys.platform.startswith("linux")
BOUND = 8.0     # a verb's wall time with one stuck session; old Linux: never
STUCK = "is not accepting connections"

TMP = tempfile.mkdtemp(dir="/tmp", prefix="dchstk")
ENV = dict(os.environ, XDG_RUNTIME_DIR=TMP, XDG_STATE_HOME=TMP + "/state",
           HOME=TMP + "/home", DCH_CONFIG=TMP + "/config",
           DCH_LOGIN_ENV="0", DCH_NO_REMOTE="1")
for k in ("DCH_SOCKET_DIR", "DCH_SESSION"):
    ENV.pop(k, None)
SOCKDIR = os.path.join(TMP, "dch-%d" % os.getuid())
fails = []


def expect(cond, what):
    print("  %s %s" % ("ok  " if cond else "FAIL", what))
    if not cond:
        fails.append(what)
    return cond


def dch(*args, timeout=30):
    t0 = time.time()
    try:
        r = subprocess.run([DCH] + list(args), env=ENV, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, timeout
    return r, time.time() - t0


def timed(what, *args):
    """A verb that must come back within BOUND: (result or None, seconds)."""
    r, dt = dch(*args, timeout=BOUND * 2)
    expect(r is not None and dt < BOUND, "%s returns in %.1fs (< %ss)%s" %
           (what, dt, BOUND, "" if r else ": HUNG"))
    return r


def master_pid(sock):
    """The listener's pid, from the kernel (the master we spawned)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock)
    if LINUX:
        pid = int.from_bytes(s.getsockopt(socket.SOL_SOCKET,
                                          socket.SO_PEERCRED, 12)[:4],
                             sys.byteorder)
    else:
        pid = int.from_bytes(s.getsockopt(0, 2, 4), sys.byteorder)  # LOCAL_PEERPID
    s.close()
    return pid


def fill_backlog(sock, held):
    """Connect until the kernel says the backlog is full: EAGAIN on Linux,
    ECONNREFUSED on macOS. The connections are kept, never accepted."""
    for _ in range(2000):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.setblocking(False)
        try:
            s.connect(sock)
        except (BlockingIOError, ConnectionRefusedError) as e:
            s.close()
            return type(e).__name__
        held.append(s)
    return None


def until(fn, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def main():
    os.makedirs(SOCKDIR, exist_ok=True)
    held, mpid = [], 0
    stk, ok = "stk%d" % os.getpid(), "fine%d" % os.getpid()
    sock = os.path.join(SOCKDIR, stk + ".sock")
    try:
        for n in (stk, ok):
            r, _ = dch("--spawn", n, "sleep", "300")
            expect(r and r.returncode == 0, "spawn %s" % n)
        recs = until(lambda: glob.glob(os.path.join(TMP, "state", "**",
                                                    stk + ".json"),
                                       recursive=True))
        mpid = master_pid(sock)
        os.kill(mpid, signal.SIGSTOP)
        how = fill_backlog(sock, held)
        expect(how == ("BlockingIOError" if LINUX
                       else "ConnectionRefusedError"),
               "backlog full after %d connects (%s)" % (len(held), how))
        if LINUX and recs:
            # long-lived and quiet for a while: --revive --all would take
            # it if it read as dead
            with open(recs[0]) as f:
                rec = json.loads(f.read())
            rec["started"] = rec["updated"] - 600
            with open(recs[0], "w") as f:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")

        r = timed("--ls-json", "--ls-json")
        rows = {}
        try:
            rows = {o["name"]: o for o in json.loads(r.stdout)} if r else {}
        except ValueError:
            pass
        expect(stk in rows and rows.get(ok, {}).get("cols"),
               "--ls-json lists the stuck session and the healthy one (cols)")
        r = timed("-ls", "-ls")
        expect(r and {stk, ok} <= set(r.stdout.split()), "-ls lists both")
        r = timed("--read of it", "--read", stk)
        expect(r and r.returncode == 1 and
               (STUCK if LINUX else "no session") in r.stderr,
               "--read of it fails, says why: %r" % (r and r.stderr))
        r = timed("--cmd of it", "--cmd", stk)
        expect(r and r.returncode == 1 and
               (STUCK if LINUX else "no session") in r.stderr,
               "--cmd of it fails, says why: %r" % (r and r.stderr))
        r = timed("--history", "--history", "--json")
        h = {}
        try:
            h = {e["name"]: e for e in json.loads(r.stdout)} if r else {}
        except ValueError:
            pass
        if LINUX:
            expect(r and stk not in h, "--history: the stuck session is live, "
                   "not an ended one: %r" % h.get(stk))
        # -f: a current master is otherwise skipped without a connect
        r = timed("-f --restart --all", "-f", "--restart", "--all")
        if LINUX:
            expect(r and STUCK in r.stderr and stk in r.stderr,
                   "-f --restart --all names it: %r" % (r and r.stderr))
        r = timed("--revive --all", "--revive", "--all")
        if LINUX:
            expect(r and stk not in r.stdout,
                   "--revive --all leaves it: %r" % (r and r.stdout))
            r = timed("--revive of it", "--revive", stk)
            expect(r and r.returncode == 1 and "is live" in r.stderr,
                   "--revive of it: refused as live: %r" % (r and r.stderr))
            r = timed("--forget of it", "--history", "--forget", stk)
            expect(r and r.returncode != 0 and recs and
                   os.path.exists(recs[0]),
                   "--history --forget of it: refused, record kept")
            r = timed("--spawn of its name", "--spawn", stk, "sh")
            expect(r and r.returncode == 1 and STUCK in r.stderr,
                   "--spawn of its name: refused: %r" % (r and r.stderr))
        expect(os.path.exists(sock), "its socket is still there")
        expect(master_alive(mpid), "its master is still there")
    finally:
        if mpid:
            try:
                os.kill(mpid, signal.SIGCONT)
            except OSError:
                pass
        for s in held:
            s.close()
        for n in (stk, ok):
            dch("-k", n, timeout=10)
        shutil.rmtree(TMP, ignore_errors=True)
    print("FAIL: %d check(s)" % len(fails) if fails else "all ok")
    return 1 if fails else 0


def master_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


if __name__ == "__main__":
    sys.exit(main())
