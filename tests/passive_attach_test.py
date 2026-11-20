#!/usr/bin/env python3
"""Passive attach (#566): a client marked passive (dch -a <name> --passive, or
dch --passive <name> --pid <pid> at runtime) still gets the output, but never
sets the session's size and makes no session busy: plain `dch` joins a
session whose only client is passive instead of starting <name>-2. --ls-json
counts it in "passive_clients", not "clients". dch --active turns it back.

Checks: a passive attach leaves the size and counts 0 clients, 1 passive; its
resize changes nothing; plain dch joins (no -2) and then counts 1 + 1; with
that active client attached, plain dch advances to -2; --active restores the
size and the busy rule; --passive at runtime marks it again; an unknown pid,
no session and an old master (DCH_ST_UNSUP) exit 1 with a reason.
Self-isolating (own XDG_RUNTIME_DIR).

Run:  python3 tests/passive_attach_test.py          (uses ./dch)
"""
import os, sys, json, time, fcntl, struct, termios, socket
import shutil, signal, tempfile, threading, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))
DUE = 20.0 if os.environ.get("DCH_TEST_NO_BUDGET") else 5.0
MSG_ACK = 11

TMP = tempfile.mkdtemp(dir=os.environ.get("DCH_TEST_TMP", "/tmp"), prefix="dchpa")
ENV = dict(os.environ, XDG_RUNTIME_DIR=TMP, XDG_STATE_HOME=TMP + "/state",
           HOME=TMP + "/home", DCH_CONFIG=TMP + "/config",
           DCH_LOGIN_ENV="0", DCH_NO_REMOTE="1", SHELL="/bin/sh")
for k in ("DCH_SOCKET_DIR", "DCH_SESSION"):
    ENV.pop(k, None)
SOCKDIR = os.path.join(TMP, "dch-%d" % os.getuid())
fails = []


def dch(*args):
    return subprocess.run([DCH] + list(args), env=ENV, capture_output=True,
                          text=True, timeout=30)


def check(cond, what):
    print(("ok   " if cond else "FAIL ") + what)
    if not cond:
        fails.append(what)
    return cond


def rows():
    r = dch("--ls-json")
    try:
        return {o["name"]: o for o in json.loads(r.stdout)}
    except ValueError:
        return {}


def wait_row(name, pred, deadline):
    """Poll --ls-json until pred(row); deadline is taken BEFORE the action."""
    o = None
    while time.time() < deadline:
        o = rows().get(name)
        if o is not None and pred(o):
            return o
        time.sleep(0.05)
    return o


def size(o):
    return o and (o.get("cols"), o.get("rows"))


def counts(o):
    return o and (o.get("clients"), o.get("passive_clients"))


def set_size(fd, cols, rows_):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows_, cols, 0, 0))


