#!/usr/bin/env python3
"""The `dch --xray <n>` view and the picker's x (#175, XR5a).

The view runs on a pty this test owns (pty.fork), so it needs no terminal
mirror and runs the same on dch-lite. A History Claude record whose chapter
title carries an OSC 0 title, a BEL and U+202E, and whose definition title
carries an SGR, is the hostile corpus.

Covers:
  1. a non-tty stdout is refused, exit 2 (a remote name's refusal is
     remote_client_test.py's);
  2. --scope, --full or --budget-ms with no --json and no --ref on a
     terminal: exit 2, a usage message, the view not opened; no such
     session: exit 1, the message after the terminal is restored;
  3. the three tabs, the chapter list, the Refs list, the card (light, `f`
     full scan, `y` OSC 52 copy), the chapter reader, back, quit (exit 0,
     terminal restored);
  4. mutant 11: no corpus ESC, BEL or bidi byte (U+202E, U+061C, U+2028)
     reaches the terminal, the strings show with `?` in their place;
  5. nothing written: no file under the test root changes;
  6. a resize to a narrow window says so, a resize back redraws; the
     message fits down to 5 columns, and k, Up, PgUp and Enter in a card
     under a too-small window neither crash nor read outside the mentions
     (QA 1.56.0 MED-V1, NIT-V3);
  7. SIGTERM and SIGINT: the terminal is restored and the view's children
     are gone;
  8. On screen on a live shell: the ref printed in two blocks (underline)
     gets hint label `a`, typing it opens its card (full build); on dch-lite
     the label line says X-ray needs the full build;
  9. the picker: the x hint is shown, x on a session row opens the view, q
     returns to the picker; X asks to kill and never opens the view (OQ-6);
 10. the Refs tab (#193): a file under the project shown relative to it,
     one under HOME from ~, one too long cut at the front (its tail kept,
     a wide-character one within the width), a long URL keeping its host
     and its end; rows by count then the most recent first; files under
     /tmp, /private/tmp, /var/folders and $TMPDIR hidden, t toggling them,
     the footer saying how many, the tab count the engine's; the filter
     matching the full key (and taking t as text), Enter opening the full
     key's card, f there showing the resolver's line (QA 1.56.0 NIT-V2);
     the faint line saying what a ref is, the empty state when
     there are none; On screen with no number until its screen is read,
     then its count, 0 included (on dch-lite: no number, no mirror);
 11. the Chapters tab (#194): a chapter the harness wrote ("auto") hidden,
     n showing it faint and titled by its summary (its title without one),
     the footer saying how many, the tab count the shown-by-default ones,
     the selection kept on its chapter, the filter matching a summary, the
     empty state when every chapter is one; a compaction (#223) hidden
     and counted with them, n showing it faint with its = marker, Enter
     reading its summary, compactions and auto chapters alone the same
     empty state; o on the Chapters list and in
     the reader leaves the view (the terminal restored as q does) and
     attaches as dch -a, from the picker's x too (the -k picker refuses);
     an ended session's o says so and stays; in the chord o goes back to the
     session;
 12. the in-attach chord (#187, K10): the detach key then x in a later read,
     in one burst, and in the kitty CSI form opens the view over the attach
     on On screen; the x never reaches the session, the attach is never
     retired (a key typed after reaches the session), and output written
     while the view was up is on screen after it closes (full build: the
     mirror's repaint); off under -E and with DCH_DOUBLE_TAP_MS=0, where the
     session receives what it did before; under detach_key = switch a late
     x is the picker's (OQ-6): it opens the row's view, q returns to the
     picker, X reaches the kill confirmation, and nothing kills.

Run:  python3 tests/xray_view_test.py      (uses ./dch)
"""
import base64, fcntl, json, os, pty, re, select, shutil, signal, struct
import subprocess, sys, tempfile, termios, time, unicodedata

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

    def __init__(self, argv, rows=30, cols=100, env=None):
        self.out = b""
        self.rc = None
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
            os.execve(argv[0], argv, dict(ENV, **(env or {})))

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
        # the fd first: on macOS a killed pty leader with output unread
        # cannot finish exiting, so the waitpid would never return
        os.close(self.fd)
        if self.reap() is None:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)


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


CUP = re.compile(rb"\033\[(\d+);1H")


def rows(out):
    """The text last drawn on each row in out, its escapes removed."""
    got = {}
    parts = CUP.split(out)
    for i in range(1, len(parts) - 1, 2):
        got[int(parts[i])] = re.sub(rb"\033\[[0-9;?]*[A-Za-z]", b"",
                                    parts[i + 1]).decode("utf-8", "replace")
    return got


def cells(s):
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


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

TITLE = ("look at #831 and o/r#7 \033]0;pwned\007 evil\u202etitle "
         "al\u061cm ls\u2028ep")


def fixture():
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    out = ("# #831 Canary \033[31mred\033[0m passage\n"
           "See #831 again\u2028and\u061c PROJ-5")
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
    for a in (["--scope", "all"], ["--full"], ["--budget-ms", "5"]):
        v = Pty([DCH, "--xray", "xv-cl"] + a)
        try:
            rc = v.exit()
            check("%s with no --json and no --ref on a terminal: exit 2, "
                  "usage, no view" % a[0], rc == 2 and
                  b"--scope, --full and --budget-ms come with --ref" in v.out
                  and b"\033[?1049h" not in v.out, (rc, v.out))
        finally:
            v.close()
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
        check("mutant 11: the chapter title's OSC, BEL, U+202E and U+061C "
              "show as ?", v.wait(b"o/r#7 ?]0;pwned? evil?title al?m"),
              v.out[-600:])
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
        check("mutant 11: a mention line's U+2028 and U+061C show as ?",
              v.wait(b"See #831 again?and? PROJ-5", start=mark),
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
                           b"\033[31mred", b"\xd8\x9c", b"\xe2\x80\xa8")
               if b in v.out]
        check("mutant 11: no corpus ESC, BEL or bidi byte in the output",
              not bad, bad)
    finally:
        v.close()
    after = files(T)
    check("nothing written: no file under the test root changed",
          before == after, set(before.items()) ^ set(after.items()))


