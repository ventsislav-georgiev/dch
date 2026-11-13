#!/usr/bin/env python3
"""A chord typed in time counts although the client looked late (#259).

The detach key opens a short window: a second key in it makes a chord (x the
X-ray view, the detach key again the picker), anything else ends it, and the
window running out is one press: detach. dch cannot tell when a byte arrived,
so what is already readable when the client gets to look counts as inside the
window. The test makes the client late on purpose: it reads the detach key,
the test stops it (SIGSTOP, its own child, exact pid), writes the second key,
waits past the window and continues it.

  x            the view opens over the attach, the client keeps running
  the key again  the picker opens, the client keeps running
  a letter     the letter reaches the session, then the client detaches
  nothing      control: one press, the client detaches, nothing sent

Run:  python3 tests/attach_chord_late_test.py      (uses ./dch)
"""
import errno, fcntl, os, pty, select, shutil, signal, struct, subprocess
import sys, tempfile, termios, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))
SESS = "chordlate-%d" % os.getpid()
WINDOW = 1.0    # DCH_DOUBLE_TAP_MS: room to stop the client inside it
# the session: every read it gets appended as hex to a file
RX = ("import os, sys, tty\n"
      "tty.setraw(0)\n"
      "f = open(sys.argv[1], 'a')\n"
      "os.write(1, b'READY\\r\\n')\n"
      "while True:\n"
      "    b = os.read(0, 256)\n"
      "    if not b: break\n"
      "    f.write(b.hex()); f.flush()\n")
fails = 0


def check(name, ok, detail=""):
    global fails
    if ok:
        print("  ok   " + name)
    else:
        fails += 1
        print("  FAIL %s %s" % (name, detail))


def until(cond, secs):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


class Client:
    """dch -a on a pty of its own; the test keeps the slave open too, to see
    when the client has read what was typed (FIONREAD on the slave)."""

    def __init__(self, env):
        r, w = os.pipe()
        pid, fd = pty.fork()
        if pid == 0:
            os.close(r)
            os.write(w, os.ttyname(0).encode())
            os.close(w)
            fcntl.ioctl(0, termios.TIOCSWINSZ,
                        struct.pack("HHHH", 24, 80, 0, 0))
            os.execve(DCH, [DCH, "-a", SESS], env)
            os._exit(127)
        os.set_inheritable(fd, False)
        os.close(w)
        name = b""
        while True:
            b = os.read(r, 256)
            if not b:
                break
            name += b
        os.close(r)
        self.pid, self.fd, self.out, self.st = pid, fd, b"", None
        self.slave = os.open(name, os.O_RDWR | os.O_NOCTTY)
        os.set_inheritable(self.slave, False)

    def pump(self, secs, cond=None):
        end = time.time() + secs
        while time.time() < end and not (cond and cond()):
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if r:
                try:
                    self.out += os.read(self.fd, 65536)
                except OSError as e:
                    if e.errno not in (errno.EIO, errno.EAGAIN):
                        raise
                    time.sleep(0.05)
        return bool(cond and cond())

    def wait(self, needle, secs=10, start=0):
        return self.pump(secs, lambda: needle in self.out[start:])

    def unread(self):
        b = fcntl.ioctl(self.slave, termios.FIONREAD, struct.pack("i", 0))
        return struct.unpack("i", b)[0]

    def reap(self):
        if self.st is None:
            p, st = os.waitpid(self.pid, os.WNOHANG)
            if p:
                self.st = st
        return self.st

    def exited(self, secs):
        self.pump(secs, lambda: self.reap() is not None)
        return self.reap()

    def close(self):
        if self.reap() is None:
            try:
                os.kill(self.pid, signal.SIGKILL)
                os.kill(self.pid, signal.SIGCONT)
            except OSError:
                pass
        for f in (self.fd, self.slave):
            try:
                os.close(f)
            except OSError:
                pass
        end = time.time() + 10
        while self.st is None and time.time() < end:
            self.reap()
            time.sleep(0.02)


