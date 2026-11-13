#!/usr/bin/env python3
"""The X-ray view repaints itself after a stop and a continue (#255).

The test plays a job-control shell, as attach_cont_test.py does: a pty
session leader (a fork of this script) runs dch as a job in its own
foreground process group. When the job stops, the shell takes the terminal
back, puts its cooked modes on and prints its "Stopped" line over the view;
`fg` gives the terminal back and continues the group. With nothing typed,
the view must draw a whole frame again (its header line back) within 1 s,
in its own raw mode.

  (a) `dch --xray <n>`: kill -TSTP the view, fg.
  (b) the in-attach chord's view (`dch -a`, then the detach key and x):
      kill -TSTP the client alone, fg; and kill -TSTP the whole job, fg.

Run:  python3 tests/xray_view_cont_test.py            (uses ./dch)
"""
import errno, os, pty, select, shutil, signal, struct, subprocess, sys
import tempfile, termios, time, fcntl

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))
SESS = "xvcont-%d" % os.getpid()
HEAD = b"X-ray  "
STOPPED = b"\r\n[1]  + suspended  dch\r\n$ fg\r\n"
COOKED = termios.ICANON | termios.ECHO | termios.ISIG
# the session: a line at its start and on each SIGWINCH (an attach's; dch-lite
# replays no screen), else quiet, so the view's screen settles
CHILD = r"""
import os, signal, tty
signal.signal(signal.SIGWINCH, lambda *a: os.write(1, b"READY\r\n"))
tty.setraw(0)
os.write(1, b"READY\r\n")
while os.read(0, 64):
    pass
"""
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


def shell(cmd, rep, argv, env):
    """The pty's session leader, as a job-control shell: one job, then one
    byte per command off cmd (w: wait for the job to stop, f: fg), one line
    per answer on rep."""
    for s in (signal.SIGTTOU, signal.SIGTTIN, signal.SIGTSTP):
        signal.signal(s, signal.SIG_IGN)
    cooked = termios.tcgetattr(0)
    job = os.fork()
    if job == 0:
        os.setpgid(0, 0)
        os.tcsetpgrp(0, os.getpgrp())
        for s in (signal.SIGTTOU, signal.SIGTTIN, signal.SIGTSTP):
            signal.signal(s, signal.SIG_DFL)
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
                # the terminal back, the shell's modes and its lines on it
                os.tcsetpgrp(0, os.getpgrp())
                termios.tcsetattr(0, termios.TCSANOW, cooked)
                os.write(1, STOPPED)
                os.write(rep, b"S %d\n" % os.WSTOPSIG(st))
            else:
                os.write(rep, b"X\n")
                return
        elif c == b"f":
            os.tcsetpgrp(0, job)
            os.killpg(job, signal.SIGCONT)
            os.write(rep, b"ok\n")


class Job:
    def __init__(self, argv, env):
        env = dict(env)
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
                shell(cr, rw, argv, env)
            finally:
                os._exit(0)
        # a later job would hold this pty open (QA 1.59.0 MED-T2)
        os.set_inheritable(fd, False)
        os.close(cr)
        os.close(rw)
        self.pid, self.fd, self.cw, self.rr = pid, fd, cw, rr
        self.out, self.rep = b"", b""
        self.job = None
        r = self.reply(10)
        self.job = int(r.split()[1]) if r.startswith("P ") else None

    def pump(self, secs, cond=None):
        end = time.time() + secs
        while time.time() < end and not (cond and cond()):
            r, _, _ = select.select([self.rr, self.fd], [], [], 0.02)
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

    def quiet(self, secs, within):
        """No output for secs, within a bound: the view has settled."""
        end = time.time() + within
        while time.time() < end:
            n = len(self.out)
            self.pump(secs)
            if len(self.out) == n:
                return True
        return False

    def reply(self, secs=25):
        self.pump(secs, lambda: b"\n" in self.rep)
        line, _, self.rep = self.rep.partition(b"\n")
        return line.decode()

    def do(self, c):
        os.write(self.cw, c)
        return self.reply()

    def tc(self):
        try:
            return termios.tcgetattr(self.fd)
        except termios.error:
            return None

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


def stop_fg(j, what, view_tc, group):
    """Stop the view (the job's leader, or its whole group), fg it, and see
    a whole frame with nothing typed."""
    if not j.quiet(1.0, 15):
        check("%s: the view settles before the stop" % what, False,
              j.out[-200:])
        return
    if group:
        os.killpg(j.job, signal.SIGTSTP)
    else:
        os.kill(j.job, signal.SIGTSTP)
    r = j.do(b"w")
    t = j.tc()
    check("%s: the job stops, the shell's modes on" % what,
          r == "S %d" % signal.SIGTSTP and t is not None and
          t[3] & COOKED == COOKED, r)
    j.pump(0.2)
    mark = len(j.out)
    t0 = time.time()
    j.do(b"f")
    got = j.pump(1.0 - (time.time() - t0), lambda: HEAD in j.out[mark:])
    check("%s: after fg a whole frame within 1 s, nothing typed (%.2fs)" %
          (what, time.time() - t0), got, j.out[mark:][-200:])
    check("%s: after fg the view's own modes are back" % what,
          until(lambda: j.tc() == view_tc, 5), (view_tc, j.tc()))


def case_view(env):
    what = "(a) dch --xray"
    j = Job(["--xray", SESS], env)
    try:
        up = j.pump(20, lambda: HEAD in j.out and SESS.encode() in j.out)
        j.pump(0.3)
        view_tc = j.tc()
        check("%s: the view opens, raw" % what, up and view_tc is not None
              and not view_tc[3] & COOKED, j.out[-200:])
        if not up:
            return
        stop_fg(j, what, view_tc, False)
        os.write(j.fd, b"q")
        r = j.do(b"w")
        check("%s: q still closes the view" % what, r == "X", r)
    finally:
        j.close()


def case_overlay(env, group):
    what = "(b) the chord's view, kill -TSTP the %s" % (
        "job" if group else "client")
    j = Job(["-a", SESS], env)
    try:
        ok = j.pump(20, lambda: b"READY" in j.out) and \
            until(lambda: j.tc() is not None and
                  not j.tc()[3] & COOKED, 5)
        check("%s: the attach is up, raw" % what, ok, j.out[-200:])
        if not ok:
            return
        view_tc = j.tc()
        mark = len(j.out)
        os.write(j.fd, b"\x1cx")
        up = j.pump(20, lambda: HEAD in j.out[mark:] and
                    SESS.encode() in j.out[mark:])
        check("%s: the view opens" % what, up, j.out[mark:][-200:])
        if not up:
            return
        stop_fg(j, what, view_tc, group)
    finally:
        j.close()


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    tmp = tempfile.mkdtemp(prefix="dch-xvcont-")
    env = dict(os.environ, DCH_SOCKET_DIR=tmp)
    try:
        r = subprocess.run([DCH, "--spawn", SESS, "--size", "80x24",
                            sys.executable, "-c", CHILD], env=env,
                           stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=30)
        check("the session spawned", r.returncode == 0, r.stderr)
        if r.returncode:
            return 1
        case_view(env)
        case_overlay(env, False)
        case_overlay(env, True)
    finally:
        subprocess.run([DCH, "-k", SESS], env=env, stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=30)
        shutil.rmtree(tmp, ignore_errors=True)
    print("%s: %d check(s) failed" % ("FAIL" if fails else "PASS", fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
