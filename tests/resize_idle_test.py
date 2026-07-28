"""A resize must reach the session, including when the parent blocked SIGWINCH.

Replicates Prosper's PtyChild: forkpty a `dch` client, then TIOCSWINSZ the
master — exactly what rotating the phone does. The client learns about it from
SIGWINCH and forwards MSG_WINCH.

A signal mask survives fork AND exec, so a parent that has SIGWINCH blocked
hands the client a signal it can never receive: the handler installs and never
runs. Prosper forkpty()s from a Swift runtime thread with SIGWINCH blocked, so
every window-size change was silently dropped — landscape rendered at the
portrait width, forever. dch now clears its mask at startup; the blocked case
below is that regression.

Run:  python3 tests/resize_idle_test.py          (uses ./dch)
      DCH=/path/to/dch python3 tests/resize_idle_test.py
"""
import os, sys, re, fcntl, termios, struct, subprocess, threading, time, signal

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))


def set_size(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def ps_lines(fmt):
    return subprocess.run(["ps", "-axo", fmt], capture_output=True, text=True).stdout.splitlines()


def session_tty(sess):
    """The tty of the program inside the session (the master daemon's child)."""
    master = None
    for line in ps_lines("pid=,command="):
        f = line.split(None, 1)
        if len(f) == 2 and "--master-of" in f[1] and sess in f[1]:
            master = f[0]
            break
    if master is None:
        return None
    for line in ps_lines("ppid=,tty="):
        f = line.split()
        if len(f) == 2 and f[0] == master and f[1] != "??":
            return "/dev/" + f[1]
    return None


def session_size(tty):
    out = subprocess.run(["stty", "-a", "-f", tty], capture_output=True, text=True).stdout
    rows = re.search(r"(\d+) rows", out)
    cols = re.search(r"(\d+) columns", out)
    return (int(cols.group(1)) if cols else 0, int(rows.group(1)) if rows else 0)


def check(block_winch):
    """Resize an idle session's client pty; the session must follow."""
    sess = "resizetest%s-%d" % ("blocked" if block_winch else "clean", os.getpid())
    how = "SIGWINCH blocked by the parent" if block_winch else "clean signal mask"
    signal.pthread_sigmask(signal.SIG_BLOCK if block_winch else signal.SIG_UNBLOCK,
                           {signal.SIGWINCH})

    pid, fd = os.forkpty()
    if pid == 0:
        # Strip DCH_SESSION so dch's nesting guard doesn't refuse (PtyChild does this).
        os.environ.pop("DCH_SESSION", None)
        os.execv(DCH, [DCH, "-E", "-n", sess, "sh", "-c", "while :; do sleep 1; done"])
        os._exit(127)

    # Drain the client's output: a client blocked in write() would stall its own
    # loop, which would mask this bug behind a different one.
    stop, last = threading.Event(), [time.time()]

    def drain():
        while not stop.is_set():
            try:
                if not os.read(fd, 65536):
                    return
            except OSError:
                return
            last[0] = time.time()

    threading.Thread(target=drain, daemon=True).start()

    try:
        set_size(fd, 80, 24)
        time.sleep(1.5)
        tty = session_tty(sess)
        if tty is None:
            return "%s: session never started" % how

        # Wait for real quiet first: any byte in flight wakes the client's
        # select() and flushes a pending WINCH as a side effect, which is the
        # accident that hid this bug whenever the session was busy.
        deadline = time.time() + 15
        while time.time() < deadline and time.time() - last[0] < 2.0:
            time.sleep(0.1)

        set_size(fd, 150, 40)          # the rotation: no keystrokes, no output
        for _ in range(30):
            time.sleep(0.1)
            if session_size(tty) == (150, 40):
                print("ok: %s — resize reached the session" % how)
                return None
        return "%s: resize never reached the session, stuck at %sx%s (client pty is 150x40)" % (
            (how,) + session_size(tty))
    finally:
        stop.set()
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        subprocess.run([DCH, "-k", sess], capture_output=True)


def main():
    failures = [f for f in (check(False), check(True)) if f]
    for f in failures:
        print("FAIL: " + f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
