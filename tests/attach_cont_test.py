#!/usr/bin/env python3
"""A stop the attach did not make itself leaves it raw again (QA 1.59.0 LOW-V9).

The test plays a job-control shell: a pty session leader (a fork of this
script) runs `dch -a` as a job in its own foreground process group. When the
job stops, the shell takes the terminal back and puts its own cooked modes
on; `fg` gives the terminal back and continues the group; `bg` continues it
without the terminal. After each resume the pty's termios must be the
attach's raw mode (ICANON, ECHO and ISIG off) and a key must reach the session
with no Enter.

  (a) ctrl-z, bg, fg: on bg the client stops again on SIGTTOU (its tcsetattr
      from the background), on fg it is raw; the resume re-arms the session's
      modes once (one 1049h, one kitty push).
  (b) a ctrl-z while `dch -a` is starting, the tty still cooked (the tty's
      SIGTSTP), at the job's start and inside the raw-mode tcsetattr, which
      waits while the terminal has not read what the shell wrote before it.
      The shell's modes stay on once the stop is reported (QA 1.60.0 LOW-V1).
  (c) kill -TSTP from outside, while attached (fg: raw, one repaint, nothing
      re-armed: nothing was reset) and while the chord's X-ray view is up (the
      client alone, and the whole job): the view runs in the attach's raw
      mode, fg puts it back, and q closes the view.
  (d) kill -TSTP inside the raw-mode tcsetattr after a fg, held the same
      way as in (b): the shell's modes stay until the next fg (QA 1.60.0
      LOW-V1).

Run:  python3 tests/attach_cont_test.py            (uses ./dch)
"""
import errno, os, pty, re, select, shutil, signal, struct, subprocess, sys
import tempfile, termios, time, fcntl

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))
SESS = "attachcont-%d" % os.getpid()
ALT_ON, PUSH = b"\x1b[?1049h", b"\x1b[>1u"
COOKED = termios.ICANON | termios.ECHO | termios.ISIG
# the session: the alternate screen and a kitty push to re-arm, a numbered
# line per SIGWINCH (a repaint: the attach's, a redraw's) and one per key read
CHILD = r"""
import os, signal, tty
n = [0]
def winch(*a):
    n[0] += 1
    os.write(1, b"W%d.\r\n" % n[0])
signal.signal(signal.SIGWINCH, winch)
tty.setraw(0)
os.write(1, b"\x1b[?1049h\x1b[>1uREADY\r\n")
while True:
    b = os.read(0, 64)
    if not b:
        break
    os.write(1, b"K" + b.hex().encode() + b"\r\n")
"""
KEYS = [bytes([c]) for c in b"abcdefghijklmnoprstuvwyz0123456789"]
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


def shell(cmd, rep, argv, pre, env):
    """The pty's session leader, as a job-control shell: one job, then one
    byte per command off cmd (w: wait for the job to stop or end, f: fg,
    b: bg), one line per answer on rep."""
    for s in (signal.SIGTTOU, signal.SIGTTIN, signal.SIGTSTP):
        signal.signal(s, signal.SIG_IGN)
    cooked = termios.tcgetattr(0)
    job = os.fork()
    if job == 0:
        os.setpgid(0, 0)
        # SIGTSTP stops the job before it owns the terminal: the shell's
        # SIG_IGN would drop a ^Z sent the moment it does (case_b, n 0)
        signal.signal(signal.SIGTSTP, signal.SIG_DFL)
        os.tcsetpgrp(0, os.getpgrp())
        for s in (signal.SIGTTOU, signal.SIGTTIN):
            signal.signal(s, signal.SIG_DFL)
        if pre:
            os.write(1, pre)
        os.execve(DCH, [DCH] + argv, env)
        os._exit(127)
    try:
        os.setpgid(job, job)
    except OSError:
        pass
    os.write(rep, b"P %d\n" % job)
    while True:
        c = os.read(cmd, 1)
        if not c:
            return
        if c == b"w":
            end, st = time.time() + 20, None
            while time.time() < end:
                p, st = os.waitpid(job, os.WUNTRACED | os.WNOHANG)
                if p:
                    break
                st = None
                time.sleep(0.01)
            if st is None:
                os.write(rep, b"T\n")
            elif os.WIFSTOPPED(st):
                # the terminal back, the shell's modes on it
                os.tcsetpgrp(0, os.getpgrp())
                termios.tcsetattr(0, termios.TCSANOW, cooked)
                os.write(rep, b"S %d\n" % os.WSTOPSIG(st))
            else:
                os.write(rep, b"X\n")
                return
        elif c in b"fF":
            if c == b"F":   # a shell's echo, left in the output queue
                os.write(1, b"fg\r\n")
            os.tcsetpgrp(0, job)
            os.killpg(job, signal.SIGCONT)
            os.write(rep, b"ok\n")
        elif c == b"b":
            os.killpg(job, signal.SIGCONT)
            os.write(rep, b"ok\n")


