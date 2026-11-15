#!/usr/bin/env python3
"""The X-ray view over a remote attach answers the program's start-up
queries (#339, QA 1.65.0 LOW-V25).

A real `dch serve` runs on 127.0.0.1 (DCH_SERVE_TEST_LOOPBACK=1, --bind
127.0.0.1 --port 0, a fake `tailscale` first on PATH), the host's sessions in
their own dir, the client in another that reaches it only through
remote_hosts. The session runs a probe that, on each go file, writes
query_answer_test.py's four queries (DECRQM 2026, XTVERSION, DA1, CPR) to its
tty and keeps what comes back in 2 s; between rounds it logs the keys it
reads. The test's terminal answers all four, as a terminal would.
  (b) `dch -a srv:name` on a pty: with no view the terminal is asked and
      answers; with the chord's view up (Ctrl-\\ x) the relay sends 0x35, the
      host's master answers (DECRQM 2026 and XTVERSION) and the terminal is
      never asked; after q keys reach the program and the queries go to the
      terminal again.
  (w) the frame on a raw connection: 0x35 before any attach is ignored (no
      reply, the next request answered); on an attached connection an
      unknown type (0x36, empty and with a body), a 0x35 with a body and a
      0x07 with no 0x35 before it change nothing (the terminal is asked); a
      flood of 0x35 makes the master answer, the stream stays up (a key
      reaches the program) and a 0x07 ends it; an attach and a 0x35 in
      one write: the host's dch client lives and the master answers (QA
      1.67.0 LOW-A5).
dch-lite has no mirror: under the view nothing answers, as locally.
Old peers: DCH_HOST=<dch> runs serve and the host's sessions from another
binary, DCH_OLD=host or DCH_OLD=client says which side is the old one (then
the view gets no answers, as before; with DCH_OLD=host the 0x35 flood is
ignored, the stream stays up).

Run:  python3 tests/remote_view_query_test.py            (uses ./dch)
"""
import fcntl, json, os, pty, re, select, shutil, signal, socket, struct
import subprocess, sys, tempfile, termios, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from remote_client_test import FAKE_TS, OWNER, WHOIS  # noqa: E402

DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
HOST = os.path.abspath(os.environ.get("DCH_HOST", DCH))
OLD = os.environ.get("DCH_OLD", "")
QUERIES = b"\033[?2026$p\033[>q\033[c\033[6n"
OUTER = {b"\033[?2026$p": b"\033[?2026;1$y", b"\033[>q": b"\033P>|outer\033\\",
         b"\033[c": b"\033[?62;22c", b"\033[6n": b"\033[9;9R"}
ANSWERS = b"".join(OUTER.values())
RESTORE = b"\033[?1049l"
fails = [0]


def check(msg, cond, detail=""):
    print("  %s %s" % ("ok  " if cond else "FAIL", msg))
    if not cond:
        fails[0] += 1
        if detail:
            print("       " + repr(detail)[-900:])


T = os.path.realpath(tempfile.mkdtemp(prefix="rvq", dir=os.environ.get(
    "DCH_TEST_TMP", "/tmp")))
FAKE = os.path.join(T, "fake")
os.makedirs(FAKE)
with open(os.path.join(FAKE, "tailscale"), "w") as f:
    f.write(FAKE_TS)
os.chmod(os.path.join(FAKE, "tailscale"), 0o755)
for nm, o in (("status.json", OWNER), ("whois.json", WHOIS)):
    with open(os.path.join(FAKE, nm), "w") as f:
        f.write(json.dumps(o))
BASE = {k: v for k, v in os.environ.items()
        if not k.startswith(("DCH_", "Malloc", "CLAUDE", "NO_COLOR",
                             "COLORTERM", "TERM_PROGRAM"))}


def side(name):
    d = os.path.join(T, name)
    os.makedirs(os.path.join(d, "home"))
    return dict(BASE, XDG_RUNTIME_DIR=d, XDG_STATE_HOME=os.path.join(d, "st"),
                HOME=os.path.join(d, "home"), DCH_LOGIN_ENV="0",
                DCH_CONFIG=os.path.join(d, "config"), SHELL="/bin/sh",
                PATH=FAKE + ":/usr/bin:/bin", TERM="xterm-256color",
                NO_COLOR="1")


