#!/usr/bin/env python3
"""The `dch --xray <n>` view and the picker's X (#175, XR5a).

The view runs on a pty this test owns (pty.fork), so it needs no terminal
mirror and runs the same on dch-lite. A History Claude record whose chapter
title carries an OSC 0 title, a BEL and U+202E, and whose definition title
carries an SGR, is the hostile corpus.

Covers:
  1. a non-tty stdout is refused, exit 2 (a remote name's refusal is
     remote_client_test.py's);
  2. no such session: exit 1, the message after the terminal is restored;
  3. the three tabs, the chapter list, the Refs list, the card (light, `f`
     full scan, `y` OSC 52 copy), the chapter reader, back, quit (exit 0,
     terminal restored);
  4. mutant 11: no corpus ESC, BEL or bidi byte reaches the terminal, the
     strings show with `?` in their place;
  5. nothing written: no file under the test root changes;
  6. a resize to a narrow window says so, a resize back redraws;
  7. SIGTERM and SIGINT: the terminal is restored and the view's children
     are gone;
  8. On screen on a live shell: the ref printed in two blocks (underline)
     gets hint label `a`, typing it opens its card (full build); on dch-lite
     the label line says X-ray needs the full build;
  9. the picker: the X hint is shown, X on a session row opens the view, q
     returns to the picker.

Run:  python3 tests/xray_view_test.py      (uses ./dch)
"""
import base64, fcntl, json, os, pty, select, shutil, signal, struct
import subprocess, sys, tempfile, termios, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
RSID = "11111111-2222-4333-8444-5555555555b7"
fails = [0]


def check(msg, cond, detail=""):
    print("  %s %s" % ("ok  " if cond else "FAIL", msg))
    if not cond:
        fails[0] += 1
        if detail:
            print("       " + repr(detail)[-800:])


T = tempfile.mkdtemp(prefix="xv")
HOME = os.path.join(T, "home")
os.makedirs(HOME)
ENV = {k: v for k, v in os.environ.items()
       if not k.startswith(("DCH_SESSION", "DCH_SOCKET_DIR", "DCH_DEBUG",
                            "CLAUDE", "Malloc", "NO_COLOR", "COLORTERM"))}
ENV.update(XDG_RUNTIME_DIR=T, XDG_STATE_HOME=os.path.join(T, "state"),
           HOME=HOME, DCH_CONFIG=os.path.join(T, "config"),
           DCH_LOGIN_ENV="0", DCH_NO_REMOTE="1", NO_COLOR="1",
           TERM="xterm-256color")
RESTORE = b"\033[?1049l"


def dch(*a):
    return subprocess.run([DCH] + list(a), capture_output=True, text=True,
                          timeout=30, env=ENV)


def until(fn, secs=10.0):
    end = time.time() + secs
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


class Pty:
    """A program on a pty: out holds every byte it wrote."""

    def __init__(self, argv, rows=30, cols=100):
        self.out = b""
        self.rc = None
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
            os.execve(argv[0], argv, ENV)

    def pump(self, secs=0.05):
        end = time.time() + secs
        while True:
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

    def wait(self, what, secs=10.0, start=0):
        end = time.time() + secs
        while what not in self.out[start:] and time.time() < end:
            self.pump(0.1)
            if self.rc is not None:
                break
        return what in self.out[start:]

    def key(self, b, settle=0.3):
        os.write(self.fd, b)
        self.pump(settle)

    def resize(self, rows, cols):
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0))

    def exit(self, secs=10.0):
        end = time.time() + secs
        while self.reap() is None and time.time() < end:
            self.pump(0.1)
        self.pump(0.1)
        return self.rc

    def close(self):
        if self.reap() is None:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        os.close(self.fd)


def kids(pid):
    r = subprocess.run(["/bin/ps", "-A", "-o", "pid=,ppid="],
                       capture_output=True, text=True)
    return [int(a) for a, b in (l.split() for l in r.stdout.splitlines())
            if int(b) == pid]


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def files(root):
    out = {}
    for dp, dn, fn in os.walk(root):
        for f in dn + fn:
            p = os.path.join(dp, f)
            st = os.lstat(p)
            if not p.endswith(".sock"):
                out[p] = (st.st_size, st.st_mtime_ns)
    return out


# ---- the fixture: a History Claude record with a hostile corpus -----------

TITLE = "look at #831 and o/r#7 \033]0;pwned\007 evil‮title"