# ---- the Refs tab (#193) ---------------------------------------------------

FSID = "11111111-2222-4333-8444-5555555555f3"
P = "/home/demo/proj"
DEEP = P + "/src/very/deep/module/"
LONG = DEEP + "with/many/more/levels/of/nesting/made/for/the/view/test/" \
    "zeta_handler.c"
WIDE = P + "/docs/\u8a2d\u8a08\u66f8/\u8a73\u7d30\u8a2d\u8a08\u306e" \
    "\u8aac\u660e\u8cc7\u6599/\u4ed5\u69d8\u4e00\u89a7\u8868\u306e" \
    "\u6700\u7d42\u7248/\u753b\u9762\u9077\u79fb.md"
URL = "https://example.com/org/repo/pull/12/files#diff-0123456789abcdef"
# the view's HOME and TMPDIR: fake, outside the test root
RENV = {"HOME": "/home/demo", "TMPDIR": "/home/demo/tmpx/"}


def fixture_refs():
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    said = ("edit %salpha_handler.c and %sbeta_handler.c, then %s and %s; "
            "scratch in /tmp/scratch-one.txt, "
            "/private/tmp/demo-502/tasks/out.log, /var/folders/xx/abc/T/t.1 "
            "and /home/demo/tmpx/note.txt; see /home/demo/notes/todo.md, "
            "%s, o/r#7, %s/src/main.c and %s/README.md; again "
            "%salpha_handler.c" % (DEEP, DEEP, LONG, WIDE, URL, P, P, DEEP))
    lines = [
        {"type": "user", "cwd": P, "timestamp": "2026-11-10T10:00:00Z",
         "message": {"content": "the refs fixture"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": said}]}}]
    with open(os.path.join(pdir, FSID + ".jsonl"), "w") as f:
        for o in lines:
            f.write(json.dumps(o) + "\n")
    sockdir = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    rec = {"name": "xv-rf", "cwd": P, "end": "exited", "argv": ["claude"],
           "started": 1794299400, "updated": 1794300000,
           "ended": 1794300000, "sock_dir": sockdir,
           "agent": {"session_id": FSID, "cwd": P, "config_dir": cfg}}
    with open(os.path.join(hdir, "xv-rf.json"), "w") as f:
        f.write(json.dumps(rec) + "\n")


ELL = "(?:\u2026|\\.\\.\\.)"


def test_refs():
    o = json.loads(dch("--xray", "xv-rf", "--json").stdout)
    keys = [x["key"] for x in o["refs"]]
    check("refs fixture: the engine lists every ref (13)",
          len(keys) == 13 and "file:" + LONG in keys and
          "file:" + WIDE in keys, keys)
    v = Pty([DCH, "--xray", "xv-rf"], env=RENV)
    try:
        v.wait(b"Chapters 1")
        v.pump(0.5)
        check("On screen: no number before its screen is read (an ended "
              "session has none)", b"On screen" in v.out and
              not re.search(rb"On screen \d", v.out), v.out[-600:])
        mark = len(v.out)
        v.key(b"\t", 1.0)
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        check("Refs: the tab count stays the engine's total (13)",
              "Refs 13" in text, text)
        check("Refs: one faint line says what a ref is and that Enter "
              "opens its card", re.search("Files, commits, pull requests, "
                                          "issues and URLs this session "
                                          "touched; (\u21b5|enter) opens a "
                                          "card", text), text)
        check("Refs: a file under the project shows relative to it",
              "file    src/very/deep/module/alpha_handler.c" in text and
              "file    src/main.c" in text and P + "/src" not in text, text)
        check("Refs: a file under HOME starts with ~",
              "file    ~/notes/todo.md" in text, text)
        check("Refs: a path too long keeps its tail, the ellipsis in front",
              re.search(ELL + "[^\n]*/for/the/view/test/zeta_handler\\.c",
                        text) and "src/very/deep/module/with" not in text,
              text)
        order = ["alpha_handler.c", "README.md", "src/main.c", "o/r#7",
                 "example.com", "~/notes/todo.md", "zeta_handler.c",
                 "beta_handler.c"]
        at = [text.find(x) for x in order]
        check("Refs: by count, then the most recent first (later in the "
              "snapshot)", -1 not in at and at == sorted(at), (at, text))
        tmp = ["/tmp/scratch-one.txt", "/private/tmp/demo-502/tasks/out.log",
               "/var/folders/xx/abc/T/t.1", "/home/demo/tmpx/note.txt",
               "~/tmpx/note.txt"]
        check("Refs: files under /tmp, /private/tmp, /var/folders and "
              "$TMPDIR are hidden", not any(x in text for x in tmp), text)
        check("Refs: the footer shows t and how many are hidden",
              "t temp files 4 hidden" in text, text)
        mark = len(v.out)
        v.key(b"t", 1.0)
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        check("Refs: t shows the temp files, the footer says t hides them",
              all(x in text for x in tmp[:3]) and "t hide temp files" in text
              and "hidden" not in text, text)
        mark = len(v.out)
        v.key(b"t", 1.0)
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        check("Refs: t again hides them", "t temp files 4 hidden" in text and
              "/tmp/scratch-one.txt" not in text, text)
        mark = len(v.out)
        v.key(b"/")
        v.key(b"scratch-one", 1.0)
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        check("Refs: the filter takes t as text; only a hidden row matches: "
              "said", "/scratch-one" in text and
              "Only temp files: t shows them" in text, text)
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("Refs: the filter done, the footer counts the hidden match",
              v.wait(b"t temp files 1 hidden", start=mark), v.out[mark:])
        mark = len(v.out)
        v.key(b"t", 1.0)
        v.key(b"\r", 1.0)
        check("Refs: Enter on the shown temp row opens its card",
              v.wait(b"/tmp/scratch-one.txt  light", start=mark),
              v.out[mark:])
        mark = len(v.out)
        v.key(b"f")
        check("card: f shows the resolver's line (QA 1.56.0 NIT-V2)",
              v.wait(b"file Outside this session's project  "
                     b"/tmp/scratch-one.txt", start=mark), v.out[mark:])
        v.key(b"\033", 0.5)
        v.key(b"\033", 0.5)
        v.key(b"t", 0.5)
        mark = len(v.out)
        v.key(b"/")
        v.key(P.encode() + b"/src/main", 1.0)
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        check("Refs: the filter matches the full key, the row shows it "
              "relative", "file    src/main.c" in text and
              "alpha_handler" not in text, text)
        mark = len(v.out)
        v.key(b"\r", 1.0)  # done, and Enter on the row, as the picker's
        check("Refs: Enter opens the card of the full key",
              v.wait(P.encode() + b"/src/main.c  light", start=mark),
              v.out[mark:])
        v.key(b"\033", 0.5)
        v.key(b"\033", 0.5)
        mark = len(v.out)
        v.resize(30, 60)
        v.pump(1.0)
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        check("Refs, 60 columns: a long URL keeps its host and its end",
              re.search("url     example\\.com" + ELL +
                        "[^\n]*#diff-0123456789abcdef", text) and
              "https://" not in text, text)
        wide = [r.rstrip() for r in scr.values()
                if r.rstrip().endswith(WIDE[-7:])]
        check("Refs, 60 columns: a wide-character path keeps its tail within "
              "the width", wide and re.search(ELL, wide[0]) and
              cells(wide[0]) <= 60 and "docs/" not in wide[0],
              (wide, [cells(r) for r in wide]))
        v.key(b"q")
        check("Refs: q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()


# ---- the Chapters tab: auto chapters, o (#194) ------------------------------

ASID = "11111111-2222-4333-8444-5555555555a4"
BSID = "11111111-2222-4333-8444-5555555555a5"
CSID = "11111111-2222-4333-8444-5555555555a6"
DSID = "11111111-2222-4333-8444-5555555555a7"


def note(inner):
    return "<task-notification>\n" + inner + "\n</task-notification>"


def fixture_auto():
    """xv-au: two prompts, three notifications between them (one with a
    summary, one with a hostile summary, one with none); xv-aa: only
    notifications. Each in a shape Claude Code writes: a user record with
    "origin", or a queued_command attachment with commandMode (and, newer,
    its own origin). xv-ac: two prompts, a compaction (a compact_boundary,
    then its isCompactSummary user record) and a notification between
    them; xv-ca: only a compaction and a notification (#223)."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    sockdir = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    au = ["fix the login bug",
          note("<task-id>t1</task-id>\n<status>completed</status>\n"
               "<summary>Build finished: 3 passed</summary>"),
          ("q", note("<task-id>t2</task-id>\n<status>failed</status>")),
          ("qo", note("<task-id>t3</task-id>\n<summary>Lint \033]0;pwned"
                      "\007 evil\u202edone</summary>")),
          "deploy it"]
    aa = [note("<task-id>t9</task-id>\n<summary>Only a notice</summary>")]
    cp = ("c", "This session is being continued: the compact summary")
    ac = ["start the work", cp, aa[0], "finish the work"]
    for sid, name, prompts in ((ASID, "xv-au", au), (BSID, "xv-aa", aa),
                               (CSID, "xv-ac", ac), (DSID, "xv-ca", [cp, aa[0]])):
        lines = []
        for i, t in enumerate(prompts):
            o = {"cwd": P, "timestamp": "2026-11-10T1%d:00:00Z" % i}
            if isinstance(t, tuple) and t[0] == "c":
                lines.append(dict(o, type="system", subtype="compact_boundary"))
                o.update(type="user", isCompactSummary=True,
                         message={"content": t[1]})
            elif isinstance(t, tuple):
                a = {"type": "queued_command", "prompt": t[1],
                     "commandMode": "task-notification"}
                if t[0] == "qo":
                    a["origin"] = {"kind": "task-notification"}
                o.update(type="attachment", attachment=a)
            else:
                o.update(type="user", message={"content": t})
                if t.startswith("<task-notification>"):
                    o["origin"] = {"kind": "task-notification"}
            lines += [o,
                      {"type": "assistant", "message": {"content": [
                          {"type": "text", "text": "reply %d" % i}]}}]
        with open(os.path.join(pdir, sid + ".jsonl"), "w") as f:
            for o in lines:
                f.write(json.dumps(o) + "\n")
        rec = {"name": name, "cwd": P, "end": "exited", "argv": ["claude"],
               "started": 1794299400, "updated": 1794300000,
               "ended": 1794300000, "sock_dir": sockdir,
               "agent": {"session_id": sid, "cwd": P, "config_dir": cfg}}
        with open(os.path.join(hdir, name + ".json"), "w") as f:
            f.write(json.dumps(rec) + "\n")


def screen(v, mark):
    """The rows drawn since mark, runs of blanks as one (a colour key chip
    pads its key)."""
    scr = rows(v.out[mark:])
    return re.sub(" +", " ", "\n".join(scr[k] for k in sorted(scr)))


def test_auto():
    o = json.loads(dch("--xray", "xv-au", "--json").stdout)
    ch = o["chapters"]
    check("auto fixture: five chapters, the three notifications auto, two "
          "with a summary", len(ch) == 5 and
          [c.get("auto", False) for c in ch] ==
          [False, True, True, True, False] and
          [("summary" in c) for c in ch] == [False, True, False, True, False],
          ch)
    bare = ch[2]["title"]
    # colours on: faint is an SGR
    v = Pty([DCH, "--xray", "xv-au"], env={"NO_COLOR": ""})
    try:
        v.wait(b"deploy it")
        v.pump(0.5)
        text = screen(v, 0)
        check("Chapters: the tab counts the prompts shown by default (2)",
              "Chapters 2" in text, text)
        check("Chapters: auto chapters are hidden",
              "fix the login bug" in text and "deploy it" in text and
              "Build finished" not in text and "task-notification" not in
              text and "Lint" not in text, text)
        check("Chapters: the footer shows n and how many are hidden",
              "n auto rows 3 hidden" in text, text)
        check("Chapters: o is offered only for a live session (none here)",
              "open session" not in text, text)
        v.key(b"k", 0.3)  # the newest is selected; up to the first prompt
        mark = len(v.out)
        v.key(b"n", 1.0)
        text = screen(v, mark)
        check("Chapters: n shows them, titled by their summary",
              "Build finished: 3 passed" in text and
              "Lint ?]0;pwned? evil?done" in text, text)
        check("Chapters: an auto chapter with no summary keeps its title",
              bare[:30] in text, (bare, text))
        check("Chapters: shown, an auto chapter is faint, a prompt is not",
              re.search(rb"\033\[0;2m[^\033]*Build finished: 3 passed",
                        v.out[mark:]) and
              re.search(rb"\033\[0m[^\033]*deploy it", v.out[mark:]),
              v.out[mark:][-800:])
        text = screen(v, 0)  # the rows n left as they were too
        check("Chapters: n shown, the footer says n hides them, the tab "
              "count stays 2", "n hide auto rows" in text and
              "hidden" not in text and "Chapters 2" in text, text)
        sel = [r for r in text.split("\n") if r.startswith("\u258c")]
        check("Chapters: n keeps the selection on its chapter",
              sel and "fix the login bug" in sel[0], (sel, text))
        mark = len(v.out)
        v.key(b"j", 0.3)
        v.key(b"\r", 1.0)
        check("Chapters: Enter on a shown auto chapter reads it",
              v.wait(b"reply 1", start=mark), v.out[mark:])
        v.key(b"\033", 0.5)
        mark = len(v.out)
        v.key(b"n", 1.0)
        v.key(b"/")
        v.key(b"Build", 1.0)
        text = screen(v, mark)
        check("Chapters: the filter matches a summary; only a hidden "
              "chapter matches: said, with the key",
              "Only auto rows: n shows them" in text, text)
        mark = len(v.out)
        v.key(b"\r", 0.5)
        v.key(b"n", 1.0)
        text = screen(v, mark)
        check("Chapters: n shows the one that matched the filter",
              "Build finished: 3 passed" in text and
              "fix the login bug" not in text, text)
        v.key(b"\033", 0.5)
        mark = len(v.out)
        v.key(b"o", 1.0)
        text = screen(v, mark)
        check("Chapters: o on an ended session says so and stays",
              "xv-au has ended: no session to open" in text and
              v.reap() is None, text)
        mark = len(v.out)
        v.key(b"\r", 1.0)
        v.wait(b"reply", start=mark)
        mark = len(v.out)
        v.key(b"o", 1.0)
        text = screen(v, mark)
        check("reader: o on an ended session says so and stays",
              "xv-au has ended: no session to open" in text and
              v.reap() is None, text)
        v.key(b"q", 0.3)
        v.key(b"q")
        check("Chapters: q exits 0", v.exit() == 0, v.out[-80:])
        bad = [b for b in (b"\033]0;pwned", b"\007 evil", b"\xe2\x80\xae")
               if b in v.out]
        check("mutant 11: no summary ESC, BEL or bidi byte in the output",
              not bad, bad)
    finally:
        v.close()
    v = Pty([DCH, "--xray", "xv-aa"])
    try:
        v.wait(b"Chapters")
        v.pump(0.8)
        text = screen(v, 0)
        check("Chapters: every chapter auto: the empty state says so and "
              "names n; the tab count is 0", "Only auto rows: n shows "
              "them" in text and "Chapters 0" in text and
              "n auto rows 1 hidden" in text, text)
        mark = len(v.out)
        v.key(b"n", 1.0)
        text = screen(v, mark)
        check("Chapters: n shows it, selected", any(
            r.startswith("\u258c") and "Only a notice" in r
            for r in text.split("\n")), text)
        v.key(b"q")
        v.exit()
    finally:
        v.close()


def test_compact():
    o = json.loads(dch("--xray", "xv-ac", "--json").stdout)
    ch = o["chapters"]
    check("compact fixture: the engine reads one compaction, not auto",
          [c["kind"] for c in ch] == ["prompt", "compact", "prompt", "prompt"] and
          [c.get("auto", False) for c in ch] == [False, False, True, False],
          ch)
    v = Pty([DCH, "--xray", "xv-ac"], env={"NO_COLOR": ""})
    try:
        v.wait(b"finish the work")
        v.pump(0.5)
        text = screen(v, 0)
        check("Chapters: a compaction is hidden by default, the tab count "
              "the prompts (2)", "start the work" in text and
              "Conversation compacted" not in text and "Chapters 2" in text,
              text)
        check("Chapters: the footer counts a compaction with the auto rows",
              "n auto rows 2 hidden" in text, text)
        mark = len(v.out)
        v.key(b"n", 1.0)
        text = screen(v, mark)
        check("Chapters: n shows the compaction, its = marker, faint",
              "= Conversation compacted" in text and
              re.search(rb"\033\[0;2m[^\033]*Conversation compacted",
                        v.out[mark:]), v.out[mark:][-800:])
        v.key(b"k", 0.3)  # the newest is selected; up past the notification
        v.key(b"k", 0.3)
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("Chapters: Enter on a shown compaction reads its summary",
              v.wait(b"the compact summary", start=mark), v.out[mark:])
        v.key(b"\033", 0.5)
        v.key(b"q")
        check("Chapters: q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()
    v = Pty([DCH, "--xray", "xv-ca"])
    try:
        v.wait(b"Chapters")
        v.pump(0.8)
        text = screen(v, 0)
        check("Chapters: only a compaction and an auto chapter: the empty "
              "state of xv-aa", "Only auto rows: n shows them" in text
              and "Chapters 0" in text and "n auto rows 2 hidden" in text,
              text)
        v.key(b"q")
        v.exit()
    finally:
        v.close()


def clients(name):
    for r in json.loads(dch("--ls-json").stdout):
        if r["name"] == name:
            return r.get("clients")
    return None


# what q leaves the view with (picker_restore), byte for byte: the attach
# comes after it
LEAVE = (b"\033[?25h\033[?1000l\033[?1002l\033[?1003l\033[?1006l\033[?1015l"
         b"\033[?2004l\033[?7h\033[<u\033[0m\033[?1049l")
DTAP = {"DCH_DOUBLE_TAP_MS": "0"}  # one Ctrl-\ detaches at once


def opened(v, mark, what, name="xv-sh"):
    """o pressed at mark: the view left as q leaves it, then attached to
    name; one Ctrl-\\ detaches."""
    ok = until(lambda: (v.pump(0.1), clients(name) == 1)[1], 10)
    i = v.out.find(LEAVE, mark)
    check("%s: o leaves the view as q does (terminal restored), then "
          "attaches to the session" % what, ok and i >= 0, v.out[mark:][-400:])
    v.key(b"\x1c", 0.5)
    rc = v.exit(5)
    check("%s: the attach is a real one: the detach key ends it" % what,
          rc == 0 and until(lambda: clients(name) == 0, 5), rc)


def test_open(lite):
    # one typed shell block, scrolled into the ring: a chapter (full build)
    prog = ("printf '\\033]133;A\\007$ \\033]133;B\\007'; read cmd; "
            "printf '\\033]133;C\\007'; echo \"ran:$cmd\"; "
            "printf '\\033]133;D;0\\007'; seq 1 40; "
            "printf '\\033]133;A\\007$ '; exec sleep 600")
    r = dch("--spawn", "xv-op", "--size", "80x24", "sh", "-c", prog)
    check("open: spawned", r.returncode == 0, r.stderr)
    until(lambda: "$" in dch("--read", "xv-op").stdout, 5)
    dch("--send", "xv-op", "make the build\n")
    until(lambda: "40" in dch("--read", "xv-op").stdout, 5)
    time.sleep(0.5)  # dch-lite: no --read; the block's time
    v = Pty([DCH, "--xray", "xv-op"], env=DTAP)
    try:
        v.wait(b"Chapters")
        v.pump(0.5)
        text = screen(v, 0)
        check("Chapters: a live session's footer offers o", "o open session"
              in text, text)
        mark = len(v.out)
        v.key(b"o", 0.3)
        opened(v, mark, "Chapters", "xv-op")
    finally:
        v.close()
    if lite:
        print("  ok   lite: a shell has no chapters to read (no corpus): o "
              "in the reader is the full build's")
    else:
        v = Pty([DCH, "--xray", "xv-op"], env=DTAP)
        try:
            v.wait(b"make the build")
            v.pump(0.3)
            mark = len(v.out)
            v.key(b"\r", 1.0)
            v.wait(b"ran:make the build", start=mark)
            v.pump(0.3)
            text = screen(v, mark)
            check("reader: a live session's footer offers o",
                  "o open session" in text and "ran:make the build" in text,
                  text)
            mark = len(v.out)
            v.key(b"o", 0.3)
            opened(v, mark, "reader", "xv-op")
        finally:
            v.close()
    dch("-k", "xv-op")  # the pickers below take their first row: xv-sh
    v = Pty([DCH, "-l"], env=DTAP)
    try:
        v.wait(b"x-ray")
        v.key(b"x", 1.0)
        v.wait(b"X-ray  xv-sh")
        mark = len(v.out)
        v.key(b"o", 0.3)
        opened(v, mark, "picker's x")
    finally:
        v.close()
    v = Pty([DCH, "-k"], env=DTAP)
    try:
        v.wait(b"x-ray")
        v.key(b"x", 1.0)
        v.wait(b"X-ray  xv-sh")
        mark = len(v.out)
        v.key(b"o", 1.0)
        check("-k picker: the view's o attaches nothing and kills nothing, "
              "said", v.wait(b"o opens a session: from dch -l or the switch",
                             start=mark) and clients("xv-sh") == 0 and
              v.reap() is None, v.out[mark:])
        v.key(b"\033")
        v.exit()
    finally:
        v.close()
    check("-k picker: xv-sh still runs", clients("xv-sh") == 0, clients("xv-sh"))
    v = Pty([DCH, "--xray", "xv-sh"], env=dict(DTAP, DCH_SESSION="outer"))
    try:
        v.wait(b"Chapters")
        mark = len(v.out)
        v.key(b"o", 1.0)
        check("o inside a dch session: refused as -a refuses it, the view "
              "stays", v.wait(b"already inside session outer; nesting "
                              b"disabled", start=mark) and
              v.reap() is None and clients("xv-sh") == 0, v.out[mark:])
        v.key(b"q")
        v.exit()
    finally:
        v.close()


def test_screen_count(lite):
    """A live shell with nothing on its screen to label: no number on On
    screen until it is read, then 0; no refs: the empty state."""
    r = dch("--spawn", "xv-0", "--size", "80x24", "sh", "-c",
            "echo READY0; exec sleep 600")
    check("screen count: spawned", r.returncode == 0, r.stderr)
    time.sleep(0.5)
    v = Pty([DCH, "--xray", "xv-0"])
    try:
        v.wait(b"Chapters")
        v.pump(0.5)
        check("On screen: no number before it is opened",
              b"On screen" in v.out and
              not re.search(rb"On screen \d", v.out), v.out[-600:])
        mark = len(v.out)
        v.key(b"\t", 1.0)
        check("Refs: no refs, the empty state says what would show",
              v.wait(b"No refs yet: the files, commits, pull requests, "
                     b"issues and URLs this session touches show here",
                     start=mark), v.out[mark:])
        mark = len(v.out)
        v.key(b"\t", 1.5)
        if lite:
            check("lite: On screen says no mirror and shows no number",
                  v.wait(b"No screen: dch-lite keeps no mirror", start=mark)
                  and not re.search(rb"On screen \d", v.out), v.out[mark:])
        else:
            check("On screen: read, its count shows, 0 included",
                  v.wait(b"On screen 0", start=mark) and
                  v.wait(b"READY0", start=mark), v.out[mark:])
        v.key(b"\003")
        check("screen count: Ctrl-C quits with 0", v.exit() == 0,
              v.out[-80:])
    finally:
        v.close()
        dch("-k", "xv-0")  # the picker test's first row is xv-sh


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


def test_small_card():
    """QA 1.56.0 MED-V1: k, Up and PgUp in a too-small window took the
    card's selection below 0 and Enter read v->m[-1] (SIGSEGV)."""
    v = Pty([DCH, "--xray", "xv-cl"])
    try:
        v.wait(b"Chapters 2")
        v.key(b"\t")
        v.wait(b"#831")
        mark = len(v.out)
        v.key(b"\r")
        check("small card: #831's card opens with its mentions",
              v.wait(b"Mentions: this session ", start=mark), v.out[mark:])
        mark = len(v.out)
        v.resize(6, 20)
        check("small card: 20 columns says it whole (NIT-V3)",
              v.wait(b"X-ray: too small", start=mark) and
              b"the window is" not in v.out[mark:], v.out[mark:])
        for k in (b"k", b"k", b"k", b"\033[A", b"\033[5~", b"\r"):
            v.key(k, 0.1)
        mark = len(v.out)
        v.resize(30, 100)
        v.wait(b"You", start=mark)
        check("small card: k, Up, PgUp then Enter: no crash, Enter reads "
              "the first mention's chapter", v.reap() is None and
              b"You" in v.out[mark:],
              (v.rc, v.out[mark:][-600:]))
        if v.reap() is not None:
            return
        for cols, want in ((12, b"too small"), (5, b"too")):
            mark = len(v.out)
            v.resize(6, cols)
            v.wait(want, start=mark)
            got = [r for r in rows(v.out[mark:]).values() if r.strip()]
            check("too small at %d columns: one row, within the width"
                  % cols, len(got) == 1 and cells(got[0]) <= cols and
                  want.decode() in got[0], got)
        v.key(b"qqq")
        check("small card: q, q, q exits 0", v.exit() == 0, v.out[-80:])
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
        check("picker: the x hint is shown",
              v.wait(b"x-ray"), v.out[-600:])
        mark = len(v.out)
        v.key(b"x", 1.0)
        check("picker: x opens the view of the row's session",
              v.wait(b"X-ray  xv-sh", start=mark) and
              v.wait(b"Chapters", start=mark), v.out[mark:])
        mark = len(v.out)
        v.key(b"q", 1.0)
        check("picker: q in the view returns to the picker",
              v.reap() is None and v.wait(b"Sessions", start=mark),
              v.out[mark:])
        mark = len(v.out)
        v.key(b"X", 1.0)
        check("picker: X asks to kill and never opens the view (OQ-6)",
              v.wait(b"Kill xv-sh? [y/N]", start=mark) and
              b"X-ray  xv-sh" not in v.out[mark:], v.out[mark:])
        mark = len(v.out)
        v.key(b"n", 1.0)
        check("picker: n keeps the session",
              v.wait(b"cancelled", start=mark) and clients("xv-sh") == 0,
              v.out[mark:])
        v.key(b"\033")
        check("picker: Esc quits it", v.exit() is not None, v.out[-80:])
    finally:
        v.close()


# ---- the in-attach chord (#187) -------------------------------------------

# The session prints each read it gets as RX:<hex> and appends the hex to a
# file, so a test sees exactly the bytes that reached it (dch-lite too, where
# --read has no mirror).
RXF = os.path.join(T, "rx.hex")
RX = ("import os, sys, tty\n"
      "tty.setraw(0)\n"
      "f = open(sys.argv[1], 'a')\n"
      "os.write(1, b'READY\\r\\n')\n"
      "while True:\n"
      "    b = os.read(0, 256)\n"
      "    if not b: break\n"
      "    f.write(b.hex()); f.flush()\n"
      "    os.write(1, b'RX:' + b.hex().encode() + b'\\r\\n')\n")


def rx():
    """Every byte the session has received, in order, as hex."""
    try:
        with open(RXF) as f:
            return f.read()
    except OSError:
        return ""


def attach(env=None, extra=()):
    a = Pty([DCH] + list(extra) + ["-a", "xv-rx"], env=env)
    a.wait(b"READY")
    a.pump(0.3)
    return a


def chord_case(what, keys, lite):
    a = attach()
    try:
        got = rx()
        mark = len(a.out)
        for k in keys:
            a.key(k, 0.05)
        ok = a.wait(b"X-ray  xv-rx", start=mark) and \
            a.wait(b"On screen", start=mark)
        check("chord, %s: the view opens over the attach" % what, ok,
              a.out[mark:][-600:])
        check("chord, %s: the client is still running" % what,
              a.reap() is None)
        dch("--send", "xv-rx", "MARK187")
        until(lambda: "4d41524b313837" in rx()[len(got):], 5)
        a.pump(0.3)
        check("chord, %s: the session got only the text sent while the "
              "view was up (no detach key, no x)" % what,
              rx()[len(got):] == "4d41524b313837", rx()[len(got):])
        if not ok or a.reap() is not None:
            return
        mark = len(a.out)
        a.key(b"q", 0.5)
        check("chord, %s: q leaves the view's alternate screen" % what,
              a.wait(RESTORE, start=mark), a.out[mark:][-300:])
        if not lite:
            # On screen may draw the line too: only what follows the
            # view's exit counts
            back = a.out.rfind(RESTORE)
            check("chord, %s: back on the attach, the screen repainted "
                  "with what came in meanwhile" % what,
                  a.wait(b"RX:4d41524b313837", start=back),
                  a.out[back:])
        before = rx()
        a.key(b"z", 0.3)
        check("chord, %s: the attach was never retired (a key after "
              "reaches the session)" % what,
              until(lambda: rx()[len(before):] == "7a", 5) and
              a.reap() is None, (rx()[len(before):], a.rc))
    finally:
        a.close()


def test_chord():
    o = json.loads(dch("--xray", "xv-sh", "--json").stdout)
    lite = o["corpus"] == "none" and o["reason"] == "lite"
    r = dch("--spawn", "xv-rx", "--size", "100x30", "python3", "-c", RX,
            RXF)
    check("chord: the probe session spawned", r.returncode == 0, r.stderr)
    chord_case("x in a later read", [b"\x1c", b"x"], lite)
    chord_case("x in the same burst", [b"\x1cx"], lite)
    chord_case("the kitty CSI form", [b"\x1c", b"\033[120u"], lite)
    chord_case("all kitty, one burst, the release between",
               [b"\033[92;5u\033[92;5:3u\033[120;1:1u"], lite)
    if lite:
        print("  ok   lite: the repaint after the view is the master's "
              "SIGWINCH (no mirror), the screen check is the full build's")

    # o on Chapters (#194): the client is on the session already; o goes
    # back to it, the attach kept
    a = attach()
    try:
        got = rx()
        a.key(b"\x1cx", 0.5)
        a.wait(b"X-ray  xv-rx")
        a.key(b"\t", 0.5)
        mark = len(a.out)
        a.key(b"o", 0.5)
        check("chord, o: the view closes, back to the session",
              a.wait(RESTORE, start=mark) and a.reap() is None,
              a.out[mark:][-300:])
        a.key(b"z", 0.3)
        check("chord, o: the attach was kept, neither Tab nor o reached "
              "the session, a key after does",
              until(lambda: rx()[len(got):] == "7a", 5) and a.reap() is None,
              (rx()[len(got):], a.rc))
    finally:
        a.close()

    # -E: no detach key, no chord; the session gets both bytes
    a = attach(extra=["-E"])
    try:
        got = rx()
        mark = len(a.out)
        a.key(b"\x1c", 0.05)
        a.key(b"x", 0.5)
        until(lambda: rx()[len(got):] == "1c78", 5)
        check("-E: the session receives the detach key and x, no view",
              rx()[len(got):] == "1c78" and b"X-ray" not in a.out[mark:]
              and a.reap() is None, (rx()[len(got):], a.out[mark:]))
    finally:
        a.close()

    # DCH_DOUBLE_TAP_MS=0: the press detaches at once, x after it or in
    # its burst never reaches the session, no view
    for keys in ([b"\x1c", b"x"], [b"\x1cx"]):
        a = attach(env={"DCH_DOUBLE_TAP_MS": "0"})
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
            check("DCH_DOUBLE_TAP_MS=0, %s: the detach key detaches at once "
                  "(exit 0), the session receives nothing, no view" %
                  ("x after" if len(keys) > 1 else "one burst"),
                  rc == 0 and rx()[len(got):] == "" and
                  b"X-ray" not in a.out[mark:],
                  (rc, rx()[len(got):], a.out[mark:]))
        finally:
            a.close()

    # detach_key = switch: one press opens the picker after the window; a
    # late x there is the picker's view (OQ-6), q comes back to it, X is
    # kill, which asks first; n keeps the session
    cfg = os.path.join(T, "config-switch")
    with open(cfg, "w") as f:
        f.write("detach_key = switch\n")
    a = attach(env={"DCH_CONFIG": cfg})
    try:
        got = rx()
        a.key(b"\x1c", 0.05)
        check("detach_key = switch: one press opens the picker",
              a.wait(b"switch to session:"), a.out[-600:])
        a.pump(0.5)
        mark = len(a.out)
        a.key(b"x", 0.5)
        check("detach_key = switch: a late x opens the picker's view",
              a.wait(b"X-ray  xv-rx", start=mark), a.out[mark:][-600:])
        mark = len(a.out)
        a.key(b"q", 0.5)
        check("detach_key = switch: q in that view returns to the picker",
              a.wait(b"switch to session:", start=mark), a.out[mark:][-600:])
        mark = len(a.out)
        a.key(b"X", 0.5)
        check("detach_key = switch: X reaches the kill confirmation",
              a.wait(b"Kill xv-rx? [y/N]", start=mark), a.out[mark:])
        a.key(b"n", 0.5)
        a.key(b"\033", 0.5)
        a.exit(5)
        late = rx()[len(got):]
        dch("--send", "xv-rx", "alive")
        check("detach_key = switch: the late x never ran -k (the session "
              "lives, x never reached it)", late == "" and
              until(lambda: rx()[len(got):] == "616c697665", 5),
              (late, rx()[len(got):]))
    finally:
        a.close()


def test_chord_end():
    """The session ends while the view is up: leaving the view ends the
    attach as a session's end does (exit 0, no frame to a closed master).
    `dch -k` while the view is up: the client ends and takes the view with
    it, no orphan left on the terminal (the client is not the pty's session
    leader here, so no hangup does it for us)."""
    r = dch("--spawn", "xv-end", "--size", "100x30", "sh", "-c",
            "echo READY; read x; echo BYE")
    check("chord, end: spawned", r.returncode == 0, r.stderr)
    a = Pty([DCH, "-a", "xv-end"])
    try:
        a.wait(b"READY")
        mark = len(a.out)
        a.key(b"\x1cx", 0.5)
        check("chord, end: the view opens", a.wait(b"X-ray  xv-end",
              start=mark), a.out[mark:][-300:])
        dch("--send", "xv-end", "go\n")
        a.pump(1.0)
        check("chord, end: the client waits for the view",
              a.reap() is None, a.out[-200:])
        a.key(b"q", 0.3)
        rc = a.exit(5)
        check("chord, end: the session ended under the view; leaving it "
              "exits 0", rc == 0, (rc, a.out[-200:]))
    finally:
        a.close()
    r = dch("--spawn", "xv-end", "--size", "100x30", "sh", "-c",
            "echo READY; exec sleep 600")
    a = Pty(["/bin/sh", "-c", "'%s' -a xv-end; echo CLIENT-GONE; "
             "exec sleep 30" % DCH])
    try:
        a.wait(b"READY")
        mark = len(a.out)
        a.key(b"\x1cx", 0.5)
        a.wait(b"X-ray  xv-end", start=mark)
        client = kids(a.pid)
        view = [k for c in client for k in kids(c)]
        dch("-k", "xv-end")
        check("chord, -k under the view: the client ends",
              a.wait(b"CLIENT-GONE", start=mark), a.out[mark:][-300:])
        check("chord, -k under the view: the view went with it",
              view and until(lambda: not any(alive(k) for k in view), 5),
              view)
    finally:
        a.close()


def test_chord_detach():
    """#196. dch -d (and SIGTERM, SIGHUP) with the chord's view up: the
    client leaves at once as a plain detach does (exit 0, the terminal's
    stty as before the attach, the reset sequence last, the client pidfile
    gone, the view gone). With kitty event types on, the detach key's
    release in a read of its own neither ends the window nor reaches the
    session: press, release, x opens the view; press, release, press opens
    the switch picker. What follows the x in its read reaches neither the
    view nor the session (a q there leaves the view up)."""
    import re
    reset = b"\033[?1049l\033[<8u"
    for how in ("-d", signal.SIGTERM, signal.SIGHUP):
        what = how if how == "-d" else signal.Signals(how).name
        a = Pty(["/bin/sh", "-c", "stty -g; '%s' -a xv-rx; echo RC=$?; "
                 "stty -g; echo GONE; exec sleep 30" % DCH])
        try:
            a.wait(b"READY")
            a.pump(0.3)
            before = a.out.split(b"\r\n")[0]
            got = rx()
            mark = len(a.out)
            a.key(b"\x1c", 0.05)
            a.key(b"x", 0.3)
            ok = a.wait(b"X-ray  xv-rx", start=mark)
            check("chord, %s: the view opens" % what, ok,
                  a.out[mark:][-300:])
            client = kids(a.pid)
            view = [k for c in client for k in kids(c)]
            pidf = [os.path.join(d, f) for d, _, fs in os.walk(T)
                    for f in fs for c in client
                    if f.endswith(".client.%d" % c)]
            mark = len(a.out)
            t0 = time.time()
            if how == "-d":
                r = dch("-d", "xv-rx")
                check("chord, -d: dch -d says it detached one client",
                      r.returncode == 0 and "detached 1 client" in r.stdout,
                      (r.returncode, r.stdout, r.stderr))
            else:
                for c in client:
                    os.kill(c, how)
            gone = a.wait(b"GONE", secs=5, start=mark)
            tail = a.out[mark:]
            check("chord, %s: the client leaves within 5 s, the view still "
                  "up (%.1f s)" % (what, time.time() - t0), gone,
                  tail[-300:])
            if not gone:
                continue
            lines = tail.split(b"RC=")[-1].split(b"\r\n")
            # PENDIN (macOS 0x20000000): the kernel sets it on any switch
            # back to canonical mode, until the next read
            pend = lambda g: re.sub(
                rb"lflag=([0-9a-f]+)", lambda m: b"lflag=%x" %
                (int(m.group(1), 16) & ~0x20000000), g)
            check("chord, %s: exit 0, the terminal's stty as before the "
                  "attach" % what, lines[0] == b"0" and
                  pend(lines[1]) == pend(before), (lines[:2], before))
            if how != signal.SIGHUP:
                check("chord, %s: the reset sequence comes last" % what,
                      tail.split(b"RC=")[0].endswith(reset),
                      tail.split(b"RC=")[0][-120:])
            check("chord, %s: the client pidfile and the view are gone" %
                  what, pidf and not any(os.path.exists(f) for f in pidf)
                  and view and until(lambda: not any(alive(k)
                                                    for k in view), 5),
                  (pidf, view))
            check("chord, %s: the session got nothing" % what,
                  rx()[len(got):] == "", rx()[len(got):])
        finally:
            a.close()

    # kitty event types on: each report in a read of its own
    for keys, want, what in (
            ([b"\033[92;5u", b"\033[92;5:3u", b"\033[120u"],
             b"X-ray  xv-rx", "press, release, x"),
            ([b"\033[92;5u", b"\033[92;5:3u", b"\033[92;5u"],
             b"switch to session:", "press, release, press")):
        a = attach(env={"DCH_DOUBLE_TAP_MS": "2000"})
        try:
            got = rx()
            mark = len(a.out)
            for k in keys:
                try:
                    a.key(k, 0.15)
                except OSError:	# the client left: the pty is gone
                    break
            check("kitty events, %s, three reads: %s opens, the client "
                  "stays" % (what, want.decode()),
                  a.wait(want, start=mark) and a.reap() is None,
                  (a.rc, a.out[mark:][-300:]))
            check("kitty events, %s: the session got nothing" % what,
                  rx()[len(got):] == "", rx()[len(got):])
        finally:
            a.close()

    # the rest of the x's read is dropped: a q in it never closes the view
    a = attach(env={"DCH_DOUBLE_TAP_MS": "2000"})
    try:
        got = rx()
        mark = len(a.out)
        a.key(b"\x1c", 0.05)
        a.key(b"xq", 0.3)
        ok = a.wait(b"X-ray  xv-rx", start=mark)
        a.pump(1.0)
        # the overlay's own reset leaves the alternate screen first
        up = a.out.find(b"X-ray  xv-rx", mark)
        check("chord, x then q in one read: the view opens and stays up, "
              "the session got nothing", ok and
              RESTORE not in a.out[up:] and a.reap() is None and
              rx()[len(got):] == "", (a.out[mark:][-200:], rx()[len(got):]))
        mark = len(a.out)
        a.key(b"q", 0.5)
        check("chord, x then q in one read: a q of its own closes it",
              a.wait(RESTORE, start=mark), a.out[mark:][-200:])
    finally:
        a.close()


def main():
    try:
        fixture()
        test_refusals()
        test_view()
        fixture_refs()
        test_refs()
        fixture_auto()
        test_auto()
        test_compact()
        test_resize()
        test_small_card()
        shell()
        test_signals()
        test_on_screen()
        o = json.loads(dch("--xray", "xv-sh", "--json").stdout)
        test_screen_count(o["corpus"] == "none" and o["reason"] == "lite")
        test_picker()
        test_open(o["corpus"] == "none" and o["reason"] == "lite")
        test_chord()
        test_chord_end()
        test_chord_detach()
    finally:
        dch("-k", "xv-rx")
        dch("-k", "xv-end")
        dch("-k", "xv-sh")
        dch("-k", "xv-0")
        shutil.rmtree(T, ignore_errors=True)
    if fails[0]:
        print("%d failed" % fails[0])
        sys.exit(1)


if __name__ == "__main__":
    main()
