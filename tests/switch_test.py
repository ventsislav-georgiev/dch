#!/usr/bin/env python3
"""Double-tapping the detach key switches sessions; one tap still detaches.

Covers, in order:
  1. two taps land you in a *different* session picked from the list;
  2. the session you left keeps no client marker behind (live_clients_on()
     counts those, so a leak makes a free session look busy forever);
  3. one tap detaches even while the session floods output — the window is a
     deadline, not an idle timeout, or a chatty program would hold it open
     forever and the press would never land;
  4. typing something else inside the window commits the detach;
  5. DCH_DOUBLE_TAP_MS=0 turns the shortcut off and detach is instant.

Run:  python3 tests/switch_test.py            (uses ./dch)
      DCH=/path/to/dch python3 tests/switch_test.py
"""
import os, sys, pty, signal, select, time, errno, re, subprocess, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))
TAG = "sw%d" % os.getpid()
A, B = TAG + "a", TAG + "b"
DETACH = b"\x1c"
SOCKDIR = os.path.join(tempfile.mkdtemp(prefix="dchswitch"), "sock")


def dch(*args, **kw):
    env = dict(os.environ, DCH_SOCKET_DIR=SOCKDIR)
    env.pop("DCH_SESSION", None)
    env.update(kw.pop("env", {}))
    return subprocess.run([DCH] + list(args), env=env, capture_output=True,
                          text=True, timeout=30)


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
    """Attach a real client on its own pty. No -E: we need the detach key."""
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.pop("DCH_SESSION", None)
        os.environ["DCH_SOCKET_DIR"] = SOCKDIR
        os.environ.update(env or {})
        os.execv(DCH, [DCH] + extra)
        os._exit(127)
    return pid, fd


def reap(pid, fd, seconds):
    """Wait for pid; return its status, or None if it outlived `seconds`.
    Keeps draining fd so the client never blocks on a full pty buffer."""
    end = time.time() + seconds
    while time.time() < end:
        got, status = os.waitpid(pid, os.WNOHANG)
        if got:
            return status
        if fd is not None:
            drain(fd, 0.05)
        else:
            time.sleep(0.05)
    return None


def kill_client(pid, fd=None):
    if pid is None:
        return
    for sig in (signal.SIGHUP, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except OSError:
            return
        if reap(pid, fd, 3.0) is not None:
            return


def picker_rows(text):
    """Session names as drawn by the picker, top row first."""
    rows = []
    for line in text.split("\n"):
        m = re.search(r"(%s[ab])" % TAG, line)
        if m and m.group(1) not in rows:
            rows.append(m.group(1))
    return rows


def client_markers(session):
    try:
        return [f for f in os.listdir(SOCKDIR)
                if f.startswith(session + ".sock.client.")]
    except OSError:
        return []


def fail(msg, *extra):
    print("FAIL:", msg)
    for e in extra:
        print("  ", repr(e))
    return 1


def main():
    if not os.access(DCH, os.X_OK):
        return fail("dch not executable at " + DCH)

    # Two headless sessions with distinct, identifiable screens. A also keeps
    # printing, so case 3 exercises the deadline against a chatty session.
    dch("--spawn", A, "--size", "80x24", "sh", "-c",
        "while :; do echo AAA_ALPHA; sleep 0.05; done")
    dch("--spawn", B, "--size", "80x24", "sh", "-c",
        "echo BBB_BRAVO; sleep 600")
    if dch("--wait", A, "--match", "AAA_ALPHA", "--timeout", "10000").returncode:
        return fail("session A never produced output")
    if dch("--wait", B, "--match", "BBB_BRAVO", "--timeout", "10000").returncode:
        return fail("session B never produced output")

    pid, fd = None, None
    try:
        # 1. Double tap -> picker -> land in the other session.
        pid, fd = spawn_client(["-f", "-n", A])
        if "AAA_ALPHA" not in drain(fd, 10.0, b"AAA_ALPHA"):
            return fail("client never showed session A")
        os.write(fd, DETACH)
        time.sleep(0.05)
        os.write(fd, DETACH)
        # Drain the whole picker frame, not just up to the header — the rows
        # arrive in the same write but `until` would stop us at the first one.
        out = drain(fd, 5.0, b"switch to session:") + drain(fd, 0.5)
        if "switch to session:" not in out:
            return fail("double tap did not open the picker", out)
        rows = picker_rows(out)
        if B not in rows:
            return fail("picker did not list the other session", rows, out)
        os.write(fd, b"j" * rows.index(B) + b"\r")
        landed = drain(fd, 10.0, b"BBB_BRAVO")
        if "BBB_BRAVO" not in landed:
            return fail("switch did not attach to the picked session", landed)

        # 2. No client marker left behind on the session we walked away from.
        stale = client_markers(A)
        if stale:
            return fail("stale client marker on the old session", stale)
        if not client_markers(B):
            return fail("no client marker on the session we switched into")
        kill_client(pid, fd)
        pid = None

        # 3. One tap detaches even while the session floods output.
        pid, fd = spawn_client(["-f", "-n", A])
        drain(fd, 10.0, b"AAA_ALPHA")
        os.write(fd, DETACH)
        if reap(pid, fd, 5.0) is None:
            return fail("single tap did not detach a chatty session")
        pid = None

        # 4. Any other key inside the window commits the detach.
        pid, fd = spawn_client(["-f", "-n", B])
        drain(fd, 10.0, b"BBB_BRAVO")
        os.write(fd, DETACH)
        time.sleep(0.02)
        os.write(fd, b"x")
        if reap(pid, fd, 5.0) is None:
            return fail("a key inside the window did not commit the detach")
        pid = None

        # 5. Opting out restores instant detach.
        pid, fd = spawn_client(["-f", "-n", B], env={"DCH_DOUBLE_TAP_MS": "0"})
        drain(fd, 10.0, b"BBB_BRAVO")
        t0 = time.time()
        os.write(fd, DETACH)
        if reap(pid, fd, 5.0) is None:
            return fail("DCH_DOUBLE_TAP_MS=0 did not detach")
        if time.time() - t0 > 0.25:
            return fail("DCH_DOUBLE_TAP_MS=0 still waited out a window")
        pid = None

        print("PASS: double tap switches sessions, single tap detaches.")
        return 0
    finally:
        kill_client(pid, fd)
        for s in (A, B):
            dch("-k", s)


if __name__ == "__main__":
    sys.exit(main())