def fixture():
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    out = "# #831 Canary \033[31mred\033[0m passage\nSee #831 again and PROJ-5"
    lines = [
        {"type": "user", "cwd": "/home/demo/proj",
         "timestamp": "2026-11-10T10:00:00Z", "message": {"content": TITLE}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "b1", "name": "Bash",
             "input": {"command": "gh issue view 831"}}]}},
        {"type": "user", "toolUseResult": {"stdout": out},
         "message": {"content": [{"type": "tool_result",
                                  "tool_use_id": "b1", "content": out}]}},
        {"type": "user", "cwd": "/home/demo/proj",
         "timestamp": "2026-11-10T11:00:00Z",
         "message": {"content": "second prompt about PROJ-5"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "noted"}]}}]
    with open(os.path.join(pdir, RSID + ".jsonl"), "w") as f:
        for o in lines:
            f.write(json.dumps(o) + "\n")
    sockdir = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    rec = {"name": "xv-cl", "cwd": "/home/demo/proj", "end": "exited",
           "argv": ["claude"], "started": 1794299400, "updated": 1794300000,
           "ended": 1794300000, "sock_dir": sockdir,
           "agent": {"session_id": RSID, "cwd": "/home/demo/proj",
                     "config_dir": cfg}}
    with open(os.path.join(hdir, "xv-cl.json"), "w") as f:
        f.write(json.dumps(rec) + "\n")


def test_refusals():
    r = dch("--xray", "xv-cl")
    check("no tty: refused with exit 2 and a clear message",
          r.returncode == 2 and r.stdout == "" and r.stderr ==
          "dch: the --xray view needs a terminal (--json prints the "
          "object)\n", (r.returncode, r.stdout, r.stderr))
    v = Pty([DCH, "--xray", "nosuch"])
    try:
        rc = v.exit()
        i = v.out.find(b"dch: no such session")
        check("no such session: exit 1, the message after the terminal is "
              "restored", rc == 1 and i > 0 and
              v.out.rfind(RESTORE, 0, i) >= 0, (rc, v.out))
    finally:
        v.close()


def test_view():
    before = files(T)
    v = Pty([DCH, "--xray", "xv-cl"])
    try:
        ok = v.wait(b"Chapters 2") and v.wait(b"Refs 3")
        check("open: the tabs carry the index's counts", ok, v.out[-600:])
        check("open: the chapter list shows both titles",
              v.wait(b"second prompt about PROJ-5") and
              v.wait(b"look at #831"), v.out[-600:])
        check("mutant 11: the chapter title's OSC, BEL and U+202E show as ?",
              v.wait(b"o/r#7 ?]0;pwned? evil?title"), v.out[-600:])
        v.key(b"\t")
        check("Refs: the keys by count", v.wait(b"#831") and
              v.wait(b"PROJ-5") and v.wait(b"o/r#7"), v.out[-600:])
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("card: #831 opens light with its definition and counts",
              v.wait(b"light", start=mark) and
              v.wait(b"Definition", start=mark) and
              v.wait(b"Mentions: this session ", start=mark),
              v.out[mark:])
        check("mutant 11: the definition title's SGR shows as ?",
              v.wait(b"Canary ?[31mred?[0m passage", start=mark),
              v.out[mark:])
        mark = len(v.out)
        v.key(b"f", 1.0)
        check("card: f runs the full scan", v.wait(b"full scan", start=mark),
              v.out[mark:])
        v.key(b"y")
        check("card: y copies the ref (OSC 52)",
              v.wait(b"\033]52;c;" + base64.b64encode(b"#831")),
              v.out[-300:])
        mark = len(v.out)
        v.key(b"\033", 0.5)
        v.key(b"\t\t")
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("reader: Enter opens the selected chapter (the newest)",
              v.wait(b"You", start=mark) and v.wait(b"noted", start=mark),
              v.out[mark:])
        mark = len(v.out)
        v.key(b"q")
        check("reader: q goes back to the list",
              v.wait(b"Chapters 2", start=mark), v.out[mark:])
        v.key(b"q")
        rc = v.exit()
        check("quit: q exits 0 and leaves the alternate screen",
              rc == 0 and RESTORE in v.out[-60:], (rc, v.out[-80:]))
        bad = [b for b in (b"\033]0;pwned", b"\007 evil", b"\xe2\x80\xae",
                           b"\033[31mred") if b in v.out]
        check("mutant 11: no corpus ESC, BEL or bidi byte in the output",
              not bad, bad)
    finally:
        v.close()
    after = files(T)
    check("nothing written: no file under the test root changed",
          before == after, set(before.items()) ^ set(after.items()))