def rx(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def late(env, rxf, what, second):
    """The detach key read, the client stopped, `second` typed, the window
    out, the client continued. Returns the client and the hex the session
    got from the detach key on."""
    c = Client(env)
    c.wait(b"READY")
    c.pump(0.3)
    got = len(rx(rxf))
    mark = len(c.out)
    os.write(c.fd, b"\x1c")
    read = until(lambda: c.unread() == 0, 5)
    time.sleep(0.1)     # not a wait: the read to set its window
    os.kill(c.pid, signal.SIGSTOP)
    # stopped for sure before the second key is typed
    stopped = until(lambda: os.waitpid(c.pid, os.WNOHANG | os.WUNTRACED)[0],
                    5)
    # past the window while stopped: the client's select times out in the
    # kernel, so on SIGCONT it is back at its deadline check with the key
    # waiting, the state a client descheduled after reading the detach key
    # comes back to (a key typed while it blocks in select wakes it instead)
    time.sleep(WINDOW + 0.5)
    if second:
        os.write(c.fd, second)
    check("%s: the client read the detach key, then stopped with the "
          "second key unread" % what,
          read and stopped and until(lambda: c.unread() == len(second), 5),
          (read, stopped, c.unread()))
    os.kill(c.pid, signal.SIGCONT)
    return c, mark, got


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    tmp = tempfile.mkdtemp(prefix="dch-chordlate-")
    rxf = os.path.join(tmp, "rx.hex")
    env = dict(os.environ, DCH_SOCKET_DIR=tmp,
               DCH_DOUBLE_TAP_MS=str(int(WINDOW * 1000)))
    env.pop("DCH_SESSION", None)
    try:
        r = subprocess.run([DCH, "--spawn", SESS, "--size", "80x24",
                            sys.executable, "-c", RX, rxf], env=env,
                           stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=30)
        check("the session spawned", r.returncode == 0, r.stderr)
        if r.returncode:
            return 1

        c, mark, got = late(env, rxf, "x late", b"x")
        try:
            up = c.wait(b"X-ray  ", start=mark) and \
                c.wait(SESS.encode(), start=mark)
            check("x late: the view opens over the attach", up,
                  c.out[mark:][-300:])
            check("x late: the client is still running", c.reap() is None,
                  c.st)
            check("x late: nothing reached the session (no detach key, "
                  "no x)", rx(rxf)[got:] == "", rx(rxf)[got:])
            if up:
                os.write(c.fd, b"q")
                c.pump(1)
                before = len(rx(rxf))
                os.write(c.fd, b"z")
                check("x late: q closes the view, a key after reaches the "
                      "session", until(lambda: rx(rxf)[before:] == "7a", 5)
                      and c.reap() is None, rx(rxf)[before:])
        finally:
            c.close()

        c, mark, got = late(env, rxf, "key again late", b"\x1c")
        try:
            check("key again late: the picker opens",
                  c.wait(b"Sessions", start=mark), c.out[mark:][-300:])
            check("key again late: the client is still running",
                  c.reap() is None, c.st)
            check("key again late: nothing reached the session",
                  rx(rxf)[got:] == "", rx(rxf)[got:])
        finally:
            c.close()

        c, mark, got = late(env, rxf, "letter late", b"y")
        try:
            st = c.exited(10)
            check("letter late: the client detaches (exit 0)",
                  st is not None and os.WIFEXITED(st) and
                  os.WEXITSTATUS(st) == 0, st)
            check("letter late: the letter reached the session, the detach "
                  "key did not", until(lambda: rx(rxf)[got:] == "79", 5),
                  rx(rxf)[got:])
        finally:
            c.close()

        c, mark, got = late(env, rxf, "nothing (control)", b"")
        try:
            st = c.exited(10)
            check("nothing (control): one press, the client detaches "
                  "(exit 0)", st is not None and os.WIFEXITED(st) and
                  os.WEXITSTATUS(st) == 0, st)
            c.pump(0.3)
            check("nothing (control): nothing reached the session",
                  rx(rxf)[got:] == "", rx(rxf)[got:])
        finally:
            c.close()
    finally:
        subprocess.run([DCH, "-k", SESS], env=env, stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=30)
        shutil.rmtree(tmp, ignore_errors=True)
    print("%s: %d check(s) failed" % ("FAIL" if fails else "PASS", fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
