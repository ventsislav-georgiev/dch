#!/usr/bin/env python3
"""Size follows focus (#100): the focused client owns the session's size.

Two clients of one session at different sizes. The attach client turns DEC
1004 on its own terminal on; a focus-in (ESC [ I) from that terminal re-sends
its size, so the session takes it back from the other client. Focus reports
reach the inner program only while it has 1004 on itself, and every other
byte passes through exactly.

  1. A attaching enables 1004 on its terminal;
  2. focus-in on A takes the size back from B (a WINCH in the program);
  3. a program without 1004 never sees ESC[I / ESC[O;
  4. keys around a report pass byte-exact; a report split across two
     writes still counts and leaks nothing; Esc then a key 20 ms later
     reaches the program as two reads, not one Meta-key (#107);
  5. after the program prints ESC[?1004h it gets the reports, also on a
     client attaching later (the master's mode re-arm); its ESC[?1004l
     turns them off again and the client turns its own 1004 back on;
  6. DCH_FOCUS_RESIZE=0: no 1004 from the client, the bytes go through
     untouched and nothing is resized; DCH_FOCUS_RESIZE=off and
     focus_resize = off in the config (DCH_CONFIG) turn the 1004 off too.

Run:  python3 tests/focus_test.py           (uses ./dch)
"""
import os, sys, pty, fcntl, termios, struct, signal, select, threading, time
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))
SESS = "focustest-%d" % os.getpid()
T = 30.0 if os.environ.get("DCH_TEST_NO_BUDGET") else 10.0

# The inner program: raw no-echo; KEY:<hex> per read, WINCH:<cols>x<rows> per
# SIGWINCH; the key E prints ESC[?1004h, D prints ESC[?1004l. It waits in
# select on stdin AND a set_wakeup_fd pipe: a SIGWINCH landing between one
# os.read and the next would otherwise leave its handler pending until the
# next key (a key plus a resize in one burst lost the WINCH line).
TUI = r'''
import os, sys, termios, signal, struct, fcntl, select
fd = 0
wr, ww = os.pipe()
os.set_blocking(ww, False)
signal.set_wakeup_fd(ww)
a = termios.tcgetattr(fd)
a[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
a[6][termios.VMIN] = 1
a[6][termios.VTIME] = 0
termios.tcsetattr(fd, termios.TCSANOW, a)
def winch(*_):
    r, c, _, _ = struct.unpack("HHHH", fcntl.ioctl(1, termios.TIOCGWINSZ, b"\0" * 8))
    os.write(1, b"WINCH:%dx%d\r\n" % (c, r))
signal.signal(signal.SIGWINCH, winch)
os.write(1, b"READY\r\n")
while True:
    r, _, _ = select.select([fd, wr], [], [])
    if wr in r:
        os.read(wr, 64)
    if fd not in r:
        continue
    d = os.read(fd, 256)
    if not d:
        break
    os.write(1, b"KEY:%s\r\n" % d.hex().encode())
    if d == b"E":
        os.write(1, b"\033[?1004h")
    if d == b"D":
        os.write(1, b"\033[?1004;2004l")
'''


class Client:
    def __init__(self, args, cols, rows, env=None):
        self.out = b""
        self.lock = threading.Lock()
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.environ.pop("DCH_SESSION", None)
            os.environ.update(env or {})
            os.execv(DCH, [DCH, "-E"] + args)
            os._exit(127)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0))
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self):
        while True:
            try:
                d = os.read(self.fd, 65536)
            except OSError:
                return
            if not d:
                return
            with self.lock:
                self.out += d

    def mark(self):
        with self.lock:
            return len(self.out)

    def since(self, m):
        with self.lock:
            return self.out[m:]

    def wait(self, pat, m, t=T):
        """pat in the output after mark m (taken BEFORE the action)."""
        end = time.time() + t
        while time.time() < end:
            if pat in self.since(m):
                return True
            time.sleep(0.02)
        return False

    def wait_keys(self, want, m, t=T):
        """The program read exactly `want` since mark m (reads may merge)."""
        end = time.time() + t
        while time.time() < end and len(keys(self.since(m))) < len(want):
            time.sleep(0.02)
        time.sleep(0.2)	# and nothing more after it
        return keys(self.since(m)) == want

    def send(self, b):
        os.write(self.fd, b)

    def kill(self):
        try:
            os.kill(self.pid, signal.SIGHUP)
            os.waitpid(self.pid, 0)
        except OSError:
            pass


