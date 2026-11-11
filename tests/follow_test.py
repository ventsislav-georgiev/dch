#!/usr/bin/env python3
"""dch --follow <name>: the screen as a stream of self-delimited frames (#093).

  dch-frame <len> <rows>x<cols> <crow> <ccol> <cvis>\\n<len payload bytes>

Checks: the frame grammar; the first frame comes at once and is byte-for-byte
`dch --read --ansi`; output, a caret-only move, a caret hide and a resize each
produce one whole frame (size always present); an idle session produces none;
an attached client keeps working and an idle session's activity stamp does not
move; reader close, session end -> exit 0 quietly; no session -> 1; no mirror
(dch-lite or DCH_NO_VT) -> 3 like --read; a master that predates --follow -> 1
with a restart hint. Self-isolating (own XDG_RUNTIME_DIR).

Run:  python3 tests/follow_test.py          (uses ./dch)
"""
import os, sys, re, pty, time, fcntl, struct, termios, socket, select
import shutil, signal, tempfile, threading, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))
NOBUDGET = bool(os.environ.get("DCH_TEST_NO_BUDGET"))
# A frame is due within one 150 ms tick plus a render; sanitizer builds only
# have to get there eventually.
DUE = 15.0 if NOBUDGET else 1.5
HDR = re.compile(rb"^dch-frame (\d+) (\d+)x(\d+) (\d+) (\d+) ([01])\n$")

TMP = tempfile.mkdtemp()
ENV = dict(os.environ, XDG_RUNTIME_DIR=TMP, XDG_STATE_HOME=TMP + "/state",
           DCH_CONFIG=TMP + "/config", DCH_LOGIN_ENV="0", DCH_NO_REMOTE="1")
for k in ("DCH_SOCKET_DIR", "DCH_SESSION", "DCH_NO_VT"):
    ENV.pop(k, None)
SOCKDIR = os.path.join(TMP, "dch-%d" % os.getuid())
fails = []


def dch(*args, env=ENV, **kw):
    return subprocess.run([DCH] + list(args), env=env, capture_output=True,
                          timeout=30, **kw)


def expect(cond, what):
    if not cond:
        fails.append(what)
        print("FAIL:", what)
    return cond


