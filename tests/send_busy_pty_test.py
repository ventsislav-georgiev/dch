#!/usr/bin/env python3
"""A paste into a program busy printing (#258).

The master wrote a session's input to the pty with a blocking write. A program
not reading its input stopped the whole master: no output to the attached
clients, no other verb served. A program that prints before it reads then
deadlocked with it for good: it waited for the master to read its output, the
master waited for it to read its input. The input now waits in the master
until the pty takes it.

The program waits for a go file, prints 300 KB, then reads its input. The
test sends 96 KB with dch --send before the go (more than a pty holds on
macOS and Linux), then says go.

Run:  python3 tests/send_busy_pty_test.py      (uses ./dch)
"""
import errno, os, pty, select, shutil, signal, subprocess, sys, tempfile
import termios, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))
SESS = "busypty-%d" % os.getpid()
OUT = 300000
SENT = "A" * 48000 + "B" * 48000
PROG = ("import os, sys, time, tty\n"
        "tty.setraw(0)\n"
        "while not os.path.exists(sys.argv[1]): time.sleep(0.02)\n"
        "os.write(1, b'x' * %d)\n"
        "got = b''\n"
        "while len(got) < %d:\n"
        "    got += os.read(0, 65536)\n"
        "open(sys.argv[2] + '.tmp', 'wb').write(got)\n"
        "os.rename(sys.argv[2] + '.tmp', sys.argv[2])\n"
        "time.sleep(600)\n" % (OUT, len(SENT)))
fails = 0


def check(name, ok, detail=""):
    global fails
    if ok:
        print("  ok   " + name)
    else:
        fails += 1
        print("  FAIL %s %s" % (name, detail))


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    tmp = tempfile.mkdtemp(prefix="dch-busypty-")
    go, got = os.path.join(tmp, "go"), os.path.join(tmp, "got")
    env = dict(os.environ, DCH_SOCKET_DIR=tmp)
    env.pop("DCH_SESSION", None)
    pid, fd = None, None
    try:
        r = subprocess.run([DCH, "--spawn", SESS, "--size", "80x24",
                            sys.executable, "-c", PROG, go, got], env=env,
                           stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=30)
        check("the session spawned", r.returncode == 0, r.stderr)
        if r.returncode:
            return 1
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(DCH, [DCH, "-a", SESS], env)
            os._exit(127)
        os.set_inheritable(fd, False)
        out = b""

        def pump(secs, cond):
            nonlocal out
            end = time.time() + secs
            while time.time() < end and not cond():
                if select.select([fd], [], [], 0.05)[0]:
                    try:
                        out += os.read(fd, 65536)
                    except OSError as e:
                        if e.errno != errno.EIO:
                            raise
                        time.sleep(0.05)
            return cond()

        # attached: raw mode on (dch-lite replays no READY on an attach)
        check("attached", pump(20, lambda: not termios.tcgetattr(fd)[3] &
                               termios.ICANON), out[-200:])
        try:
            rc = subprocess.run([DCH, "--send", SESS, SENT], env=env,
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                timeout=20).returncode
        except subprocess.TimeoutExpired:
            rc = "still writing after 20 s"     # the master stopped reading
        check("--send of 96 KB to a program not reading: exit 0", rc == 0,
              rc)
        open(go, "w").close()
        check("the program's 300 KB of output reached the attach while its "
              "input waited (the master did not block on the pty)",
              pump(30, lambda: out.count(b"x") >= OUT), out.count(b"x"))
        pump(30, lambda: os.path.exists(got))
        data = open(got, "rb").read() if os.path.exists(got) else b""
        check("the program then read all the input, in order",
              data == SENT.encode(), (len(data), data[:20], data[-20:]))
    finally:
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            os.close(fd)
            os.waitpid(pid, 0)
        subprocess.run([DCH, "-k", SESS], env=env, stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=30)
        shutil.rmtree(tmp, ignore_errors=True)
    print("%s: %d check(s) failed" % ("FAIL" if fails else "PASS", fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
