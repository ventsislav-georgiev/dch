#!/usr/bin/env python3
"""The X-ray view of a remote session (#188, XR5c): `dch --xray host:name`
and the picker's x on a remote row.

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
     ref --full, chapter); k, Up, PgUp and Enter in its card under a
     too-small window neither crash nor read outside the mentions (QA
     1.56.0 MED-V1);
  2. On screen of srv:xv-sh, a live shell on the host: no number on its tab
     before the screen is read (#193), the screen read with
     ctl --read xv-sh --ansi, o/r#41 gets hint a, typing it opens
     its card (full build); on dch-lite "No screen: dch-lite keeps no
     mirror" and still no number;
  3. a hostile host: raw ESC, BEL, C1 and bidi bytes in its snapshot and in
     its --read screen are drawn as ?, its OSC and CSI dropped, and its
     screen still gets its hint; invalid UTF-8 a ? per byte and a NUL a ?
     with the rest after it; a later failed read's stderr said in place of
     the screen, as clean (QA 1.57.0 LOW-R1, NIT-R1);
  4. a host without "xray" (exit 6), a closed port (exit 7) and a gate that
     refuses us (exit 4): the line in place of the tabs, the view open until
     q, then the exit code and the line on stderr;
  5. the picker: x on the remote Sessions row srv:xv-sh opens its view, q
     returns to the picker, X (kill) there opens no view (OQ-6); x on the
     remote History row srv:xv-cl opens that record's view; next to a local
     session literally named srv:xv-sh, x on each row opens that row's
     session (QA 1.57.0 LOW-R2), and Enter on the remote row, o in its
     view and o in a dch --xray srv:xv-sh view opened before the local
     one came attach the host's, o in the local row's view the local one
     (#210);
  6. the in-attach chord over `dch -a srv:xv-rx` (#189): the detach key then
     x (a later read, one burst, the kitty CSI form, --host srv -a xv-rx,
     detach_key = switch) opens the view of srv:xv-rx over the attach, On
     screen read with its own ctl --read; the session never gets the detach
     key or the x; q goes back, the host is asked to redraw (the program's
     SIGWINCH), a key after reaches the session, and the host saw one attach
     (never retired); next to a local srv:xv-rx too (LOW-R2). Off under -E
     and DCH_DOUBLE_TAP_MS=0, as the local chord. dch -d xv-rx on the
     host, or dch -d srv:xv-rx from the client, with the view up: the
     client leaves at once, exit 0, the view and the relay gone, the
     session untouched (QA 1.58.0 MED-R6); a session that ends under the
     view, or whose master is SIGKILLed there (its socket file left, #230),
     leaves it up until q, as before;
  7. the view's failure lines over an attach to a fake host without "xray"
     (exit 6), whose gate refuses the view's connection (exit 4), or whose
     port closes once the attach is in (exit 7): the line, q, any key, and
     the attach goes on (the host gets the redraw, then only the next key);
     an arrow key, a UTF-8 character or a paste as the any key is all
     swallowed (QA 1.58.0 LOW-R8), and so is a paste over 4096 bytes,
     bracketed or not, and one in several writes 0.3 s apart (#230);
  8. the host re-arms on its redraw (#189 round 2): a program on the
     alternate screen with bracketed paste, mouse reporting and kitty flags
     on, over `dch -a srv:xv-md`; after the chord's view the client's output
     past the view's ESC[?1049l holds ESC[?1049h, then the kitty push (on the
     alternate screen), ESC[?2004h, ESC[?1000h and ESC[?1006h; the full build
     repaints the screen from its mirror, dch-lite has none and the program
     repaints on its SIGWINCH. Three SIGUSR2 redraws (0x07, no reset before
     them) re-arm again, each with one kitty pop before its one push, so the
     flag stack does not grow (a per-screen stack model of the whole attach
     ends at 1 on the alternate screen, 0 on the main one); the first attach
     pops nothing. A key after still reaches the session;
  9. o (#194): on srv:xv-sh's Chapters the view is left as q leaves it and
     the client attaches to xv-sh on the host (the host's log says so);
     on the ended srv:xv-cl it says the session has ended and stays;
  10. stopping a remote child (#204): a fake host whose answers stream
      without end (more than a pipe holds) and never take a 0x34: q while
      the follow streams (3 times), 20 cards each left with Esc while its
      lines stream (every other one after f replaced the light card), Esc
      then q; a silent host: Esc from a pending card, f over a pending
      card, Esc then q, q with no snapshot. Each within STOP_S (1 s; it used
      to hang for good, or wait 3 s per child), only the follow child after
      each card, no zombie, the view's fd count flat over the 20 cards, the
      terminal restored, exit 0, and no child the test saw left running.
  11. a link that drops and comes back (QA 1.57.0 LOW-R3): the status says
      live updates stopped and how to get them back, and no failed read
      replaces it; a failed read shows in place of the screen; once the
      host answers On screen reads again and no unreachable line is left.

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
        # pty.fork()'s master is inheritable: the next kid would hold it,
        # and closing ours would not hang this pty up (QA 1.59.0 MED-T2)
        os.set_inheritable(self.fd, False)

    def pump(self, secs=0.05):
        end = time.time() + secs
        while True:
            if time.time() >= end:
                return
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
        # the fd first: on macOS a killed pty leader with output unread
        # cannot finish exiting, so the waitpid would never return
        os.close(self.fd)
        if self.reap() is None:
            os.kill(self.pid, signal.SIGKILL)
            end = time.time() + 10
            while self.reap() is None and time.time() < end:
                time.sleep(0.05)
            if self.rc is None:
                print("  leaving pty child %d unreaped" % self.pid)


def ctls(what):
    with open(SLOG) as f:
        return f.read().count(" ctl %s " % what)


def refused():
    """The host's gate refusals: a whois past its 3 s is refused and cached
    30 s, and no ctl of that window is logged (#249). For failure details."""
    with open(SLOG) as f:
        return [l for l in f.read().splitlines() if ": refused" in l][-3:]


def serve():
    with open(SLOG, "w") as lf:
        sp = subprocess.Popen([DCH, "serve", "--bind", "127.0.0.1",
                               "--port", "0"],
                              env=dict(SENV, DCH_SERVE_TEST_LOOPBACK="1"),
                              stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=lf)
    end = time.time() + 30     # a loaded machine: room, still bounded
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
        # #227: p is the scope toggle (o until 1.60.x); o opens, as in the list
        mark = len(v.out)
        v.key(b"p", 1.0)
        check("card: p scans other projects on the host",
              v.wait("full scan \u00b7 other projects".encode(), start=mark)
              and v.wait(b"p this project", start=mark), v.out[mark:])
        mark = len(v.out)
        v.key(b"o", 0.5)
        check("card: o on the ended srv:xv-cl: said, the view stays",
              v.wait(b"xv-cl has ended: no session to open", start=mark) and
              v.reap() is None, v.out[mark:])
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
        mark = len(v.out)
        v.key(b"o", 0.5)
        check("o on the ended srv:xv-cl: said, the view stays",
              v.wait(b"xv-cl has ended: no session to open", start=mark) and
              v.reap() is None, v.out[mark:])
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
    # follow, ref (light), ref --full, p's ref --full --scope all, chapter:
    # five, each its own ctl
    until(lambda: ctls("--xray") - n0["--xray"] >= 5, 5)
    check("every query ran on the host as its own ctl --xray (5), no --read "
          "for an ended session", ctls("--xray") - n0["--xray"] == 5 and
          ctls("--read") == n0["--read"],
          (ctls("--xray") - n0["--xray"], ctls("--read") - n0["--read"],
           refused()))


def test_small_card():
    """QA 1.56.0 MED-V1 over a host:name view: k, Up and PgUp in a
    too-small window took the card's selection below 0 and Enter read
    v->m[-1] (SIGSEGV)."""
    v = Pty([DCH, "--xray", "srv:xv-cl"])
    try:
        v.wait(b"Chapters 2")
        v.key(b"\t")
        v.wait(b"#831")
        mark = len(v.out)
        v.key(b"\r")
        check("small card: srv:xv-cl's #831 card opens with its mentions",
              v.wait(b"Mentions: this session ", start=mark), v.out[mark:])
        mark = len(v.out)
        v.resize(6, 20)
        small = v.wait(b"too small", start=mark)
        n0 = ctls("--xray")
        for k in (b"k", b"k", b"k", b"\033[A", b"\033[5~", b"\r"):
            v.key(k, 0.1)
        mark = len(v.out)
        v.resize(30, 100)
        v.wait(b"You", start=mark)
        check("small card: k, Up, PgUp then Enter on srv:xv-cl: no crash, "
              "Enter reads the first mention's chapter from the host",
              v.reap() is None and b"You" in v.out[mark:],
              (v.rc, small, ctls("--xray") - n0, refused(),
               v.out[mark:][-600:]))
        v.key(b"qqq")
        check("small card: q, q, q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()


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


# ---- 6: o attaches to the host's session (#194) ----------------------------

def test_open():
    with open(SLOG) as f:
        n0 = f.read().count(" attach xv-sh (")
    CENV["DCH_DOUBLE_TAP_MS"] = "0"  # one Ctrl-\ detaches at once
    v = Pty([DCH, "--xray", "srv:xv-sh"])
    try:
        v.wait(b"Chapters")
        v.pump(0.5)
        check("o: srv:xv-sh's footer offers it", v.wait(b"o open session"),
              v.out[-600:])
        mark = len(v.out)
        v.key(b"o", 0.3)
        attached = lambda: open(SLOG).read().count(" attach xv-sh (") > n0
        end = time.time() + 10
        while not attached() and time.time() < end:
            v.pump(0.1)
        check("o: the view left as q leaves it, then the host attaches "
              "xv-sh for us", attached() and RESTORE in v.out[mark:] and
              v.reap() is None, (v.out[mark:][-400:], open(SLOG).read()[-400:]))
        v.key(b"\x1c", 0.5)
        check("o: a real attach, the detach key ends it", v.exit(5) == 0,
              v.out[-200:])
    finally:
        del CENV["DCH_DOUBLE_TAP_MS"]
        v.close()


# ---- 3: a hostile host -------------------------------------------------------

HOSTILE_TITLE = "t\x1b]0;pwned\x07 a\u009bb c‮d e⁦f"
HOSTILE_SCREEN = (b"$ echo hi\x1b]0;pwned\x07\r\n"
                  b"see o/r#41 \x1b[31mred\x1b[0m\x1b[5Cgap\x07bel"
                  b" c1\xc2\x9b2J bidi\xe2\x80\xaex \x1b[2J\r\n"
                  b"sur[\xed\xa0\x80] f8[\xf8\x88\x80\x80] "
                  b"trunc[\xe4\xb8A] f4[\xf4\x90\x80\x80] nul[\x00]after\r\n")
# a later --read fails: the host's stderr, raw, is the line said
HOSTILE_ERR = b"err\x1b]0;pwned\x07 sur\xed\xa0\x80 tr\xe4\xb8\n"
BAD_UTF8 = (b"\xed\xa0\x80", b"\xf8\x88", b"\xe4\xb8", b"\xf4\x90", b"\x00")


class Hostile(Fake):
    """A host with ctl, ctl-cancel and xray: --follow answers a snapshot
    with raw control and bidi bytes and holds the stream; the first --read
    answers a screen with OSC, CSI, BEL, C1, bidi, invalid UTF-8 and NUL
    bytes, every later one fails (exit 1) with such a stderr; anything else
    ends 0."""

    def __init__(self):
        super().__init__("ok", ops=["ctl", "ctl-cancel", "xray"])
        self.reads = 0

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
                            self.reads += 1
                            if self.reads > 1:
                                c.sendall(frame(0x32, HOSTILE_ERR) +
                                          frame(0x33, {"exit": 1}))
                                continue
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
        check("hostile: the screen's invalid UTF-8 (surrogate, F8 lead, "
              "truncated, past U+10FFFF) shows as ?, a byte each, and a NUL "
              "as ? with the rest after it (QA 1.57.0 LOW-R1, NIT-R1)",
              v.wait(b"sur[???] f8[????] trunc[??A] f4[????] nul[?]after",
                     start=mark), v.out[mark:])
        check("hostile: a failed read's stderr is said in place of the screen,"
              " its OSC, BEL and invalid UTF-8 as ?",
              v.wait(b"No screen: err?]0;pwned? sur??? tr??", start=mark),
              v.out[mark:][-600:])
        v.key(b"q")
        rc = v.exit()
        check("hostile: q exits 0", rc == 0, (rc, v.out[-80:]))
        bad = [b for b in (b"\x1b]0;pwned", b"\x07 a", b"\x07bel",
                           b"\xc2\x9b", b"\xe2\x80\xae", b"\xe2\x81\xa6")
               + BAD_UTF8 if b in v.out]
        check("hostile: none of the host's control, bidi or invalid UTF-8 "
              "bytes reached the terminal", not bad, bad)
        check("hostile: the screen was asked as --read fk --ansi (no "
              "--cursor)", ["--read", "fk", "--ansi"] in h.bodies, h.bodies)
    finally:
        v.close()
        h.close()


# ---- 11: a link that drops and comes back (QA 1.57.0 LOW-R3) ---------------

class Flaky(Fake):
    """A host with ctl, ctl-cancel and xray whose link drops: --follow
    answers a live snapshot and holds the stream, --read a screen with
    self.word; down() cuts every connection and each new one at once, up()
    answers again."""

    def __init__(self):
        super().__init__("ok", ops=["ctl", "ctl-cancel", "xray"])
        self.up, self.word = True, b"LINK-UP"

    def down(self):
        self.up = False
        for c in list(self.conns):
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def serve(self, c):
        if not self.up:
            c.close()
            return
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
                        if "--follow" in argv:
                            c.sendall(frame(0x31, SNAP))
                            continue    # a stream: held until the close
                        if argv[0] == "--read":
                            c.sendall(frame(0x31, b"$ echo hi\r\n" +
                                            self.word + b"\r\n"))
                        c.sendall(frame(0x33, {"exit": 0}))
        except OSError:
            return


def redraw(v, cols):
    """The whole frame, drawn again after a resize."""
    v.resize(30, cols)
    mark = len(v.out)
    v.pump(1.0)
    return v.out[mark:]


def test_link_drop():
    h = Flaky()
    hosts("q=127.0.0.1:%d" % h.port)
    v = Pty([DCH, "--xray", "q:fk"])
    stop = b"live updates stopped (reopen the view for them): dch: q: "
    try:
        check("link drop: the view opens on the snapshot",
              v.wait(b"X-ray  q:fk") and v.wait(b"Chapters 1"),
              v.out[-600:])
        mark = len(v.out)
        v.key(b"\t\t", 1.5)
        check("link drop: On screen shows the host's screen",
              v.wait(b"LINK-UP", start=mark), v.out[mark:])
        mark = len(v.out)
        h.down()
        v.pump(3.5)
        down = v.out[mark:]
        frame_ = redraw(v, 101)
        # lines are drawn whole: each host line in the bytes is one drawn
        check("link drop: the status says live updates stopped and how to "
              "get them back, and no failed read replaces it over 3 s",
              stop in frame_ and
              all(down[:m.start()].endswith((b"No screen: ", stop[:-8]))
                  for m in re.finditer(b"dch: q: ", down)),
              (frame_[-400:], down[-600:]))
        check("link drop: a failed read is said in place of the screen",
              b"No screen: dch: q: unreachable" in frame_, frame_[-600:])
        h.word = b"LINK-BACK"
        h.up = True
        mark = len(v.out)
        v.pump(2.5)
        frame_ = redraw(v, 100)
        check("link drop, host back: On screen reads again",
              v.wait(b"LINK-BACK", start=mark), v.out[mark:][-600:])
        check("link drop, host back: no line says the host is unreachable "
              "but the stop's own, which stays",
              stop in frame_ and b"No screen" not in frame_ and
              frame_.count(b"unreachable") ==
              frame_.count(stop + b"unreachable"), frame_[-600:])
        v.key(b"q")
        check("link drop: q exits 0", v.exit() == 0, v.out[-80:])
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


# ---- 10: stopping a remote child (#204) --------------------------------------

STOP_S = 1.0    # the bug is a hang or RM_CANCEL_MS (3 s) per child
SNAP = ('{"schema":1,"snapshot":{"session":"fk","session_id":'
        '"m0000000000000001","corpus":"claude","live":true,"lines":3,'
        '"project_label":"/x","chapters":[{"anchor":"s1","title":"first",'
        '"kind":"prompt","at":1794300000,"offset":0}],"refs":[{"key":'
        '"pull:o/r#41","shape":"pull","text":"o/r#41","title":"a ref",'
        '"count":2,"underline":true}]}}\n').encode()
PAD = b"".join(b'{"pad":"%s"}\n' % (b"p" * 240) for _ in range(128))
MENTIONS = b"".join(json.dumps({"mention": {
    "session": "fk", "line": "mention %d " % k + "m" * 200, "chapter": "s1",
    "at": k}}).encode() + b"\n" for k in range(128))


class Stopper(Fake):
    """A host with ctl, ctl-cancel and xray that never answers a 0x34.
    stream: --follow's snapshot then pad lines without end (follow=1, else
    held), --ref's mention lines without end, both until the close. silent:
    the snapshot (snap=1), then nothing at all."""

    def __init__(self, mode, follow=0, snap=1):
        super().__init__("ok", ops=["ctl", "ctl-cancel", "xray"])
        self.smode, self.follow, self.snap = mode, follow, snap

    def flood(self, c, data):
        while True:
            c.sendall(frame(0x31, data))
            time.sleep(0.01)    # ~3 MB/s: a pipe's worth in ~20 ms

    def run(self, c, argv):
        try:
            if "--follow" in argv:
                if self.snap:
                    c.sendall(frame(0x31, SNAP))
                if self.smode == "stream" and self.follow:
                    self.flood(c, PAD)
            elif "--ref" in argv and self.smode == "stream":
                self.flood(c, MENTIONS)
            elif self.smode == "stream":
                c.sendall(frame(0x33, {"exit": 0}))
        except OSError:
            pass

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
                        threading.Thread(target=self.run, args=(c, argv),
                                         daemon=True).start()
        except OSError:
            return


def kids_of(pid):
    """The view's children: {pid: ps state}."""
    o = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,stat="],
                       capture_output=True, text=True).stdout
    return {int(p[0]): p[2] for p in (ln.split() for ln in o.splitlines())
            if len(p) == 3 and p[1] == str(pid)}


def alive(pids):
    """Which of pids still run as a dch (pid reuse aside)."""
    if not pids:
        return []
    o = subprocess.run(["ps", "-o", "pid=,comm=", "-p",
                        ",".join(str(p) for p in pids)],
                       capture_output=True, text=True).stdout
    return [ln.strip() for ln in o.splitlines() if "dch" in ln]


def nfds(pid):
    """The view's open fds, or None when neither /proc nor lsof says."""
    if os.path.isdir("/proc/%d/fd" % pid):
        return len(os.listdir("/proc/%d/fd" % pid))
    if not os.path.exists("/usr/sbin/lsof"):
        return None
    o = subprocess.run(["/usr/sbin/lsof", "-p", str(pid)],
                       capture_output=True, text=True).stdout
    return len(o.splitlines()) - 1


def gone(what, v, rc, seen):
    check("%s: the terminal restored, exit 0" % what,
          rc == 0 and RESTORE in v.out[-60:], (rc, v.out[-80:]))
    time.sleep(0.2)
    left = alive(sorted(seen))
    check("%s: no child of the view left running" % what, not left, left)


def card(v, tab=False):
    if tab:
        v.key(b"\t", 0.3)  # to Refs; Esc from a card keeps the tab
    mark = len(v.out)
    v.key(b"\r", 0.1)
    return v.wait(b"light", start=mark, secs=5)


def esc_q(v):
    """Esc, the list back, then q (one write would be Alt-q): the exit
    code or None, and the seconds both took."""
    mark = len(v.out)
    t0 = time.time()
    v.key(b"\033", 0)
    if not v.wait(b"Chapters 1", start=mark, secs=STOP_S):
        return None, time.time() - t0
    v.key(b"q", 0)
    return v.exit(STOP_S), time.time() - t0


def shut(v):
    """Pty.close, reading the pty meanwhile: a hung view's output fills it,
    and its exit then waits for the drain."""
    if v.reap() is None:
        os.kill(v.pid, signal.SIGKILL)
        end = time.time() + 10
        while v.reap() is None and time.time() < end:
            v.pump(0.1)
    os.close(v.fd)
    end = time.time() + 10
    while v.reap() is None and time.time() < end:
        time.sleep(0.05)
    if v.rc is None:
        print("  leaving view %d unreaped" % v.pid)


def test_stop():
    # (a) an answer larger than a pipe still streaming
    for i in range(3):
        h = Stopper("stream", follow=1)
        seen = set()
        hosts("q=127.0.0.1:%d" % h.port)
        v = Pty([DCH, "--xray", "q:fk"])
        try:
            v.wait(b"Chapters 1")
            v.pump(0.5)
            seen.update(kids_of(v.pid))
            t0 = time.time()
            v.key(b"q", 0)
            rc = v.exit(STOP_S)
            dt = time.time() - t0
            check("stream %d: q while the follow streams exits within %.1f s "
                  "(%.2f s)" % (i, STOP_S, dt), rc is not None, v.out[-200:])
            v.exit(10)
            gone("stream %d" % i, v, v.rc, seen)
        finally:
            shut(v)
            h.close()
    h = Stopper("stream")
    seen = set()
    hosts("q=127.0.0.1:%d" % h.port)
    v = Pty([DCH, "--xray", "q:fk"])
    try:
        check("stream: the view opens", v.wait(b"Chapters 1"), v.out[-300:])
        seen.update(kids_of(v.pid))
        n0 = None
        worst = [0.0, 0.0]
        bad = []
        for i in range(20):
            ok = card(v, i == 0)
            v.pump(0.2)     # the mention lines stream
            seen.update(kids_of(v.pid))
            if i % 2:
                # f: the full card stops the light one still streaming
                mark = len(v.out)
                t0 = time.time()
                v.key(b"f", 0)
                ok = v.wait(b"full scan", start=mark, secs=STOP_S) and ok
                worst[1] = max(worst[1], time.time() - t0)
                v.pump(0.2)
                seen.update(kids_of(v.pid))
            mark = len(v.out)
            t0 = time.time()
            v.key(b"\033", 0)
            ok = v.wait(b"Chapters 1", start=mark, secs=STOP_S) and ok
            worst[0] = max(worst[0], time.time() - t0)
            v.pump(0.1)
            k = kids_of(v.pid)
            if not ok or len(k) != 1 or "Z" in "".join(k.values()):
                bad.append((i, ok, k))
            if v.reap() is not None or len(bad) > 2:
                break
            if i == 0:
                n0 = nfds(v.pid)
        n1 = nfds(v.pid)
        check("stream: 20 cards, each left with Esc while its lines stream "
              "(odd ones after f replaced the light card) within %.1f s "
              "(worst Esc %.2f s, f %.2f s), only the follow child after "
              "each, no zombie" % (STOP_S, worst[0], worst[1]),
              not bad and i == 19, bad)
        if n0 is None or n1 is None:
            print("  skip the view's fd count (no /proc, no lsof)")
        else:
            check("stream: the view's fds do not grow over 20 cards "
                  "(%s, %s)" % (n0, n1), n1 <= n0, (n0, n1))
        ok = card(v)
        v.pump(0.2)
        seen.update(kids_of(v.pid))
        rc, dt = esc_q(v)
        check("stream: Esc then q while a card streams exits within %.1f s "
              "(%.2f s)" % (STOP_S, dt), ok and rc is not None and dt < STOP_S,
              v.out[-200:])
        v.exit(10)
        gone("stream", v, v.rc, seen)
    finally:
        shut(v)
        h.close()
    # (b) a silent host: each stop used to wait RM_CANCEL_MS
    h = Stopper("silent")
    seen = set()
    hosts("q=127.0.0.1:%d" % h.port)
    v = Pty([DCH, "--xray", "q:fk"])
    try:
        check("silent: the view opens", v.wait(b"Chapters 1"), v.out[-300:])
        seen.update(kids_of(v.pid))
        ok = card(v, True)
        seen.update(kids_of(v.pid))
        mark = len(v.out)
        t0 = time.time()
        v.key(b"\033", 0)
        ok = v.wait(b"Chapters 1", start=mark, secs=STOP_S) and ok
        check("silent: Esc from a pending card within %.1f s (%.2f s)" % (
            STOP_S, time.time() - t0), ok, v.out[mark:][-300:])
        ok = card(v)
        seen.update(kids_of(v.pid))
        mark = len(v.out)
        t0 = time.time()
        v.key(b"f", 0)
        ok = v.wait(b"full scan", start=mark, secs=STOP_S) and ok
        check("silent: f (a new card) over a pending one within %.1f s "
              "(%.2f s)" % (STOP_S, time.time() - t0), ok,
              v.out[mark:][-300:])
        seen.update(kids_of(v.pid))
        k = kids_of(v.pid)
        check("silent: only the follow and the new card's child run, no "
              "zombie", len(k) == 2 and "Z" not in "".join(k.values()), k)
        rc, dt = esc_q(v)
        check("silent: Esc then q (card and follow pending) exits within "
              "%.1f s (%.2f s)" % (STOP_S, dt), rc is not None and dt < STOP_S,
              v.out[-200:])
        v.exit(10)
        gone("silent", v, v.rc, seen)
    finally:
        shut(v)
        h.close()
    h = Stopper("silent", snap=0)
    seen = set()
    hosts("q=127.0.0.1:%d" % h.port)
    v = Pty([DCH, "--xray", "q:fk"])
    try:
        v.pump(1.0)
        seen.update(kids_of(v.pid))
        t0 = time.time()
        v.key(b"q", 0)
        rc = v.exit(STOP_S)
        dt = time.time() - t0
        check("silent, no snapshot: q exits within %.1f s (%.2f s)" % (
            STOP_S, dt), len(seen) == 1 and rc is not None,
            (seen, v.out[-200:]))
        v.exit(10)
        gone("silent, no snapshot", v, v.rc, seen)
    finally:
        shut(v)
        h.close()


# ---- 5: the picker's x on a remote row --------------------------------------

def test_picker(port):
    hosts("srv=127.0.0.1:%d" % port)
    v = Pty([DCH, "-l"], cols=140)
    try:
        check("picker: the remote row srv:xv-sh is listed",
              v.wait(b"xv-sh", 15), v.out[-600:])
        v.pump(1.0)
        mark = len(v.out)
        v.key(b"x", 1.0)
        check("picker: x on the remote row opens its view",
              v.wait(b"X-ray  srv:xv-sh", start=mark) and
              v.wait(b"Chapters", start=mark), v.out[mark:][-600:])
        mark = len(v.out)
        v.key(b"q", 1.0)
        check("picker: q in the view returns to the picker",
              v.reap() is None and v.wait(b"Sessions", start=mark),
              v.out[mark:][-600:])
        n0 = ctls("--xray")
        mark = len(v.out)
        v.key(b"X", 1.0)
        check("picker: X on the remote row opens no view and kills nothing "
              "(OQ-6)", v.wait(b"Enter attaches and x opens its X-ray",
                                start=mark)
              and b"X-ray  srv:xv-sh" not in v.out[mark:] and
              ctls("--xray") == n0, v.out[mark:][-600:])
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
        v.key(b"x", 1.0)
        check("picker: x on the remote History row opens its record's view",
              v.wait(b"X-ray  srv:xv-cl", start=mark) and
              v.wait(b"Chapters 2", start=mark), v.out[mark:][-600:])
        v.key(b"q", 1.0)
        v.key(b"\033")
        check("picker: Esc quits it", v.exit() is not None, v.out[-80:])
    finally:
        v.close()


def host_attaches():
    with open(SLOG) as f:
        return f.read().count(" attach xv-sh (")


def same_attach(what, v, keys, remote):
    """keys on the picker or view v attach a srv:xv-sh: the host's when
    remote (the host logs an attach), else the local one (its LOCAL-MARK
    drawn, the host asked nothing); the detach key ends it, exit 0."""
    n0 = host_attaches()
    if keys == b"o":
        # the view drops o until its first answer: wait for the footer
        # that offers it, past the view's newest title (#249)
        check("same name: %s: the view offers o" % what,
              v.wait(b"o open session",
                     start=max(0, v.out.rfind(b"X-ray  srv:xv-sh"))),
              (refused(), v.out[-300:]))
    mark = len(v.out)
    v.key(keys, 0.3)
    if remote:
        ok = until(lambda: host_attaches() > n0)
        v.pump(0.5)
    else:
        ok = v.wait(b"LOCAL-MARK", start=mark) and \
            (v.pump(1.5) or host_attaches() == n0)
    check("same name: %s attaches the %s srv:xv-sh" %
          (what, "host's" if remote else "local"), ok and v.reap() is None,
          (host_attaches() - n0, v.rc, v.out[mark:][-300:]))
    v.key(b"\x1c", 0.5)
    check("same name: %s: a real attach, the detach key ends it" % what,
          v.exit(5) == 0, v.out[-200:])


def row_view(v, remote):
    """In the picker v, x on the srv:xv-sh row that is remote (or not):
    the view is left open on it. False when neither row is."""
    for keys in (b"kkk", b"jjj"):
        n0 = ctls("--xray")
        mark = len(v.out)
        v.key(keys + b"x", 1.0)
        # its first answer: a remote view's ctl is logged by then
        v.wait(b"o open session", start=mark)
        if (ctls("--xray") > n0) == remote:
            return True
        mark = len(v.out)
        v.key(b"q", 0)
        v.wait(b"[Sessions 2]", start=mark)     # the picker is back
    return False


def test_same_name():
    """QA 1.57.0 LOW-R2: a local session literally named srv:xv-sh next to
    the host's xv-sh; x on each row opens that row's session (only the
    remote one asks the host). #210: Enter on the remote row, o in the
    remote view (dch --xray srv:xv-sh opened before the local one came,
    the picker's x) attach the host's; o in the local row's view the
    local one."""
    # its own client dirs, the same remote_hosts: the History record the
    # local srv:xv-sh leaves would route srv:xv-sh here for the next tests
    lenv = side("same", DCH_CONFIG=CENV["DCH_CONFIG"],
                DCH_REMOTE_BOX_MS="5000", DCH_DOUBLE_TAP_MS="0")
    try:
        v = Pty([DCH, "--xray", "srv:xv-sh"], env=lenv)
        try:
            ok = v.wait(b"Chapters")
            r = dch(dict(lenv, DCH_NO_REMOTE="1"), "--spawn", "srv:xv-sh",
                    "--size", "80x24", "sh", "-c",
                    "while :; do echo LOCAL-MARK; sleep 1; done")
            check("same name: a local session named srv:xv-sh spawned "
                  "under the view of the host's", ok and r.returncode == 0,
                  r.stderr)
            v.pump(0.5)
            same_attach("o in dch --xray srv:xv-sh's view", v, b"o", True)
        finally:
            v.close()
        v = Pty([DCH, "-l"], cols=140, env=lenv)
        try:
            # the host's row comes with its probe: both rows listed
            check("same name: the picker lists both srv:xv-sh rows",
                  v.wait(b"2 sessions", 15), (refused(), v.out[-300:]))
            asked = []
            # the top row, then the bottom one: the two srv:xv-sh rows
            for keys in (b"kkk", b"jjj"):
                n0 = ctls("--xray")
                mark = len(v.out)
                v.key(keys + b"x", 1.0)
                ok = v.wait(b"X-ray  srv:xv-sh", start=mark) and \
                    v.wait(b"o open session", start=mark)
                asked.append((ok, ctls("--xray") - n0, keys))
                mark = len(v.out)
                v.key(b"q", 0)
                v.wait(b"[Sessions 2]", start=mark)     # the picker is back
            check("same name: x on the remote row opens the host's "
                  "session, x on the local row the local one",
                  all(a[0] for a in asked) and
                  sorted(a[1] > 0 for a in asked) == [False, True], asked)
            check("same name: the remote row found for Enter",
                  row_view(v, True))
            mark = len(v.out)
            v.key(b"q", 0)
            v.wait(b"[Sessions 2]", start=mark)     # the picker is back
            same_attach("Enter on the remote row", v, b"\r", True)
        finally:
            v.close()
        for what, remote in (("o in the remote row's view", True),
                             ("o in the local row's view", False)):
            v = Pty([DCH, "-l"], cols=140, env=lenv)
            try:
                v.wait(b"2 sessions", 15)
                check("same name: x on the %s row opens its view" %
                      ("remote" if remote else "local"), row_view(v, remote))
                same_attach(what, v, b"o", remote)
            finally:
                v.close()
    finally:
        dch(dict(lenv, DCH_NO_REMOTE="1"), "-k", "srv:xv-sh")


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
    # the client reads keys once it put the terminal in raw mode (#249)
    until(lambda: not termios.tcgetattr(a.fd)[3] & termios.ICANON)
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
        # its answer: a number on the tab, or dch-lite's line (#249)
        read = until(lambda: a.pump(0.1) or re.search(
            rb"On screen \d|keeps no mirror", a.out[mark:]), 15)
        check("remote chord, %s: On screen read over its own ctl --read"
              % what, until(lambda: ctls("--read") > r0, 5),
              (ctls("--read") - r0, bool(read), refused()))
        # two checks (#233, #249): MARK189 goes dch --send -> master -> pty,
        # not through the attach, so its absence is a stalled session, not
        # the chord; a leak is any other byte. The details print even empty
        sent = dch(SENV, "--send", "xv-rx", "MARK189").returncode
        until(lambda: "4d41524b313839" in rx()[len(got):], 5)
        a.pump(0.3)
        rxd = rx()[len(got):]
        check("remote chord, %s: the text sent while the view was up reached "
              "the session (dch --send, not the attach)" % what,
              "4d41524b313839" in rxd, (rxd, sent))
        check("remote chord, %s: nothing else reached the session (no detach "
              "key, no x)" % what,
              rxd.replace("4d41524b313839", "", 1) == "", (rxd, sent))
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
    # a local session named srv:xv-rx, in its own client dirs: the
    # attach's own view still opens (QA 1.57.0 LOW-R2)
    lenv = side("same-rx", DCH_CONFIG=CENV["DCH_CONFIG"])
    r = dch(dict(lenv, DCH_NO_REMOTE="1"), "--spawn", "srv:xv-rx", "--size",
            "80x24", "sh", "-c", "while :; do sleep 1; done")
    check("remote chord: a local session named srv:xv-rx spawned",
          r.returncode == 0, r.stderr)
    chord_case("--host srv -a xv-rx, a local srv:xv-rx too", [b"\x1cx"],
               lenv, tail=("--host", "srv", "-a", "xv-rx"))
    dch(dict(lenv, DCH_NO_REMOTE="1"), "-k", "srv:xv-rx")
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


def test_chord_detach():
    """QA 1.58.0 MED-R6: dch -d xv-rx on the host, or dch -d srv:xv-rx from
    the client, with the chord's view up: the client leaves as a local
    dch -d makes it (#196): the view goes, exit 0, no line, and the session
    gets nothing and lives on. A session that ends under the view leaves
    the view up as before (#217: serve's 0x12 tells the two apart), then q
    exits 0. So does a master SIGKILLed under the view (#230): its socket
    file is left behind, and that is no detach."""
    for how in ("on the host", "-d srv:xv-rx from the client"):
        a, _ = rattach()
        try:
            got = rx()
            mark = len(a.out)
            a.key(b"\x1cx", 0.05)
            ok = a.wait(b"X-ray  srv:xv-rx", start=mark)
            check("remote -d, %s: the view opens" % how, ok,
                  a.out[mark:][-300:])
            kids = list(kids_of(a.pid))
            mark = len(a.out)
            r = dch(SENV, "-d", "xv-rx") if how == "on the host" else \
                dch(CENV, "-d", "srv:xv-rx")
            check("remote -d, %s: it says it detached one client" % how,
                  r.returncode == 0 and "detached 1 client" in r.stdout,
                  (r.returncode, r.stdout, r.stderr))
            # the bug keeps the view up until q: 15 s is far past a detach
            rc = a.exit(15)
            tail = a.out[mark:]
            check("remote -d, %s: the client leaves without q, exit 0, no "
                  "line" % how, rc == 0 and b"lost the connection" not in
                  tail, (rc, tail[-300:]))
            check("remote -d, %s: the view's alternate screen was left" % how,
                  RESTORE in tail, tail[-300:])
            check("remote -d, %s: the view and the relay are gone" % how,
                  kids and until(lambda: not alive(kids), 5), kids)
            dch(SENV, "--send", "xv-rx", "alive")
            check("remote -d, %s: the session got nothing and lives on" % how,
                  until(lambda: rx()[len(got):] == "616c697665", 5),
                  rx()[len(got):])
        finally:
            a.close()
        time.sleep(0.5)

    # the session ends under the view: the view stays up, as before
    for nm, how in (("xv-re", "end"), ("xv-rk", "SIGKILL")):
        chord_end(nm, how)


def chord_end(nm, how):
    """The session nm ends under the chord's view: its program exits, or
    (#230) its master is SIGKILLed, leaving its socket file."""
    what = "remote end" if how == "end" else "remote SIGKILL"
    r = dch(SENV, "--spawn", nm, "--size", "100x30", "sh", "-c",
            "echo READY; read x; echo BYE")
    check("%s: the session spawned on the host" % what, r.returncode == 0,
          r.stderr)
    a = Pty([DCH, "-a", "srv:" + nm])
    try:
        a.wait(b"READY")
        a.pump(0.5)
        mark = len(a.out)
        a.key(b"\x1cx", 0.05)
        # its screen read before the end (dch-lite: read, no mirror): a
        # view slower than that (ASan) says "no such session" itself
        a.wait(("X-ray  srv:" + nm).encode(), start=mark)
        check("%s: the view opens and reads the screen" % what,
              until(lambda: a.pump(0.1) or re.search(
                  rb" READY\033\[K|No screen: ", a.out[mark:])),
              a.out[mark:][-300:])
        mark = len(a.out)
        sock = "/%s.sock -- " % nm
        pids = [int(ln.split()[0]) for ln in subprocess.run(
                ["ps", "-eo", "pid,command"], capture_output=True,
                text=True).stdout.splitlines()
                if "--master-of" in ln and T in ln and sock in ln]
        if how == "end":
            dch(SENV, "--send", nm, "go\n")
        elif len(pids) == 1:
            os.kill(pids[0], signal.SIGKILL)	# this test's session's master
        ended = until(lambda: " %s exited" % nm in open(SLOG).read())
        a.pump(3)
        check("%s: the session ended under the view; the view stays" % what,
              ended and a.reap() is None and RESTORE not in a.out[mark:],
              (ended, pids, a.rc, a.out[mark:][-300:]))
        a.key(b"q", 0.3)
        rc = a.exit(10)
        check("%s: q leaves, exit 0, no line" % what, rc == 0 and
              b"lost the connection" not in a.out[mark:],
              (rc, a.out[mark:][-300:]))
    finally:
        a.close()
        dch(SENV, "-k", nm)
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


def chord_fail(what, mode, line, close=False, anykey=b" "):
    h = Attachable(mode)
    hosts("q=127.0.0.1:%d" % h.port)
    a = Pty([DCH, "-a", "q:fk"])
    try:
        ok = a.wait(b"FAKE-SCREEN") and h.attached.wait(5)
        if close:
            # the attach is in; the view's dial is refused. Linux keeps a
            # listener accepting while a thread sits in accept(): shutdown
            # ends it there (macOS says ENOTCONN and close() is enough)
            try:
                h.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            h.sock.close()
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
        for k in anykey if isinstance(anykey, list) else [anykey]:
            a.key(k, 0.3)
        a.pump(0.2)
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
    # QA 1.58.0 LOW-R8: the any key is the whole read, none of it for the
    # session
    for what, k in (("an arrow key", b"\033[A"),
                    ("a UTF-8 character", "\u00a9".encode()),
                    ("a paste", b"\033[200~pasted text\033[201~"),
                    # #230: past the one 4096-byte read, and over several
                    ("a bracketed paste of 10000 bytes",
                     b"\033[200~" + b"p" * 10000 + b"\033[201~"),
                    ("a plain paste of 10000 bytes", b"q" * 10000),
                    ("a bracketed paste in three writes 0.3 s apart",
                     [b"\033[200~" + b"a" * 3000, b"b" * 3000,
                      b"c" * 3000 + b"\033[201~"])):
        chord_fail("%s as the any key" % what, "ok",
                   "dch: q: does not support --xray (needs dch serve "
                   "1.55.0+)", anykey=k)


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
KSEQ = re.compile(rb"\033\[(\?1049[hl]|>\d*u|<\d*u)")


def kitty_stacks(out):
    """The kitty flag stack depths (main, alt) a terminal ends with after
    out: a stack per screen, 1049h/l switch, ESC[<Nu pops N (default 1)."""
    st, alt = [0, 0], 0
    for m in KSEQ.finditer(out):
        t = m.group(1)
        if t[:1] == b"?":
            alt = int(t[-1:] == b"h")
        elif t[:1] == b">":
            st[alt] += 1
        else:
            st[alt] = max(0, st[alt] - int(t[1:-1] or 1))
    return tuple(st)


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
        check("re-arm: the first attach pops no kitty flags (push only)",
              b"\033[<" not in a.out and a.out.count(b"\033[>1u") == 1,
              a.out[-300:])
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
        # redraws with no reset before them (SIGUSR2: 0x07) re-arm again,
        # one pop before each push: the stack stays as it was
        for i in range(1, 4):
            mark = len(a.out)
            os.kill(a.pid, signal.SIGUSR2)
            until(lambda: (a.pump(0.1), not rearmed(a.out, mark))[1], 5)
            a.pump(0.5)
            got = a.out[mark:]
            check("re-arm: SIGUSR2 redraw %d over the attach re-arms again"
                  % i, not rearmed(a.out, mark),
                  (rearmed(a.out, mark), got[-400:]))
            check("re-arm: SIGUSR2 redraw %d: one kitty pop, before its one "
                  "push" % i, got.count(b"\033[<u") == 1 and
                  len(re.findall(rb"\033\[<\d*u", got)) == 1 and
                  got.count(b"\033[>1u") == 1 and
                  got.find(b"\033[<u") < got.find(b"\033[>1u"), got[-400:])
        check("re-arm: over the whole attach pushes minus pops is 1 (the "
              "stacks end at main 0, alternate 1)",
              kitty_stacks(a.out) == (0, 1), kitty_stacks(a.out))
        mark = len(a.out)
        a.key(b"z", 0.3)
        check("re-arm: the attach goes on (a key after reaches the session)",
              a.wait(b"GOT:7a", start=mark, secs=5) and a.reap() is None,
              (a.rc, a.out[mark:][-300:]))
    finally:
        a.close()
    time.sleep(0.5)


# ---- 12: a ctrl-z suspend over the attach drops the host's output (#211) ---

BZ = ("import os, select, sys, time, tty\n"
      "tty.setraw(0)\n"
      "os.write(1, b'\\033[?1049hBUSY\\r\\n')\n"
      "while True:\n"
      "    if select.select([0], [], [], 0.0005)[0]:\n"
      "        b = os.read(0, 256)\n"
      "        if not b: break\n"
      "        os.write(1, b'GOT:' + b.hex().encode() + b'\\r\\n')\n"
      "    os.write(1, b'\\033[5;1HT%.4f ' % time.time())\n")
# the client as a shell runs a job: its own foreground process group, so
# its ctrl-z SIGTSTP really stops it; STOPPED is written then, after its
# own clock's time (the resume is a full second after that time, however
# late the test reads the marker), and it is continued a second later;
# exits with the client's exit code
JOB = ("import os, signal, sys, time\n"
       "job = os.fork()\n"
       "if job == 0:\n"
       "    os.setpgid(0, 0)\n"
       "    signal.signal(signal.SIGTTOU, signal.SIG_IGN)\n"
       "    os.tcsetpgrp(0, os.getpgrp())\n"
       "    signal.signal(signal.SIGTTOU, signal.SIG_DFL)\n"
       "    os.execv(sys.argv[1], sys.argv[1:])\n"
       "while True:\n"
       "    _, st = os.waitpid(job, os.WUNTRACED)\n"
       "    if not os.WIFSTOPPED(st):\n"
       "        sys.exit(os.waitstatus_to_exitcode(st))\n"
       "    os.write(1, b'<<at %.4f>><<stopped>>' % time.time())\n"
       "    time.sleep(1)\n"
       "    os.kill(job, signal.SIGCONT)\n")
STOPPED = b"<<stopped>>"


def test_suspend():
    r = dch(SENV, "--spawn", "xv-bz", "--size", "100x30", sys.executable,
            "-c", BZ)
    check("suspend: the busy session spawned on the host", r.returncode == 0,
          r.stderr)
    a = Pty([sys.executable, "-c", JOB, DCH, "-a", "srv:xv-bz"])
    # the attach is live: the session's ticks reach us after `at` (#249)
    live = lambda at: until(lambda: a.pump(0.1) or TICK.search(a.out, at), 10)
    try:
        a.wait(b"BUSY" if dch(SENV, "--build").stdout.strip() == "full"
               else b"\033[?1049h")
        live(len(a.out))
        for i in range(1, 4):
            mark = len(a.out)
            a.key(b"\x1a", 0)
            ok = a.wait(STOPPED, start=mark)
            stop = a.out.find(STOPPED, mark) + len(STOPPED)
            at = re.findall(rb"<<at (\d+\.\d+)>>", a.out[mark:stop])
            t0 = float(at[-1]) if at else time.time()
            a.wait(b"\033[?1049h", start=stop)
            alt = a.out.find(b"\033[?1049h", stop)
            late = [t for t in re.findall(rb"T(\d+\.\d+) ",
                                          a.out[stop:alt]) if
                    t0 + 0.05 < float(t) < t0 + 0.85]
            check("suspend: ctrl-z %d over the attach: no output the host "
                  "sent while it was stopped before the resume's 1049h" % i,
                  ok and alt > stop and not late,
                  (ok, alt - stop,
                   ["+%.2f" % (float(t) - t0) for t in late][:5],
                   a.out[stop:stop + 200]))
            live(alt)    # the next ctrl-z over a live attach, not a resume
        mark = len(a.out)
        a.key(b"z", 0.3)
        check("suspend: the attach goes on (a key after reaches the session)",
              a.wait(b"GOT:7a", start=mark, secs=5) and a.reap() is None,
              (a.rc, a.out[mark:][-300:]))
        mark = len(a.out)
        a.key(b"\x1a", 0)
        ok = a.wait(STOPPED, start=mark)
        dch(SENV, "-k", "xv-bz")
        rc = a.exit()
        check("suspend: the session ending while the attach is stopped ends "
              "it at the resume, exit 0, no lost-link line",
              ok and rc == 0 and b"lost the connection" not in a.out[mark:],
              (ok, rc, a.out[mark:][-300:]))
    finally:
        a.close()
        dch(SENV, "-k", "xv-bz")
    time.sleep(0.5)


# #231: the host's re-arm after a resume (or the X-ray overlay's return) is
# an RTT away; a proxy passes the 0x07 SLOW s late so that window is wide.
# What the busy session sent in it is for the main screen the reset left.
SLOW = 0.25    # well inside the relay's 1 s hold (RM_REARM_MS)
TICK = re.compile(rb"T\d+\.\d+ ")


def test_suspend_rtt(port):
    from remote_client_test import Proxy
    r = dch(SENV, "--spawn", "xv-bw", "--size", "100x30", sys.executable,
            "-c", BZ)
    check("suspend rtt: the busy session spawned on the host",
          r.returncode == 0, r.stderr)
    full = dch(SENV, "--build").stdout.strip() == "full"
    px = Proxy(port, "slow07", delay=SLOW)
    hosts("srv=127.0.0.1:%d" % px.port)
    a = Pty([sys.executable, "-c", JOB, DCH, "-a", "srv:xv-bw"])
    try:
        a.wait(b"BUSY" if full else b"\033[?1049h")
        a.pump(0.5)
        for i in range(1, 4):
            mark = len(a.out)
            a.key(b"\x1a", 0)
            ok = a.wait(STOPPED, start=mark)
            stop = a.out.find(STOPPED, mark) + len(STOPPED)
            a.wait(b"\033[?1049h", start=stop)
            alt = a.out.find(b"\033[?1049h", stop)
            got = TICK.findall(a.out[stop:alt])
            check("suspend rtt: ctrl-z %d, the host's re-arm %.2f s late: "
                  "no session output on the main screen before the "
                  "resume's 1049h" % (i, SLOW),
                  ok and alt > stop and not got,
                  (ok, len(got), a.out[stop:stop + 200]))
            a.pump(0.3)
        mark = len(a.out)
        a.key(b"\x1cx", 0.5)
        ok = a.wait(b"X-ray  srv:xv-bw", start=mark)
        mark = len(a.out)
        a.key(b"q", 0.5)
        a.wait(RESTORE, start=mark)
        back = a.out.find(RESTORE, mark)
        a.wait(b"\033[?1049h", start=back)
        alt = a.out.find(b"\033[?1049h", back)
        got = TICK.findall(a.out[back:alt])
        check("suspend rtt: the X-ray overlay's return, the re-arm %.2f s "
              "late: no session output on the main screen before its 1049h"
              % SLOW, ok and back >= mark and alt > back and not got,
              (ok, len(got), a.out[back:back + 200]))
        mark = len(a.out)
        a.key(b"z", 0.3)
        check("suspend rtt: the attach goes on (a key after reaches the "
              "session)", a.wait(b"GOT:7a", start=mark, secs=5) and
              a.reap() is None, (a.rc, a.out[mark:][-300:]))
    finally:
        a.close()
        px.close()
    # a host that never re-arms (its 0x07 lost, as a host before 1.58.0
    # never re-arms): the client must not hold the session's output forever
    px = Proxy(port, "no07")
    hosts("srv=127.0.0.1:%d" % px.port)
    a = Pty([sys.executable, "-c", JOB, DCH, "-a", "srv:xv-bw"])
    try:
        a.wait(b"BUSY" if full else b"\033[?1049h")
        a.pump(0.5)
        mark = len(a.out)
        a.key(b"\x1a", 0)
        ok = a.wait(STOPPED, start=mark)
        stop = a.out.find(STOPPED, mark) + len(STOPPED)
        until(lambda: (a.pump(0.1), TICK.search(a.out, stop))[1], 6)
        check("suspend rtt: a host that never re-arms: the session's output "
              "still comes back after the resume",
              ok and TICK.search(a.out, stop) is not None and
              a.reap() is None, (ok, a.rc, a.out[stop:stop + 200]))
    finally:
        a.close()
        px.close()
        dch(SENV, "-k", "xv-bw")
        hosts("srv=127.0.0.1:%d" % port)
    time.sleep(0.5)


def main():
    sp = None
    try:
        sp, port = serve()
        if port is None:
            # one failure for the serve half, not one per check; a return
            # here skipped the exit status below (#249)
            check("dch serve listens on a loopback test port: %s; its log "
                  "tail:" % ("exited %d" % sp.returncode if sp.poll() is not
                             None else "no listening line in 30 s"), False)
            for l in open(SLOG).read().splitlines()[-12:]:
                print("       " + l)
            print("  skip every other check: they need that serve")
            return 1
        check("dch serve listens on a loopback test port", True)
        fixture()
        shell()
        hosts("srv=127.0.0.1:%d" % port)
        test_view()
        test_small_card()
        test_on_screen()
        test_picker(port)
        test_same_name()
        test_open()
        test_hostile()
        test_failures()
        test_link_drop()
        test_stop()
        test_chord(port)
        test_chord_detach()
        test_chord_fail()
        hosts("srv=127.0.0.1:%d" % port)
        test_rearm()
        test_suspend()
        test_suspend_rtt(port)
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
    sys.exit(main())
