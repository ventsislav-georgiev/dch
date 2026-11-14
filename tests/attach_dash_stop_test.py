#!/usr/bin/env python3
"""dash as the parent shell: a stop of dch -a leaves it cooked (QA 1.63.0 MED-V14).

dash (/bin/sh on Debian and Ubuntu) does not save and restore the terminal's
modes around a stopped job, as bash and zsh do: the modes the job left on are
the ones dash reads its next line with. So the client puts the shell's modes
back itself before it stops, and its raw mode again on the fg. A real dash -i
on a pty runs `dch -a`:

  (a) ^Z typed while `dch -a` starts, the terminal reading nothing for a beat:
      the raw-mode tcsetattr waits for the shell's echo to drain and the tty's
      SIGTSTP, held until it ends (QA 1.60.0 LOW-V1), lands right after it.
  (b) kill -TSTP the job from outside, attached.
  (c) ^Z typed once attached: the client's own suspend (it passed before).

After each stop dash's terminal is cooked (ICANON and ECHO on); after `fg` it
is raw again and a key reaches the session with no Enter. Skipped, with the
reason, where no dash exists.

Run:  python3 tests/attach_dash_stop_test.py       (uses ./dch)
"""
import errno, fcntl, os, pty, select, shlex, shutil, signal, struct
import subprocess, sys, tempfile, termios, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))
DASH = next((p for p in ("/bin/dash", "/usr/bin/dash")
             if os.access(p, os.X_OK)), None)
SESS = "dashstop-%d" % os.getpid()
PROMPT = b"DASHQA$ "
RAWOFF = termios.ICANON | termios.ECHO | termios.ISIG
# the session: one K<hex> line per read
CHILD = ("import os, tty\n"
         "tty.setraw(0)\n"
         "os.write(1, b'READY\\r\\n')\n"
         "while True:\n"
         "    b = os.read(0, 64)\n"
         "    if not b: break\n"
         "    os.write(1, b'K' + b.hex().encode() + b'\\r\\n')\n")
KEYS = [bytes([c]) for c in b"abcdefghijklmnoprstuvwyz"]
fails = 0


def check(name, ok, detail=""):
    global fails
    if ok:
        print("  ok   " + name)
    else:
        fails += 1
        print("  FAIL %s %s" % (name, detail))


class Dash:
    def __init__(self, env):
        pid, fd = pty.fork()
        if pid == 0:
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ,
                            struct.pack("HHHH", 24, 80, 0, 0))
                os.execve(DASH, [DASH, "-i"], env)
            finally:
                os._exit(127)
        os.set_inheritable(fd, False)
        self.pid, self.fd, self.out = pid, fd, b""

    def pump(self, secs, cond=None):
        end = time.time() + secs
        while time.time() < end and not (cond and cond()):
            if select.select([self.fd], [], [], 0.05)[0]:
                try:
                    b = os.read(self.fd, 65536)
                except OSError as e:
                    if e.errno != errno.EIO:
                        raise
                    b = b""
                if not b:
                    break
                self.out += b
        return bool(cond and cond())

    def prompts(self):
        return self.out.count(PROMPT)

    def prompt(self, n, secs=15):
        """dash printed a prompt past the n-th: the job stopped or ended"""
        return self.pump(secs, lambda: self.prompts() > n)

    def lflag(self):
        try:
            return termios.tcgetattr(self.fd)[3]
        except termios.error:
            return None

    def modes(self):
        lf = self.lflag()
        return "lflag?" if lf is None else "ICANON=%d ECHO=%d ISIG=%d" % (
            bool(lf & termios.ICANON), bool(lf & termios.ECHO),
            bool(lf & termios.ISIG))

    def cooked(self):
        lf = self.lflag()
        return lf is not None and lf & termios.ICANON and lf & termios.ECHO

    def raw(self):
        lf = self.lflag()
        return lf is not None and not lf & RAWOFF

    def key(self):
        """A key reaches the session: its K<hex> line comes back."""
        b = KEYS.pop(0)
        mark = len(self.out)
        os.write(self.fd, b)
        return self.pump(5, lambda: b"K" + b.hex().encode() in
                         self.out[mark:])

    def close(self):
        try:
            os.write(self.fd, b"\nexit\n")
        except OSError:
            pass
        self.pump(1, lambda: False)
        try:
            os.close(self.fd)
        except OSError:
            pass
        end = time.time() + 5
        while time.time() < end:
            if os.waitpid(self.pid, os.WNOHANG)[0]:
                return
            time.sleep(0.02)
        os.kill(self.pid, signal.SIGKILL)
        os.waitpid(self.pid, 0)


