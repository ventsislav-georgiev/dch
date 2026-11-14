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
is raw again and a key reaches the session with no Enter. And (QA 1.65.0):

  (d) kill -TSTP while the detach's restore drains output the terminal has
      not read: the restore's modes are on before the stop (LOW-V22).
  (e) kill -TSTP the `dch -l` picker, under dash and under bash: the shell
      gets its modes back, and after `fg` the picker is raw and takes a q
      with no Enter (LOW-V24).
  (f) SIGSTOP the client under bash, which puts its own modes on: after
      `fg` the attach is raw again and a key gets through (LOW-V26).

Skipped, with the reason, where no dash exists.

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
         "    if b == b'P': os.write(1, b'p' * 900 + b'\\r\\n')\n"
         "    else: os.write(1, b'K' + b.hex().encode() + b'\\r\\n')\n")
BASH = "/bin/bash" if os.access("/bin/bash", os.X_OK) else None
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
    def __init__(self, env, argv=None):
        pid, fd = pty.fork()
        if pid == 0:
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ,
                            struct.pack("HHHH", 24, 80, 0, 0))
                argv = argv or [DASH, "-i"]
                os.execve(argv[0], argv, env)
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


def case_d(env, trace):
    what = "(d) kill -TSTP inside the detach's drain"
    sh = Dash(env)
    try:
        sh.prompt(0)
        n = sh.prompts()
        pg = attach(sh, what)
        if pg is None:
            return
        # the session's 900 bytes, left unread: the restore's TCSADRAIN
        # waits on them until the stop
        os.write(sh.fd, b"P")
        out = select.select([sh.fd], [], [], 15)[0]
        with open(trace, "rb") as f:
            mark = len(f.read())
        os.write(sh.fd, b"\x1c")
        end = time.time() + 15
        while time.time() < end:
            with open(trace, "rb") as f:
                if b"client restore" in f.read()[mark:]:
                    break
            time.sleep(0.02)
        else:
            out = []
        check("%s: the detach's restore is draining" % what, out)
        # Linux's TCSADRAIN on a pty slave does not wait for unread output:
        # the client may be gone before the stop (#336). Strict rows run
        # whenever the job is still there to stop.
        try:
            os.killpg(pg, signal.SIGTSTP)
        except ProcessLookupError:
            got = sh.prompt(n)
            sh.pump(0.3)
            check("%s: the detach ended before the stop could land, dash "
                  "cooked (%s)" % (what, sh.modes()),
                  got and sh.cooked(), sh.out[-200:])
            print("skip: %s: the drain rows (this terminal's drain does not "
                  "block)" % what)
            return
        got = sh.prompt(n)
        sh.pump(0.3)
        check("%s: the job stops, dash's terminal is cooked (%s)" %
              (what, sh.modes()), got and sh.cooked(), sh.out[-200:])
        n = sh.prompts()
        os.write(sh.fd, b"fg\n")
        got = sh.prompt(n)
        check("%s: after fg the detach ends, dash cooked (%s)" %
              (what, sh.modes()), got and sh.cooked(), sh.out[-200:])
    finally:
        sh.close()


def case_e(env, argv, what):
    sh = Dash(env, argv)
    try:
        sh.prompt(0)
        n = sh.prompts()
        mark = len(sh.out)
        os.write(sh.fd, ("%s -l\n" % shlex.quote(DCH)).encode())
        ok = sh.pump(15, lambda: sh.raw() and SESS.encode() in sh.out[mark:])
        check("%s: the picker is up, raw" % what, ok, (sh.modes(),
              sh.out[-200:]))
        if not ok or sh.prompts() != n:
            return
        os.killpg(os.tcgetpgrp(sh.fd), signal.SIGTSTP)
        got = sh.prompt(n)
        sh.pump(0.3)
        check("%s: the picker stops, the shell's terminal is cooked (%s)" %
              (what, sh.modes()), got and sh.cooked(), sh.out[-200:])
        n = sh.prompts()
        os.write(sh.fd, b"fg\n")
        ok = sh.pump(15, sh.raw)
        check("%s: after fg the picker is raw (%s)" % (what, sh.modes()),
              ok, sh.out[-200:])
        os.write(sh.fd, b"q")
        got = sh.prompt(n)
        sh.pump(0.3)
        check("%s: q with no Enter quits it, cooked (%s)" %
              (what, sh.modes()), got and sh.cooked(), sh.out[-200:])
    finally:
        sh.close()


def case_f(env):
    what = "(f) SIGSTOP the client under bash"
    sh = Dash(env, [BASH, "--norc", "--noprofile", "--noediting", "-i"])
    try:
        sh.prompt(0)
        n = sh.prompts()
        pg = attach(sh, what)
        if pg is None:
            return
        os.killpg(pg, signal.SIGSTOP)
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
               HOME=tmp, XDG_STATE_HOME=os.path.join(tmp, "state"))
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
        # QA 1.65.0's rows: their own state dir (NIT-T11)
        trace = os.path.join(tmp, "trace")
        open(trace, "w").close()
        env = dict(env, XDG_STATE_HOME=os.path.join(tmp, "state"),
                   DCH_NO_REMOTE="1")
        case_d(dict(env, DCH_DEBUG=trace, DCH_DOUBLE_TAP_MS="0"), trace)
        case_e(env, None, "(e dash) kill -TSTP the dch -l picker")
        if BASH:
            case_e(env, [BASH, "--norc", "--noprofile", "--noediting",
                         "-i"], "(e bash) kill -TSTP the dch -l picker")
            case_f(env)
    finally:
        subprocess.run([DCH, "-k", SESS], env=env, stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=30)
        shutil.rmtree(tmp, ignore_errors=True)
    print("%s: %d check(s) failed" % ("FAIL" if fails else "PASS", fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
