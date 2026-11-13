#!/usr/bin/env python3
"""Why a live session has no X-ray corpus while an agent runs (#256): the
--xray --json answer's "claudes" (the live Claudes found under the session,
there only when its ring is paused for an agent) and the view's line for it.

Two sessions on the host, each a plain child with hand-written Claude Code
records (procStart verified) under a temp HOME, never ~/.claude:
  nc-two   two sibling sleeps, both with a record: "claudes": 2, the view
           says "2 Claude sessions run here; X-ray reads one";
  nc-none  one sleep whose record sits under the master's own HOME, which
           the X-ray client's HOME does not hold: the master pauses its ring
           for that Claude, the client finds none: "claudes": 0, the view
           says "No Claude session found here" (dch-lite keeps no ring: its
           reason "lite" stands, the count is still there).
A plain shell with no agent has no "claudes". Each checked locally on the
host, then from a client over a real `dch serve` on 127.0.0.1
(DCH_SERVE_TEST_LOOPBACK=1, --port 0, a fake `tailscale` for whois), as
`dch --xray srv:<name> --json` and the view of srv:<name>: the host's answer
passes through. Each view runs on a pty this test owns.

Run:  python3 tests/xray_claudes_test.py      (uses ./dch)
"""
import fcntl, json, os, pty, re, select, shutil, signal, struct, subprocess
import sys, tempfile, termios, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from remote_client_test import FAKE_TS, OWNER, WHOIS  # noqa: E402

DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
TWO = b"2 Claude sessions run here; X-ray reads one"
NONE = b"No Claude session found here"
LITE = b"X-ray needs the full dch build"
fails = [0]


def check(msg, cond, detail=""):
    print("  %s %s" % ("ok  " if cond else "FAIL", msg))
    if not cond:
        fails[0] += 1
        if detail:
            print("       " + repr(detail)[-900:])


T = os.path.realpath(tempfile.mkdtemp(prefix="xrc", dir="/tmp"))
FAKE = os.path.join(T, "fake")
os.makedirs(FAKE)
with open(os.path.join(FAKE, "tailscale"), "w") as f:
    f.write(FAKE_TS)
os.chmod(os.path.join(FAKE, "tailscale"), 0o755)
for nm, o in (("status.json", OWNER), ("whois.json", WHOIS)):
    with open(os.path.join(FAKE, nm), "w") as f:
        f.write(json.dumps(o))
BASE = {k: v for k, v in os.environ.items()
        if not k.startswith(("DCH_", "Malloc", "CLAUDE", "NO_COLOR",
                             "COLORTERM"))}


def side(name):
    d = os.path.join(T, name)
    os.makedirs(os.path.join(d, "home"))
    return dict(BASE, XDG_RUNTIME_DIR=d, XDG_STATE_HOME=os.path.join(d, "st"),
                HOME=os.path.join(d, "home"), DCH_LOGIN_ENV="0",
                DCH_CONFIG=os.path.join(d, "config"), SHELL="/bin/sh",
                PATH=FAKE + ":/usr/bin:/bin", TERM="xterm-256color",
                NO_COLOR="1")


SENV = side("srv")
CENV = side("cli")
OTHER = os.path.join(T, "masterhome")	# nc-none's master's HOME
SLOG = os.path.join(T, "serve.log")


def dch(env, *a):
    return subprocess.run([DCH] + list(a), capture_output=True, text=True,
                          timeout=30, env=env, stdin=subprocess.DEVNULL)


def until(fn, secs=15.0):
    end = time.time() + secs
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def answer(env, name):
    try:
        return json.loads(dch(env, "--xray", name, "--json").stdout)
    except ValueError:
        return {}


class Pty:
    """A program on a pty: out holds every byte it wrote."""

    def __init__(self, argv, env):
        self.out = b""
        self.rc = None
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ,
                        struct.pack("HHHH", 30, 100, 0, 0))
            os.execve(argv[0], argv, env)
        os.set_inheritable(self.fd, False)

    def pump(self, secs):
        end = time.time() + secs
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [],
                                    max(0, end - time.time()))
            if not r:
                return
            try:
                b = os.read(self.fd, 65536)
            except OSError:
                b = b""
            if not b:
                self.reap()
                return
            self.out += b

    def reap(self):
        if self.rc is None:
            p, st = os.waitpid(self.pid, os.WNOHANG)
            if p:
                self.rc = os.waitstatus_to_exitcode(st)
        return self.rc

    def wait(self, what, secs=10.0):
        end = time.time() + secs
        while what not in self.out and time.time() < end and \
                self.rc is None:
            self.pump(0.1)
        return what in self.out

    def close(self):
        try:
            os.write(self.fd, b"q")
        except OSError:
            pass
        end = time.time() + 5
        while self.reap() is None and time.time() < end:
            self.pump(0.1)
        os.close(self.fd)
        if self.reap() is None:
            os.kill(self.pid, signal.SIGKILL)
            end = time.time() + 10
            while self.reap() is None and time.time() < end:
                time.sleep(0.05)
        return self.rc