SENV = side("srv")
CENV = side("cli")
SLOG = os.path.join(T, "serve.log")
PROBE = os.path.join(T, "probe.py")
with open(PROBE, "w") as f:
    f.write('''import os, select, sys, tty, time
d = sys.argv[1]
fd = os.open("/dev/tty", os.O_RDWR)
tty.setraw(fd)
os.write(fd, b"PROBE READY\\r\\n")
i = 1
while True:
    if os.path.exists(os.path.join(d, "go%%d" %% i)):
        os.write(fd, %r)
        got, end = b"", time.time() + 2.0
        while time.time() < end:
            if select.select([fd], [], [], max(0, end - time.time()))[0]:
                got += os.read(fd, 4096)
        p = os.path.join(d, "out%%d" %% i)
        with open(p + ".t", "wb") as f:
            f.write(got)
        os.rename(p + ".t", p)
        i += 1
    elif select.select([fd], [], [], 0.05)[0]:
        with open(os.path.join(d, "keys"), "ab") as f:
            f.write(os.read(fd, 4096))
''' % QUERIES)


class Pty:
    """dch on a pty, 30x100: out holds every byte it wrote."""

    def __init__(self, argv):
        self.out, self.rc = b"", None
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ,
                        struct.pack("HHHH", 30, 100, 0, 0))
            os.execve(argv[0], argv, CENV)
        os.set_inheritable(self.fd, False)

    def pump(self, secs):
        end = time.time() + secs
        while time.time() < end:
            if not select.select([self.fd], [], [],
                                 max(0, end - time.time()))[0]:
                return
            try:
                b = os.read(self.fd, 65536)
            except OSError:
                b = b""
            if not b:
                return
            self.out += b

    def close(self):
        # the fd first: a killed pty leader with output unread on macOS
        # cannot finish exiting
        os.close(self.fd)
        os.kill(self.pid, signal.SIGKILL)
        os.waitpid(self.pid, 0)


def hdch(*a):
    return subprocess.run([HOST] + list(a), capture_output=True, text=True,
                          timeout=30, env=SENV, stdin=subprocess.DEVNULL)


def probe(name):
    d = os.path.join(T, "p-" + name)
    os.makedirs(d)
    r = hdch("--spawn", name, "--size", "80x24", sys.executable, PROBE, d)
    check("host: the probe session %s spawned" % name, r.returncode == 0,
          r.stderr)
    return d


def ask(d, i, pump, secs=15.0):
    """Round i: start it and pump the terminal until the probe's result."""
    open(os.path.join(d, "go%d" % i), "w").close()
    out, end = os.path.join(d, "out%d" % i), time.time() + secs
    while not os.path.exists(out) and time.time() < end:
        pump(0.1)
    if not os.path.exists(out):
        return None
    with open(out, "rb") as f:
        return f.read()


def keys(d):
    p = os.path.join(d, "keys")
    return open(p, "rb").read() if os.path.exists(p) else b""


def until(f, secs):
    end = time.time() + secs
    while not f() and time.time() < end:
        time.sleep(0.05)
    return f()


def up(d, write, pump):
    """lite replays no screen, so what the attach prints at its start says
    nothing about the session: the attach is up when a typed key reaches the
    program. A key typed before that can be lost: one a second until the
    first arrives, then the ones behind it are waited out."""
    end, n = time.time() + 60, 0
    while b"u" not in keys(d) and time.time() < end:
        write(b"u")
        n += 1
        until(lambda: (pump(0.1), b"u" in keys(d))[1], 1.0)
    until(lambda: (pump(0.1), keys(d).count(b"u") >= n)[1], 2.0)
    return b"u" in keys(d)


class Term:
    """Answers the four queries in what it is shown, each time once."""

    def __init__(self, write):
        self.write, self.seen, self.done = write, b"", dict.fromkeys(OUTER, 0)

    def show(self, b):
        self.seen += b
        for q, a in OUTER.items():
            while self.seen.count(q) > self.done[q]:
                self.done[q] += 1
                self.write(a)

    def asked(self, mark):
        return [q for q in OUTER if q in self.seen[mark:]]


def serve():
    with open(SLOG, "w") as lf:
        sp = subprocess.Popen([HOST, "serve", "--bind", "127.0.0.1",
                               "--port", "0"],
                              env=dict(SENV, DCH_SERVE_TEST_LOOPBACK="1"),
                              stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=lf)
    end = time.time() + 30
    while time.time() < end and sp.poll() is None:
        m = re.search(r"listening on 127\.0\.0\.1:(\d+)", open(SLOG).read())
        if m:
            return sp, int(m.group(1))
        time.sleep(0.05)
    return sp, None


# ---- (b) dch -a srv:name, the chord's view over it -----------------------