def reads(out):
    """The program's KEY reports in out, one byte string per read."""
    return [bytes.fromhex(l[l.find(b"KEY:") + 4:].strip().decode())
            for l in out.split(b"\r\n") if b"KEY:" in l]


def keys(out):
    """The program's KEY reports in out, as one byte string."""
    got = b""
    for line in out.split(b"\r\n"):
        i = line.find(b"KEY:")
        if i >= 0:
            got += bytes.fromhex(line[i + 4:].decode())
    return got


fails = []


def check(name, ok, *ctx):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        fails.append(name)
        for c in ctx:
            print("       " + repr(c)[-600:])


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    subprocess.run([DCH, "-k", SESS], capture_output=True)
    cl = []
    try:
        a = Client(["-n", SESS, sys.executable, "-c", TUI], 100, 30)
        cl.append(a)
        if not a.wait(b"READY", 0, 15.0):
            print("FAIL: no READY from the program:", a.since(0))
            return 1
        check("1. attaching turns 1004 on in the client's terminal",
              b"\033[?1004h" in a.since(0), a.since(0))

        m = a.mark()
        b = Client(["-f", "-a", SESS], 80, 24)
        cl.append(b)
        check("B attaching sizes the session to B (80x24)",
              a.wait(b"WINCH:80x24", m), a.since(m))

        # 2 + 3: focus-in on A takes the size back; nothing typed.
        m = a.mark()
        a.send(b"\033[I")
        check("2. focus-in on A: the session takes A's size back (100x30)",
              a.wait(b"WINCH:100x30", m), a.since(m))
        b.send(b"\033[O")
        m = a.mark()
        b.send(b"\033[I")
        check("2. focus-in on B: back to 80x24", a.wait(b"WINCH:80x24", m),
              a.since(m))

        # 4: keys around a report, then a report split across two writes.
        m = a.mark()
        a.send(b"x\033[O\033[Iy")
        ok = a.wait(b"WINCH:100x30", m) and a.wait_keys(b"xy", m)
        check("4. keys around reports pass byte-exact, the report acts",
              ok, a.since(m))
        m = a.mark()
        b.send(b"\033[I")
        a.wait(b"WINCH:80x24", m)
        m = a.mark()
        a.send(b"\033")
        time.sleep(0.001)	# inside the hold (FOCUS_HOLD_MS)
        a.send(b"[I")
        ok = a.wait(b"WINCH:100x30", m)
        a.send(b"z")
        ok = a.wait_keys(b"z", m) and ok
        check("4. a report split across writes: acted on, nothing typed",
              ok, a.since(m))
        # A lone Esc still arrives (after the hold), alone.
        m = a.mark()
        a.send(b"\033")
        check("4. a lone Esc goes through", a.wait_keys(b"\033", m),
              a.since(m))
        # Esc, then j a beat later (#107): the held Esc goes out on its own
        # before j comes, as with DCH_FOCUS_RESIZE=0 — not glued to it as
        # Meta-j. A terminal's own Alt-j (one write) stays one read.
        m = a.mark()
        a.send(b"\033")
        time.sleep(0.02)
        a.send(b"j")
        ok = a.wait_keys(b"\033j", m)
        m2 = a.mark()
        a.send(b"\033j")
        ok = a.wait_keys(b"\033j", m2) and ok
        check("4. Esc then j 20 ms later: two reads; Alt-j: one",
              ok and reads(a.since(m)) == [b"\033", b"j", b"\033j"],
              a.since(m))
        # j inside the hold: the Esc goes out as its own packet first;
        # every byte still arrives, once, in order.
        m = a.mark()
        a.send(b"\033")
        time.sleep(0.001)
        a.send(b"j")
        check("4. Esc then j inside the hold: both, in order",
              a.wait_keys(b"\033j", m), a.since(m))
        check("3. the program without 1004 never saw a report",
              b"KEY:1b5b49" not in a.since(0) and
              b"KEY:1b5b4f" not in a.since(0) and
              b"5b49" not in a.since(0).replace(b"WINCH", b""), a.since(0))

        # 5: the program turns 1004 on: reports reach it.
        m = a.mark()
        a.send(b"E")
        ok = a.wait(b"\033[?1004h", m)
        m = a.mark()
        a.send(b"\033[O")
        a.send(b"\033[I")
        ok = a.wait_keys(b"\033[O\033[I", m) and ok
        check("5. after ESC[?1004h the program gets ESC[O and ESC[I",
              ok, a.since(m))
        m = a.mark()
        c = Client(["-f", "-a", SESS], 90, 20)
        cl.append(c)
        a.wait(b"WINCH:90x20", m)
        m = a.mark()
        c.send(b"\033[I")
        check("5. a later client learns 1004 from the re-arm and forwards",
              a.wait_keys(b"\033[I", m), a.since(m))
        c.kill()
        cl.remove(c)

        # The program turns 1004 off (combined with 2004): swallowed again,
        # and the client re-enables its own.
        m = a.mark()
        a.send(b"D")
        ok = a.wait(b"\033[?1004;2004l", m)
        ok = a.wait(b"\033[?1004h", m) and ok
        after = a.since(m)
        check("5. ESC[?1004;2004l: the client turns its 1004 back on after",
              ok and after.rfind(b"\033[?1004h") >
              after.find(b"\033[?1004;2004l"), after)
        m = a.mark()
        b.send(b"\033[I")
        a.wait(b"WINCH:80x24", m)
        m = a.mark()
        a.send(b"\033[Iq")
        ok = a.wait(b"WINCH:100x30", m) and a.wait_keys(b"q", m)
        check("5. after ESC[?1004l reports are swallowed again",
              ok, a.since(m))

        # 6: DCH_FOCUS_RESIZE=0: today's behaviour.
        m = a.mark()
        d = Client(["-f", "-a", SESS], 90, 20, {"DCH_FOCUS_RESIZE": "0"})
        cl.append(d)
        a.wait(b"WINCH:90x20", m)
        m = a.mark()
        b.send(b"\033[I")
        a.wait(b"WINCH:80x24", m)
        m = a.mark()
        d.send(b"\033[I")
        ok = a.wait_keys(b"\033[I", m)
        time.sleep(0.3)
        check("6. DCH_FOCUS_RESIZE=0: bytes pass untouched, no resize",
              ok and b"WINCH" not in a.since(m), a.since(m))
        check("6. DCH_FOCUS_RESIZE=0: the client never enables 1004",
              b"\033[?1004h" not in d.since(0), d.since(0))
        d.kill()
        cl.remove(d)
        m = a.mark()
        d = Client(["-f", "-a", SESS], 85, 20, {"DCH_FOCUS_RESIZE": "off"})
        cl.append(d)
        a.wait(b"WINCH:85x20", m)
        time.sleep(0.2)
        check("6. DCH_FOCUS_RESIZE=off: no 1004 either (the config's words)",
              b"\033[?1004h" not in d.since(0), d.since(0))
        cfg = os.environ.get("DCH_CONFIG")
        if cfg:
            with open(cfg, "a") as f:
                f.write("focus_resize = off\n")
            try:
                m = a.mark()
                e = Client(["-f", "-a", SESS], 70, 20)
                cl.append(e)
                a.wait(b"WINCH:70x20", m)
                time.sleep(0.2)
                check("6. focus_resize = off in the config: no 1004 either",
                      b"\033[?1004h" not in e.since(0), e.since(0))
            finally:
                os.unlink(cfg)
    finally:
        for c in cl:
            c.kill()
        subprocess.run([DCH, "-k", SESS], capture_output=True)
    if fails:
        print("FAIL: %d check(s)" % len(fails))
        return 1
    print("PASS: size follows focus")
    return 0


if __name__ == "__main__":
    sys.exit(main())
