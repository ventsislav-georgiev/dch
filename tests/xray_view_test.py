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
  3. the tabs (no On screen, #294; Prompts for a Claude session,
     Commands for a shell's, never Chapters, #295; Shift-Tab from Prompts
     to Refs, #290), the chapter list, the Refs list,
     the card (light, `f` full scan, `y` OSC 52 copy, `p` other projects
     and back, `o` on the ended session said), the chapter reader, back,
     quit (exit 0, terminal restored);
  4. mutant 11: no corpus ESC, BEL or bidi byte (U+202E, U+061C, U+2028)
     reaches the terminal, the strings show with `?` in their place;
  5. nothing written: no file under the test root changes;
  6. a resize to a narrow window says so, a resize back redraws; the
     message fits down to 5 columns, and k, Up, PgUp and Enter in a card
     under a too-small window neither crash nor read outside the mentions
     (QA 1.56.0 MED-V1, NIT-V3);
  7. SIGTERM and SIGINT: the terminal is restored and the view's children
     are gone;
  8. Refs on a live shell (#294): the refs on its screen first, under a
     faint "on screen now" line, the lowest row's first (o/r#77, then
     o/r#41, one printed once and not underlined, #262), neither repeated
     below; Enter on the first opens its card (full build); on dch-lite
     the label line says X-ray needs the full build and there is no group;
  9. the picker: the x hint is shown, x on a session row opens the view, q
     returns to the picker; X asks to kill and never opens the view (OQ-6);
 10. the Refs tab (#193): a file under the project shown relative to it,
     one under HOME from ~, one too long cut at the front (its tail kept,
     a wide-character one within the width), a long URL keeping its host
     and its end; rows by latest mention (#262), then count, then the most
     recent first; pulls, issues and tickets only until a shows files,
     commits and URLs, the footer counting them; files under
     /tmp, /private/tmp, /var/folders and $TMPDIR hidden, t toggling them,
     the footer saying how many, the tab count the engine's; the filter
     matching the full key (and taking t as text), Enter opening the full
     key's card, f there showing the resolver's line (QA 1.56.0 NIT-V2);
     the faint line saying what a ref is, the empty state when
     there are none; an ended session: no group line (#294). A live shell
     whose master keeps no ring (the index holds nothing): no group line
     while its screen has no ref; once o/r#5 and a URL are on it, the group
     lists o/r#5 with no count, a hides the URL and the footer counts it,
     Enter opens o/r#5's card
     (dch-lite: no mirror, never a group);
 11. the Prompts tab (#194; Chapters before #295): a chapter the harness wrote ("auto") hidden,
     n showing it faint and titled by its summary (its title without one),
     the footer saying how many, the tab count the shown-by-default ones,
     the selection kept on its chapter, the filter matching a summary, the
     empty state when every chapter is one; a compaction (#223) hidden
     and counted with them, n showing it faint with its = marker, Enter
     reading its summary, compactions and auto chapters alone the same
     empty state; o on the Prompts (a shell's Commands) list, in
     the reader and on a card (full build; #227) leaves the view (the terminal restored as q does) and
     attaches as dch -a, from the picker's x too (the -k picker refuses);
     an ended session's o says so and stays; in the chord o goes back to the
     session;
 12. the in-attach chord (#187, K10): the detach key then x in a later read,
     in one burst, and in the kitty CSI form opens the view over the attach
     on Refs (#294); the x never reaches the session, the attach is never
     retired (a key typed after reaches the session), and output written
     while the view was up is on screen after it closes (full build: the
     mirror's repaint); off under -E and with DCH_DOUBLE_TAP_MS=0, where the
     session receives what it did before; under detach_key = switch a late
     x is the picker's (OQ-6): it opens the row's view, q returns to the
     picker, X reaches the kill confirmation, and nothing kills.
 13. the lookup (#263): a ref typed in the filter that no row has (a
     ticket, a full commit hash whose row is held short), whose row is
     hidden (a file), or that a chapter title holds (not the wanted row):
     the footer names ^R, the empty line says ^R looks it up, ^R opens its
     card (outer blanks cut), Enter reads its mention's chapter, Esc goes
     back with the filter kept; plain words: one faint line says ^R takes
     refs, not words, and ^R says so with no card.
 14. Responses (#290): xv-rs, a History Claude record: Tab from Prompts
     opens it, the tabs Prompts, Responses, Refs in that order with their
     counts; newest first, the newest selected; a turn with tools and a
     2-line answer: its tool line then the lines; a 12-line answer: 8 lines
     then an ellipsis; an answer over 2 KB with ESC, BEL and U+202E: drawn
     with ? (none reach the terminal), and at 300 columns, where its 2048
     bytes fit in 8 lines, the ellipsis from response_cut alone; a turn
     that ended on a tool call: its tool line, no working line; the auto
     row hidden until n, the selection kept; j, Enter reads that prompt at
     its Reply (the first row in a window shorter than the chapter), q back
     to the item; / matches answer text; g, G; Tab on to Refs, Tab wraps to
     Prompts, Shift-Tab back to Refs, h to Responses. xv-lv, a live Claude
     (a sleep with a session record): the running turn says working under
     its tool line, its answer replaces it when --follow re-sends the
     chapter, a new turn on top keeps a selection on its prompt and one on
     the first item on the first. xv-cx, a live fake Codex over the
     codex-0167 rollout: its answer and tool line, its last turn working.
     A shell (xv-0) has no Responses tab.

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
        ok = v.wait(b"Prompts 2") and v.wait(b"Refs 3")
        check("open: the tabs carry the index's counts", ok, v.out[-600:])
        check("#295: a Claude session's first tab is Prompts, the word "
              "Chapters gone", ok and b"Chapters" not in v.out, v.out[-600:])
        check("open: the chapter list shows both titles",
              v.wait(b"second prompt about PROJ-5") and
              v.wait(b"look at #831"), v.out[-600:])
        check("mutant 11: the chapter title's OSC, BEL, U+202E and U+061C "
              "show as ?", v.wait(b"o/r#7 ?]0;pwned? evil?title al?m"),
              v.out[-600:])
        mark = len(v.out)
        v.key(b"\033[Z", 1.0)  # Shift-Tab: Prompts back to Refs (#290)
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        at = [text.find(x) for x in ("PROJ-5", "#831", "o/r#7")]
        check("Refs: the latest mentioned first (#262)",
              -1 not in at and at == sorted(at), (at, text))
        v.key(b"\033[B", 0.3)
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
        # #227: p is the scope toggle (o until 1.60.x); o opens, as in the list
        mark = len(v.out)
        v.key(b"p", 1.0)
        ok = until(lambda: (v.pump(0.1), re.search(
            r"full scan (\u00b7|-) other projects", screen(v, mark)) and
            "p this project" in screen(v, mark))[1], 5)
        check("card: p scans other projects, its key then says this project",
              ok, screen(v, mark))
        mark = len(v.out)
        v.key(b"p", 1.0)
        ok = until(lambda: (v.pump(0.1), "p other projects" in
                            screen(v, mark))[1], 5)
        text = screen(v, mark)
        check("card: p again scans this project only",
              ok and "full scan" in text and not re.search(
                  r"full scan (\u00b7|-) other projects", text), text[:800])
        mark = len(v.out)
        v.key(b"o", 1.0)
        text = screen(v, mark)
        check("card: o on the ended xv-cl says so and stays on the card",
              "xv-cl has ended: no session to open" in text and
              v.reap() is None, text)
        mark = len(v.out)
        v.key(b"\033", 0.5)
        v.key(b"\t")  # Refs, then Prompts: Tab wraps (#290)
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("reader: Enter opens the selected chapter (the newest)",
              v.wait(b"You", start=mark) and v.wait(b"noted", start=mark),
              v.out[mark:])
        mark = len(v.out)
        v.key(b"q")
        check("reader: q goes back to the list",
              v.wait(b"Prompts 2", start=mark), v.out[mark:])
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
        v.wait(b"Prompts 1")
        v.pump(0.5)
        check("#294: two tabs, Prompts and Refs, no On screen",
              b"Refs 13" in v.out and b"On screen" not in v.out,
              v.out[-600:])
        mark = len(v.out)
        v.key(b"\033[Z", 1.0)
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        check("Refs: the tab count stays the engine's total (13)",
              "Refs 13" in text, text)
        check("Refs #294: an ended session has no screen, no group line",
              "on screen now" not in text and "the rest" not in text, text)
        check("Refs #262: opens on pulls, issues and tickets only; the "
              "footer names a and counts the other kinds",
              "o/r#7" in text and "alpha_handler" not in text and
              "example.com" not in text and
              "a files, commits, URLs 12 hidden" in text, text)
        mark = len(v.out)
        v.key(b"a", 1.0)
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        check("Refs: one faint line says what a ref is and that Enter "
              "opens its card", re.search("Every ref this session touched, "
                                          "latest first; (\u21b5|enter) "
                                          "opens a card", text), text)
        check("Refs #262: a shows the other kinds, the footer says a hides "
              "them", "alpha_handler" in text and
              "a hide files, commits, URLs" in text, text)
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
        check("Refs: one line, so one latest mention: by count, then the most "
              "recent first (later in the snapshot)", -1 not in at and at == sorted(at), (at, text))
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


# ---- Refs: the latest mentioned first, the other kinds behind a (#262) ------

LSID = "11111111-2222-4333-8444-5555555555f4"


def fixture_latest():
    """o/r#1 said three times first, then PROJ-9, then #77 and a commit and a
    file last: the latest mentioned on top, not the most counted."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    said = ["o/r#1 and o/r#1 and o/r#1 in %s/old.c" % P, "now PROJ-9",
            "last #77, commit deadbeef1 in %s/new.c" % P]
    lines = [{"type": "user", "cwd": P, "timestamp": "2026-11-10T10:00:00Z",
              "message": {"content": "the latest fixture"}}]
    lines += [{"type": "assistant", "message": {"content": [
        {"type": "text", "text": t}]}} for t in said]
    with open(os.path.join(pdir, LSID + ".jsonl"), "w") as f:
        for o in lines:
            f.write(json.dumps(o) + "\n")
    sockdir = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    rec = {"name": "xv-lt", "cwd": P, "end": "exited", "argv": ["claude"],
           "started": 1794299400, "updated": 1794300000,
           "ended": 1794300000, "sock_dir": sockdir,
           "agent": {"session_id": LSID, "cwd": P, "config_dir": cfg}}
    with open(os.path.join(hdir, "xv-lt.json"), "w") as f:
        f.write(json.dumps(rec) + "\n")


def test_latest():
    v = Pty([DCH, "--xray", "xv-lt"], env=RENV)
    try:
        v.wait(b"Prompts 1")
        v.pump(0.5)
        mark = len(v.out)
        v.key(b"\033[Z", 1.0)
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        at = [text.find(x) for x in ("#77", "PROJ-9", "o/r#1")]
        check("Refs #262: the latest mentioned on top, not the most counted",
              -1 not in at and at == sorted(at), (at, text))
        check("Refs #262: commits and files hidden, the footer counts them",
              "deadbee" not in text and "old.c" not in text and
              "a files, commits, URLs 3 hidden" in text, text)
        mark = len(v.out)
        v.key(b"a", 1.0)
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        at = [text.find(x) for x in ("new.c", "PROJ-9", "old.c")]
        check("Refs #262: a shows them, in the same latest-first order",
              "deadbee" in text and -1 not in at and at == sorted(at),
              (at, text))
        mark = len(v.out)
        v.key(b"a", 1.0)
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        check("Refs #262: a again hides them",
              "deadbee" not in text and
              "a files, commits, URLs 3 hidden" in text, text)
        v.key(b"q")
        check("Refs #262: q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()


def shown(v, mark):
    scr = rows(v.out[mark:])
    return "\n".join(scr[k] for k in sorted(scr))


def test_lookup():
    """#263: a ref typed in the filter, matched by no row or not by the one
    wanted, opens its card with ^R; plain words say ^R takes refs only."""
    v = Pty([DCH, "--xray", "xv-lt"], env=RENV)
    try:
        v.wait(b"Prompts 1")
        v.pump(0.5)
        mark = len(v.out)
        v.key(b"/PROJ-9", 1.0)
        text = shown(v, mark)
        check("lookup #263: a ref in the Prompts filter, no title has it: "
              "the empty line and the footer name ^R",
              "No match: ^R looks it up" in text and "^R look up" in text,
              text)
        mark = len(v.out)
        v.key(b"\x12", 1.0)
        check("lookup #263: ^R opens the typed ref's card with its mention",
              v.wait(b"PROJ-9  light", start=mark) and
              v.wait(b"now PROJ-9", start=mark), v.out[mark:])
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("lookup #263: Enter on its mention reads the chapter",
              v.wait(b"the latest fixture", start=mark) and
              v.wait(b"now PROJ-9", start=mark), v.out[mark:])
        v.key(b"\033", 0.5)
        mark = len(v.out)
        v.key(b"\033", 1.0)
        text = shown(v, mark)
        check("lookup #263: Esc back to the list, the filter kept",
              "/PROJ-9" in text and "Prompts 1" in text, text)
        v.key(b"/\x15", 0.3)
        mark = len(v.out)
        v.key(b"just words", 1.0)
        text = shown(v, mark)
        check("lookup #263: plain words matching nothing: one faint line "
              "says ^R takes refs, not words; no ^R in the footer",
              "No match; ^R looks up a ticket, issue, pull, commit, file or "
              "URL, not words" in text and "^R look up" not in text, text)
        mark = len(v.out)
        v.key(b"\x12", 1.0)
        check("lookup #263: ^R on words says what it takes, no card",
              v.wait(b"^R looks up the ref typed after /", start=mark) and
              b"light" not in v.out[mark:], v.out[mark:])
        v.key(b"\033", 0.3)
        v.key(b"\033[Z", 1.0)
        mark = len(v.out)
        v.key(b"/ deadbeef1 ", 1.0)
        text = shown(v, mark)
        check("lookup #263: a full commit hash, its row held short: no row, "
              "^R named", "No match: ^R looks it up" in text and
              "^R look up" in text, text)
        mark = len(v.out)
        v.key(b"\x12", 1.0)
        check("lookup #263: ^R opens the hidden commit's card, outer blanks "
              "cut", v.wait(b"deadbeef1  light", start=mark) and
              v.wait(b"commit deadbeef1", start=mark), v.out[mark:])
        v.key(b"\033", 0.5)
        v.key(b"/\x15", 0.3)
        mark = len(v.out)
        v.key(P.encode() + b"/new.c", 1.0)
        text = shown(v, mark)
        check("lookup #263: a file, its kind hidden: a and ^R both named",
              "Only files, commits and URLs: a shows them" in text and
              "^R look up" in text, text)
        mark = len(v.out)
        v.key(b"\x12", 1.0)
        check("lookup #263: ^R opens a hidden file's card",
              v.wait(P.encode() + b"/new.c  light", start=mark) and
              v.wait(b"commit deadbeef1 in", start=mark), v.out[mark:])
        v.key(b"q", 0.5)
        v.key(b"q")
        v.exit()
    finally:
        v.close()
    v = Pty([DCH, "--xray", "xv-cl"])
    try:
        v.wait(b"Prompts 2")
        v.pump(0.5)
        mark = len(v.out)
        v.key(b"/o/r#7", 1.0)
        text = shown(v, mark)
        check("lookup #263: a ref a chapter title holds: the row stays, the "
              "footer names ^R", "^R look up" in text and
              "No match" not in text, text)
        mark = len(v.out)
        v.key(b"\x12", 1.0)
        check("lookup #263: ^R opens the ref's card, not the row's chapter",
              v.wait(b"o/r#7  light", start=mark), v.out[mark:])
        v.key(b"\033", 0.5)
        v.key(b"q")
        v.exit()
    finally:
        v.close()


# ---- Refs: a project under $TMPDIR, a URL's host (#220) ---------------------

TPD = os.path.join(T, "tmpd")      # the view's $TMPDIR
TPP = os.path.join(TPD, "proj")    # a project inside it
TPL = os.path.join(T, "plink")     # a symlink to it, another session's cwd
# a real project outside every temp dir (this checkout), and a symlink into it
# from the temp dir: a row under it is never resolved, so it stays scratch
REPO = os.path.realpath(os.path.join(HERE, ".."))
HOSTILE = "https://github.com." + "a" * 50 + ".example/github.com/o/r/pull/1"
HUGE = "https://" + "z" * 120 + ".example/x/y/end"
IDN = "https://x" + "\u4f8b" * 25 + "e\u0301" * 6 + ".jp/" + "p/" * 30 + "fin"


def fixture_tmp():
    os.makedirs(os.path.join(TPP, "src"))
    for f in ("main.c", "real.c"):
        open(os.path.join(TPP, "src", f), "w").close()
    os.symlink(TPP, TPL)
    os.symlink(REPO, os.path.join(TPD, "rlink"))
    cfg = os.path.join(HOME, ".claude")
    sockdir = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    for name, sid, cwd, said in (
            ("xv-tp", "11111111-2222-4333-8444-5555555555c1", TPP,
             "edit %s/src/main.c, scratch in %s/other.txt; see %s, %s "
             "and %s" % (TPP, TPD, HOSTILE, HUGE, IDN)),
            ("xv-tl", "11111111-2222-4333-8444-5555555555c2", TPL,
             "edit %s/src/real.c, scratch in %s/other.txt" % (TPP, TPD)),
            ("xv-tx", "11111111-2222-4333-8444-5555555555c3", TPP,
             "edit %s/src/main.c, then %s/../leak.txt and %s/../esc/x"
             % (TPP, TPP, TPD)),
            ("xv-tr", "11111111-2222-4333-8444-5555555555c4", REPO,
             "see %s/rlink/README.md" % TPD)):
        pdir = os.path.join(cfg, "projects", cwd.replace("/", "-"))
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, sid + ".jsonl"), "w") as f:
            f.write(json.dumps({"type": "user", "cwd": cwd,
                                "timestamp": "2026-11-10T10:00:00Z",
                                "message": {"content": "tmp"}}) + "\n")
            f.write(json.dumps({"type": "assistant", "message": {
                "content": [{"type": "text", "text": said}]}}) + "\n")
        rec = {"name": name, "cwd": cwd, "end": "exited",
               "argv": ["claude"], "started": 1794299400,
               "updated": 1794300000, "ended": 1794300000,
               "sock_dir": sockdir,
               "agent": {"session_id": sid, "cwd": cwd, "config_dir": cfg}}
        with open(os.path.join(hdir, name + ".json"), "w") as f:
            f.write(json.dumps(rec) + "\n")


def refs_tab(name):
    v = Pty([DCH, "--xray", name], env={"TMPDIR": TPD + "/"})
    v.wait(b"Prompts 1")
    v.pump(0.5)
    mark = len(v.out)
    v.key(b"\033[Z", 1.0)
    mark = len(v.out)
    v.key(b"a", 1.0)  # files and URLs too (#262)
    scr = rows(v.out[mark:])
    return v, scr, "\n".join(scr[k] for k in sorted(scr))


def test_tmp():
    v, scr, text = refs_tab("xv-tp")
    try:
        check("Refs #220: a project under $TMPDIR shows its own file, only "
              "the scratch file outside it is hidden",
              "file    src/main.c" in text and "other.txt" not in text and
              "t temp files 1 hidden" in text, text)
        url = [r.rstrip() for r in scr.values() if "url     " in r]
        hit = [r for r in url if ".example/github.com/o/r/pull/1" in r]
        check("Refs #220: a host wider than half the row keeps its start, "
              "then the ellipsis and the URL's end",
              hit and re.search("url     github\\.com\\.a+" + ELL +
                                "a*\\.example/github\\.com/o/r/pull/1$",
                                hit[0]), url)
        big = [r for r in url if "zzzz" in r]
        check("Refs #220: a host wider than the row is cut at its end",
              big and re.search("url     z+" + ELL + "$", big[0]) and
              "/end" not in big[0] and cells(big[0]) <= 100,
              (big, [cells(r) for r in big]))
        idn = [r for r in url if "fin" in r]
        check("Refs #220: a wide-character host keeps its start within the "
              "width", idn and "url     x\u4f8b" in idn[0] and
              re.search(ELL, idn[0]) and cells(idn[0]) <= 100,
              (url, [cells(r) for r in url]))
        v.key(b"q")
    finally:
        v.close()
    v, scr, text = refs_tab("xv-tl")
    try:
        check("Refs #220: a project's file is not scratch by realpath either "
              "(the session's cwd is a symlink to it)",
              re.search(r"file    \S*src/real\.c", text) and
              "t temp files 1 hidden" in text, text)
        v.key(b"q")
    finally:
        v.close()
    v, scr, text = refs_tab("xv-tx")
    try:
        # the engine folds ".." (xray.c, 2.5) before the view sees the text;
        # the view's own ".." rule is behind it
        check("Refs #220: a temp path written with .. is scratch, also one "
              "through the project dir",
              "file    src/main.c" in text and "leak.txt" not in text and
              "esc/x" not in text and "t temp files 2 hidden" in text, text)
        v.key(b"q")
    finally:
        v.close()
    # the checkout stands for a project outside every temp dir; a checkout
    # in one is scratch itself (QA 1.60.0 NIT-T5)
    if REPO.startswith(("/tmp/", "/private/tmp/", "/var/folders/",
                        "/private/var/folders/")):
        print("  skip Refs #220: a project outside the temp dirs: this "
              "checkout, %s, is in one" % REPO)
        return
    v, scr, text = refs_tab("xv-tr")
    try:
        check("Refs #220: a project outside the temp dirs resolves no row: "
              "a temp-dir symlink into it stays scratch",
              "README.md" not in text and "t temp files 1 hidden" in text,
              text)
        v.key(b"q")
    finally:
        v.close()


# ---- the Prompts tab: auto chapters, o (#194) ------------------------------

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
        check("Prompts: the tab counts the prompts shown by default (2)",
              "Prompts 2" in text, text)
        check("Prompts: auto chapters are hidden",
              "fix the login bug" in text and "deploy it" in text and
              "Build finished" not in text and "task-notification" not in
              text and "Lint" not in text, text)
        check("Prompts: the footer shows n and how many are hidden",
              "n auto rows 3 hidden" in text, text)
        check("Prompts: o is offered only for a live session (none here)",
              "open session" not in text, text)
        v.key(b"k", 0.3)  # the newest is selected; up to the first prompt
        mark = len(v.out)
        v.key(b"n", 1.0)
        text = screen(v, mark)
        check("Prompts: n shows them, titled by their summary",
              "Build finished: 3 passed" in text and
              "Lint ?]0;pwned? evil?done" in text, text)
        check("Prompts: an auto chapter with no summary keeps its title",
              bare[:30] in text, (bare, text))
        check("Prompts: shown, an auto chapter is faint, a prompt is not",
              re.search(rb"\033\[0;2m[^\033]*Build finished: 3 passed",
                        v.out[mark:]) and
              re.search(rb"\033\[0m[^\033]*deploy it", v.out[mark:]),
              v.out[mark:][-800:])
        text = screen(v, 0)  # the rows n left as they were too
        check("Prompts: n shown, the footer says n hides them, the tab "
              "count stays 2", "n hide auto rows" in text and
              "hidden" not in text and "Prompts 2" in text, text)
        sel = [r for r in text.split("\n") if r.startswith("\u258c")]
        check("Prompts: n keeps the selection on its chapter",
              sel and "fix the login bug" in sel[0], (sel, text))
        mark = len(v.out)
        v.key(b"j", 0.3)
        v.key(b"\r", 1.0)
        check("Prompts: Enter on a shown auto chapter reads it",
              v.wait(b"reply 1", start=mark), v.out[mark:])
        v.key(b"\033", 0.5)
        mark = len(v.out)
        v.key(b"n", 1.0)
        v.key(b"/")
        v.key(b"Build", 1.0)
        text = screen(v, mark)
        check("Prompts: the filter matches a summary; only a hidden "
              "chapter matches: said, with the key",
              "Only auto rows: n shows them" in text, text)
        mark = len(v.out)
        v.key(b"\r", 0.5)
        v.key(b"n", 1.0)
        text = screen(v, mark)
        check("Prompts: n shows the one that matched the filter",
              "Build finished: 3 passed" in text and
              "fix the login bug" not in text, text)
        v.key(b"\033", 0.5)
        mark = len(v.out)
        v.key(b"o", 1.0)
        text = screen(v, mark)
        check("Prompts: o on an ended session says so and stays",
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
        check("Prompts: q exits 0", v.exit() == 0, v.out[-80:])
        bad = [b for b in (b"\033]0;pwned", b"\007 evil", b"\xe2\x80\xae")
               if b in v.out]
        check("mutant 11: no summary ESC, BEL or bidi byte in the output",
              not bad, bad)
    finally:
        v.close()
    v = Pty([DCH, "--xray", "xv-aa"])
    try:
        v.wait(b"Prompts")
        v.pump(0.8)
        text = screen(v, 0)
        check("Prompts: every chapter auto: the empty state says so and "
              "names n; the tab count is 0", "Only auto rows: n shows "
              "them" in text and "Prompts 0" in text and
              "n auto rows 1 hidden" in text, text)
        mark = len(v.out)
        v.key(b"n", 1.0)
        text = screen(v, mark)
        check("Prompts: n shows it, selected", any(
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
        check("Prompts: a compaction is hidden by default, the tab count "
              "the prompts (2)", "start the work" in text and
              "Conversation compacted" not in text and "Prompts 2" in text,
              text)
        check("Prompts: the footer counts a compaction with the auto rows",
              "n auto rows 2 hidden" in text, text)
        mark = len(v.out)
        v.key(b"n", 1.0)
        text = screen(v, mark)
        check("Prompts: n shows the compaction, its = marker, faint",
              "= Conversation compacted" in text and
              re.search(rb"\033\[0;2m[^\033]*Conversation compacted",
                        v.out[mark:]), v.out[mark:][-800:])
        v.key(b"k", 0.3)  # the newest is selected; up past the notification
        v.key(b"k", 0.3)
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("Prompts: Enter on a shown compaction reads its summary",
              v.wait(b"the compact summary", start=mark), v.out[mark:])
        v.key(b"\033", 0.5)
        v.key(b"q")
        check("Prompts: q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()
    v = Pty([DCH, "--xray", "xv-ca"])
    try:
        v.wait(b"Prompts")
        v.pump(0.8)
        text = screen(v, 0)
        check("Prompts: only a compaction and an auto chapter: the empty "
              "state of xv-aa", "Only auto rows: n shows them" in text
              and "Prompts 0" in text and "n auto rows 2 hidden" in text,
              text)
        v.key(b"q")
        v.exit()
    finally:
        v.close()


# ---- Responses (#290): each turn's answer, newest first ---------------------

ESID = "11111111-2222-4333-8444-5555555555e1"   # xv-rs, a History record
VSID = "11111111-2222-4333-8444-5555555555e2"   # xv-lv, a live Claude
LONGA = "\n".join("line %02d of the long answer" % i for i in range(1, 13))
BIG = "BIG \033]0;pwned\007 evil\u202eend\n" + "y" * 3000


def tl(sid):
    return os.path.join(HOME, ".claude", "projects", "-home-demo-proj",
                        sid + ".jsonl")


def turn(prompt, ts, *after, auto=False):
    """A prompt record, then what followed it."""
    u = {"type": "user", "cwd": P, "timestamp": ts,
         "message": {"content": prompt}}
    if auto:
        u["origin"] = {"kind": "task-notification"}
    return [u] + list(after)


def say(text):
    return {"type": "assistant", "message": {"content": [
        {"type": "text", "text": text}]}}


def call(tid, name, inp):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def result(tid, out, extra=None):
    o = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid, "content": out}]}}
    o.update(extra or {})
    return o


def fixture_resp():
    """xv-rs: a turn with tools and a 2-line answer, an auto row, a 12-line
    answer, one over 2 KB with hostile bytes (cut), one that ended on a
    tool call (no answer)."""
    patch = {"filePath": P + "/src/parse.c", "structuredPatch": [{
        "oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1,
        "lines": ["-a", "+b"]}]}
    lines = (turn("fix the parser", "2026-11-10T10:00:00Z",
                  call("e1", "Edit", {"file_path": P + "/src/parse.c",
                                      "old_string": "a", "new_string": "b"}),
                  result("e1", "edited", {"toolUseResult": patch}),
                  call("b1", "Bash", {"command": "make test"}),
                  result("b1", "12 passed"),
                  say("Fixed the parser.\nAll 12 tests pass.")) +
             turn(note("<task-id>t1</task-id>\n<summary>Build finished: 3 "
                       "passed</summary>"), "2026-11-10T10:10:00Z",
                  say("noticed the build"), auto=True) +
             turn("explain at length", "2026-11-10T10:20:00Z", say(LONGA)) +
             turn("write a big one", "2026-11-10T10:30:00Z", say(BIG)) +
             turn("the last, ended mid-tool", "2026-11-10T10:40:00Z",
                  call("r1", "Read", {"file_path": P + "/x.c"})))
    os.makedirs(os.path.dirname(tl(ESID)), exist_ok=True)
    with open(tl(ESID), "w") as f:
        for o in lines:
            f.write(json.dumps(o) + "\n")
    sockdir = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    rec = {"name": "xv-rs", "cwd": P, "end": "exited", "argv": ["claude"],
           "started": 1794299400, "updated": 1794300000,
           "ended": 1794300000, "sock_dir": sockdir,
           "agent": {"session_id": ESID, "cwd": P,
                     "config_dir": os.path.join(HOME, ".claude")}}
    with open(os.path.join(hdir, "xv-rs.json"), "w") as f:
        f.write(json.dumps(rec) + "\n")


def items(scr):
    """Responses' items on screen, top down, over the rule: (its title,
    [its lines])."""
    out = []
    for k in sorted(x for x in scr if x >= 4):
        r = scr[k]
        if r.startswith(("\u2500", "-")):
            break
        if re.match(r"^.\s{0,3}\d+  ", r):
            out.append((r.rstrip().split("  ")[-1].strip(), []))
        elif out and r.strip():
            out[-1][1].append(r.strip())
    return out


def head(v):
    """The selected item's header row."""
    sel = [r for r in rows(v.out).values() if r.startswith("\u258c")]
    return sel[0] if sel else ""


def test_responses():
    o = json.loads(dch("--xray", "xv-rs", "--json").stdout)
    ch = o["chapters"]
    check("Responses fixture: the engine's answers, the cut one, the tools, "
          "none for the turn that ended on a call",
          [bool(c.get("response")) for c in ch] ==
          [True, True, True, True, False] and ch[3].get("response_cut") and
          "Edit 1" in ch[0].get("tools", "") and
          ch[4].get("tools") == "Read 1", ch)
    v = Pty([DCH, "--xray", "xv-rs"], rows=44, cols=100)
    try:
        v.wait(b"Prompts 4")
        v.pump(0.5)
        mark = len(v.out)
        v.key(b"\t", 1.0)
        scr = rows(v.out)
        tabs = scr.get(2, "")
        check("Responses: Tab from Prompts opens it; the tabs Prompts, "
              "Responses, Refs in that order, each with its count",
              0 <= tabs.find("Prompts 4") < tabs.find("Responses 4") <
              tabs.find("Refs"), tabs)
        it = items(scr)
        check("Responses: newest first, the auto row hidden",
              [h for h, _ in it][:4] ==
              ["the last, ended mid-tool", "write a big one",
               "explain at length", "fix the parser"], it)
        check("Responses: the newest item is selected",
              "the last, ended mid-tool" in head(v), head(v))
        d = dict(it)
        check("Responses: a turn that ended on a call: its tool line, no "
              "text, no working line (an ended session)",
              d.get("the last, ended mid-tool") == ["Read 1"], d)
        check("Responses: the tool line, then the answer's lines",
              d.get("fix the parser", [""])[0].startswith("Edit 1 (1 file") and
              d["fix the parser"][1:] == ["Fixed the parser.",
                                          "All 12 tests pass."], d)
        check("Responses: a long answer: 8 lines, then a faint ellipsis",
              d.get("explain at length") ==
              LONGA.split("\n")[:8] + ["\u2026"], d)
        check("Responses: hostile bytes in an answer show as ?",
              d.get("write a big one", [""])[0] == "BIG ?]0;pwned? evil?end",
              d)
        text = screen(v, 0)
        check("Responses: the footer names Enter read, filter, n and tabs",
              "read" in text and "/ filter" in text and
              "n auto rows 1 hidden" in text and "tabs" in text, text)
        v.key(b"n", 1.0)
        it = items(rows(v.out))
        check("Responses: n shows the auto row, its summary as the title, "
              "the selection kept", any("Build finished" in h and
                                        b == ["noticed the build"]
                                        for h, b in it) and
              "the last, ended mid-tool" in head(v), (it, head(v)))
        v.key(b"n", 1.0)
        v.key(b"j", 0.5)
        check("Responses: j selects the next (older) item",
              "write a big one" in head(v), head(v))
        v.resize(20, 100)   # the chapter longer than the window
        v.pump(0.5)
        mark = len(v.out)
        v.key(b"\r", 1.5)
        v.wait(b"Reply", start=mark)
        v.pump(0.5)
        scr = rows(v.out[mark:])
        check("Responses: Enter reads that prompt, at its answer (the "
              "reader's first row is the Reply)",
              scr.get(2, "").startswith("4 of 5") and
              scr.get(4, "").startswith("Reply") and
              "BIG ?]0;pwned" in scr.get(5, ""), scr)
        mark = len(v.out)
        v.key(b"q", 1.0)
        v.resize(44, 100)
        v.pump(0.5)
        check("Responses: q goes back to the item it came from",
              v.wait(b"Responses 4", start=mark) and
              "write a big one" in head(v), head(v))
        v.key(b"j", 0.5)
        mark = len(v.out)
        v.key(b"\r", 1.5)
        v.wait(b"Reply", start=mark)
        v.pump(0.5)
        scr = rows(v.out[mark:])
        r = [k for k in scr if scr[k].startswith("Reply")]
        check("Responses: Enter on the long answer reads it, its first line "
              "under its Reply", r and
              "line 01 of the long answer" in scr.get(r[0] + 1, ""), scr)
        v.key(b"\033", 1.0)
        v.key(b"/line 05", 1.0)
        it = items(rows(v.out))
        check("Responses: / matches an answer's text",
              [h for h, _ in it] ==
              ["explain at length"], it)
        v.key(b"\033", 0.5)
        v.key(b"G", 0.5)
        check("Responses: G selects the oldest", "fix the parser" in head(v),
              head(v))
        v.key(b"g", 0.5)
        check("Responses: g the newest",
              "the last, ended mid-tool" in head(v), head(v))
        mark = len(v.out)
        v.key(b"\t", 1.0)
        check("Responses: Tab goes on to Refs", v.wait(b"[Refs", start=mark)
              or b"Pull requests, issues" in v.out[mark:], v.out[mark:][-400:])
        mark = len(v.out)
        v.key(b"\t", 1.0)
        check("Refs: Tab wraps to Prompts", v.wait(b"[Prompts", start=mark),
              v.out[mark:][-400:])
        mark = len(v.out)
        v.key(b"\033[Z", 1.0)
        v.key(b"h", 1.0)
        check("Shift-Tab from Prompts to Refs, h back to Responses",
              v.wait(b"[Responses", start=mark) and
              "the last, ended mid-tool" in head(v), v.out[mark:][-400:])
        v.key(b"q")
        check("Responses: q exits 0", v.exit() == 0, v.out[-80:])
        bad = [b for b in (b"\033]0;pwned", b"\007 evil", b"\xe2\x80\xae")
               if b in v.out]
        check("Responses: no answer ESC, BEL or bidi byte reached the "
              "terminal", not bad, bad)
    finally:
        v.close()
    # wide: the cut answer's 2048 bytes fit in 8 lines, so only
    # response_cut puts the ellipsis there
    v = Pty([DCH, "--xray", "xv-rs"], rows=50, cols=300)
    try:
        v.wait(b"Prompts 4")
        v.pump(0.5)
        v.key(b"\t", 1.0)
        d = dict(items(rows(v.out)))
        big = d.get("write a big one", [])
        check("Responses: an answer the engine cut ends in an ellipsis "
              "though its lines fit", len(big) == 9 and big[-1] == "\u2026"
              and big[-2].startswith("y"), big)
        check("Responses: a whole answer has none",
              d.get("fix the parser", [""])[-1] == "All 12 tests pass.", d)
        v.key(b"q")
        v.exit()
    finally:
        v.close()


def test_responses_live():
    """xv-lv: a live Claude (a sleep with a session record): its running
    turn says working; --follow's re-sent chapters keep the selection on
    its prompt, the first item on the first."""
    cwd = os.path.realpath(T)
    path = os.path.join(HOME, ".claude", "projects", cwd.replace("/", "-"),
                        VSID + ".jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for o in (turn("first live prompt", "2026-11-10T10:00:00Z",
                       say("answer one")) +
                  turn("second, running", "2026-11-10T11:00:00Z",
                       call("b1", "Bash", {"command": "sleep 1"}))):
            f.write(json.dumps(dict(o, cwd=cwd) if "cwd" in o else o) + "\n")
    dch("--spawn", "xv-lv", "/bin/sleep", "300")
    pid = until(lambda: json.loads(dch("--cmd", "xv-lv").stdout or "{}")
                .get("pid"), 5)
    start = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(pid)],
                           capture_output=True, text=True,
                           env={"LC_ALL": "C", "TZ": "UTC"}).stdout.strip()
    sd = os.path.join(HOME, ".claude", "sessions")
    os.makedirs(sd, exist_ok=True)
    with open(os.path.join(sd, "%d.json" % pid), "w") as f:
        json.dump({"pid": pid, "sessionId": VSID, "cwd": cwd,
                   "procStart": start, "version": "2.1.0",
                   "kind": "interactive", "status": "busy",
                   "statusUpdatedAt": 1}, f)

    def add(*recs):
        with open(path, "a") as f:
            for o in recs:
                f.write(json.dumps(o) + "\n")

    v = Pty([DCH, "--xray", "xv-lv"], rows=40, cols=100)
    try:
        v.wait(b"Prompts 2")
        v.pump(0.5)
        v.key(b"\t", 1.0)
        it = items(rows(v.out))
        check("Responses, live: the running turn says working, under its "
              "tool line", it and it[0][0] == "second, running" and
              it[0][1] == ["Bash 1", "working\u2026"], (it, screen(v, 0)))
        check("Responses, live: the footer offers o", "o open session" in
              screen(v, 0), screen(v, 0))
        add(result("b1", "ok"), say("the second answer"))
        ok = until(lambda: (v.pump(0.1), any(
            h == "second, running" and "the second answer" in b
            for h, b in items(rows(v.out))))[1], 10)
        check("Responses, live: the answer replaces working when the turn "
              "ends (--follow re-sends the chapter)", ok, items(rows(v.out)))
        v.key(b"j", 0.5)
        add(*turn("third prompt", "2026-11-10T12:00:00Z", say("answer 3")))
        ok = until(lambda: (v.pump(0.1), any(
            h == "third prompt" for h, b in items(rows(v.out))))[1], 10)
        check("Responses, live: a new turn on top, the selection stays on "
              "its prompt", ok and "first live prompt" in head(v),
              (items(rows(v.out)), head(v)))
        v.key(b"g", 0.5)
        add(*turn("fourth prompt", "2026-11-10T13:00:00Z", say("answer 4")))
        ok = until(lambda: (v.pump(0.1), any(
            h == "fourth prompt" for h, b in items(rows(v.out))))[1], 10)
        check("Responses, live: the first item selected stays the first",
              ok and "fourth prompt" in head(v), head(v))
        v.key(b"q")
        v.exit()
    finally:
        v.close()
        dch("-k", "xv-lv")


def test_responses_codex():
    """xv-cx: a live fake Codex over the codex-0167 rollout: its answer
    and tool line on Responses, its last turn working."""
    tid = "019a1111-2222-7333-8444-555566667702"
    src = os.path.join(HERE, "xray", "fixtures", "codex-0167", "codex",
                       tid + ".jsonl")
    sess = os.path.join(HOME, ".codex", "sessions", "2026", "11", "10")
    snaps = os.path.join(HOME, ".codex", "shell_snapshots")
    os.makedirs(sess, mode=0o700, exist_ok=True)
    os.makedirs(snaps, mode=0o700, exist_ok=True)
    shutil.copy(src, os.path.join(
        sess, "rollout-2026-11-10T10-00-00-%s.jsonl" % tid))
    bindir = os.path.join(T, "bin")
    os.makedirs(bindir, exist_ok=True)
    fake = os.path.join(bindir, "codex")
    b = subprocess.run(["cc", "-o", fake, os.path.join(HERE, "fake_codex.c")],
                       capture_output=True, text=True, timeout=60)
    check("codex: the fake codex builds", b.returncode == 0, b.stderr)
    log = os.path.join(T, "codex.log")
    cwd = os.path.join(T, "work", "xv-cx")
    os.makedirs(cwd, exist_ok=True)
    r = subprocess.run([DCH, "--spawn", "xv-cx", "--size", "80x24", "--cwd",
                        cwd, fake], capture_output=True, text=True,
                       timeout=30, env=dict(ENV, FAKE_CODEX_LOG=log))
    got = until(lambda: os.path.exists(log) and
                open(log).read().endswith("\n") and open(log).read().split(),
                10)
    check("codex: the fake codex runs", r.returncode == 0 and got, r.stderr)
    marker = got[1] if got and got[1] != "-" else None
    p = os.path.join(snaps, tid + ".1.sh")
    with open(p, "w") as f:
        f.write("# Snapshot file\nexport DCH_SESSION=xv-cx\n%s" % (
            "export DCH_NATIVE_BRIDGE_ID=%s\n" % marker if marker else ""))
    os.chmod(p, 0o600)
    v = Pty([DCH, "--xray", "xv-cx"], rows=40, cols=100)
    try:
        ok = v.wait(b"Prompts 2", 15)
        v.pump(0.5)
        v.key(b"\t", 1.0)
        it = items(rows(v.out))
        check("Responses, Codex: the tab, the answer and the tool line, the "
              "running turn working", ok and len(it) == 2 and
              "Ship it" in it[0][0] and it[0][1] == ["working\u2026"] and
              it[1][1] == ["exec 1, wait 1",
                           "PR #78 fixes it (commit 9f8e7d6c)."], it)
        v.key(b"q")
        v.exit()
    finally:
        v.close()
        subprocess.run([DCH, "-k", "xv-cx"], capture_output=True, timeout=30,
                       env=ENV)


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


def ready(v, mark=0):
    """The view holds the session's answer: its footer offers o. The first
    paint (tabs, "Reading") comes before it, and an o read then does
    nothing (QA 1.58.0 LOW-T2)."""
    return until(lambda: (v.pump(0.1), "o open session" in
                          screen(v, mark))[1], 10)


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
    dch("--send", "xv-op", "make the build o/r#41\n")  # a ref: the card's o
    until(lambda: "40" in dch("--read", "xv-op").stdout, 5)
    time.sleep(0.5)  # dch-lite: no --read; the block's time
    v = Pty([DCH, "--xray", "xv-op"], env=DTAP)
    try:
        ok = ready(v)
        text = screen(v, 0)
        check("Commands: a live session's footer offers o", ok, text)
        check("#295: a shell session's first tab is Commands",
              "Commands" in text and "Prompts" not in text and
              "Chapters" not in text, text)
        mark = len(v.out)
        v.key(b"o", 0.3)
        opened(v, mark, "Commands", "xv-op")
    finally:
        v.close()
    if lite:
        print("  ok   lite: a shell has no chapters to read (no corpus): o "
              "in the reader and on a card is the full build's")
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
            check("#295: a shell's reader footer says ] next command",
                  "] next command" in text and "chapter" not in text, text)
            check("reader: a live session's footer offers o",
                  "o open session" in text and "ran:make the build" in text,
                  text)
            mark = len(v.out)
            v.key(b"G", 0.3)
            v.key(b"j", 0.5)
            text = screen(v, mark)
            check("#295: past its only command's end the status says the "
                  "end of the last command", "end of the last command" in
                  text, text)
            mark = len(v.out)
            v.key(b"o", 0.3)
            opened(v, mark, "reader", "xv-op")
        finally:
            v.close()
        v = Pty([DCH, "--xray", "xv-op"], env=DTAP)
        try:
            ready(v)
            v.key(b"\t", 0.3)
            v.wait(b"o/r#41")
            mark = len(v.out)
            v.key(b"\r", 1.0)
            ok = until(lambda: (v.pump(0.1), "o open session" in
                                screen(v, mark) and "p other projects" in
                                screen(v, mark))[1], 10)
            check("card: a live session's footer offers o and p", ok,
                  screen(v, mark))
            mark = len(v.out)
            v.key(b"o", 0.3)
            opened(v, mark, "card", "xv-op")
        finally:
            v.close()
    dch("-k", "xv-op")  # the pickers below take their first row: xv-sh
    v = Pty([DCH, "-l"], env=DTAP)
    try:
        v.wait(b"x-ray")
        v.key(b"x", 1.0)
        v.wait(b"X-ray  xv-sh")
        ready(v)
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
        ready(v)
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
        ready(v)
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


def test_screen_group(lite):
    """#294: Refs' on-screen group on a live shell whose master keeps no
    ring (xray_ring = 0): the index holds nothing, the screen is read all
    the same."""
    cfg = os.path.join(T, "config-noring")
    with open(cfg, "w") as f:
        f.write("xray_ring = 0\n")
    r = subprocess.run([DCH, "--spawn", "xv-0", "--size", "80x24", "sh", "-c",
                        'echo READY0; while read -r x; do '
                        'echo "see $x https://example.com/p"; done'],
                       capture_output=True, text=True, timeout=30,
                       env=dict(ENV, DCH_CONFIG=cfg))
    check("screen group: spawned", r.returncode == 0, r.stderr)
    time.sleep(0.5)
    v = Pty([DCH, "--xray", "xv-0"])
    try:
        v.wait(b"Commands")
        v.pump(0.5)
        check("#294: no On screen tab", b"Refs 0" in v.out and
              b"On screen" not in v.out, v.out[-600:])
        check("#290: a shell has no Responses tab, never drawn",
              b"Responses" not in v.out, v.out[-600:])
        mark = len(v.out)
        v.key(b"\t", 1.5)  # over 1 s: the screen read at least once
        check("Refs: no refs, the empty state says what would show",
              v.wait(b"No refs yet: the files, commits, pull requests, "
                     b"issues and URLs this session touches show here",
                     start=mark), v.out[mark:])
        check("Refs #294: no ref on the screen (READY0), no group line",
              b"on screen now" not in v.out[mark:], v.out[mark:])
        mark = len(v.out)
        dch("--send", "xv-0", "o/r#5\n")
        if lite:
            v.pump(2.5)
            check("lite: no mirror, never a group line",
                  b"on screen now" not in v.out[mark:], v.out[mark:])
        else:
            ok = v.wait(b"on screen now", start=mark) and \
                v.wait(b"o/r#5", start=mark)
            v.pump(0.5)
            text = screen(v, 0)  # rows drawn last: the whole frame
            check("Refs #294: o/r#5 on the screen, the index holding "
                  "nothing, shows under the group line", ok, text)
            check("Refs #294: its row has no count (not the index's)",
                  re.search(r"(?m)^\W*pull o/r#5\b", text) and
                  "Refs 0" in text, text)
            check("Refs #294: the URL on the screen is hidden by the a "
                  "filter, the footer counting it",
                  "example.com" not in text and
                  "a files, commits, URLs 1 hidden" in text, text)
            mark = len(v.out)
            v.key(b"a", 1.0)
            text = screen(v, 0)
            check("Refs #294: a shows the URL in the group, after o/r#5 "
                  "on its row", 0 <= text.find("o/r#5") <
                  text.find("example.com/p"), text)
            v.key(b"a", 0.5)
            mark = len(v.out)
            v.key(b"\r", 1.0)
            check("Refs #294: Enter on the group's row opens its card",
                  v.wait(b"o/r#5  light", start=mark), v.out[mark:])
            # the rows change under the selection each second (#294)
            v.key(b"\033", 0.5)
            mark = len(v.out)
            dch("--send", "xv-0", "o/r#6\n")
            v.wait(b"o/r#6", start=mark)
            v.pump(1.5)
            mark = len(v.out)
            v.key(b"\r", 1.0)
            check("Refs #294: on the first row the selection stays there: "
                  "o/r#6, new on the screen, is under it",
                  v.wait(b"o/r#6  light", start=mark), v.out[mark:])
            v.key(b"\033", 0.5)
            v.key(b"j", 0.5)  # o/r#5, the second row
            mark = len(v.out)
            dch("--send", "xv-0", "o/r#7\n")
            v.wait(b"o/r#7", start=mark)
            v.pump(1.5)
            text = screen(v, 0)
            check("Refs #294: o/r#7 comes on the screen, first in the group",
                  0 <= text.find("o/r#7") < text.find("o/r#6") <
                  text.find("o/r#5"), text)
            mark = len(v.out)
            v.key(b"\r", 1.0)
            check("Refs #294: the selection stays on its ref (o/r#5) when "
                  "a new one comes above it; Enter opens o/r#5's card",
                  v.wait(b"o/r#5  light", start=mark), v.out[mark:])
        v.key(b"\003")
        check("screen group: Ctrl-C quits with 0", v.exit() == 0,
              v.out[-80:])
    finally:
        v.close()
        dch("-k", "xv-0")  # the picker test's first row is xv-sh


def test_resize():
    v = Pty([DCH, "--xray", "xv-cl"])
    try:
        v.wait(b"Prompts 2")
        mark = len(v.out)
        v.resize(6, 40)
        check("resize: a narrow window says it is too small",
              v.wait(b"the window is too small", start=mark), v.out[mark:])
        mark = len(v.out)
        v.resize(30, 100)
        check("resize: back to 100x30 redraws the list",
              v.wait(b"Prompts 2", start=mark), v.out[mark:])
        v.key(b"\003")
        check("Ctrl-C quits with 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()


def test_small_card():
    """QA 1.56.0 MED-V1: k, Up and PgUp in a too-small window took the
    card's selection below 0 and Enter read v->m[-1] (SIGSEGV)."""
    v = Pty([DCH, "--xray", "xv-cl"])
    try:
        v.wait(b"Prompts 2")
        v.key(b"\033[Z")
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

    def ran(n):
        """the n-th echo ran: its line and its output on the screen (#258:
        a --send can land late on a loaded machine). dch-lite has no
        --read (rc 3): nothing there reads what the echoes left"""
        r = dch("--read", "xv-sh")
        return r.returncode != 0 or r.stdout.count("see o/r#41") >= 2 * n

    for n in (1, 2):
        time.sleep(0.4)
        dch("--send", "xv-sh", "echo 'see o/r#41'\n")
        until(lambda: ran(n), 20)
    time.sleep(0.4)
    # printed once (the command line holds none): not underlined (#262)
    dch("--send", "xv-sh", "printf 'once o/r#%d\\n' 77\n")
    time.sleep(0.5)


def test_signals():
    for sig in (signal.SIGTERM, signal.SIGINT):
        name = signal.Signals(sig).name
        v = Pty([DCH, "--xray", "xv-sh"])
        try:
            v.wait(b"Commands")
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
    """#294: Refs on the live xv-sh: the refs on its screen first."""
    rc = subprocess.run([DCH, "--xray", "xv-sh", "--json"], env=ENV,
                        capture_output=True, text=True, timeout=30)
    o = json.loads(rc.stdout)
    lite = o["corpus"] == "none" and o["reason"] == "lite"
    v = Pty([DCH, "--xray", "xv-sh"])
    try:
        v.wait(b"Commands")
        if lite:
            check("lite: the label says X-ray needs the full dch build",
                  v.wait(b"X-ray needs the full dch build"), v.out[-600:])
            mark = len(v.out)
            v.key(b"\t", 1.5)
            check("lite: Refs has no group line (no mirror)",
                  v.wait(b"[Refs", start=mark) and
                  b"on screen now" not in v.out[mark:], v.out[mark:])
        else:
            u = [x for x in o["refs"] if x["key"] == "pull:o/r#41"]
            check("shell: o/r#41 is underlined in the index (count 2)",
                  u and u[0]["underline"], o["refs"])
            u = [x for x in o["refs"] if x["key"] == "pull:o/r#77"]
            check("shell: o/r#77, printed once, is not underlined",
                  u and not u[0]["underline"], o["refs"])
            mark = len(v.out)
            v.key(b"\t", 1.5)
            ok = v.wait(b"on screen now", start=mark)
            v.pump(0.5)
            text = screen(v, mark)
            g = text.find("on screen now")
            check("Refs #294: the group line, then o/r#77 (the lowest row) "
                  "and o/r#41, with their counts",
                  ok and 0 <= g < text.find("o/r#77") < text.find("o/r#41")
                  and re.search(r"\d pull o/r#41", text), text)
            check("Refs #294: a ref in the group is not repeated below, "
                  "and nothing else is held: no rest line",
                  text.count("o/r#41") == 1 and text.count("o/r#77") == 1
                  and "the rest" not in text, text)
            mark = len(v.out)
            v.key(b"\r", 1.0)
            check("Refs #294: Enter on the first row opens o/r#77's card",
                  v.wait(b"o/r#77  light", start=mark) and
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
              v.wait(b"Commands", start=mark), v.out[mark:])
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
            a.wait(b"[Refs", start=mark)
        check("chord, %s: the view opens over the attach, on Refs (#294)"
              % what, ok,
              a.out[mark:][-600:])
        check("chord, %s: the client is still running" % what,
              a.reap() is None)
        # two checks (#233): MARK187 goes dch --send -> master -> pty, not
        # through the attach, so its absence is a stalled session, not the
        # chord; a leak is any other byte. The details print even when empty
        sent = dch("--send", "xv-rx", "MARK187").returncode
        until(lambda: "4d41524b313837" in rx()[len(got):], 5)
        a.pump(0.3)
        rxd = rx()[len(got):]
        check("chord, %s: the text sent while the view was up reached the "
              "session (dch --send, not the attach)" % what,
              "4d41524b313837" in rxd, (rxd, sent))
        check("chord, %s: nothing else reached the session (no detach key, "
              "no x)" % what, rxd.replace("4d41524b313837", "", 1) == "",
              (rxd, sent))
        if not ok or a.reap() is not None:
            return
        mark = len(a.out)
        a.key(b"q", 0.5)
        check("chord, %s: q leaves the view's alternate screen" % what,
              a.wait(RESTORE, start=mark), a.out[mark:][-300:])
        if not lite:
            # only what follows the view's exit counts
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

    # o on Prompts (#194): the client is on the session already; o goes
    # back to it, the attach kept
    a = attach()
    try:
        got = rx()
        a.key(b"\x1cx", 0.5)
        a.wait(b"X-ray  xv-rx")
        mark = len(a.out)
        a.key(b"\t", 0.5)
        ready(a, mark)
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
              a.wait(b"Sessions"), a.out[-600:])
        a.pump(0.5)
        mark = len(a.out)
        a.key(b"x", 0.5)
        check("detach_key = switch: a late x opens the picker's view",
              a.wait(b"X-ray  xv-rx", start=mark), a.out[mark:][-600:])
        mark = len(a.out)
        a.key(b"q", 0.5)
        check("detach_key = switch: q in that view returns to the picker",
              a.wait(b"Sessions", start=mark), a.out[mark:][-600:])
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
             b"Sessions", "press, release, press")):
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
        fixture_latest()
        test_latest()
        test_lookup()
        fixture_tmp()
        test_tmp()
        fixture_auto()
        test_auto()
        test_compact()
        fixture_resp()
        test_responses()
        test_responses_live()
        test_responses_codex()
        test_resize()
        test_small_card()
        shell()
        test_signals()
        test_on_screen()
        o = json.loads(dch("--xray", "xv-sh", "--json").stdout)
        test_screen_group(o["corpus"] == "none" and o["reason"] == "lite")
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
