#!/usr/bin/env python3
"""The X-ray view of a remote session (#188, XR5c): `dch --xray host:name`
and the picker's X on a remote row.

A real `dch serve` runs on 127.0.0.1 (DCH_SERVE_TEST_LOOPBACK=1, --bind
127.0.0.1 --port 0, a fake `tailscale` first on PATH), its sessions in their
own socket and state dirs. The client has others and names the host in
remote_hosts (dch --xray takes no --hosts), without DCH_NO_REMOTE. Fake
servers on loopback play a hostile host, one without "xray", one whose gate
refuses us; a closed port is an unreachable one. Nothing else is dialled.
Each view runs on a pty this test owns (pty.fork), so it needs no terminal
mirror on the client.

Covers:
  1. srv:xv-cl, a History Claude record on the host with a hostile corpus:
     the title names srv:, the tabs carry the host's counts, the chapter
     list, the Refs list, the card (light, `f` full scan), the reader, q
     exits 0; the corpus's ESC, BEL and bidi bytes never reach the
     terminal; every query ran on the host as its own ctl (follow, ref,
     ref --full, chapter);
  2. On screen of srv:xv-sh, a live shell on the host: no number on its tab
     before the screen is read (#193), the screen read with
     ctl --read xv-sh --ansi, o/r#41 gets hint a, typing it opens
     its card (full build); on dch-lite "No screen: dch-lite keeps no
     mirror" and still no number;
  3. a hostile host: raw ESC, BEL, C1 and bidi bytes in its snapshot and in
     its --read screen are drawn as ?, its OSC and CSI dropped, and its
     screen still gets its hint;
  4. a host without "xray" (exit 6), a closed port (exit 7) and a gate that
     refuses us (exit 4): the line in place of the tabs, the view open until
     q, then the exit code and the line on stderr;
  5. the picker: X on the remote Sessions row srv:xv-sh opens its view, q
     returns to the picker; X on the remote History row srv:xv-cl opens
     that record's view;
  6. the in-attach chord over `dch -a srv:xv-rx` (#189): the detach key then
     x (a later read, one burst, the kitty CSI form, --host srv -a xv-rx,
     detach_key = switch) opens the view of srv:xv-rx over the attach, On
     screen read with its own ctl --read; the session never gets the detach
     key or the x; q goes back, the host is asked to redraw (the program's
     SIGWINCH), a key after reaches the session, and the host saw one attach
     (never retired). Off under -E and DCH_DOUBLE_TAP_MS=0, as the local
     chord;
  7. the view's failure lines over an attach to a fake host without "xray"
     (exit 6), whose gate refuses the view's connection (exit 4), or whose
     port closes once the attach is in (exit 7): the line, q, any key, and
     the attach goes on (the host gets the redraw, then only the next key);
  8. the host re-arms on its redraw (#189 round 2): a program on the
     alternate screen with bracketed paste, mouse reporting and kitty flags
     on, over `dch -a srv:xv-md`; after the chord's view the client's output
     past the view's ESC[?1049l holds ESC[?1049h, then the kitty push (on the
     alternate screen), ESC[?2004h, ESC[?1000h and ESC[?1006h; the full build
     repaints the screen from its mirror, dch-lite has none and the program
     repaints on its SIGWINCH. A SIGUSR2 redraw (0x07, no reset before it)
     re-arms again, and a key after still reaches the session.

Run:  python3 tests/xray_remote_view_test.py      (uses ./dch)
"""
import fcntl, json, os, pty, re, select, shutil, signal, socket, struct
import subprocess, sys, tempfile, termios, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from remote_client_test import (FAKE_TS, OWNER, REFUSAL, WHOIS, Fake,  # noqa
                                frame)

DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
RSID = "11111111-2222-4333-8444-5555555555c8"
RESTORE = b"\033[?1049l"
fails = [0]


def check(msg, cond, detail=""):
    print("  %s %s" % ("ok  " if cond else "FAIL", msg))
    if not cond:
        fails[0] += 1
        if detail:
            print("       " + repr(detail)[-900:])


T = os.path.realpath(tempfile.mkdtemp(prefix="xrv", dir="/tmp"))
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


