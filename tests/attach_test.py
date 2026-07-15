#!/usr/bin/env python3
"""Attach must repaint via WINCH and must NOT type ^L into the program.

Regression test for the "press ctrl+l again to clear" bug: dch's attach-time
MSG_REDRAW used to resolve to REDRAW_CTRL_L, so every attach typed a ^L into a
raw-mode inner program. Claude Code >=2.1.94 binds ^L to clear-input — each
attach flashed its confirm hint and two quick attaches wiped the session.

The inner TUI (attach_tui.py) sits in raw no-echo mode — the exact state that
made the master type ^L — and reports KEY:<hex> per stdin byte and WINCH per
SIGWINCH. A second client attaching must produce a WINCH and zero KEYs.

Run:  python3 tests/attach_test.py            (uses ./dch)
      DCH=/path/to/dch python3 tests/attach_test.py
"""
import os, sys, pty, signal, select, time, errno

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))
SESS = "attachtest-%d" % os.getpid()
TUI = os.path.join(HERE, "attach_tui.py")


def drain(fd, seconds):
    out = b""
    end = time.time() + seconds
    while time.time() < end:
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


def spawn_client(extra):
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.pop("DCH_SESSION", None)
        os.execv(DCH, [DCH, "-E"] + extra)
        os._exit(127)
    return pid, fd


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH); return 1
    os.system("'%s' -k %s >/dev/null 2>&1" % (DCH, SESS))

    # Client A creates the session running the raw-mode TUI.
    pid_a, fd_a = spawn_client(["-n", SESS, sys.executable, TUI])
    pid_b = None
    try:
        first = drain(fd_a, 2.0)
        if "HELLO" not in first:
            print("FAIL: no HELLO from inner program. Got:", repr(first)); return 1

        # Client B attaches (mirror). This is the phone reconnect path.
        pid_b, fd_b = spawn_client(["-f", "-n", SESS])
        out_b = drain(fd_b, 2.0)
        out_a = drain(fd_a, 0.5)
        combined = out_a + out_b

        if "KEY:" in combined:
            print("FAIL: attach typed bytes into the inner program:", repr(combined))
            return 1
        if "WINCH" not in combined:
            print("FAIL: attach produced no WINCH — no repaint requested.")
            print("  a:", repr(out_a)); print("  b:", repr(out_b))
            return 1
        print("PASS: attach repainted via WINCH and typed no ^L.")
        return 0
    finally:
        for p in (pid_b, pid_a):
            if p:
                try: os.kill(p, signal.SIGHUP)
                except OSError: pass
        os.system("'%s' -k %s >/dev/null 2>&1" % (DCH, SESS))


if __name__ == "__main__":
    sys.exit(main())