def test_client(mirror):
    # lite replays nothing on attach: wait on the focus mode it sets instead
    ready = b"PROBE READY" if mirror else b"\033[?1004h"
    d = probe("rvq-b")
    a = Pty([DCH, "-a", "srv:rvq-b"])
    term = Term(lambda b: os.write(a.fd, b))

    def pump(secs):
        n = len(a.out)
        a.pump(secs)
        term.show(a.out[n:])
    try:
        until(lambda: (pump(0.1), ready in a.out)[1], 15)
        check("(b) the remote attach shows the probe" if mirror else
              "(b) lite: the remote attach is up (no replay of the screen)",
              ready in a.out and
              (mirror or up(d, lambda k: os.write(a.fd, k), pump)),
              a.out[-300:])
        mark = len(term.seen)
        got = ask(d, 1, pump)
        check("(b) no view: the terminal is asked all four",
              term.asked(mark) == list(OUTER), term.asked(mark))
        check("(b) no view: the terminal's answers reach the program",
              got is not None and all(v in got for v in OUTER.values()), got)
        mark = len(a.out)
        os.write(a.fd, b"\x1cx")
        until(lambda: (pump(0.1), b"X-ray  " in a.out[mark:])[1], 20)
        check("(b) the chord's X-ray view opens over the remote attach",
              b"X-ray  " in a.out[mark:], a.out[mark:][-300:])
        pump(1.0)   # the 0x35's way to the host's master
        mark = len(term.seen)
        got = ask(d, 2, pump)
        want = b"" if OLD else mirror
        check("(b) the view up: the terminal is not asked",
              term.asked(mark) == [], term.asked(mark))
        check("(b) the view up: %s" % (
              "an old peer: no answers, as before" if OLD else
              "the host's mirror answers, once" if mirror else
              "lite, nothing answers"), got == want, got)
        mark = len(a.out)
        os.write(a.fd, b"q")
        until(lambda: (pump(0.1), RESTORE in a.out[mark:])[1], 10)
        pump(1.0)   # the 0x07's re-attach
        check("(b) q closes the view", RESTORE in a.out[mark:],
              a.out[mark:][-300:])
        os.write(a.fd, b"after-view")
        until(lambda: (pump(0.1), b"after-view" in keys(d))[1], 10)
        check("(b) after the view: keys reach the program",
              b"after-view" in keys(d), keys(d))
        mark = len(term.seen)
        got = ask(d, 3, pump)
        check("(b) after the view: the terminal is asked again, its answers "
              "reach the program", term.asked(mark) == list(OUTER) and
              got is not None and all(v in got for v in OUTER.values()),
              (term.asked(mark), got))
    finally:
        a.close()


# ---- (w) the frame on a raw connection ------------------------------------

def frame(t, payload=b""):
    if isinstance(payload, dict):
        payload = json.dumps(payload).encode()
    return struct.pack(">BI", t, len(payload)) + payload


class Raw:
    """A remote-frames client: its 0x10 bytes go to a Term."""

    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.buf, self.frames = b"", []
        self.term = Term(lambda b: self.s.sendall(frame(0x10, b)))
        self.s.sendall(frame(0x08, {"proto": "1.0"}))

    def pump(self, secs):
        end = time.time() + secs
        while time.time() < end:
            if not select.select([self.s], [], [], max(0, end - time.time()))[0]:
                break
            try:
                b = self.s.recv(65536)
            except OSError:
                b = b""
            if not b:
                self.frames.append(None)
                return
            self.buf += b
            while len(self.buf) >= 5:
                t, n = struct.unpack(">BI", self.buf[:5])
                if len(self.buf) < 5 + n:
                    break
                p, self.buf = self.buf[5:5 + n], self.buf[5 + n:]
                self.frames.append(t)
                if t == 0x10:
                    self.term.show(p)

    def close(self):
        self.s.close()


