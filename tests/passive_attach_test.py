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
#578: with the program's focus reporting (1004) on, a passive client's
ESC[I / ESC[O never reach it, split across two pushes neither, while an
active client's do, after --active its own do again, and a runtime
--passive stops them; its other input,
a lone Esc too, still goes in.
Self-isolating (own XDG_RUNTIME_DIR).

Run:  python3 tests/passive_attach_test.py          (uses ./dch)
"""
import os, sys, json, time, fcntl, struct, termios, socket
import shutil, signal, tempfile, threading, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
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

    def __init__(self, argv, cols, rows_, cwd=None, env=None):
        self.pid, self.fd = os.forkpty()
        if self.pid == 0:
            try:	# a failed exec must not run the parent's finally: it kills p
                if cwd:
                    os.chdir(cwd)
                os.execve(DCH, [DCH] + argv, env or ENV)
            finally:
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


REC = r'''
import os, sys, tty
tty.setraw(0)
os.write(1, b"\033[?1004hrec\r\n")
with open(sys.argv[1], "ab", buffering=0) as f:
    while True:
        b = os.read(0, 4096)
        if not b:
            break
        f.write(b)
        with open(sys.argv[1] + ".reads", "ab", buffering=0) as g:
            g.write(b.hex().encode() + b"\n")	# one line per read
'''


def focus_rows():
    """#578: a program with 1004 on records what it reads."""
    name = "paf%d" % os.getpid()
    rec, log = os.path.join(TMP, "rec.py"), os.path.join(TMP, "rec.log")
    with open(rec, "w") as f:
        f.write(REC)
    open(log, "wb").close()
    r = dch("--spawn", name, "--size", "100x30", sys.executable, rec, log)
    check(r.returncode == 0, "focus spawn: rc=%d err=%r"
          % (r.returncode, r.stderr))
    seen = [0]

    def fed(c, data, mark, pause=0.0):
        """Type data then mark on c; what the program read up to mark."""
        for d in data:
            os.write(c.fd, d)
            time.sleep(pause)
        os.write(c.fd, mark)
        dl = time.time() + DUE
        while time.time() < dl:
            with open(log, "rb") as f:
                b = f.read()
            i = b.find(mark, seen[0])
            if i >= 0:
                got, seen[0] = b[seen[0]:i], i + len(mark)
                return got
            time.sleep(0.05)
        return None

    cl = []
    try:
        wait_row(name, lambda o: "clients" in o, time.time() + DUE)
        time.sleep(0.5)	# its 1004h is out: the attaches' replay has it
        a = Client(["-E", "-a", name], 100, 30)
        p = Client(["-E", "-a", name, "--passive"], 90, 25)
        q = Client(["-E", "-a", name, "--passive"], 90, 25,
                   env=dict(ENV, DCH_FOCUS_RESIZE="0"))
        cl += [a, p, q]
        wait_row(name, lambda o: counts(o) == (1, 2), time.time() + DUE)
        time.sleep(0.5)

        got = fed(a, [b"\033[I"], b"A")
        check(got == b"\033[I",
              "1004 on: an active client's focus-in reaches the program:"
              " %r" % (got,))
        got = fed(p, [b"\033[I", b"\033[O"], b"P", 0.2)
        check(got == b"",
              "1004 on: a passive client's focus-in/out never reach the"
              " program: %r" % (got,))
        got = fed(q, [b"\033[", b"O"], b"Q", 0.01)
        check(got == b"",
              "a passive client's focus-out split across two pushes never"
              " reaches the program: %r" % (got,))
        got = fed(p, [b"x\033[Iy"], b"P")
        check(got == b"xy",
              "a passive client's other input still reaches the program"
              " (around a focus-in): %r" % (got,))
        t0 = time.time()
        got = fed(q, [b"\033"], b"Q", 0.3)
        check(got == b"\033" and time.time() - t0 < 2,
              "a passive client's lone Esc still goes in: %r" % (got,))

        # #585: no second hold on top of attach.c's own (50 ms). The Esc
        # must reach the program before j exists, so a slow reader cannot
        # merge them, and within 90 ms: attach.c's hold plus a master hold
        # is >= 100. A loaded runner may blow it once, so any of 5 tries
        # may pass.
        def reads():
            with open(log + ".reads", "rb") as f:
                return f.read().split()

        for _ in range(5):
            n0 = len(reads())
            t0 = time.time()
            os.write(p.fd, b"\033")
            while time.time() < t0 + DUE and reads()[n0:] != [b"1b"]:
                time.sleep(0.001)
            lat = time.time() - t0
            got = fed(p, [b"j"], b"P")
            rd = reads()[n0:]
            if lat < 0.09 and got == b"\033j" and rd[:1] == [b"1b"]:
                break
        check(lat < 0.09 and got == b"\033j" and rd[:1] == [b"1b"]
              and not any(x.startswith(b"1b6a") for x in rd),
              "a passive client's lone Esc reaches the program on its own,"
              " not held: %.0f ms %r" % (lat * 1000, rd))
        fed(p, [], b"P")

        r = dch("--active", name, "--pid", str(p.pid))
        got = fed(p, [b"\033[O"], b"P")
        check(r.returncode == 0 and got == b"\033[O",
              "after --active its focus-out reaches the program again:"
              " rc=%d %r" % (r.returncode, got))
        got = fed(a, [b"\033[O"], b"A")
        check(got == b"\033[O",
              "an active client's focus-out reaches the program: %r"
              % (got,))
        r = dch("--passive", name, "--pid", str(a.pid))
        got = fed(a, [b"\033[I", b"\033[O"], b"A", 0.2)
        check(r.returncode == 0 and got == b"",
              "--passive at runtime: its focus-in/out no longer reach the"
              " program: rc=%d %r" % (r.returncode, got))
    finally:
        for c in cl:
            c.kill()
        dch("-k", name)


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
        j = Client(["--"], 80, 24, cwd=proj)
        cl.append(j)
        o = wait_row(name, lambda o: counts(o) == (1, 1), dl)
        check(counts(o) == (1, 1) and name + "-2" not in rows(),
              "plain dch joins a session whose only client is passive, no"
              " -2: %r" % (o,))
        check(counts(o) == (1, 1) and size(o) == (80, 24),
              "the joined active client sets the size: %r" % (o,))

        dl = time.time() + DUE
        k = Client(["--"], 80, 24, cwd=proj)
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
        k = Client(["--"], 80, 24, cwd=proj)
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

    focus_rows()

    shutil.rmtree(TMP, ignore_errors=True)
    print("%d failure(s)" % len(fails) if fails else "ok")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
