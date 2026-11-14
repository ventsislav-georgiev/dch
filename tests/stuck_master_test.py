#!/usr/bin/env python3
"""A stopped master with a full listen backlog (#265). On Linux a connect to
it waited for ever, so every verb that walks the socket dir hung; now it
waits CTL_CONNECT_MS and the session reads as live but stuck, never as dead.
macOS refuses that connect at once, as it does a dead socket; a refused
socket whose master's argv still names it reads as stuck too (#268).

Checks: a real master, SIGSTOPped, its backlog filled by connects it never
accepts. --ls-json, -ls, --history, -f --restart --all and --revive --all each
return within a bound and still list the healthy session beside it. --read
and --cmd of it fail at once. The verbs name it as stuck; --spawn of its
name, --revive of it and --history --forget of it refuse and its socket and
record stay. #269: dch -a of it fails within the bound as stuck; the picker's
Enter on it stays in the picker and says why; dch -n of it says it is stuck
and starts nothing over it. On Linux, through dch serve, an attach to it
ends with that line and a 0x12 {"code":1} and leaves no client behind, and a
client attached before the stop that detaches gets "why":"detach"
(sock_live: stuck is live). Its master SIGKILLed, the same socket is dead
and --spawn of the name starts a new one. Self-isolating (own
XDG_RUNTIME_DIR).

Run:  python3 tests/stuck_master_test.py          (uses ./dch)
"""
import os, sys, json, glob, time, signal, socket, tempfile, subprocess
import shutil, pty, re, struct, fcntl, termios
from select import select as fdselect

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))
LINUX = sys.platform.startswith("linux")
BOUND = 8.0     # a verb's wall time with one stuck session; old Linux: never
STUCK = "is not accepting connections"

TMP = tempfile.mkdtemp(dir=os.environ.get("DCH_TEST_TMP", "/tmp"), prefix="dchstk")
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


def on_pty(args, keys=(), want=None, timeout=BOUND * 2):
    """dch args on a 200x50 pty: (exit status or None if killed at timeout,
    output, seconds). keys: (wait-for bytes, bytes to type) in order; once
    want shows, Ctrl-C."""
    pid, fd = pty.fork()
    if pid == 0:
        os.execve(DCH, [DCH] + list(args),
                  dict(ENV, TERM="xterm-256color", NO_COLOR="1"))
        os._exit(127)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
    t0, out, st, keys = time.time(), b"", None, list(keys)
    while time.time() - t0 < timeout:
        if fdselect([fd], [], [], 0.1)[0]:
            try:
                out += os.read(fd, 65536)
            except OSError:
                pass
        while keys and keys[0][0] in out:
            os.write(fd, keys.pop(0)[1])
        if want and want in out:
            want = None
            os.write(fd, b"\x03")
        w, s = os.waitpid(pid, os.WNOHANG)
        if w:
            st = s
            break
    dt = time.time() - t0
    if st is None:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    os.close(fd)
    return st, out.decode(errors="replace"), dt


# dch serve's tailscale: a fake owner (tests/serve_test.py's, cut down)
FAKE_TS = """#!/bin/sh
d=${0%/*}
case "$1" in
status) f=status.json ;; whois) f=whois.json ;; ip) f=ip.txt ;; *) exit 1 ;;
esac
[ -f "$d/$f" ] || exit 1
while IFS= read -r l; do printf '%s\\n' "$l"; done < "$d/$f"
printf '%s' "$l"
"""


def serve_start():
    """dch serve on 127.0.0.1, a kernel-picked port: (proc, port)."""
    fake = os.path.join(TMP, "fake")
    os.makedirs(fake)
    for n, body in (("tailscale", FAKE_TS),
                    ("status.json", json.dumps(
                        {"Self": {"ID": "1001", "UserID": 42, "Tags": []},
                         "User": {"42": {"LoginName": "me@example.com"}}})),
                    ("whois.json", json.dumps(
                        {"UserProfile": {"ID": 42,
                                         "LoginName": "me@example.com"},
                         "Node": {"ID": 2002, "StableID": "2002",
                                  "Tags": []}})),
                    ("ip.txt", "100.100.1.1\n")):
        with open(os.path.join(fake, n), "w") as f:
            f.write(body)
    os.chmod(os.path.join(fake, "tailscale"), 0o755)
    log = os.path.join(TMP, "serve.log")
    env = dict(ENV, PATH=fake + ":/usr/bin:/bin", SHELL="/bin/sh",
               DCH_SERVE_TEST_LOOPBACK="1")
    with open(log, "w") as lf:
        p = subprocess.Popen([DCH, "serve", "--bind", "127.0.0.1", "--port",
                              "0"], env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=lf)
    end = time.time() + 10
    while time.time() < end and p.poll() is None:
        with open(log) as f:
            m = re.search(r"listening on 127\.0\.0\.1:(\d+)", f.read())
        if m:
            return p, int(m.group(1))
        time.sleep(0.05)
    return p, None


def sv_attach(port, name):
    s = socket.create_connection(("127.0.0.1", port), timeout=BOUND * 2)
    body = json.dumps({"name": name, "cols": 80, "rows": 24}).encode()
    s.sendall(struct.pack(">BI", 0x01, len(body)) + body)
    return s


def sv_end(s):
    """Frames to the end: (0x10 bytes, the 0x12 as a dict or what came
    instead, EOF after it, seconds)."""
    t0, out, end = time.time(), b"", None
    try:
        while end is None:
            h = b""
            while len(h) < 5:
                c = s.recv(5 - len(h))
                if not c:
                    raise EOFError
                h += c
            t, n = struct.unpack(">BI", h)
            pl = b""
            while len(pl) < n:
                c = s.recv(n - len(pl))
                if not c:
                    raise EOFError
                pl += c
            if t == 0x10:
                out += pl
            else:
                end = json.loads(pl) if t == 0x12 else (t, pl)
        eof = s.recv(1) == b""
    except (EOFError, OSError) as e:
        end, eof = end or repr(e), False
    s.close()
    return out, end, eof, time.time() - t0


