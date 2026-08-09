#!/usr/bin/env python3
"""An attached client must survive `--restart` with its stream intact.

The control-verb restart tests only prove the master comes back. They cannot
see the part that actually breaks: an attached client's socket is carried
across the exec, so the new image has to re-adopt that fd, restore whatever
half-frame was pending on it, repaint the screen (nothing else will — the
program does not know a restart happened), and keep routing typed input to the
same child.

Run:  python3 tests/restart_attach_test.py       (uses ./dch)
      DCH=/path/to/dch python3 tests/restart_attach_test.py
"""
import os, sys, pty, signal, select, time, errno, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))
MARK = "BEFORE-MARK"
AFTER = "after-ok"


def drain(fd, seconds, until=None):
    out = b""
    end = time.time() + seconds
    while time.time() < end:
        if until and until in out:
            break
        r, _, _ = select.select([fd], [], [], 0.1)
        if fd in r:
            try:
                chunk = os.read(fd, 65536)
            except OSError as e:
                if e.errno in (errno.EIO, errno.EAGAIN):
                    break
                raise
            if not chunk:
                break
            out += chunk
    return out.decode("utf-8", "replace")


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1

    sess = "restartatt-%d" % os.getpid()
    os.system("'%s' -k %s >/dev/null 2>&1" % (DCH, sess))

    pid, fd = pty.fork()
    if pid == 0:
        os.environ.pop("DCH_SESSION", None)
        os.execv(DCH, [DCH, "-E", "-n", sess, "sh"])
        os._exit(127)

    try:
        os.write(fd, ("printf '%s\\n'\n" % MARK).encode())
        if MARK not in drain(fd, 15.0, MARK.encode()):
            print("FAIL: shell never echoed the marker before restart")
            return 1
        drain(fd, 1.0)          # let it go quiet

        rc = subprocess.call([DCH, "--restart", sess],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        if rc != 0:
            print("FAIL: --restart exited", rc)
            return 1

        # The carried client must be repainted by the new master; nothing else
        # is writing, so the marker can only come from the mirror.
        seen = drain(fd, 10.0, MARK.encode())
        if MARK not in seen:
            print("FAIL: attached client not repainted after restart. Saw:",
                  repr(seen))
            return 1
        # And the repaint has to clear first: the client's screen still holds
        # the pre-restart frame, and the snapshot is trimmed, so painting over
        # it without an erase leaves stale rows at the wrong offset.
        erase = seen.index("\033[H\033[J") if "\033[H\033[J" in seen else -1
        if erase < 0 or erase > seen.index(MARK):
            print("FAIL: repaint did not clear the screen before painting. "
                  "Saw:", repr(seen))
            return 1

        # And it must still be the same shell, still reachable.
        os.write(fd, ("printf '%s\\n'\n" % AFTER).encode())
        seen = drain(fd, 10.0, AFTER.encode())
        if AFTER not in seen:
            print("FAIL: typed input did not reach the child after restart. "
                  "Saw:", repr(seen))
            return 1
    finally:
        try:
            os.kill(pid, signal.SIGHUP)
        except OSError:
            pass
        os.system("'%s' -k %s >/dev/null 2>&1" % (DCH, sess))

    print("PASS: attached client survives --restart, repainted and live.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