class Job:
    def __init__(self, argv, pre=b"", env=None):
        env = dict(env or os.environ)
        env.pop("DCH_SESSION", None)
        cr, cw = os.pipe()
        rr, rw = os.pipe()
        pid, fd = pty.fork()
        if pid == 0:
            try:
                os.close(cw)
                os.close(rr)
                fcntl.ioctl(0, termios.TIOCSWINSZ,
                            struct.pack("HHHH", 24, 80, 0, 0))
                shell(cr, rw, argv, pre, env)
            finally:
                os._exit(0)
        # a later job would hold this pty open (QA 1.59.0 MED-T2)
        os.set_inheritable(fd, False)
        os.close(cr)
        os.close(rw)
        self.pid, self.fd, self.cw, self.rr = pid, fd, cw, rr
        self.out, self.rep = b"", b""
        self.job = None
        r = self.reply(10, read_tty=False)
        self.job = int(r.split()[1]) if r.startswith("P ") else None

    def pump(self, secs, cond=None, read_tty=True):
        end = time.time() + secs
        while time.time() < end and not (cond and cond()):
            fds = [self.rr] + ([self.fd] if read_tty else [])
            r, _, _ = select.select(fds, [], [], 0.05)
            for f in r:
                try:
                    b = os.read(f, 65536)
                except OSError as e:
                    if e.errno in (errno.EIO, errno.EAGAIN):
                        b = b""
                    else:
                        raise
                if f == self.fd:
                    self.out += b
                else:
                    self.rep += b
        return bool(cond and cond())

    def reply(self, secs=25, read_tty=True):
        self.pump(secs, lambda: b"\n" in self.rep, read_tty)
        line, _, self.rep = self.rep.partition(b"\n")
        return line.decode()

    def do(self, c, read_tty=True):
        os.write(self.cw, c)
        return self.reply(read_tty=read_tty)

    def tc(self):
        try:
            return termios.tcgetattr(self.fd)
        except termios.error:
            return None

    def raw(self):
        t = self.tc()
        return t is not None and not (t[3] & COOKED)

    def lflag(self):
        t = self.tc()
        return None if t is None else "ICANON=%d ECHO=%d ISIG=%d" % (
            bool(t[3] & termios.ICANON), bool(t[3] & termios.ECHO),
            bool(t[3] & termios.ISIG))

    def key(self):
        """A key reaches the session: its K<hex> line comes back. Each key
        once in the test: a screen replay repeats the old lines."""
        b = KEYS.pop(0)
        mark = len(self.out)
        os.write(self.fd, b)
        return self.pump(5, lambda: b"K" + b.hex().encode() in
                         self.out[mark:])

    def close(self):
        if self.job:
            try:
                os.killpg(self.job, signal.SIGKILL)
            except OSError:
                pass
        try:
            os.kill(self.pid, signal.SIGKILL)
        except OSError:
            pass
        # the fds first (#215), then a bounded wait
        for f in (self.fd, self.cw, self.rr):
            try:
                os.close(f)
            except OSError:
                pass
        end = time.time() + 10
        while time.time() < end:
            if os.waitpid(self.pid, os.WNOHANG)[0]:
                return
            time.sleep(0.02)


def repaints(b):
    """The session's last repaint number in b."""
    return max([int(x) for x in re.findall(rb"W(\d+)\.", b)] or [0])


def attached(j, what):
    """The attach is up: its re-arm came, raw mode, and a key went through.
    The master sends the attach's SIGWINCH before it writes the key to the
    session, so that repaint's line is in j.out by the key's."""
    ok = j.pump(20, lambda: PUSH in j.out) and until(j.raw, 5) and \
        j.key()
    check("%s: the attach is up, raw" % what, ok,
          (j.lflag(), j.out[-200:]))
    return ok


