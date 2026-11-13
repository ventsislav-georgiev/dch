#!/usr/bin/env python3
"""--ls-json "cols", "rows", "clients" (#099): the pty winsize the master holds
and how many clients are attached, from the MSG_CMD round trip --ls-json
already makes (DCH_CMD_F_STAT). Never by attaching, never resizing.

Checks: a headless session reports its --size and 0 clients; two attached
clients give 2 and one detach gives 1; a client resize shows in cols/rows;
polling --ls-json moves neither the size nor the activity stamp; a fake old
master (answers MSG_CMD without MSG_CMD_STAT) and one that answers every verb
with DCH_ST_UNSUP get no keys, not 0s. Self-isolating (own XDG_RUNTIME_DIR).

Run:  python3 tests/ls_json_size_test.py          (uses ./dch)
"""
import os, sys, json, time, fcntl, struct, termios, socket
import shutil, signal, tempfile, threading, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))
DUE = 20.0 if os.environ.get("DCH_TEST_NO_BUDGET") else 5.0
MSG_DETACH, MSG_READ_DATA, MSG_READ_END, MSG_ACK, MSG_CMD = 2, 8, 9, 11, 15

TMP = tempfile.mkdtemp(dir=os.environ.get("DCH_TEST_TMP", "/tmp"), prefix="dchls")
ENV = dict(os.environ, XDG_RUNTIME_DIR=TMP, XDG_STATE_HOME=TMP + "/state",
           HOME=TMP + "/home", DCH_CONFIG=TMP + "/config",
           DCH_LOGIN_ENV="0", DCH_NO_REMOTE="1")
for k in ("DCH_SOCKET_DIR", "DCH_SESSION"):
    ENV.pop(k, None)
SOCKDIR = os.path.join(TMP, "dch-%d" % os.getuid())
fails = []


def dch(*args):
    return subprocess.run([DCH] + list(args), env=ENV, capture_output=True,
                          text=True, timeout=30)


def expect(cond, what):
    if not cond:
        fails.append(what)
        print("FAIL:", what)
    return cond


def row(name):
    r = dch("--ls-json")
    for o in json.loads(r.stdout):
        if o["name"] == name:
            return o
    return None


def wait_row(name, pred, deadline):
    """Poll --ls-json until pred(row); deadline is taken BEFORE the action."""
    o = None
    while time.time() < deadline:
        o = row(name)
        if o is not None and pred(o):
            return o
        time.sleep(0.05)
    return o


