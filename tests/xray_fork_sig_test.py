#!/usr/bin/env python3
"""A query child of the X-ray view stopped right after its fork (QA 1.58.0
MED-V4).

xv_spawn forks each query child while the view's picker_sigint is installed
for SIGINT, SIGTERM and SIGHUP; the child resets them a few lines later. A
SIGTERM from xv_stop in between ran picker_sigint in the child: it
xv_kill()ed its siblings and restored the view's terminal (cooked mode,
ESC[<u, ESC[?1049l) under the live view, which then echoed keys and ignored
q until Enter. The signals are now blocked over the fork.

The window is a few microseconds, so the test forces it many times: in a
card every f restarts the query, stopping the child the previous f forked
at once. Bursts of Enter, f x8, h in one write each, then the terminal is
checked: no ESC[?1049l and no ESC[<u written while the view is up, the tty
still raw (direct view), no "live updates stopped" (the chord: the follow
child alive), and q still leaves the view.

Covers: the direct view (`dch --xray <record>`, Refs tab) and the view over
the in-attach chord (a shell session, Refs tab; skipped on dch-lite, where a
shell has no refs without the mirror).

Run:  python3 tests/xray_fork_sig_test.py      (uses ./dch)
"""
import json, os, pty, select, shutil, signal, subprocess, sys, tempfile
import termios, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
RSID = "11111111-2222-4333-8444-5555555555d1"
BURSTS = int(os.environ.get("XFS_BURSTS", "6"))
BURST = b"\r" + b"f" * 8 + b"h"
RESTORE = b"\033[?1049l"
POP = b"\033[<u"
fails = [0]


def check(msg, cond, detail=""):
    print("  %s %s" % ("ok  " if cond else "FAIL", msg))
    if not cond:
        fails[0] += 1
        if detail:
            print("       " + repr(detail)[-800:])


T = os.path.realpath(tempfile.mkdtemp(prefix="xfs"))
HOME = os.path.join(T, "home")
os.makedirs(HOME)
ENV = {k: v for k, v in os.environ.items()
       if not k.startswith(("DCH_SESSION", "DCH_SOCKET_DIR", "DCH_DEBUG",
                            "CLAUDE", "Malloc", "NO_COLOR", "COLORTERM"))}
ENV.update(XDG_RUNTIME_DIR=T, XDG_STATE_HOME=os.path.join(T, "state"),
           HOME=HOME, DCH_CONFIG=os.path.join(T, "config"),
           DCH_LOGIN_ENV="0", DCH_NO_REMOTE="1", NO_COLOR="1",
           TERM="xterm-256color")


def dch(*a):
    return subprocess.run([DCH] + list(a), capture_output=True, text=True,
                          timeout=30, env=ENV)


class Pty:
    """A program on a pty: out holds every byte it wrote. Every read has
    a deadline; close() closes the fd before the blocking waitpid."""

    def __init__(self, argv):
        self.out = b""
        self.rc = None
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execve(argv[0], argv, ENV)

    def pump(self, secs=0.05):
        end = time.time() + secs
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], end - time.time())
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

    def wait(self, what, secs=10.0, start=0):
        end = time.time() + secs
        while what not in self.out[start:] and time.time() < end:
            self.pump(0.1)
            if self.rc is not None:
                break
        return what in self.out[start:]

    def exit(self, secs=10.0):
        end = time.time() + secs
        while self.reap() is None and time.time() < end:
            self.pump(0.1)
        return self.rc

    def key(self, b, settle=0.3):
        os.write(self.fd, b)
        self.pump(settle)

    def close(self):
        if self.reap() is None:
            os.kill(self.pid, signal.SIGKILL)
        os.close(self.fd)
        if self.rc is None:
            os.waitpid(self.pid, 0)


def raw(fd):
    lf = termios.tcgetattr(fd)[3]
    return not lf & (termios.ICANON | termios.ECHO)