class Follower:
    """`dch --follow` with a thread parsing frames off its stdout."""

    def __init__(self, name):
        self.p = subprocess.Popen([DCH, "--follow", name], env=ENV,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
        self.frames, self.bad = [], None
        self.cv = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        f = self.p.stdout
        while True:
            line = f.readline()
            if not line:
                break
            m = HDR.match(line)
            if not m:
                self.bad = line
                break
            n = int(m.group(1))
            body = f.read(n)
            if len(body) != n:
                self.bad = b"short payload"
                break
            fr = dict(rows=int(m.group(2)), cols=int(m.group(3)),
                      crow=int(m.group(4)), ccol=int(m.group(5)),
                      cvis=int(m.group(6)), body=body, t=time.time())
            with self.cv:
                self.frames.append(fr)
                self.cv.notify_all()
        with self.cv:
            self.cv.notify_all()

    def wait(self, pred, secs, start=None):
        """First frame from index start on (default: from now) matching
        pred. Take start = len(frames) BEFORE the action that should cause
        the frame: a slow action (sanitizer builds) can return after it."""
        if start is None:
            start = len(self.frames)
        end = time.time() + secs
        with self.cv:
            while True:
                for fr in self.frames[start:]:
                    if pred(fr):
                        return fr
                left = end - time.time()
                if left <= 0:
                    return None
                self.cv.wait(left)

    def stop(self):
        if self.p.poll() is None:
            self.p.kill()
            self.p.wait(10)


def set_size(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def drain(fd, secs, until=None):
    out, end = b"", time.time() + secs
    while time.time() < end and not (until and until in out):
        r, _, _ = select.select([fd], [], [], 0.1)
        if fd in r:
            try:
                b = os.read(fd, 65536)
            except OSError:
                break
            if not b:
                break
            out += b
    return out


def act_mtime(name):
    try:
        return os.stat(os.path.join(SOCKDIR, name + ".sock.act")).st_mtime_ns
    except OSError:
        return None


def wait_read(name, mark, secs=10):
    end = time.time() + secs
    while time.time() < end:
        if mark in dch("--read", name).stdout:
            return True
        time.sleep(0.1)
    return False


def no_mirror(name, env):
    """--follow fails exactly like --read does without a mirror: exit 3."""
    dch("--spawn", name, "sh", env=env)
    r = dch("--follow", name)
    rr = dch("--read", name, "--ansi")
    expect(r.returncode == 3 and rr.returncode == 3,
           "no mirror: --follow exits %d, --read %d (want 3, 3)"
           % (r.returncode, rr.returncode))
    expect(b"no terminal mirror" in r.stderr and r.stdout == b"",
           "no mirror: message %r / stdout %r" % (r.stderr, r.stdout))
    dch("-k", name)


def old_master(name, ignore_flags):
    """A fake master: rejects unknown request flags with DCH_ST_ERR (1.36),
    or (pre-flags) ignores them; both still serve a plain --read."""
    os.makedirs(SOCKDIR, mode=0o700, exist_ok=True)
    path = os.path.join(SOCKDIR, name + ".sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(4)
    srv.settimeout(10)

    def serve():
        try:
            for _ in range(2):
                c, _ = srv.accept()
                # the whole packet: the client may send its header and
                # payload in two writes, and closing on unread bytes
                # resets the reply away (macOS)
                req = b""
                while len(req) < 3 or len(req) < 3 + req[1] + req[2] * 256:
                    more = c.recv(64)
                    if not more:
                        break
                    req += more
                if len(req) > 7 and req[7] and not ignore_flags:
                    c.sendall(bytes([9, 1, 0]))         # READ_END ERR
                else:
                    c.sendall(bytes([8, 2, 0]) + b"$ " + bytes([9, 0, 0]))
                c.close()
        except OSError:
            pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    r = dch("--follow", name)
    expect(r.returncode == 1 and b"predates --follow" in r.stderr,
           "old master (%s): rc %d stderr %r" % (
               "ignores flags" if ignore_flags else "rejects flags",
               r.returncode, r.stderr))
    srv.close()
    os.unlink(path)


def main():
    r = dch("-h")
    expect(b"dch --follow <name>" in r.stdout + r.stderr, "--help lists --follow")
    r = dch("--follow", "nosuch")
    expect(r.returncode == 1 and b"no session" in r.stderr,
           "missing session: rc %d %r" % (r.returncode, r.stderr))
    old_master("old1", False)
    old_master("old0", True)
    if dch("--build").stdout.strip() != b"full":
        no_mirror("flite", ENV)
        return
    no_mirror("fnovt", dict(ENV, DCH_NO_VT="1"))

    name = "follow%d" % os.getpid()
    dch("--spawn", name, "--size", "40x10", "sh")
    # No job control: macOS bash can print "child setpgid ... Operation not
    # permitted" when it forks, into a frame compared byte for byte (#154).
    dch("--run", name, "set +m; echo READY")
    expect(wait_read(name, b"READY"), "session came up")
    # From idle, so a stamp by --follow would show; a "working" baseline
    # could age into "idle" mid-check on a slow box (#154).
    end = time.time() + 15
    while dch("--status", name).stdout != b"idle\n" and time.time() < end:
        time.sleep(0.2)
    act0 = act_mtime(name)
    status0 = dch("--status", name).stdout
    want = dch("--read", name, "--ansi").stdout
    t0 = time.time()
    f = Follower(name)
    try:
        first = f.wait(lambda fr: True, DUE)
        if not expect(first, "first frame within %.1fs (bad=%r)" % (DUE, f.bad)):
            return
        expect(first["t"] - t0 < DUE, "first frame immediate")
        expect((first["rows"], first["cols"]) == (10, 40),
               "header size 10x40, got %(rows)dx%(cols)d" % first)
        expect(first["body"] == want, "payload == --read --ansi stdout:\n"
               "%r\n%r" % (first["body"], want))
        expect(first["crow"] >= 1 and first["ccol"] >= 1 and
               first["cvis"] == 1, "cursor fields 1-based + visible: %r" % first)

        # idle: no frame, no activity stamp, same --status
        time.sleep(2.2)
        expect(len(f.frames) == 1, "idle session sent %d frames" % len(f.frames))
        expect(act_mtime(name) == act0, "following moved the .act stamp")
        expect(dch("--status", name).stdout == status0,
               "following changed --status")

        # output -> one frame, soon
        t1, mark = time.time(), len(f.frames)
        dch("--run", name, "echo FOLLOW-MARK")
        fr = f.wait(lambda x: b"FOLLOW-MARK\r\n" in x["body"] or
                    re.search(rb"\nFOLLOW-MARK", x["body"]), DUE, mark)
        expect(fr, "output frame within %.1fs" % DUE)
        if fr:
            print("latency: %.0f ms" % ((fr["t"] - t1) * 1000))

        # caret-only move, then caret hide: whole frames, size present
        time.sleep(0.6)
        base, mark = f.frames[-1], len(f.frames)
        dch("--run", name,
            "clear; sleep 1; printf '\\033[5;5H'; sleep 1; "
            "printf '\\033[?25l'; sleep 30")
        # the deadlines cover `sleep 1` + a fork on a loaded box, not latency
        mv = f.wait(lambda x: (x["crow"], x["ccol"]) == (5, 5), DUE + 8,
                    mark)
        if expect(mv, "caret-only move produced a frame"):
            expect((mv["rows"], mv["cols"]) == (10, 40) and mv["cvis"] == 1,
                   "caret frame carries 10x40, visible: %r" % mv)
            prev = f.frames[f.frames.index(mv) - 1]
            expect(prev["body"] == mv["body"] and
                   (prev["crow"], prev["ccol"]) != (5, 5),
                   "caret move frame differs only in the caret")
            hid = f.wait(lambda x: x["cvis"] == 0, DUE + 8,
                         f.frames.index(mv) + 1)
            expect(hid and (hid["rows"], hid["cols"]) == (10, 40) and
                   hid["body"] == mv["body"] and
                   (hid["crow"], hid["ccol"]) == (5, 5),
                   "caret hide: whole frame, same screen: %r" % hid)
        dch("--keys", name, "ctrl+c")
        dch("--run", name, "printf '\\033[?25h'; clear")
        time.sleep(0.6)
        expect(base["rows"] == 10, "sanity")

        # attached client: resize reaches the header; client undisturbed
        pid, fd = pty.fork()
        if pid == 0:
            os.environ.clear()
            os.environ.update(ENV)
            os.execv(DCH, [DCH, "-E", "-a", name])
            os._exit(127)
        try:
            mark = len(f.frames)
            set_size(fd, 60, 15)
            drain(fd, 1.0)
            fr = f.wait(lambda x: (x["rows"], x["cols"]) == (15, 60), DUE,
                        mark)
            expect(fr, "resize shows 15x60 in the header (last %dx%d)" % (
                f.frames[-1]["rows"], f.frames[-1]["cols"]))
            dch("--run", name, "echo CLIENT-MARK")
            got = drain(fd, 5.0, b"CLIENT-MARK")
            expect(b"CLIENT-MARK" in got, "attached client still gets output")
            expect(os.waitpid(pid, os.WNOHANG) == (0, 0),
                   "attached client still running")
        finally:
            try:
                os.kill(pid, signal.SIGHUP)
                os.waitpid(pid, 0)
            except OSError:
                pass

        # --restart drops control connections: follow reconnects
        mark = len(f.frames)
        r = dch("--restart", name)
        expect(r.returncode == 0, "--restart: %r" % r.stderr)
        dch("--run", name, "echo AFTER-RESTART")
        expect(f.wait(lambda x: re.search(rb"\nAFTER-RESTART", x["body"]),
                      DUE + 3, mark) and f.p.poll() is None,
               "follow survives --restart (rc %r)" % f.p.poll())

        # session end -> exit 0, quietly
        dch("--run", name, "exit")
        try:
            rc = f.p.wait(DUE + 5)
        except subprocess.TimeoutExpired:
            rc = None
        expect(rc == 0, "session end: follow exits %r (want 0)" % rc)
        expect(f.p.stderr.read() == b"", "session end: stderr quiet")
        expect(f.bad is None, "bad frame: %r" % f.bad)
    finally:
        f.stop()
        dch("-k", name)

    # reader closes the pipe on an idle session -> exit 0, no stderr
    name2 = name + "b"
    dch("--spawn", name2, "--size", "40x10", "sh")
    p = subprocess.Popen([DCH, "--follow", name2], env=ENV,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.stdout.readline()
    time.sleep(0.5)
    p.stdout.close()
    try:
        rc = p.wait(DUE + 2)
    except subprocess.TimeoutExpired:
        p.kill()
        rc = None
    expect(rc == 0 and p.stderr.read() == b"",
           "reader close: rc %r (want 0, quiet)" % rc)
    dch("-k", name2)


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    if fails:
        sys.exit(1)
    print("PASS: --follow frames, coalescing, read-only, exits")