def test_resize():
    v = Pty([DCH, "--xray", "xv-cl"])
    try:
        v.wait(b"Chapters 2")
        mark = len(v.out)
        v.resize(6, 40)
        check("resize: a narrow window says it is too small",
              v.wait(b"the window is too small", start=mark), v.out[mark:])
        mark = len(v.out)
        v.resize(30, 100)
        check("resize: back to 100x30 redraws the list",
              v.wait(b"Chapters 2", start=mark), v.out[mark:])
        v.key(b"\003")
        check("Ctrl-C quits with 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()


def shell():
    with open(ENV["DCH_CONFIG"], "w") as f:
        f.write("xray_ring = 64K\n")
    prog = ("while :; do printf '\\033]133;A\\007$ \\033]133;B\\007'; "
            "read -r cmd || exit; printf '\\033]133;C\\007'; eval \"$cmd\"; "
            "printf '\\033]133;D;0\\007'; done")
    r = dch("--spawn", "xv-sh", "--size", "80x24", "sh", "-c", prog)
    check("shell: spawned", r.returncode == 0, r.stderr)
    for _ in range(2):
        time.sleep(0.4)
        dch("--send", "xv-sh", "echo 'see o/r#41'\n")
    time.sleep(0.5)


def test_signals():
    for sig in (signal.SIGTERM, signal.SIGINT):
        name = signal.Signals(sig).name
        v = Pty([DCH, "--xray", "xv-sh"])
        try:
            v.wait(b"Chapters")
            v.pump(0.5)
            ks = kids(v.pid)
            os.kill(v.pid, sig)
            rc = v.exit()
            check("%s: the view exits and restores the terminal" % name,
                  rc is not None and RESTORE in v.out[-60:],
                  (rc, v.out[-80:]))
            check("%s: the view's follow child is gone" % name,
                  ks and until(lambda: not any(alive(k) for k in ks), 5),
                  ks)
        finally:
            v.close()


def test_on_screen():
    rc = subprocess.run([DCH, "--xray", "xv-sh", "--json"], env=ENV,
                        capture_output=True, text=True, timeout=30)
    o = json.loads(rc.stdout)
    lite = o["corpus"] == "none" and o["reason"] == "lite"
    v = Pty([DCH, "--xray", "xv-sh"])
    try:
        v.wait(b"Chapters")
        if lite:
            check("lite: the label says X-ray needs the full dch build",
                  v.wait(b"X-ray needs the full dch build"), v.out[-600:])
            mark = len(v.out)
            v.key(b"\t\t", 1.0)
            check("lite: On screen says dch-lite keeps no mirror",
                  v.wait(b"No screen: dch-lite keeps no mirror", start=mark),
                  v.out[mark:])
        else:
            u = [x for x in o["refs"] if x["key"] == "pull:o/r#41"]
            check("shell: o/r#41 is underlined in the index (count 2)",
                  u and u[0]["underline"], o["refs"])
            mark = len(v.out)
            v.key(b"\t\t", 1.5)
            check("On screen: o/r#41 gets hint a",
                  v.wait(b"On screen 1", start=mark) and
                  v.wait(b"o/r#41", start=mark), v.out[mark:])
            mark = len(v.out)
            v.key(b"a", 1.0)
            check("On screen: typing a opens the card of o/r#41",
                  v.wait(b"o/r#41  light", start=mark) and
                  v.wait(b"Mentions", start=mark), v.out[mark:])
        v.key(b"\003")
        check("on screen: Ctrl-C quits with 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()


def test_picker():
    v = Pty([DCH, "-l"])
    try:
        check("picker: the X hint is shown",
              v.wait(b"x-ray"), v.out[-600:])
        mark = len(v.out)
        v.key(b"X", 1.0)
        check("picker: X opens the view of the row's session",
              v.wait(b"X-ray  xv-sh", start=mark) and
              v.wait(b"Chapters", start=mark), v.out[mark:])
        mark = len(v.out)
        v.key(b"q", 1.0)
        check("picker: q in the view returns to the picker",
              v.reap() is None and v.wait(b"Sessions", start=mark),
              v.out[mark:])
        v.key(b"\033")
        check("picker: Esc quits it", v.exit() is not None, v.out[-80:])
    finally:
        v.close()


def main():
    try:
        fixture()
        test_refusals()
        test_view()
        test_resize()
        shell()
        test_signals()
        test_on_screen()
        test_picker()
    finally:
        dch("-k", "xv-sh")
        shutil.rmtree(T, ignore_errors=True)
    if fails[0]:
        print("%d failed" % fails[0])
        sys.exit(1)


if __name__ == "__main__":
    main()