def set_size(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class Client:
    """`dch -a name` on its own pty, output drained so it never blocks."""

    def __init__(self, name, cols, rows):
        self.pid, self.fd = os.forkpty()
        if self.pid == 0:
            os.execve(DCH, [DCH, "-a", name], ENV)
            os._exit(127)
        set_size(self.fd, cols, rows)
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self):
        while True:
            try:
                if not os.read(self.fd, 65536):
                    return
            except OSError:
                return

    def resize(self, cols, rows):
        set_size(self.fd, cols, rows)
        os.kill(self.pid, signal.SIGWINCH)

    def detach(self):
        os.write(self.fd, b"\x1c")  # the default detach key, ^\

    def gone(self, deadline):
        while time.time() < deadline:
            p, _ = os.waitpid(self.pid, os.WNOHANG)
            if p:
                return True
            time.sleep(0.05)
        return False

    def kill(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        except OSError:
            pass


def recv_packet(c):
    """One whole client packet: the header, then its len-byte payload (they
    may arrive in two writes)."""
    buf = b""
    while len(buf) < 3:
        b = c.recv(3 - len(buf))
        if not b:
            return None, None
        buf += b
    t, n = buf[0], buf[1] | buf[2] << 8
    p = b""
    while len(p) < n:
        b = c.recv(n - len(p))
        if not b:
            return None, None
        p += b
    return t, p


def fake_master(name, reply_for):
    """A listening socket that answers each packet with reply_for(type)."""
    path = os.path.join(SOCKDIR, name + ".sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(8)

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            with c:
                c.settimeout(2)
                try:
                    t, _ = recv_packet(c)
                    if t is not None:
                        c.sendall(reply_for(t))
                        # hold until the client closes: no unread-data RST
                        while c.recv(4096):
                            pass
                except OSError:
                    pass
    threading.Thread(target=loop, daemon=True).start()
    return srv, path


def frame(t, payload=b"", length=None):
    return struct.pack("<BH", t, len(payload) if length is None else length) \
        + payload


def main():
    os.makedirs(SOCKDIR, exist_ok=True)
    name = "lsz%d" % os.getpid()
    r = dch("--spawn", name, "--size", "100x30", "sh")
    expect(r.returncode == 0, "spawn: rc=%d err=%r" % (r.returncode, r.stderr))
    o = wait_row(name, lambda o: "clients" in o, time.time() + DUE)
    expect(o and (o.get("cols"), o.get("rows"), o.get("clients")) ==
           (100, 30, 0), "headless: want 100x30, 0 clients, got %r" % (o,))
    expect(o and all(k in o for k in ("alias", "state", "activity_epoch",
                                      "meta", "transcript_path")),
           "existing keys still there: %r" % (o,))

    # polling is read-only: the activity stamp and the size stay put
    act = os.path.join(SOCKDIR, name + ".sock.act")
    mt = lambda: os.stat(act).st_mtime if os.path.exists(act) else None
    # the shell's startup output stamps it too (twice, microseconds apart,
    # seen in CI as #106): take m0 once the stamp has settled
    m0, end = mt(), time.time() + DUE
    while time.time() < end:
        time.sleep(0.5)
        m, m0 = m0, mt()
        if m == m0:
            break
    time.sleep(1.1)
    for _ in range(3):
        o = row(name)
    m1 = mt()
    expect(m0 == m1, "--ls-json moved the activity stamp: %r -> %r" % (m0, m1))
    expect(o and (o.get("cols"), o.get("rows")) == (100, 30),
           "--ls-json resized the session: %r" % (o,))

    a = b = None
    try:
        dl = time.time() + DUE
        a = Client(name, 90, 25)
        b = Client(name, 90, 25)
        o = wait_row(name, lambda o: o.get("clients") == 2, dl)
        expect(o and o.get("clients") == 2, "two attached: %r" % (o,))
        expect(o and (o.get("cols"), o.get("rows")) == (90, 25),
               "attach size: want 90x25, got %r" % (o,))

        dl = time.time() + DUE
        a.resize(120, 40)
        o = wait_row(name, lambda o: (o.get("cols"), o.get("rows")) ==
                     (120, 40), dl)
        expect(o and (o.get("cols"), o.get("rows")) == (120, 40),
               "after resize: want 120x40, got %r" % (o,))

        dl = time.time() + DUE
        b.detach()
        expect(b.gone(dl), "client b did not exit on the detach key")
        o = wait_row(name, lambda o: o.get("clients") == 1, dl)
        expect(o and o.get("clients") == 1, "one detached: %r" % (o,))
    finally:
        for c in (a, b):
            if c:
                c.kill()
        dch("-k", name)

    # an old master: MSG_CMD answered the pre-#099 way, no MSG_CMD_STAT
    body = b"42\x00running\x00-1\x000\x001\x00sh\x00"
    old = lambda t: (frame(MSG_READ_DATA, body) + frame(MSG_READ_END, length=0)
                     if t == MSG_CMD else frame(MSG_ACK, length=5))
    srv, path = fake_master("old" + name, old)
    # older still: every verb is DCH_ST_UNSUP
    srv2, path2 = fake_master("older" + name, lambda t: frame(MSG_ACK, length=5))
    try:
        for n in ("old" + name, "older" + name):
            t0 = time.time()
            o = row(n)
            expect(o is not None and not any(k in o for k in
                   ("cols", "rows", "clients")),
                   "%s: keys must be omitted, got %r" % (n, o))
            expect(time.time() - t0 < DUE, "%s: --ls-json took %.1fs" %
                   (n, time.time() - t0))
    finally:
        for s, p in ((srv, path), (srv2, path2)):
            s.close()
            os.unlink(p)

    shutil.rmtree(TMP, ignore_errors=True)
    print("%d failure(s)" % len(fails) if fails else "ok")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
