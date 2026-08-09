#!/usr/bin/env python3
"""A half-written packet must survive `--restart`.

The master re-execs with its client sockets carried across, so whatever bytes
it had already read off one of them but not yet consumed as a whole frame have
to be carried too. Drop them and the next read() lands mid-packet: the tail of
a payload is reinterpreted as a header, and a leading 0x00 there is MSG_PUSH —
i.e. the rest of a keystroke burst gets typed into the program.

Nothing in the normal client can produce a torn write on demand, so this test
speaks the wire protocol directly: attach a raw socket, send the first half of
a MSG_PUSH frame, restart, send the second half, and check the program saw the
whole thing exactly once.

Run:  python3 tests/restart_partial_test.py       (uses ./dch)
"""
import os, sys, socket, subprocess, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))
MSG_PUSH, MSG_ATTACH = 0, 1
LINE = b"echo torn-ok\n"


def sock_path(name):
    d = os.environ.get("DCH_SOCKET_DIR")
    if not d:
        xdg = os.environ.get("XDG_RUNTIME_DIR")
        d = ("%s/dch-%d" % (xdg, os.getuid()) if xdg
             else "/tmp/dch-%d" % os.getuid())
    return os.path.join(d, name + ".sock")


def frame(mtype, payload):
    return bytes([mtype, len(payload) & 0xff, len(payload) >> 8]) + payload


def read_screen(sess):
    return subprocess.run([DCH, "--read", sess], capture_output=True,
                          text=True).stdout


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1

    sess = "restartpart-%d" % os.getpid()
    subprocess.call([DCH, "-k", sess], stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL)
    if subprocess.call([DCH, "--spawn", sess, "sh"],
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL) != 0:
        print("FAIL: could not spawn the session")
        return 1

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(sock_path(sess))
        s.sendall(frame(MSG_ATTACH, b""))
        time.sleep(0.3)

        # Half a frame: header plus the first few payload bytes.
        whole = frame(MSG_PUSH, LINE)
        s.sendall(whole[:6])
        time.sleep(0.3)

        rc = subprocess.call([DCH, "--restart", sess],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        if rc != 0:
            print("FAIL: --restart exited", rc)
            return 1

        # The rest, to the same socket, now served by the new image.
        s.sendall(whole[6:])
        time.sleep(1.0)

        screen = read_screen(sess)
        # Command line echoed by the tty, then its output: exactly two.
        # A dropped prefix types only the payload tail, so the command line
        # would be truncated and the output line would never appear at all.
        if "echo torn-ok" not in screen or screen.count("torn-ok") != 2:
            print("FAIL: the split frame did not arrive intact. Screen:",
                  repr(screen))
            return 1
    finally:
        s.close()
        subprocess.call([DCH, "-k", sess], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)

    print("PASS: a half-written packet survives --restart intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
