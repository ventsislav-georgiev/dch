"""`dch --cmd` against a fake master sending odd but legal reply frames.

Regression (#076, found by the clang static analyzer): two empty
MSG_READ_DATA frames made cmd_query call realloc(buf, 0), which glibc
answers by freeing buf and returning NULL; the error path then freed buf a
second time. The sanitizer CI job turns that into a hard failure.

Run:  python3 tests/cmd_frames_test.py        (uses ./dch)
"""
import os, socket, struct, subprocess, sys, tempfile, threading

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
MSG_READ_DATA, MSG_READ_END, MSG_CMD = 8, 9, 15


def frame(t, payload=b"", length=None):
    n = len(payload) if length is None else length
    return struct.pack("<BH", t, n) + payload


def serve(srv, reply):
    c, _ = srv.accept()
    with c:
        hdr = c.recv(3)
        if hdr[:1] == bytes([MSG_CMD]):
            c.sendall(reply)


def cmd(sockdir, name, reply):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    path = os.path.join(sockdir, name + ".sock")
    srv.bind(path)
    srv.listen(1)
    t = threading.Thread(target=serve, args=(srv, reply))
    t.start()
    r = subprocess.run([DCH, "--cmd", name], capture_output=True, text=True,
                       timeout=20)
    t.join(5)
    srv.close()
    os.unlink(path)
    return r


def main():
    # run.sh exports an isolated XDG_RUNTIME_DIR; standalone gets its own
    env_dir = os.environ.get("XDG_RUNTIME_DIR") or tempfile.mkdtemp(dir="/tmp")
    os.environ["XDG_RUNTIME_DIR"] = env_dir
    os.environ.pop("DCH_SOCKET_DIR", None)
    sockdir = os.path.join(env_dir, "dch-%d" % os.getuid())
    os.makedirs(sockdir, exist_ok=True)
    body = b"42\x00running\x00-1\x000\x001\x00sh\x00tail"
    fails = 0

    for label, reply in [
        ("empty frames first", frame(MSG_READ_DATA) * 2 +
         frame(MSG_READ_DATA, body) + frame(MSG_READ_END, length=0)),
        ("empty frames between", frame(MSG_READ_DATA, body[:5]) +
         frame(MSG_READ_DATA) * 2 + frame(MSG_READ_DATA, body[5:]) +
         frame(MSG_READ_END, length=0)),
    ]:
        r = cmd(sockdir, "fake", reply)
        want = ('{"pid":42,"state":"running","argv":["sh"],"exit_code":null,'
                '"exit_signal":null,"tail":"tail"}')
        if r.returncode != 0 or r.stdout.strip() != want:
            fails += 1
            print("FAIL %s: rc=%d out=%r err=%r" % (label, r.returncode,
                                                   r.stdout, r.stderr))
    # Nothing but empty frames: no fields at all is a malformed reply.
    r = cmd(sockdir, "fake", frame(MSG_READ_DATA) * 3 +
            frame(MSG_READ_END, length=0))
    if r.returncode != 1 or "malformed" not in r.stderr:
        fails += 1
        print("FAIL only empty frames: rc=%d err=%r" % (r.returncode,
                                                        r.stderr))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