def resumed(j, what, mark):
    """After fg: the resume's re-arm came, the terminal is raw, a key goes
    through with no Enter."""
    got = j.pump(15, lambda: PUSH in j.out[mark:])
    j.pump(0.3)
    check("%s: after fg the terminal is raw (%s)" % (what, j.lflag()),
          got and j.raw(), j.out[mark:][-200:])
    check("%s: after fg a key reaches the session with no Enter" % what,
          j.key(), j.out[mark:][-200:])
    return got


def case_a(env):
    j = Job(["-a", SESS], env=env)
    try:
        if not attached(j, "(a)"):
            return
        mark = len(j.out)
        os.write(j.fd, b"\x1a")
        r = j.do(b"w")
        check("(a) ctrl-z stops the client", r == "S %d" % signal.SIGTSTP, r)
        j.do(b"b")
        r = j.do(b"w")
        check("(a) bg: the client stops on SIGTTOU, the shell keeps the "
              "terminal (%s)" % j.lflag(),
              r == "S %d" % signal.SIGTTOU and not j.raw(), r)
        mark = len(j.out)
        j.do(b"f")
        resumed(j, "(a) ctrl-z, bg, fg", mark)
        post = j.out[mark:]
        check("(a) the resume re-arms once: one 1049h, one kitty push",
              post.count(ALT_ON) == 1 and post.count(PUSH) == 1, post[-300:])
    finally:
        j.close()


def case_b(env, n, trace):
    """n: 0 the ^Z as the job starts, 1 and 2 once the client is about to
    set raw mode (its trace says so), 2 after a beat more; the terminal
    reads nothing meanwhile, so the raw tcsetattr waits for the shell's
    echo to drain."""
    what = "(b%d) ^Z during startup" % n
    open(trace, "w").close()
    j = Job(["-a", SESS], pre=b"$ dch -a %s\r\n" % SESS.encode(),
            env=dict(env, DCH_DEBUG=trace))
    try:
        ok = until(lambda: os.tcgetpgrp(j.fd) == j.job, 10)
        if n:
            ok = ok and until(lambda: b"attach hot-path" in
                              open(trace, "rb").read(), 10)
            if n == 2:
                time.sleep(0.1)     # not a wait: a later point to stop at
        check("%s: the client starts" % what, ok, j.job)
        os.write(j.fd, b"\x1a")
        r = held_stop(j)
        check("%s: the tty's SIGTSTP stops the client" % what,
              r == "S %d" % signal.SIGTSTP, r)
        kept(j, what)
        mark = len(j.out)
        j.do(b"f")
        resumed(j, what, mark)
    finally:
        j.close()


def held_stop(j):
    """The shell's wait for the stop, the terminal reading nothing for up to
    1 s first: a stop that lands inside the held raw tcsetattr is reported
    (and the shell's modes put on) before the drain lets the tcsetattr end."""
    os.write(j.cw, b"w")
    j.pump(1, lambda: b"\n" in j.rep, read_tty=False)
    return j.reply()


def kept(j, what):
    """The shell's modes stay on its terminal once the stop is reported: a
    raw tcsetattr the stop interrupted must not finish after them (QA 1.60.0
    LOW-V1: a shell that does not set its own modes, bash --noediting, then
    reads a raw terminal). The pump drains what held the tcsetattr."""
    j.pump(0.5)
    check("%s: the shell's terminal stays cooked (%s)" % (what, j.lflag()),
          not j.raw())


def case_d(env):
    """kill -TSTP inside the raw tcsetattr of the repaint after a fg: the
    shell's echo of `fg` unread holds it (TCSADRAIN)."""
    what = "(d) kill -TSTP in the fg's raw-mode tcsetattr"
    j = Job(["-a", SESS], env=env)
    try:
        if not attached(j, "(d)"):
            return
        os.kill(j.job, signal.SIGTSTP)
        r = j.do(b"w")
        check("(d) kill -TSTP stops the client", r == "S %d" % signal.SIGTSTP,
              r)
        j.do(b"F", read_tty=False)
        time.sleep(1)   # not a wait: the client reaches the held tcsetattr
        os.kill(j.job, signal.SIGTSTP)
        r = held_stop(j)
        check("%s: the client stops" % what, r == "S %d" % signal.SIGTSTP, r)
        kept(j, what)
        mark = len(j.out)
        j.do(b"f")
        ok = until(j.raw, 10)
        check("%s: after fg the terminal is raw (%s)" % (what, j.lflag()), ok,
              j.out[mark:][-200:])
        check("%s: after fg a key reaches the session with no Enter" % what,
              j.key(), j.out[mark:][-200:])
    finally:
        j.close()


