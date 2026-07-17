#!/usr/bin/env python3
"""Headless end-to-end test of the on-demand redraw path (dch v0.14).

Replicates Prosper's PtyChild EXACTLY: forkpty a `dch` client, bridge its pty,
then `kill(client_pid, SIGUSR2)` — which must make the client send
MSG_REDRAW(REDRAW_WINCH), the master raise SIGWINCH at the inner program, and the
inner program repaint. No Prosper, no iOS, no release/bundle needed.

Run:  python3 tests/redraw_test.py            (uses ./dch)
      DCH=/path/to/dch python3 tests/redraw_test.py
"""
import os, sys, pty, signal, select, time, errno

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))
SESS = "redrawtest-%d" % os.getpid()
TUI = os.path.join(HERE, "redraw_tui.py")


def drain(fd, seconds, until=None):
    """Read from fd for `seconds` (early exit once `until` appears)."""
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
    if not os.path.exists(DCH):
        print("FAIL: dch binary not found at", DCH); return 1
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH); return 1

    # Clean any stale session of this name (ignore errors).
    os.system("'%s' -k %s >/dev/null 2>&1" % (DCH, SESS))

    # forkpty + exec dch as a CLIENT — identical to PtyChild.
    pid, fd = pty.fork()
    if pid == 0:
        # child: become the dch client attached to a fresh session running the TUI.
        # Strip DCH_SESSION so dch's nesting guard doesn't refuse (PtyChild does this).
        os.environ.pop("DCH_SESSION", None)
        os.execv(DCH, [DCH, "-E", "-n", SESS, sys.executable, TUI])
        os._exit(127)

    try:
        # 1. Initial paint: expect HELLO from the inner TUI (it repeats, so a
        #    slow attach still sees one; big deadline for loaded CI runners).
        first = drain(fd, 15.0, b"HELLO")
        if "HELLO" not in first:
            print("FAIL: no initial HELLO from inner program. Got:", repr(first))
            return 1
        baseline = first.count("REPAINT")

        # 2. Fire the redraw trigger (what PtyChild.redraw() does).
        os.kill(pid, signal.SIGUSR2)

        # 3. The inner program must repaint (receive SIGWINCH → emit REPAINT).
        after = drain(fd, 10.0, b"REPAINT")
        if after.count("REPAINT") < 1:
            print("FAIL: no REPAINT after SIGUSR2 — redraw did NOT reach the inner program.")
            print("  initial:", repr(first))
            print("  after  :", repr(after))
            return 1

        # 4. Idempotent: a second trigger repaints again (proves it's repeatable,
        #    not a one-shot attach artifact).
        os.kill(pid, signal.SIGUSR2)
        after2 = drain(fd, 10.0, b"REPAINT")
        if after2.count("REPAINT") < 1:
            print("FAIL: second SIGUSR2 produced no REPAINT (not repeatable).")
            return 1

        print("PASS: SIGUSR2 -> MSG_REDRAW(REDRAW_WINCH) -> inner SIGWINCH repaint (x2).")
        return 0
    finally:
        try: os.kill(pid, signal.SIGHUP)
        except OSError: pass
        os.system("'%s' -k %s >/dev/null 2>&1" % (DCH, SESS))


if __name__ == "__main__":
    sys.exit(main())
