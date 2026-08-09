#!/usr/bin/env python3
"""Attaching must replay the screen the session already has.

Regression test for the blank-attach bug: dch inherited dtach's contract of
clearing the client's screen and poking the program with SIGWINCH, trusting it
to repaint. Diff-based renderers (Ink, and so Claude Code) don't — they compare
against their own model of the screen, which dch just invalidated behind their
back, so unchanged rows are never re-emitted and the attach looks blank until
you resize the window.

The inner program here prints once and then never writes again, which is the
same situation from the client's side: nothing new arrives, so anything the
attaching client sees had to come from the master's mirror.

Run:  python3 tests/replay_test.py            (uses ./dch)
      DCH=/path/to/dch python3 tests/replay_test.py
"""
import os, sys, pty, signal, select, time, errno

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))
MARK = "REPLAY-MARK"


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


def spawn_client(extra, env=None):
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.pop("DCH_SESSION", None)
        if env:
            os.environ.update(env)
        os.execv(DCH, [DCH, "-E"] + extra)
        os._exit(127)
    return pid, fd


def attach_sees_marker(sess, env):
    """Start a quiet session, attach a second client, return what it saw."""
    os.system("'%s' -k %s >/dev/null 2>&1" % (DCH, sess))
    pid_a, fd_a = spawn_client(
        ["-n", sess, "sh", "-c", "printf '%s\\n'; sleep 60" % MARK], env)
    pid_b = None
    try:
        if MARK not in drain(fd_a, 15.0, MARK.encode()):
            return None
        # Quiet period: make sure the program really has stopped writing, so
        # the attaching client can only get the marker from the mirror.
        drain(fd_a, 1.0)
        pid_b, fd_b = spawn_client(["-f", "-n", sess], env)
        return drain(fd_b, 10.0, MARK.encode())
    finally:
        for p in (pid_b, pid_a):
            if p:
                try:
                    os.kill(p, signal.SIGHUP)
                except OSError:
                    pass
        os.system("'%s' -k %s >/dev/null 2>&1" % (DCH, sess))


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1

    got = attach_sees_marker("replaytest-%d" % os.getpid(), None)
    if got is None:
        print("FAIL: inner program never printed the marker")
        return 1
    if MARK not in got:
        print("FAIL: attach did not replay the screen. Client B saw:",
              repr(got))
        return 1

    # Negative control: with replay disabled the marker cannot appear, which
    # is what proves the check above is actually testing the replay and not
    # some incidental repaint.
    off = attach_sees_marker("replayoff-%d" % os.getpid(),
                             {"DCH_NO_REPLAY": "1"})
    if off is None:
        print("FAIL: inner program never printed the marker (no-replay run)")
        return 1
    if MARK in off:
        print("FAIL: DCH_NO_REPLAY=1 still replayed. Client B saw:", repr(off))
        return 1

    print("PASS: attach replays the screen; DCH_NO_REPLAY=1 disables it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
