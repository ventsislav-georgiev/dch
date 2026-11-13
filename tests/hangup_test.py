"""An attached client's signal exit (#029).

The client used to exit() from its signal handler: the atexit terminal
restore then ran stdio and waited in tcsetattr(TCSADRAIN) for output that,
after a hangup, nobody reads, and the client could hang forever. die() is now
async-signal-safe and does the cleanup itself. Covers: SIGHUP exits at once
and removes the client marker (<sock>.client.<pid>, once an atexit job);
SIGTERM on a read terminal still sends the reset sequence (cursor shown).
The hang itself needs a full pty at the instant of the signal, which the
master's eviction of a slow reader races: not reproduced here.

Run:  python3 tests/hangup_test.py        (uses ./dch)
"""
import os, pty, select, shutil, signal, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))


def main():
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchh", dir=os.environ.get("DCH_TEST_TMP", "/tmp")))
    for k in list(os.environ):
        if k.startswith("DCH_"):
            del os.environ[k]
    os.environ.update(XDG_RUNTIME_DIR=tmp, DCH_LOGIN_ENV="0",
                      XDG_STATE_HOME=os.path.join(tmp, "state"),
                      HOME=os.path.join(tmp, "home"),
                      DCH_CONFIG=os.path.join(tmp, "config"))
    os.makedirs(os.environ["HOME"])
    sockdir = os.path.join(tmp, "dch-%d" % os.getuid())
    fails = []

    def check(msg, cond):
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails.append(msg)

    def attach(name):
        pid, fd = pty.fork()
        if pid == 0:
            os.execv(DCH, [DCH, "-E", "-n", name])
            os._exit(127)
        return pid, fd

    def markers(name, pid):
        return os.path.exists(os.path.join(
            sockdir, "%s.sock.client.%d" % (name, pid)))

    def exited(pid, t):
        end = time.time() + t
        while time.time() < end:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                return True
            time.sleep(0.02)
        return False

    def read_some(fd, t):
        out, end = b"", time.time() + t
        while time.time() < end:
            if select.select([fd], [], [], 0.05)[0]:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
        return out

    try:
        subprocess.run([DCH, "--spawn", "calm", "--size", "80x24", "sleep",
                        "120"], capture_output=True, timeout=30)

        pid, fd = attach("calm")
        read_some(fd, 1.0)
        had = markers("calm", pid)
        os.kill(pid, signal.SIGHUP)
        gone = exited(pid, 3.0)
        check("SIGHUP: the client exits within 3 s", gone)
        check("SIGHUP: its client marker is removed",
              had and not markers("calm", pid))
        os.close(fd)
        if not gone:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)

        # a live terminal: SIGTERM still sends the reset sequence
        pid, fd = attach("calm")
        read_some(fd, 1.0)
        os.kill(pid, signal.SIGTERM)
        tail = read_some(fd, 1.5)
        check("SIGTERM on a read terminal: exits, cursor shown again",
              exited(pid, 3.0) and b"\x1b[?25h" in tail)
        os.close(fd)
    finally:
        subprocess.run([DCH, "-kl"], capture_output=True, timeout=30)
        shutil.rmtree(tmp, ignore_errors=True)
    print("%d failed" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
