#!/usr/bin/env python3
"""The child, not dch, owns DEC alternate-screen mode 1049."""
import errno, os, pty, select, signal, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))
SESS = "altscreentest-%d" % os.getpid()


def spawn(args):
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.pop("DCH_SESSION", None)
        os.execv(DCH, [DCH, "-E"] + args)
    return pid, fd


def drain(fd, marker, timeout=10):
    out = b""
    end = time.time() + timeout
    while marker not in out and time.time() < end:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if ready:
            try:
                out += os.read(fd, 65536)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                break
    return out


def main():
    child = "import sys,time;sys.stdout.write('\\x1b[?1049hALT\\n');sys.stdout.flush();time.sleep(30)"
    pid_a, fd_a = spawn(["-n", SESS, sys.executable, "-c", child])
    pid_b = None
    try:
        first = drain(fd_a, b"ALT")
        if first.count(b"\x1b[?1049h") != 1:
            print("FAIL: dch injected alternate-screen mode:", repr(first))
            return 1
        os.kill(pid_a, signal.SIGHUP)
        os.waitpid(pid_a, 0)
        pid_a = None

        pid_b, fd_b = spawn(["-f", "-n", SESS])
        replay = drain(fd_b, b"ALT")
        if replay.count(b"\x1b[?1049h") != 1:
            print("FAIL: child alternate-screen mode was not replayed once:", repr(replay))
            return 1
        print("PASS: child owns alternate-screen mode and reattach restores it.")
        return 0
    finally:
        for pid in (pid_b, pid_a):
            if pid:
                try:
                    os.kill(pid, signal.SIGHUP)
                except OSError:
                    pass
        os.system("'%s' -k %s >/dev/null 2>&1" % (DCH, SESS))


if __name__ == "__main__":
    sys.exit(main())