def attach(sh, what):
    """`dch -a` typed at dash's prompt: the attach is up, raw, a key goes
    through. Returns the job's process group (the tty's foreground)."""
    n = sh.prompts()
    os.write(sh.fd, ("%s -a %s\n" % (shlex.quote(DCH), SESS)).encode())
    ok = sh.pump(15, sh.raw) and sh.key()
    check("%s: the attach is up, raw" % what, ok, (sh.modes(), sh.out[-200:]))
    return os.tcgetpgrp(sh.fd) if ok and sh.prompts() == n else None


def stopped_then_fg(sh, n, what):
    """The job stopped (dash's next prompt): dash's terminal is cooked; fg
    gives the attach its raw mode back."""
    got = sh.prompt(n)
    sh.pump(0.3)
    check("%s: the job stops, dash's terminal is cooked (%s)" %
          (what, sh.modes()), got and sh.cooked(), sh.out[-200:])
    n = sh.prompts()
    os.write(sh.fd, b"fg\n")
    ok = sh.pump(15, sh.raw)
    sh.pump(0.3)
    check("%s: after fg the terminal is raw (%s)" % (what, sh.modes()),
          ok and sh.raw(), sh.out[-200:])
    check("%s: after fg a key reaches the session with no Enter" % what,
          sh.key(), sh.out[-200:])
    return sh.prompts() == n


def detach(sh, what):
    n = sh.prompts()
    os.write(sh.fd, b"\x1c")
    got = sh.prompt(n)
    check("%s: the detach key ends the job, dash cooked (%s)" %
          (what, sh.modes()), got and sh.cooked())


def case_a(env, d):
    what = "(a %.1fs) ^Z while dch -a starts" % d
    sh = Dash(env)
    try:
        if not sh.prompt(0):
            check("%s: dash prompts" % what, False, sh.out[-200:])
            return
        n = sh.prompts()
        os.write(sh.fd, ("%s -a %s\n" % (shlex.quote(DCH), SESS)).encode())
        time.sleep(d)   # not a wait: the terminal reads nothing meanwhile
        os.write(sh.fd, b"\x1a")
        if stopped_then_fg(sh, n, what):
            detach(sh, what)
    finally:
        sh.close()


def case_b(env):
    what = "(b) kill -TSTP from outside, attached"
    sh = Dash(env)
    try:
        sh.prompt(0)
        n = sh.prompts()
        pg = attach(sh, what)
        if pg is None:
            return
        os.killpg(pg, signal.SIGTSTP)
        if stopped_then_fg(sh, n, what):
            detach(sh, what)
    finally:
        sh.close()


def case_c(env):
    what = "(c) ^Z typed, attached (the client's own suspend)"
    sh = Dash(env)
    try:
        sh.prompt(0)
        n = sh.prompts()
        if attach(sh, what) is None:
            return
        os.write(sh.fd, b"\x1a")
        if stopped_then_fg(sh, n, what):
            detach(sh, what)
    finally:
        sh.close()


def main():
    if not DASH:
        print("skip: no dash on this system (/bin/dash, /usr/bin/dash)")
        return 0
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    tmp = tempfile.mkdtemp(prefix="dch-dash-")
    env = dict(os.environ, DCH_SOCKET_DIR=tmp, PS1=PROMPT.decode(),
               HOME=tmp)
    for k in ("DCH_SESSION", "ENV"):
        env.pop(k, None)
    try:
        r = subprocess.run([DCH, "--spawn", SESS, "--size", "80x24",
                            sys.executable, "-c", CHILD], env=env,
                           stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=30)
        check("the session spawned", r.returncode == 0, r.stderr)
        if r.returncode:
            return 1
        for d in (0.1, 0.5):
            case_a(env, d)
        case_b(env)
        case_c(env)
    finally:
        subprocess.run([DCH, "-k", SESS], env=env, stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=30)
        shutil.rmtree(tmp, ignore_errors=True)
    print("%s: %d check(s) failed" % ("FAIL" if fails else "PASS", fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