def case_c(env):
    j = Job(["-a", SESS], env=env)
    try:
        if not attached(j, "(c)"):
            return
        os.kill(j.job, signal.SIGTSTP)
        r = j.do(b"w")
        check("(c) kill -TSTP stops the client (%s)" % j.lflag(),
              r == "S %d" % signal.SIGTSTP and not j.raw(), r)
        w, mark = repaints(j.out), len(j.out)
        t0 = time.time()
        j.do(b"f")
        # a quiet session, nothing typed: the repaint must not wait for a
        # wake (bound loose for load; the miss is "never", not "late")
        got = j.pump(5, lambda: repaints(j.out[mark:]) > w)
        check("(c) after fg the repaint comes with nothing typed (%.2fs)" %
              (time.time() - t0), got, j.out[mark:][-200:])
        ok = until(j.raw, 5)
        check("(c) after fg the terminal is raw (%s)" % j.lflag(), ok,
              j.out[mark:][-200:])
        check("(c) after fg a key reaches the session with no Enter",
              j.key(), j.out[mark:][-200:])
        post = j.out[mark:]
        check("(c) one repaint (W%d. only), nothing re-armed (no 1049h, no "
              "kitty push)" % (w + 1), repaints(post) == w + 1 and
              ALT_ON not in post and PUSH not in post, post[-300:])
    finally:
        j.close()


def case_view(env, group):
    what = "(c-view, %s)" % ("kill -TSTP the job" if group else
                             "kill -TSTP the client")
    j = Job(["-a", SESS], env=env)
    try:
        if not attached(j, what):
            return
        attach_tc = j.tc()
        mark = len(j.out)
        os.write(j.fd, b"\x1cx")
        up = j.pump(20, lambda: b"X-ray  " in j.out[mark:] and
                    SESS.encode() in j.out[mark:])
        j.pump(0.3)
        check("%s: the view opens, its modes are the attach's" % what,
              up and j.tc() == attach_tc,
              (attach_tc, j.tc(), j.out[mark:][-200:]))
        if not up:
            return
        if group:
            os.killpg(j.job, signal.SIGTSTP)
        else:
            os.kill(j.job, signal.SIGTSTP)
        r = j.do(b"w")
        check("%s: the client stops (%s)" % (what, j.lflag()),
              r == "S %d" % signal.SIGTSTP and not j.raw(), r)
        mark = len(j.out)
        j.do(b"f")
        ok = until(lambda: j.tc() == attach_tc, 10)
        check("%s: after fg the view's raw mode is back (%s)" %
              (what, j.lflag()), ok)
        os.write(j.fd, b"q")
        back = j.pump(15, lambda: PUSH in j.out[mark:])
        j.pump(0.3)
        check("%s: q closes the view, the attach re-arms once, raw (%s)" %
              (what, j.lflag()),
              back and j.raw() and j.out[mark:].count(PUSH) == 1,
              j.out[mark:][-300:])
        check("%s: a key reaches the session after" % what,
              j.key())
    finally:
        j.close()


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    tmp = tempfile.mkdtemp(prefix="dch-cont-")
    env = dict(os.environ, DCH_SOCKET_DIR=tmp)
    try:
        r = subprocess.run([DCH, "--spawn", SESS, "--size", "80x24",
                            sys.executable, "-c", CHILD], env=env,
                           stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=30)
        check("the session spawned", r.returncode == 0, r.stderr)
        if r.returncode:
            return 1
        case_a(env)
        for n in range(3):
            case_b(env, n, os.path.join(tmp, "trace"))
        case_c(env)
        case_d(env)
        case_view(env, False)
        case_view(env, True)
    finally:
        subprocess.run([DCH, "-k", SESS], env=env, stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=30)
        shutil.rmtree(tmp, ignore_errors=True)
    print("%s: %d check(s) failed" % ("FAIL" if fails else "PASS", fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