def record(pid, sid, sessdir):
    start = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(pid)],
                           capture_output=True, text=True,
                           env={"LC_ALL": "C", "TZ": "UTC"}).stdout.strip()
    rec = {"pid": pid, "sessionId": sid, "cwd": T, "procStart": start,
           "version": "2.1.0", "kind": "interactive", "status": "idle",
           "statusUpdatedAt": 1}
    os.makedirs(sessdir, exist_ok=True)
    path = os.path.join(sessdir, "%d.json" % pid)
    with open(path + ".tmp", "w") as f:
        json.dump(rec, f)
    os.rename(path + ".tmp", path)


def child(env, name):
    try:
        return json.loads(dch(env, "--cmd", name).stdout)["pid"]
    except (ValueError, KeyError, TypeError):
        return None


def kids(pid, n):
    return until(lambda: (lambda k: k if len(k) == n else None)(
        subprocess.run(["/usr/bin/pgrep", "-P", str(pid)],
                       capture_output=True, text=True).stdout.split()))


def sessions():
    with open(SENV["DCH_CONFIG"], "w") as f:
        f.write("xray_ring = 64K\n")
    dch(SENV, "--spawn", "nc-sh", "/bin/sleep", "300")
    dch(SENV, "--spawn", "nc-two", "/bin/sh", "-c",
        "/bin/sleep 300 & /bin/sleep 300 & wait")
    sh = until(lambda: child(SENV, "nc-two"))
    two = kids(sh, 2) if sh else None
    for i, k in enumerate(two or []):
        record(int(k), "%08d-2560-4000-8000-000000000000" % i,
               os.path.join(SENV["HOME"], ".claude", "sessions"))
    # the master's HOME holds the record, the host's X-ray client's not
    os.makedirs(OTHER)
    dch(dict(SENV, HOME=OTHER), "--spawn", "nc-none", "/bin/sleep", "300")
    one = until(lambda: child(SENV, "nc-none"))
    if one:
        record(one, "00000000-2561-4000-8000-000000000000",
               os.path.join(OTHER, ".claude", "sessions"))
    check("host: nc-two's two Claudes and nc-none's one recorded",
          two and one, (two, one))


def serve():
    with open(SLOG, "w") as lf:
        sp = subprocess.Popen([DCH, "serve", "--bind", "127.0.0.1",
                               "--port", "0"],
                              env=dict(SENV, DCH_SERVE_TEST_LOOPBACK="1"),
                              stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=lf)
    end = time.time() + 10
    while time.time() < end and sp.poll() is None:
        m = re.search(r"listening on 127\.0\.0\.1:(\d+)", open(SLOG).read())
        if m:
            return sp, int(m.group(1))
        time.sleep(0.05)
    return sp, None


def checks(env, pre, where):
    o = answer(env, pre + "nc-two")
    check("%s: %snc-two answers corpus none, claudes 2" % (where, pre),
          o.get("corpus") == "none" and o.get("claudes") == 2, o)
    o = answer(env, pre + "nc-none")
    lite = o.get("reason") == "lite"
    check("%s: %snc-none answers corpus none, claudes 0" % (where, pre),
          o.get("corpus") == "none" and o.get("claudes") == 0, o)
    o = answer(env, pre + "nc-sh")
    check("%s: %snc-sh, no agent: no claudes" % (where, pre),
          o.get("corpus") and "claudes" not in o, o)
    for name, want in (("nc-two", TWO), ("nc-none", LITE if lite else NONE)):
        v = Pty([DCH, "--xray", pre + name], env)
        try:
            check("%s: the view of %s%s says %r" % (where, pre, name,
                                                    want.decode()),
                  v.wait(want), v.out[-600:])
        finally:
            rc = v.close()
        check("%s: q exits the view of %s%s with 0" % (where, pre, name),
              rc == 0, rc)


def main():
    sp = None
    try:
        sessions()
        checks(SENV, "", "local")
        sp, port = serve()
        check("dch serve listens on a loopback test port", port is not None,
              open(SLOG).read())
        if port is not None:
            with open(CENV["DCH_CONFIG"], "w") as f:
                f.write("remote_hosts = srv=127.0.0.1:%d\n" % port)
            checks(CENV, "srv:", "remote")
    finally:
        for n in ("nc-sh", "nc-two", "nc-none"):
            dch(SENV, "-k", n)
        if sp:
            sp.terminate()
            try:
                sp.wait(10)
            except subprocess.TimeoutExpired:
                sp.kill()
                sp.wait()
        shutil.rmtree(T, ignore_errors=True)
    if fails[0]:
        print("%d failed" % fails[0])
        sys.exit(1)


if __name__ == "__main__":
    main()