def test_wire(port, mirror):
    r = Raw(port)
    try:
        r.pump(1.0)
        r.s.sendall(frame(0x35))
        r.pump(0.5)
        n = len(r.frames)
        r.s.sendall(frame(0x03))
        until(lambda: (r.pump(0.1), 0x11 in r.frames[n:])[1], 10)
        check("(w) 0x35 before any attach: no reply, the connection stays "
              "and the next request is answered",
              r.frames[:n] == [0x18] and r.frames[n:n + 1] == [0x11],
              r.frames)
    finally:
        r.close()

    d = probe("rvq-w")
    ready = b"PROBE READY" if mirror else b"\033[?1004h"
    r = Raw(port)
    try:
        r.s.sendall(frame(0x01, {"name": "rvq-w", "cols": 80, "rows": 24}))
        until(lambda: (r.pump(0.1), ready in r.term.seen)[1], 15)
        check("(w) the raw attach shows the probe" if mirror else
              "(w) lite: the raw attach is up (no replay of the screen)",
              ready in r.term.seen and
              (mirror or up(d, lambda k: r.s.sendall(frame(0x10, k)),
                            r.pump)), r.term.seen[-300:])
        r.s.sendall(frame(0x36) + frame(0x36, b"x" * 100) +
                    frame(0x35, b"{}"))
        r.pump(1.0)
        mark = len(r.term.seen)
        got = ask(d, 1, r.pump)
        check("(w) an unknown 0x36 (empty, with a body) and a 0x35 with a "
              "body on an attached connection: ignored, the terminal is "
              "asked and answers", r.term.asked(mark) == list(OUTER) and
              got == ANSWERS and None not in r.frames,
              (r.term.asked(mark), got, r.frames[-5:]))
        r.s.sendall(frame(0x07))
        r.pump(1.0)
        mark = len(r.term.seen)
        got = ask(d, 2, r.pump)
        check("(w) a 0x07 with no 0x35 before it (a plain redraw): the "
              "terminal is still asked, as before", r.term.asked(mark) ==
              list(OUTER) and got == ANSWERS, (r.term.asked(mark), got))
        r.s.sendall(frame(0x35) * 2000)
        r.pump(1.0)
        mark = len(r.term.seen)
        got = ask(d, 3, r.pump)
        old = OLD == "host"
        check("(w) a flood of 2000 0x35: %s" % (
              "an old host ignores them, the terminal is asked" if old else
              "the terminal is not asked, the host's master answers"),
              (r.term.asked(mark) == list(OUTER) and got == ANSWERS) if old
              else (r.term.asked(mark) == [] and got == mirror),
              (r.term.asked(mark), got))
        r.s.sendall(frame(0x10, b"under-flood"))
        until(lambda: (r.pump(0.1), b"under-flood" in keys(d))[1], 10)
        check("(w) after the flood the stream is up: a key reaches the "
              "program", b"under-flood" in keys(d) and None not in r.frames,
              (keys(d), r.frames[-5:]))
        r.s.sendall(frame(0x07))
        r.pump(1.0)
        mark = len(r.term.seen)
        got = ask(d, 4, r.pump)
        check("(w) the 0x07 after them: the terminal is asked again",
              r.term.asked(mark) == list(OUTER) and got == ANSWERS,
              (r.term.asked(mark), got))
    finally:
        r.close()

    # QA 1.67.0 LOW-A5: the 0x35 in the attach's own write reaches the kid
    # before its handler. It must not kill it, and the view-up must not be
    # lost: rounds until the master answers (the base never does).
    r = Raw(port)
    try:
        r.s.sendall(frame(0x01, {"name": "rvq-w", "cols": 80, "rows": 24}) +
                    frame(0x35))
        until(lambda: (r.pump(0.1), ready in r.term.seen or
                       None in r.frames)[1], 15)
        old, i, end = OLD == "host", 5, time.time() + 40
        mark, got = len(r.term.seen), None
        while None not in r.frames and time.time() < end:
            mark = len(r.term.seen)
            got = ask(d, i, r.pump)
            i += 1
            if old or (r.term.asked(mark) == [] and got == mirror):
                break
        check("(w) an attach and a 0x35 in one write: the host's dch client "
              "lives, %s" % ("an old host ignores it, the terminal is asked"
                             if old else "the host's master answers"),
              None not in r.frames and 0x12 not in r.frames and
              ((r.term.asked(mark) == list(OUTER) and got == ANSWERS) if old
               else (r.term.asked(mark) == [] and got == mirror)),
              (r.term.asked(mark), got, r.frames[-5:]))
    finally:
        r.close()


def main():
    build = hdch("--build").stdout.strip()
    ver = hdch("-V").stdout.split()[1]
    mirror = (b"\033[?2026;2$y\033P>|dch " + ver.encode() + b"\033\\"
              if build == "full" else b"")
    sp, port = serve()
    try:
        if port is None:
            check("dch serve listens on a loopback test port", False,
                  open(SLOG).read()[-600:])
            return 1
        check("dch serve listens on a loopback test port", True)
        with open(CENV["DCH_CONFIG"], "w") as f:
            f.write("remote_hosts = srv=127.0.0.1:%d\n" % port)
        test_client(mirror)
        test_wire(port, mirror)
        check("dch serve is still up", sp.poll() is None)
    finally:
        for n in ("rvq-b", "rvq-w"):
            hdch("-k", n)
        sp.terminate()
        try:
            sp.wait(10)
        except subprocess.TimeoutExpired:
            sp.kill()
            sp.wait()
        shutil.rmtree(T, ignore_errors=True)
    print("PASS" if not fails[0] else "FAILED: %d" % fails[0])
    return 1 if fails[0] else 0


if __name__ == "__main__":
    sys.exit(main())