def side(name, **kw):
    d = os.path.join(T, name)
    os.makedirs(os.path.join(d, "home"))
    e = dict(BASE, XDG_RUNTIME_DIR=d, XDG_STATE_HOME=os.path.join(d, "st"),
             HOME=os.path.join(d, "home"), DCH_LOGIN_ENV="0",
             DCH_CONFIG=os.path.join(d, "config"), SHELL="/bin/sh",
             PATH=FAKE + ":/usr/bin:/bin", TERM="xterm-256color",
             NO_COLOR="1")
    e.update(kw)
    return e


SENV = side("srv")
CENV = side("cli")
# the picker probes the host once inside --ls's box (300 ms): a loaded
# machine or a sanitizer build misses it and the picker says "no sessions"
CENV["DCH_REMOTE_BOX_MS"] = "5000"
SLOG = os.path.join(T, "serve.log")


def hosts(line):
    with open(CENV["DCH_CONFIG"], "w") as f:
        f.write("remote_hosts = %s\n" % line)


def dch(env, *a):
    return subprocess.run([DCH] + list(a), capture_output=True, text=True,
                          timeout=30, env=env, stdin=subprocess.DEVNULL)


class Pty:
    """A program on a pty: out holds every byte it wrote."""

    def __init__(self, argv, rows=30, cols=100, env=None):
        self.out = b""
        self.rc = None
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
            os.execve(argv[0], argv, env or CENV)

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
        try:
            os.write(self.fd, b)
        except OSError:
            pass    # it has exited: the checks say so
        self.pump(settle)

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


def ctls(what):
    with open(SLOG) as f:
        return f.read().count(" ctl %s " % what)


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


# ---- the host's corpus: a History Claude record, hostile text -------------

TITLE = ("look at #831 and o/r#7 \033]0;pwned\007 evil‮title "
         "al؜m ls ep")


