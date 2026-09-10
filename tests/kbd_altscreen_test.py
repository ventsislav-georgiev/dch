#!/usr/bin/env python3
"""Kitty keyboard protocol vs the alternate screen.

Ghostty and kitty keep the kitty-keyboard flag stack PER SCREEN and do not
copy it across 1049h/l. Two consequences dch has to respect:

  1. On attach the master must re-arm 1049h BEFORE it replays the child's
     ESC[>..u push, so the flags land on the alt screen the child reads keys
     from (and where the detaching client can pop them).
  2. On detach the client must pop on BOTH screens: once before leaving the
     alt screen and once after, so neither screen keeps stale flags. This is
     the "ctrl-d types 0;5u in the bare shell after detach" bug.

Run:  python3 tests/kbd_altscreen_test.py            (uses ./dch)
"""
import errno, os, pty, re, select, signal, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))
SESS = "kbdaltscreen-%d" % os.getpid()
POP = re.compile(rb"\x1b\[<\d*u")
PUSH = b"\x1b[>1u"
ALT_ON, ALT_OFF = b"\x1b[?1049h", b"\x1b[?1049l"


def spawn(args):
    pid, fd = pty.fork()
    if pid == 0:
        # No -E: the detach key must stay live.
        os.environ.pop("DCH_SESSION", None)
        os.execv(DCH, [DCH] + args)
        os._exit(127)
    return pid, fd


def drain(fd, timeout, until=None):
    out = b""
    end = time.time() + timeout
    while time.time() < end:
        if until and until in out:
            break
        ready, _, _ = select.select([fd], [], [], 0.1)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.EAGAIN):
                    break
                raise
            if not chunk:
                break
            out += chunk
    return out


def detach(pid, fd):
    """Ctrl-\\ once; the double-tap window expires and the client detaches.
    Returns everything the client wrote to the terminal on its way out."""
    os.write(fd, b"\x1c")
    out = drain(fd, 5.0)
    os.waitpid(pid, 0)
    return out


def check_detach(who, out):
    off = out.rfind(ALT_OFF)
    if off < 0:
        print("FAIL: %s detach did not leave the alt screen: %r" % (who, out))
        return 1
    if not POP.search(out, off):
        print("FAIL: %s detach popped kitty flags only on the alt screen: %r"
              % (who, out[off - 40:]))
        return 1
    if not POP.search(out, 0, off):
        print("FAIL: %s detach never popped kitty flags on the alt screen: %r"
              % (who, out[:off + len(ALT_OFF)]))
        return 1
    return 0


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH); return 1
    os.system("'%s' -k %s >/dev/null 2>&1" % (DCH, SESS))

    # Make the child write before its first client attaches. A no-mirror
    # master cannot replay this startup output, so first attach must forward it.
    tmp = tempfile.mkdtemp(prefix="dch-kbd-pending-")
    pending = os.path.join(tmp, "ready")
    os.environ["DCH_SOCKET_DIR"] = tmp
    sock = os.path.join(tmp, SESS + ".sock")
    child = ("import sys,time;sys.stdout.write('\\x1b[?1049h\\x1b[>1uREADY\\n');"
             "sys.stdout.flush();open(%r,'w').close();time.sleep(60)" % pending)
    env = dict(os.environ, DCH_NO_VT="1")
    master = subprocess.Popen([DCH, "--master-of", sock, "--",
                               sys.executable, "-c", child], env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(100):
        if os.path.exists(sock):
            break
        time.sleep(0.05)
    if not os.path.exists(sock):
        master.terminate()
        master.wait()
        shutil.rmtree(tmp, ignore_errors=True)
        print("FAIL: no master socket"); return 1
    for _ in range(100):
        if os.path.exists(pending):
            break
        time.sleep(0.05)
    if not os.path.exists(pending):
        os.system("'%s' -k %s >/dev/null 2>&1" % (DCH, SESS))
        master.terminate()
        shutil.rmtree(tmp, ignore_errors=True)
        print("FAIL: inner program did not queue startup output"); return 1
    pid_a, fd_a = spawn(["-f", "-n", SESS])
    pid_b = None
    try:
        first = drain(fd_a, 15.0, b"READY")
        if b"READY" not in first:
            print("FAIL: inner program never started:", repr(first)); return 1
        if first.count(ALT_ON) != 1 or first.count(PUSH) != 1:
            print("FAIL: first attach duplicated child modes:", repr(first)); return 1
        if check_detach("first", detach(pid_a, fd_a)):
            return 1
        pid_a = None

        pid_b, fd_b = spawn(["-f", "-n", SESS])
        replay = drain(fd_b, 10.0, PUSH)
        alt, push = replay.find(ALT_ON), replay.find(PUSH)
        if alt < 0 or push < 0 or replay.count(ALT_ON) != 1 \
           or replay.count(PUSH) != 1:
            print("FAIL: reattach did not re-arm alt screen + kitty push:", repr(replay))
            return 1
        if push < alt:
            print("FAIL: kitty push replayed before 1049h, lands on the primary screen:",
                  repr(replay))
            return 1
        if check_detach("reattach", detach(pid_b, fd_b)):
            return 1
        pid_b = None
        print("PASS: kitty push follows 1049h on attach; detach pops on both screens.")
        return 0
    finally:
        for pid in (pid_b, pid_a):
            if pid:
                try:
                    os.kill(pid, signal.SIGHUP)
                except OSError:
                    pass
        os.system("'%s' -k %s >/dev/null 2>&1" % (DCH, SESS))
        if master.poll() is None:
            master.terminate()
            master.wait()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