def kids(name):
    """dch serve's clients of name (Linux /proc): dch -E -f -n name."""
    pids = []
    for d in os.listdir("/proc"):
        try:
            with open("/proc/%s/cmdline" % d, "rb") as f:
                a = f.read().split(b"\0")
        except (OSError, ValueError):
            continue
        if b"-E" in a and b"-n" in a and name.encode() in a:
            pids.append(int(d))
    return pids


def clients(name):
    r, _ = dch("--ls-json")
    try:
        return {o["name"]: o for o in json.loads(r.stdout)}[name]["clients"]
    except (AttributeError, ValueError, KeyError, TypeError):
        return 0


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
    held, mpid, srv, sa = [], 0, None, None
    stk, ok = "stk%d" % os.getpid(), "fine%d" % os.getpid()
    sock = os.path.join(SOCKDIR, stk + ".sock")
    try:
        for n in (stk, ok):
            r, _ = dch("--spawn", n, "sleep", "300")
            expect(r and r.returncode == 0, "spawn %s" % n)
        recs = until(lambda: glob.glob(os.path.join(TMP, "state", "**",
                                                    stk + ".json"),
                                       recursive=True))
        if LINUX:
            # a remote client attached before the stop (#269)
            srv, port = serve_start()
            expect(port, "dch serve up on a loopback port")
            sa = port and sv_attach(port, stk)
            expect(sa and until(lambda: kids(stk)) and
                   until(lambda: clients(stk)),
                   "a serve client is attached to it")
        mpid = master_pid(sock)
        os.kill(mpid, signal.SIGSTOP)
        how = fill_backlog(sock, held)
        expect(how == ("BlockingIOError" if LINUX
                       else "ConnectionRefusedError"),
               "backlog full after %d connects (%s)" % (len(held), how))
        if recs:
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
               STUCK in r.stderr,
               "--read of it fails, says why: %r" % (r and r.stderr))
        r = timed("--cmd of it", "--cmd", stk)
        expect(r and r.returncode == 1 and
               STUCK in r.stderr,
               "--cmd of it fails, says why: %r" % (r and r.stderr))
        r = timed("--history", "--history", "--json")
        h = {}
        try:
            h = {e["name"]: e for e in json.loads(r.stdout)} if r else {}
        except ValueError:
            pass
        expect(r and stk not in h, "--history: the stuck session is live, "
               "not an ended one: %r" % h.get(stk))
        # -f: a current master is otherwise skipped without a connect
        r = timed("-f --restart --all", "-f", "--restart", "--all")
        expect(r and STUCK in r.stderr and stk in r.stderr,
               "-f --restart --all names it: %r" % (r and r.stderr))
        r = timed("--revive --all", "--revive", "--all")
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
        # #269: the attach paths
        st, out, dt = on_pty(["-a", stk])
        expect(st is not None and os.waitstatus_to_exitcode(st) == 1 and
               STUCK in out,
               "dch -a of it fails in %.1fs, says why: %r%s" %
               (dt, out[-200:], "" if st is not None else ": HUNG"))
        st, out, dt = on_pty(["-l"], keys=[(b"q quit", ("/" + stk).encode()),
                                           (stk.encode(), b"\r")],
                             want=b"not accepting connections")
        expect(st is not None and "not accepting connections" in out,
               "the picker's Enter on it stays in the picker, says why "
               "(%.1fs)%s" % (dt, "" if st is not None else ": HUNG"))
        st, out, dt = on_pty(["-n", stk])
        expect(st is not None and os.waitstatus_to_exitcode(st) == 1 and
               STUCK in out,
               "dch -n of it: says it is stuck in %.1fs, starts "
               "nothing: %r%s" % (dt, out[-200:], "" if st is not None
                                  else ": HUNG"))
        if LINUX:
            # the serve client attached before the stop detaches: its
            # session is stuck, not gone
            k = kids(stk)
            if sa and len(k) == 1:
                os.kill(k[0], signal.SIGHUP)
                _, end, eof, dt = sv_end(sa)
                expect(end == {"code": 0, "why": "detach"} and eof,
                       "serve: a client of it detached: 0x12 %r in %.1fs"
                       % (end, dt))
            else:
                expect(False, "serve: one client of it to detach: %r" % k)
            if port:
                out, end, eof, dt = sv_end(sv_attach(port, stk))
                expect(STUCK.encode() in out and end == {"code": 1} and eof
                       and dt < BOUND,
                       "serve: an attach to it ends in %.1fs with the "
                       "stuck line and 0x12 %r" % (dt, end))
                expect(until(lambda: not kids(stk), 3),
                       "serve: no client of it left behind")
        expect(os.path.exists(sock), "its socket is still there")
        expect(master_alive(mpid), "its master is still there")
        # #268: once its master is gone the same refused socket is dead
        os.kill(mpid, signal.SIGKILL)
        gone = until(lambda: not master_alive(mpid))
        mpid = 0
        r, _ = dch("--spawn", stk, "sleep", "300")
        expect(gone and r and r.returncode == 0 and master_pid(sock),
               "master SIGKILLed: its stale socket is dead, --spawn of the "
               "name starts a new one: %r" % (r and r.stderr))
    finally:
        if mpid:
            try:
                os.kill(mpid, signal.SIGCONT)
            except OSError:
                pass
        for s in held:
            s.close()
        if srv:
            srv.terminate()
            try:
                srv.wait(10)
            except subprocess.TimeoutExpired:
                srv.kill()
                srv.wait()
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
