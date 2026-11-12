#!/usr/bin/env python3
"""A terminal-mode sequence split across two pty reads is still remembered.

The master scans the child's output for the modes it re-arms on a later
attach (DEC private modes: alt screen, mouse, bracketed paste; kitty
keyboard push/pop; modifyOtherKeys). The child here writes each sequence in
two writes and the second half only after a first attached client has
received the first half, so the master has consumed it in an earlier read:
the split is forced, not timing luck. Splits: after ESC, after ESC[, inside
the parameter digits, after the marker, inside the parameters. A second
client then attaches and its re-arm bytes are checked; then a whole set of
kitty push + 1000h is undone by a split pop and a split 1000l, and a third
client must not be re-armed with either. Runs on a DCH_NO_VT master and on
the mirror one (the same on a lite build).

Run:  python3 tests/mode_split_test.py            (uses ./dch)
"""
import binascii, os, sys, shutil, signal, subprocess, tempfile, time
from kbd_altscreen_test import drain, detach, spawn

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))

# Each (first write, second write).
PHASE1 = (
    (b"\x1b", b"[?1049h"),               # ESC at the very end of a read
    (b"\x1b[?10", b"00;1006h"),          # inside the digits, grouped params
    (b"\x1b[", b"?2004h"),               # ESC[ at the end
    (b"\x1b[>", b"1u"),                  # kitty push, after the marker
    (b"\x1b[>4;", b"2m"),                # modifyOtherKeys, inside params
)
WANT1 = (b"\x1b[?1049h", b"\x1b[?1000h", b"\x1b[?1006h", b"\x1b[?2004h",
         b"\x1b[>1u", b"\x1b[>4;2m")
WHOLE2 = b"\x1b[>1u\x1b[?1000h\x1b[?1002h"     # one write, the anchor 1002h
PHASE2 = (
    (b"\x1b[<", b"u"),                   # kitty pop
    (b"\x1b[?100", b"0l"),               # reset, inside the digits
)

CHILD = """import binascii, os, sys
f = open(sys.argv[1], 'rb')
os.write(1, b'READY')
for line in f:
    os.write(1, binascii.unhexlify(line.strip()))
"""


def run(label, env_extra):
    sess = "modesplit-%d-%s" % (os.getpid(), label)
    tmp = tempfile.mkdtemp(prefix="dch-modesplit-")
    os.environ["DCH_SOCKET_DIR"] = tmp
    sock = os.path.join(tmp, sess + ".sock")
    fifo = os.path.join(tmp, "cmd")
    os.mkfifo(fifo)
    master = subprocess.Popen([DCH, "--master-of", sock, "--", sys.executable,
                               "-c", CHILD, fifo],
                              env=dict(os.environ, **env_extra),
                              stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,
                              start_new_session=True)
    pids, w = [], None
    seen = [b""]

    def send(want):
        """The child writes want in one write; wait until the watcher
        shows it, so the master has read all of it."""
        w.write(binascii.hexlify(want) + b"\n")
        w.flush()
        end = time.time() + 10
        while want not in seen[0] and time.time() < end:
            seen[0] += drain(fd_a, 0.2)
        if want not in seen[0]:
            raise RuntimeError("watcher never saw %r" % want)

    def attach_check(name):
        pid, fd = spawn(["-f", "-n", sess])
        pids.append(pid)
        out = drain(fd, 10.0, b"\x1b[?1002h" if name == "third" else
                    b"\x1b[?1006h")
        out += drain(fd, 0.5)
        detach(pid, fd)
        pids.remove(pid)
        return out

    try:
        for _ in range(100):
            if os.path.exists(sock):
                break
            time.sleep(0.05)
        if not os.path.exists(sock):
            print("FAIL: %s: no master socket" % label)
            return 1
        pid_a, fd_a = spawn(["-f", "-n", sess])
        pids.append(pid_a)
        w = open(fifo, "wb")
        seen[0] = drain(fd_a, 15.0, b"READY")
        if b"READY" not in seen[0]:
            print("FAIL: %s: child never started: %r" % (label, seen[0]))
            return 1
        for i, (a, b) in enumerate(PHASE1):
            send(b"<%da>" % i + a)
            send(b + b"<%db>" % i)
        bad = 0
        out = attach_check("second")
        miss = [m for m in WANT1 if m not in out]
        if miss:
            print("FAIL: %s: split sequences not re-armed on attach: missing "
                  "%r in %r" % (label, miss, out))
            bad = 1
        send(b"<w>" + WHOLE2)
        for i, (a, b) in enumerate(PHASE2):
            send(b"<%dp>" % i + a)
            send(b + b"<%dq>" % i)
        out = attach_check("third")
        if b"\x1b[?1002h" not in out:
            print("FAIL: %s: third attach not re-armed at all: %r"
                  % (label, out))
            bad = 1
        if b"\x1b[>1u" in out or b"\x1b[?1000h" in out:
            print("FAIL: %s: a split pop / 1000l was not remembered, the "
                  "third attach re-arms what the child turned off: %r"
                  % (label, out))
            bad = 1
        return bad
    finally:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGHUP)
            except OSError:
                pass
        if w:
            w.close()
        os.system("'%s' -k %s >/dev/null 2>&1" % (DCH, sess))
        if master.poll() is None:
            master.terminate()
            master.wait()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    rc = run("novt", {"DCH_NO_VT": "1"}) | run("mirror", {})
    if not rc:
        print("PASS: mode sequences split across two pty reads are re-armed "
              "on a later attach (DCH_NO_VT and mirror).")
    return rc


if __name__ == "__main__":
    sys.exit(main())