def fixture():
    """A History Claude record with one ref, #831."""
    cwd = "/home/demo/proj"
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", cwd.replace("/", "-"))
    os.makedirs(pdir)
    out = "# #831 Heading\n#831 mention\n"
    lines = [
        {"type": "user", "cwd": cwd, "timestamp": "2026-11-10T10:00:00Z",
         "message": {"content": "look at #831"}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "b1", "name": "Bash",
             "input": {"command": "gh issue view 831"}}]}},
        {"type": "user", "toolUseResult": {"stdout": out},
         "message": {"content": [
             {"type": "tool_result", "tool_use_id": "b1", "content": out}]}},
    ]
    with open(os.path.join(pdir, RSID + ".jsonl"), "w") as f:
        for l in lines:
            f.write(json.dumps(l) + "\n")
    sockdir = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    os.makedirs(hdir)
    rec = {"name": "xfs-rec", "cwd": cwd, "end": "exited",
           "argv": ["claude"], "started": 1794299400,
           "updated": 1794300000, "ended": 1794300000, "sock_dir": sockdir,
           "agent": {"session_id": RSID, "cwd": cwd, "config_dir": cfg}}
    with open(os.path.join(hdir, "xfs-rec.json"), "w") as f:
        f.write(json.dumps(rec) + "\n")


def bursts(p):
    for _ in range(BURSTS):
        p.key(BURST, 0.4)


def test_direct():
    v = Pty([DCH, "--xray", "xfs-rec"])
    try:
        ok = v.wait(b"Chapters", 20)
        v.pump(0.3)
        v.key(b"\t", 0.3)
        ok = ok and v.wait(b"#831", 10)
        check("direct view: the Refs tab lists #831", ok, v.out[-600:])
        if not ok:
            return
        mark = len(v.out)
        bursts(v)
        check("direct view: %d bursts of Enter, f x8, h: the card opened" %
              BURSTS, b"Mentions" in v.out[mark:], v.out[mark:][-400:])
        check("direct view: the terminal is still raw", raw(v.fd))
        check("direct view: no ESC[?1049l or ESC[<u while the view is up",
              RESTORE not in v.out[mark:] and POP not in v.out[mark:],
              v.out[mark:][-400:])
        mark = len(v.out)
        v.key(b"q", 0.3)
        check("direct view: q leaves the view, exit 0, terminal restored",
              v.exit() == 0 and RESTORE in v.out[mark:], (v.rc, v.out[-300:]))
    finally:
        v.close()


def test_chord():
    r = dch("--spawn", "xfs-sh", "--size", "100x30", "/bin/sh", "-c",
            "echo 'see owner/repo#831'; sleep 600")
    check("chord: the shell session starts", r.returncode == 0, r.stderr)
    o = json.loads(dch("--xray", "xfs-sh", "--json").stdout or "{}")
    if o.get("reason") == "lite":
        # a shell's refs come from the mirror: no card to restart; the
        # direct view above runs the same xv_spawn
        print("  skip chord: dch-lite has no shell refs (no mirror)")
        return
    a = Pty([DCH, "-a", "xfs-sh"])
    try:
        ok = a.wait(b"owner/repo#831", 10)
        a.pump(0.3)
        a.key(b"\x1cx", 0.3)
        ok = ok and a.wait(b"On screen", 10)
        a.pump(1.0)
        mark = len(a.out)
        a.key(b"\t\t", 0.5)
        ok = ok and a.wait(b"owner/repo#831", 10, mark)
        check("chord: the view's Refs tab lists owner/repo#831", ok,
              a.out[-600:])
        if not ok:
            return
        mark = len(a.out)
        bursts(a)
        tail = a.out[mark:]
        check("chord: %d bursts of Enter, f x8, h: the card opened" %
              BURSTS, b"Mentions" in tail, tail[-400:])
        check("chord: no ESC[?1049l or ESC[<u while the view is up",
              RESTORE not in tail and POP not in tail, tail[-400:])
        check("chord: live updates still on (the follow child alive)",
              b"live updates stopped" not in tail, tail[-400:])
        mark = len(a.out)
        a.key(b"q", 0.3)
        check("chord: q leaves the view, the client still attached",
              a.wait(RESTORE, 10, mark) and a.reap() is None,
              (a.rc, a.out[-300:]))
    finally:
        a.close()


def main():
    try:
        fixture()
        test_direct()
        test_chord()
    finally:
        dch("-k", "xfs-sh")
        shutil.rmtree(T, ignore_errors=True)
    if fails[0]:
        print("%d failed" % fails[0])
        sys.exit(1)


if __name__ == "__main__":
    main()