class Client:
    """dch with argv on its own pty (cwd cwd), output drained."""

    def __init__(self, argv, cols, rows_, cwd=None):
        self.pid, self.fd = os.forkpty()
        if self.pid == 0:
            if cwd:
                os.chdir(cwd)
            os.execve(DCH, [DCH] + argv, ENV)
            os._exit(127)
        set_size(self.fd, cols, rows_)
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self):
        while True:
            try:
                if not os.read(self.fd, 65536):
                    return
            except OSError:
                return

    def resize(self, cols, rows_):
        set_size(self.fd, cols, rows_)
        os.kill(self.pid, signal.SIGWINCH)

    def kill(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        except OSError:
            pass


def old_master(name):
    """A listening socket that answers every verb with DCH_ST_UNSUP."""
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
                    h = c.recv(3)
                    n = h[1] | h[2] << 8 if len(h) == 3 else 0
                    while n > 0:
                        b = c.recv(n)
                        if not b:
                            break
                        n -= len(b)
                    c.sendall(struct.pack("<BH", MSG_ACK, 5))
                    while c.recv(4096):
                        pass
                except OSError:
                    pass
    threading.Thread(target=loop, daemon=True).start()
    return srv, path


def main():
    os.makedirs(SOCKDIR, exist_ok=True)
    name = "pas%d" % os.getpid()
    proj = os.path.join(TMP, name)	# plain dch here auto-names it `name`
    os.makedirs(proj)
    os.makedirs(TMP + "/home", exist_ok=True)
    r = dch("--spawn", name, "--size", "100x30", "sh")
    check(r.returncode == 0, "spawn: rc=%d err=%r" % (r.returncode, r.stderr))
    wait_row(name, lambda o: "clients" in o, time.time() + DUE)

    cl = []
    try:
        dl = time.time() + DUE
        p = Client(["-E", "-a", name, "--scrollback", "2000", "--passive"],
                   90, 25)
        cl.append(p)
        o = wait_row(name, lambda o: counts(o) == (0, 1), dl)
        check(counts(o) == (0, 1),
              "ls-json: a passive attach counts 0 clients, 1 passive: %r"
              % (o,))
        check(counts(o) == (0, 1) and size(o) == (100, 30),
              "a passive attach leaves the size: %r" % (o,))

        p.resize(120, 40)
        time.sleep(1.0)
        o = rows().get(name)
        check(counts(o) == (0, 1) and size(o) == (100, 30),
              "a passive client's resize leaves the size: %r" % (o,))

        dl = time.time() + DUE
        j = Client([], 80, 24, cwd=proj)
        cl.append(j)
        o = wait_row(name, lambda o: counts(o) == (1, 1), dl)
        check(counts(o) == (1, 1) and name + "-2" not in rows(),
              "plain dch joins a session whose only client is passive, no"
              " -2: %r" % (o,))
        check(counts(o) == (1, 1) and size(o) == (80, 24),
              "the joined active client sets the size: %r" % (o,))

        dl = time.time() + DUE
        k = Client([], 80, 24, cwd=proj)
        cl.append(k)
        o = wait_row(name + "-2", lambda o: True, dl)
        check(o is not None and counts(rows().get(name)) == (1, 1),
              "with an active and a passive client, plain dch advances to"
              " -2")
        k.kill()
        dch("-k", name + "-2")

        r = dch("--active", name, "--pid", str(p.pid))
        check(r.returncode == 0, "--active: rc=%d err=%r"
              % (r.returncode, r.stderr))
        o = rows().get(name)
        check(counts(o) == (2, 0), "--active counts it as a client: %r"
              % (o,))
        dl = time.time() + DUE
        p.resize(130, 45)
        o = wait_row(name, lambda o: size(o) == (130, 45), dl)
        check(size(o) == (130, 45),
              "--active: its next resize sets the size: %r" % (o,))

        # busy again: with j gone, p alone makes plain dch advance
        j.kill()
        wait_row(name, lambda o: counts(o) == (1, 0), time.time() + DUE)
        dl = time.time() + DUE
        k = Client([], 80, 24, cwd=proj)
        cl.append(k)
        o = wait_row(name + "-2", lambda o: True, dl)
        check(o is not None, "--active: the session is busy again (-2)")
        k.kill()
        dch("-k", name + "-2")

        r = dch("--passive", name, "--pid", str(p.pid))
        o = rows().get(name)
        check(r.returncode == 0 and counts(o) == (0, 1),
              "--passive at runtime marks it passive: rc=%d %r"
              % (r.returncode, o))
        p.resize(70, 20)
        time.sleep(1.0)
        o = rows().get(name)
        check(size(o) == (130, 45),
              "--passive at runtime: its resize leaves the size: %r" % (o,))

        r = dch("--restart", name, "-f")
        o = wait_row(name, lambda o: counts(o) == (0, 1), time.time() + DUE)
        check(r.returncode == 0 and counts(o) == (0, 1),
              "--restart carries the passive flag: rc=%d %r"
              % (r.returncode, o))

        r = dch("--active", name, "--pid", "1")
        check(r.returncode == 1 and "no attached client" in r.stderr,
              "--active with an unknown pid exits 1: rc=%d err=%r"
              % (r.returncode, r.stderr))
        r = dch("--passive", "nosuch" + name, "--pid", str(p.pid))
        check(r.returncode == 1 and "no session" in r.stderr,
              "--passive on no session exits 1: rc=%d err=%r"
              % (r.returncode, r.stderr))
    finally:
        for c in cl:
            c.kill()
        dch("-k", name)
        dch("-k", name + "-2")

    srv, path = old_master("old" + name)
    try:
        r = dch("--passive", "old" + name, "--pid", "1")
        check(r.returncode == 1 and "older than this dch" in r.stderr,
              "--passive on an old master exits 1 with a reason: rc=%d "
              "err=%r" % (r.returncode, r.stderr))
    finally:
        srv.close()
        os.unlink(path)

    shutil.rmtree(TMP, ignore_errors=True)
    print("%d failure(s)" % len(fails) if fails else "ok")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