def fixture():
    cfg = os.path.join(SENV["HOME"], ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    out = ("# #831 Canary \033[31mred\033[0m passage\n"
           "See #831 again and؜ PROJ-5")
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
    sockdir = os.path.join(SENV["XDG_RUNTIME_DIR"], "dch-%d" % os.getuid())
    hdir = os.path.join(SENV["XDG_STATE_HOME"], "dch", "sessions",
                        sockdir.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    rec = {"name": "xv-cl", "cwd": "/home/demo/proj", "end": "exited",
           "argv": ["claude"], "started": 1794299400, "updated": 1794300000,
           "ended": 1794300000, "sock_dir": sockdir,
           "agent": {"session_id": RSID, "cwd": "/home/demo/proj",
                     "config_dir": cfg}}
    with open(os.path.join(hdir, "xv-cl.json"), "w") as f:
        f.write(json.dumps(rec) + "\n")


def shell():
    with open(SENV["DCH_CONFIG"], "w") as f:
        f.write("xray_ring = 64K\n")
    prog = ("while :; do printf '\\033]133;A\\007$ \\033]133;B\\007'; "
            "read -r cmd || exit; printf '\\033]133;C\\007'; eval \"$cmd\"; "
            "printf '\\033]133;D;0\\007'; done")
    r = dch(SENV, "--spawn", "xv-sh", "--size", "80x24", "sh", "-c", prog)
    check("host: the shell spawned", r.returncode == 0, r.stderr)
    for _ in range(2):
        time.sleep(0.4)
        dch(SENV, "--send", "xv-sh", "echo 'see o/r#41'\n")
    time.sleep(0.5)


# ---- 1: the view on the host's record --------------------------------------

def test_view():
    n0 = {w: ctls(w) for w in ("--xray", "--read")}
    v = Pty([DCH, "--xray", "srv:xv-cl"])
    try:
        check("view: the title names the host's session as srv:xv-cl",
              v.wait(b"X-ray  srv:xv-cl"), v.out[-600:])
        check("view: the tabs carry the host's counts",
              v.wait(b"Chapters 2") and v.wait(b"Refs 3"), v.out[-600:])
        check("view: the chapter list shows both titles",
              v.wait(b"second prompt about PROJ-5") and
              v.wait(b"look at #831"), v.out[-600:])
        check("hostile host text: the chapter title's OSC, BEL, U+202E and "
              "U+061C show as ?", v.wait(b"o/r#7 ?]0;pwned? evil?title al?m"),
              v.out[-600:])
        v.key(b"\t")
        check("Refs: the host's keys", v.wait(b"#831") and
              v.wait(b"PROJ-5") and v.wait(b"o/r#7"), v.out[-600:])
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("card: #831 opens light with its definition and counts",
              v.wait(b"light", start=mark) and
              v.wait(b"Definition", start=mark) and
              v.wait(b"Mentions: this session ", start=mark), v.out[mark:])
        check("card: the definition title's SGR shows as ?",
              v.wait(b"Canary ?[31mred?[0m passage", start=mark),
              v.out[mark:])
        mark = len(v.out)
        v.key(b"f", 1.0)
        check("card: f runs the full scan on the host",
              v.wait(b"full scan", start=mark), v.out[mark:])
        v.key(b"\033", 0.5)
        v.key(b"\t\t")
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("reader: Enter reads the newest chapter from the host",
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
                           b"\033[31mred", b"\xd8\x9c", b"\xe2\x80\xa8")
               if b in v.out]
        check("hostile host text: no corpus ESC, BEL or bidi byte reached "
              "the terminal", not bad, bad)
    finally:
        v.close()
    time.sleep(0.3)
    # follow, ref (light), ref --full, chapter: four, each its own ctl
    check("every query ran on the host as its own ctl --xray (4), no --read "
          "for an ended session", ctls("--xray") - n0["--xray"] == 4 and
          ctls("--read") == n0["--read"],
          (ctls("--xray") - n0["--xray"], ctls("--read") - n0["--read"]))


# ---- 2: On screen of a live shell on the host -------------------------------

def test_on_screen():
    o = json.loads(dch(SENV, "--xray", "xv-sh", "--json").stdout)
    lite = o["corpus"] == "none" and o["reason"] == "lite"
    n0 = ctls("--read")
    v = Pty([DCH, "--xray", "srv:xv-sh"])
    try:
        check("On screen: the view of srv:xv-sh opens",
              v.wait(b"X-ray  srv:xv-sh") and v.wait(b"Chapters"),
              v.out[-600:])
        check("On screen: no number on its tab before its screen is read",
              not re.search(rb"On screen \d", v.out), v.out[-600:])
        mark = len(v.out)
        v.key(b"\t\t", 2.0)
        if lite:
            check("lite: On screen says dch-lite keeps no mirror, no number",
                  v.wait(b"No screen: dch-lite keeps no mirror", start=mark)
                  and not re.search(rb"On screen \d", v.out), v.out[mark:])
        else:
            check("On screen: the host's screen, o/r#41 with hint a",
                  v.wait(b"On screen 1", start=mark) and
                  v.wait(b"see [a]o/r#41", start=mark), v.out[mark:])
            mark = len(v.out)
            v.key(b"a", 1.5)
            check("On screen: typing a opens the card of o/r#41",
                  v.wait(b"o/r#41  light", start=mark) and
                  v.wait(b"Mentions", start=mark), v.out[mark:])
        v.key(b"\003")
        check("On screen: Ctrl-C quits with 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()
    with open(SLOG) as f:
        log = f.read()
    check("On screen: read on the host with ctl --read xv-sh --ansi",
          ctls("--read") > n0 and " ctl --read xv-sh " in log,
          ctls("--read") - n0)


# ---- 3: a hostile host -------------------------------------------------------

HOSTILE_TITLE = "t\x1b]0;pwned\x07 a\u009bb c‮d e⁦f"
HOSTILE_SCREEN = (b"$ echo hi\x1b]0;pwned\x07\r\n"
                  b"see o/r#41 \x1b[31mred\x1b[0m\x1b[5Cgap\x07bel"
                  b" c1\xc2\x9b2J bidi\xe2\x80\xaex \x1b[2J\r\n")


class Hostile(Fake):
    """A host with ctl, ctl-cancel and xray: --follow answers a snapshot
    with raw control and bidi bytes and holds the stream; --read answers a
    screen with OSC, CSI, BEL, C1 and bidi bytes; anything else ends 0."""

    def __init__(self):
        super().__init__("ok", ops=["ctl", "ctl-cancel", "xray"])

    def serve(self, c):
        buf = b""
        try:
            while True:
                d = c.recv(65536)
                if not d:
                    return
                buf += d
                while len(buf) >= 5:
                    t, n = struct.unpack(">BI", buf[:5])
                    if len(buf) < 5 + n:
                        break
                    body, buf = buf[5:5 + n], buf[5 + n:]
                    if t == 0x08:
                        c.sendall(frame(0x18, {"proto": "1.0",
                                               "server": "fake",
                                               "ops": self.ops}))
                    elif t == 0x30:
                        argv = json.loads(body)["argv"]
                        self.bodies.append(argv)
                        if "--follow" in argv:
                            snap = ('{"schema":1,"snapshot":{"session":"fk",'
                                    '"session_id":"m0000000000000001",'
                                    '"corpus":"shell","live":true,'
                                    '"lines":3,"project_label":"/x",'
                                    '"chapters":[{"anchor":"s1","title":"'
                                    + HOSTILE_TITLE + '","kind":"shell",'
                                    '"at":1794300000,"offset":0}],'
                                    '"refs":[{"key":"pull:o/r#41",'
                                    '"shape":"pull","text":"o/r#41",'
                                    '"title":"' + HOSTILE_TITLE + '",'
                                    '"count":2,"underline":true}]}}\n')
                            c.sendall(frame(0x31, snap.encode()))
                            continue    # a stream: held until the close
                        if argv[0] == "--read":
                            c.sendall(frame(0x31, HOSTILE_SCREEN) +
                                      frame(0x32, b"cursor 2 1 1 0\n"))
                        c.sendall(frame(0x33, {"exit": 0}))
        except OSError:
            return


def test_hostile():
    h = Hostile()
    hosts("q=127.0.0.1:%d" % h.port)
    v = Pty([DCH, "--xray", "q:fk"])
    try:
        check("hostile: the view opens on the snapshot",
              v.wait(b"X-ray  q:fk") and v.wait(b"Chapters 1"),
              v.out[-600:])
        check("hostile: the snapshot's ESC, BEL, C1 and bidi bytes show as ?",
              v.wait(b"t?]0;pwned? a?b c?d e?f"), v.out[-600:])
        mark = len(v.out)
        v.key(b"\t\t", 2.0)
        check("hostile: On screen draws the host's screen, its OSC and CSI "
              "dropped, CSI 5 C as five blanks, BEL, C1 and bidi as ?",
              v.wait(b"$ echo hi", start=mark) and
              v.wait(b"red     gap?bel c1?2J bidi?x", start=mark),
              v.out[mark:])
        check("hostile: its o/r#41 gets hint a",
              v.wait(b"On screen 1", start=mark), v.out[mark:])
        v.key(b"q")
        rc = v.exit()
        check("hostile: q exits 0", rc == 0, (rc, v.out[-80:]))
        bad = [b for b in (b"\x1b]0;pwned", b"\x07 a", b"\x07bel",
                           b"\xc2\x9b", b"\xe2\x80\xae", b"\xe2\x81\xa6")
               if b in v.out]
        check("hostile: none of the host's control or bidi bytes reached the "
              "terminal", not bad, bad)
        check("hostile: the screen was asked as --read fk --ansi (no "
              "--cursor)", ["--read", "fk", "--ansi"] in h.bodies, h.bodies)
    finally:
        v.close()
        h.close()


# ---- 4: the three failure lines ----------------------------------------------

def fail_case(what, port, want, line, prefix=False):
    hosts("q=127.0.0.1:%d" % port)
    v = Pty([DCH, "--xray", "q:x"])
    try:
        ok = v.wait(line.encode())
        v.pump(1.0)
        i = v.out.find(line.encode())
        check("%s: the line in place of the tabs, the view kept open" % what,
              ok and v.reap() is None and b"Chapters" not in v.out[i:] and
              v.out.rfind(b"\033[2;1H", 0, i) == i - 6 and
              v.out.rfind(b"\033[?1049h", 0, i) >= 0, v.out[-600:])
        mark = len(v.out)
        v.key(b"\t")
        check("%s: other keys do nothing" % what, v.reap() is None and
              b"Chapters" not in v.out[mark:], v.out[mark:])
        v.key(b"q")
        rc = v.exit()
        i = v.out.rfind(line.encode())
        check("%s: q leaves it with exit %d, the line on stderr after the "
              "terminal is restored" % (what, want),
              rc == want and v.out.rfind(RESTORE, 0, i) >= 0,
              (rc, v.out[-300:]))
    finally:
        v.close()


def test_failures():
    nox = Fake("ctlerr", ops=["ctl", "ctl-cancel", "watch"])
    fail_case("a host without xray (exit 6)", nox.port, 6,
              "dch: q: does not support --xray (needs dch serve 1.55.0+)")
    check("a host without xray: nothing sent", not nox.bodies, nox.bodies)
    nox.close()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = s.getsockname()[1]
    s.close()
    fail_case("an unreachable host (exit 7)", dead, 7,
              "dch: q: unreachable: ")
    ref = Fake("refuse")
    fail_case("a gate that refuses us (exit 4)", ref.port, 4,
              "dch: q refused: " + REFUSAL["message"])
    ref.close()


# ---- 5: the picker's X on a remote row --------------------------------------

def test_picker(port):
    hosts("srv=127.0.0.1:%d" % port)
    v = Pty([DCH, "-l"], cols=140)
    try:
        check("picker: the remote row srv:xv-sh is listed",
              v.wait(b"xv-sh", 15), v.out[-600:])
        v.pump(1.0)
        mark = len(v.out)
        v.key(b"X", 1.0)
        check("picker: X on the remote row opens its view",
              v.wait(b"X-ray  srv:xv-sh", start=mark) and
              v.wait(b"Chapters", start=mark), v.out[mark:][-600:])
        mark = len(v.out)
        v.key(b"q", 1.0)
        check("picker: q in the view returns to the picker",
              v.reap() is None and v.wait(b"Sessions", start=mark),
              v.out[mark:][-600:])
        # History: the host's record srv:xv-cl, its only row
        for _ in range(3):
            mark = len(v.out)
            v.key(b"\t", 0.8)
            if v.wait(b"[History", 1, start=mark):
                break
        check("picker: History lists the host's record srv:xv-cl",
              v.wait(b"xv-cl", 15, start=mark), v.out[mark:][-600:])
        v.pump(0.5)
        mark = len(v.out)
        v.key(b"X", 1.0)
        check("picker: X on the remote History row opens its record's view",
              v.wait(b"X-ray  srv:xv-cl", start=mark) and
              v.wait(b"Chapters 2", start=mark), v.out[mark:][-600:])
        v.key(b"q", 1.0)
        v.key(b"\033")
        check("picker: Esc quits it", v.exit() is not None, v.out[-80:])
    finally:
        v.close()


# ---- 6: the in-attach chord over a remote attach (#189) ---------------------

# The host's probe session prints each read it gets as RX:<hex>, appends the
# hex to a file and says WINCH on a SIGWINCH (the host's redraw), so a test
# sees exactly the bytes that reached it, dch-lite too.
RXF = os.path.join(T, "rx.hex")
RX = ("import os, signal, sys, tty\n"
      "tty.setraw(0)\n"
      "signal.signal(signal.SIGWINCH,\n"
      "              lambda *a: os.write(1, b'WINCH\\r\\n'))\n"
      "f = open(sys.argv[1], 'a')\n"
      "os.write(1, b'READY\\r\\n')\n"
      "while True:\n"
      "    b = os.read(0, 256)\n"
      "    if not b: break\n"
      "    f.write(b.hex()); f.flush()\n"
      "    os.write(1, b'RX:' + b.hex().encode() + b'\\r\\n')\n")


def rx():
    """Every byte the host's session has received, in order, as hex."""
    try:
        with open(RXF) as f:
            return f.read()
    except OSError:
        return ""


def until(fn, secs=10.0):
    end = time.time() + secs
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return fn()


def attaches():
    with open(SLOG) as f:
        return f.read().count(" attach xv-rx (")


def rattach(env=None, extra=(), tail=("-a", "srv:xv-rx")):
    n0 = attaches()
    a = Pty([DCH] + list(extra) + list(tail), env=env)
    until(lambda: attaches() > n0)
    a.wait(b"READY", 3)
    a.pump(0.5)
    return a, n0


def chord_case(what, keys, env=None, tail=("-a", "srv:xv-rx")):
    a, n0 = rattach(env, tail=tail)
    try:
        got = rx()
        r0 = ctls("--read")
        mark = len(a.out)
        for k in keys:
            a.key(k, 0.05)
        ok = a.wait(b"X-ray  srv:xv-rx", start=mark) and \
            a.wait(b"On screen", start=mark)
        check("remote chord, %s: the view of srv:xv-rx opens over the attach"
              % what, ok, a.out[mark:][-600:])
        check("remote chord, %s: the client is still running" % what,
              a.reap() is None)
        check("remote chord, %s: On screen read over its own ctl --read"
              % what, until(lambda: ctls("--read") > r0, 5),
              ctls("--read") - r0)
        dch(SENV, "--send", "xv-rx", "MARK189")
        until(lambda: "4d41524b313839" in rx()[len(got):], 5)
        a.pump(0.3)
        check("remote chord, %s: the session got only the text sent while "
              "the view was up (no detach key, no x)" % what,
              rx()[len(got):] == "4d41524b313839", rx()[len(got):])
        if not ok or a.reap() is not None:
            return
        mark = len(a.out)
        a.key(b"q", 0.5)
        check("remote chord, %s: q leaves the view's alternate screen" % what,
              a.wait(RESTORE, start=mark), a.out[mark:][-300:])
        back = a.out.rfind(RESTORE)
        check("remote chord, %s: back on the attach, the host was asked to "
              "redraw (the program got its SIGWINCH)" % what,
              a.wait(b"WINCH", start=back), a.out[back:])
        before = rx()
        a.key(b"z", 0.3)
        check("remote chord, %s: the attach was never retired (a key after "
              "reaches the session, the host saw one attach)" % what,
              until(lambda: rx()[len(before):] == "7a", 5) and
              a.reap() is None and attaches() == n0 + 1,
              (rx()[len(before):], a.rc, attaches() - n0))
    finally:
        a.close()
    time.sleep(0.5)


def test_chord(port):
    hosts("srv=127.0.0.1:%d" % port)
    r = dch(SENV, "--spawn", "xv-rx", "--size", "100x30", sys.executable,
            "-c", RX, RXF)
    check("remote chord: the probe session spawned on the host",
          r.returncode == 0, r.stderr)
    chord_case("x in a later read", [b"\x1c", b"x"])
    chord_case("x in the same burst", [b"\x1cx"])
    chord_case("the kitty CSI form", [b"\x1c", b"\033[120u"])
    chord_case("--host srv -a xv-rx", [b"\x1cx"],
               tail=("--host", "srv", "-a", "xv-rx"))
    cfg = os.path.join(T, "config-switch")
    with open(cfg, "w") as f:
        f.write("remote_hosts = srv=127.0.0.1:%d\ndetach_key = switch\n"
                % port)
    chord_case("detach_key = switch", [b"\x1c", b"x"],
               dict(CENV, DCH_CONFIG=cfg))

    # -E: no detach key, no chord; the session gets both bytes
    a, _ = rattach(extra=["-E"])
    try:
        got = rx()
        mark = len(a.out)
        a.key(b"\x1c", 0.05)
        a.key(b"x", 0.5)
        until(lambda: rx()[len(got):] == "1c78", 5)
        check("remote chord, -E: the session receives the detach key and x, "
              "no view", rx()[len(got):] == "1c78" and
              b"X-ray" not in a.out[mark:] and a.reap() is None,
              (rx()[len(got):], a.out[mark:]))
    finally:
        a.close()
    time.sleep(0.5)

    # DCH_DOUBLE_TAP_MS=0: the press detaches at once, no view, nothing
    # reaches the session
    for keys in ([b"\x1c", b"x"], [b"\x1cx"]):
        a, _ = rattach(dict(CENV, DCH_DOUBLE_TAP_MS="0"))
        try:
            got = rx()
            mark = len(a.out)
            a.key(keys[0], 0.05)
            rc = a.exit(5)
            if len(keys) > 1:
                try:
                    os.write(a.fd, keys[1])
                except OSError:
                    pass
            time.sleep(0.5)
            check("remote chord, DCH_DOUBLE_TAP_MS=0, %s: the detach key "
                  "detaches at once (exit 0), the session receives nothing, "
                  "no view" % ("x after" if len(keys) > 1 else "one burst"),
                  rc == 0 and rx()[len(got):] == "" and
                  b"X-ray" not in a.out[mark:],
                  (rc, rx()[len(got):], a.out[mark:]))
        finally:
            a.close()
        time.sleep(0.5)


# ---- 7: the view's failure lines over a remote attach ----------------------

class Attachable(Fake):
    """A host without "xray": its first connection is the attach, its 0x10
    keys kept in keys and its 0x07 redraws counted; a later one (the view's)
    is Fake's, `mode` "ok" (a hello without "xray") or "refuse" (the gate's
    0x13)."""

    def __init__(self, mode):
        self.keys, self.redraws = b"", 0
        super().__init__(mode, rows=[{"name": "fk"}],
                         ops=["ctl", "ctl-cancel"])

    def serve(self, c):
        if c is not self.conns[0]:
            return Fake.serve(self, c)
        buf = b""
        try:
            while True:
                d = c.recv(65536)
                if not d:
                    return
                buf += d
                while len(buf) >= 5:
                    t, n = struct.unpack(">BI", buf[:5])
                    if len(buf) < 5 + n:
                        break
                    body, buf = buf[5:5 + n], buf[5 + n:]
                    if t == 0x08:
                        c.sendall(frame(0x18, {"proto": "1.0",
                                               "server": "fake",
                                               "ops": self.ops}))
                    elif t == 0x03:
                        c.sendall(frame(0x11, self.rows))
                    elif t == 0x01:
                        self.attached.set()
                        c.sendall(frame(0x10, b"FAKE-SCREEN\r\n"))
                    elif t == 0x10:
                        self.keys += body
                    elif t == 0x07:
                        self.redraws += 1
        except OSError:
            return


def chord_fail(what, mode, line, close=False):
    h = Attachable(mode)
    hosts("q=127.0.0.1:%d" % h.port)
    a = Pty([DCH, "-a", "q:fk"])
    try:
        ok = a.wait(b"FAKE-SCREEN") and h.attached.wait(5)
        if close:
            h.sock.close()  # the attach is in; the view's dial is refused
        a.pump(0.5)
        mark = len(a.out)
        a.key(b"\x1cx", 0.5)
        check("remote chord, %s: the view opens with its line" % what,
              ok and a.wait(line.encode(), start=mark), a.out[mark:][-600:])
        mark = len(a.out)
        a.key(b"q", 0.5)
        check("remote chord, %s: q says the line and waits for a key" % what,
              a.wait(b"(any key: back to the session)", start=mark) and
              a.reap() is None, a.out[mark:][-300:])
        a.key(b" ", 0.5)
        a.key(b"z", 0.3)
        until(lambda: h.keys == b"z", 5)
        check("remote chord, %s: a key goes back to the attach: the host got "
              "the redraw, then only the next key (no detach key, x or the "
              "key that left)" % what, h.keys == b"z" and h.redraws == 1 and
              a.reap() is None, (h.keys, h.redraws, a.rc))
    finally:
        a.close()
        h.close()


def test_chord_fail():
    chord_fail("a host without xray (exit 6)", "ok",
               "dch: q: does not support --xray (needs dch serve 1.55.0+)")
    chord_fail("a gate that refuses the view (exit 4)", "refuse",
               "dch: q refused: " + REFUSAL["message"])
    chord_fail("an unreachable host (exit 7)", "ok", "dch: q: unreachable: ",
               close=True)


# ---- 8: the host re-arms the session's modes on 0x07 (#189 round 2) --------

MD = ("import os, signal, sys, tty\n"
      "tty.setraw(0)\n"
      "signal.signal(signal.SIGWINCH,\n"
      "              lambda *a: os.write(1, b'WINCH\\r\\n'))\n"
      "os.write(1, b'\\033[?1049h\\033[?2004h\\033[?1000h\\033[?1006h'\n"
      "         b'\\033[>1uALTSCREEN\\r\\n')\n"
      "while True:\n"
      "    b = os.read(0, 256)\n"
      "    if not b: break\n"
      "    os.write(1, b'GOT:' + b.hex().encode() + b'\\r\\n')\n")
MODES = (b"\033[?2004h", b"\033[?1000h", b"\033[?1006h", b"\033[>1u")


def rearmed(out, at):
    """The modes missing from out past at."""
    return [m for m in MODES + (b"\033[?1049h",) if m not in out[at:]]


def test_rearm():
    lite = dch(SENV, "--build").stdout.strip() == "lite"
    r = dch(SENV, "--spawn", "xv-md", "--size", "100x30", sys.executable,
            "-c", MD)
    check("re-arm: the probe session spawned on the host", r.returncode == 0,
          r.stderr)
    a = Pty([DCH, "-a", "srv:xv-md"])
    try:
        # dch-lite keeps no screen: the attach re-arms the modes only
        ok = a.wait(b"\033[>1u" if lite else b"ALTSCREEN")
        a.pump(0.5)
        check("re-arm: the attach shows the program's modes%s"
              % ("" if lite else " and screen"),
              ok and not rearmed(a.out, 0), a.out[-300:])
        mark = len(a.out)
        a.key(b"\x1cx", 0.5)
        ok = a.wait(b"X-ray  srv:xv-md", start=mark)
        check("re-arm: the chord opens the view over the attach", ok,
              a.out[mark:][-600:])
        if not ok:
            return
        mark = len(a.out)
        a.key(b"q", 0.5)
        a.wait(RESTORE, start=mark)
        back = a.out.find(RESTORE, mark)
        until(lambda: (a.pump(0.1), not rearmed(a.out, back))[1], 5)
        a.pump(0.5)
        out = a.out
        check("re-arm: after the view's ESC[?1049l the host re-armed "
              "bracketed paste, mouse reporting, kitty flags and 1049h",
              back >= 0 and not rearmed(out, back),
              (rearmed(out, back), out[back:][-600:]))
        alt = out.find(b"\033[?1049h", back)
        check("re-arm: the program is back on the alternate screen, the "
              "kitty push after its 1049h (on that screen)",
              alt > back and out.find(b"\033[>1u", alt) > alt,
              out[back:][-600:])
        if lite:
            # no mirror to replay: the program repaints on its SIGWINCH
            check("re-arm, dch-lite: no screen replay, the program got its "
                  "SIGWINCH after the 1049h", alt > back and
                  b"ALTSCREEN" not in out[alt:] and
                  a.wait(b"WINCH", start=alt), out[back:][-600:])
        else:
            check("re-arm: the screen is replayed from the mirror after the "
                  "1049h", alt > back and b"ALTSCREEN" in out[alt:],
                  out[back:][-600:])
        # a redraw with no reset before it (SIGUSR2: 0x07) re-arms again
        mark = len(a.out)
        os.kill(a.pid, signal.SIGUSR2)
        until(lambda: (a.pump(0.1), not rearmed(a.out, mark))[1], 5)
        check("re-arm: a SIGUSR2 redraw over the attach re-arms again",
              not rearmed(a.out, mark), (rearmed(a.out, mark),
                                         a.out[mark:][-400:]))
        mark = len(a.out)
        a.key(b"z", 0.3)
        check("re-arm: the attach goes on (a key after reaches the session)",
              a.wait(b"GOT:7a", start=mark, secs=5) and a.reap() is None,
              (a.rc, a.out[mark:][-300:]))
    finally:
        a.close()
    time.sleep(0.5)


def main():
    sp = None
    try:
        sp, port = serve()
        check("dch serve listens on a loopback test port", port is not None,
              open(SLOG).read())
        if port is None:
            return
        fixture()
        shell()
        hosts("srv=127.0.0.1:%d" % port)
        test_view()
        test_on_screen()
        test_picker(port)
        test_hostile()
        test_failures()
        test_chord(port)
        test_chord_fail()
        hosts("srv=127.0.0.1:%d" % port)
        test_rearm()
    finally:
        dch(SENV, "-k", "xv-sh")
        dch(SENV, "-k", "xv-rx")
        dch(SENV, "-k", "xv-md")
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
