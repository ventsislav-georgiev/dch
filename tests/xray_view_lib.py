"""The `dch --xray <n>` view and the picker's x (#175, XR5a): the harness
and fixtures of its four test files (split for the pool, #365):
xray_view_test.py, xray_view_turns_test.py, xray_view_tasks_test.py,
xray_view_shell_test.py. Each imports this, so each gets its own T.

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
     Commands for a shell's, never Chapters, #295; Shift-Tab twice from
     Prompts to Refs, by Search, #291), the chapter list, the Refs list,
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
     session; the rest of the o's write (oo, o Enter, a held o) never
     reaches the session, from dch --xray and the chord (LOW-V12);
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
     back with the filter kept; plain words: one faint line says ^R
     searches for them, and ^R runs them on Search (#291), no card.
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
     A shell (xv-0) has no Responses tab. xv-cxt, a live fake Codex with
     two update_plan calls: Tasks from the newest plan (#320).
 15. Search (#291): xv-se, a History Claude record with the needle in a
     prompt, a tool call, its result and two answers: Tab cycles Prompts,
     Responses, Refs, Search (no count before a search) and wraps; Search
     opens typing with its hint; Enter searches: the tab counts the hits,
     the text's line stays above them, newest first, each with its time,
     who said it (You, Reply, the tool's name) and its prompt's title; the
     match in accent bold in either case; Enter reads the hit's prompt
     scrolled to the text (deep in a long chapter), q back to the same
     hit, also on the third; j, g, G; a 1-byte text refused in the status
     line, the hits kept; Esc ends typing with the last text back; no
     match; Shift-Tab back keeps the text and its answer; plain words in
     the Prompts filter: ^R search in the footer and on the empty line, ^R
     lands on Search with their hit. xv-sm, 55 prompts: 50+ in the tab,
     the faint "the 50 newest are shown", G the 50th. A shell (xv-sh):
     Commands, Refs, Search (no Responses, no Tasks); its ring searched
     (each line as Output); on dch-lite the list says no x-ray corpus.
 16. the card's mentions newest first (#304, QA 1.62.0 MED-V13): a
     transcript under 1 MiB (one light window), one spanning several (no
     zigzag of the windows), and after f (oldest first from the engine);
     the selected mention stays selected while f's lines arrive; Enter on
     a mention reads its prompt.
 17. Tasks (#320): xv-nt, a Claude record with no task record: no Tasks
     tab, Tab never lands on one. xv-tk, six tasks over three prompts: the
     tab last, counting the open ones (Tasks 3); Shift-Tab opens it; group
     lines In progress, Pending, Done (newest first), Dropped, each
     counted; the active row by its active text with its age (2h), blocked
     by 1 on a row, ages 5m and 3d; a title with ESC, BEL, U+202E drawn
     with ? and cut to one line, none of them reaching the terminal; j
     past the group lines; Enter reads the turn that last changed the task
     (3 of 3), q back to the same task; / filters in any case, keeping
     only the groups with a match, also over a note; no match said; Esc
     clears it. xv-tb, 20000 tasks: counted within 5 s, G, g, PgDn, j each
     painted within 0.5 s, a filter within 1 s.
 18. Memory (#323): xv-nt has no Memory tab. xv-me, four memories and the
     project's CLAUDE.md: the tab last, counting every row (Memory 5);
     This session (wrote, then loaded), Memories (newest first),
     Instructions (the path short), kind and note on the rows, no
     "chapter" on screen; Enter opens the card (note, path, kind, the
     text), G scrolls the text to its end, Enter on a row this session did
     not touch says so, q back to the row; a text over 16 KB says it was
     cut and names the path; Enter on a session row's card reads the turn
     that wrote it (2 of 2), q back to the card, q back to the row; / over
     a memory's text. xv-cxt (Codex): its AGENTS.md under Instructions,
     Enter shows its text. xv-mb, 20000 memories: the tab and the whole
     count within 15 s (the time printed), G within 0.5 s.
 19. #325: xv-sp, one turn saying needle in its prompt (PLACE-A), in a
     reply 60 lines on (PLACE-B) and in a Bash output 60 lines after that
     (PLACE-C): Enter on each Search hit opens the reader with that hit's
     line on screen, not the prompt's, q back to the same hit (MED-V21); a
     good search after a refused one clears "bad search" (LOW-V18); the
     word chapter on no screen. Its six tabs at widths 24, 30, 40 and 60,
     each tab selected in turn: no word cut, the selected tab shown, a
     < or > mark when tabs are left out (NIT-V9); at 20 the view says it
     is too small, no tab drawn. A held o (MED-V20): a second o 10 ms and
     50 ms after the first and 8 o's 33 ms apart (dch --xray), 8 o's from
     the chord's view, the picker's x and Search: nothing reaches the
     session, a z 300 ms after the last o does; a failed run whose own
     o's came over 700 ms apart is run again (3 at most), the row says so.
 20. #331: the attach a view's o opens (dch --xray, the picker's x, the
     chord) drops o's that follow within 700 ms of each other: a held o's
     repeats from 300 ms on type nothing; an x 300 ms on, and the x of an
     oox, reach the session; an o 1 s after the attach does; a plain -a's
     o 100 ms after attaching does.
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
QUIET = 0.1     # Pty.key: no byte for this long after the answer ends it


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

    def key(self, b, settle=0.3, full=False):
        """Type b, then read until the answer has come and QUIET s passed
        with nothing more, settle s at most (#365). full: all settle s, for
        a row that checks nothing more comes or that times the next key."""
        try:
            os.write(self.fd, b)
        except OSError:
            pass    # it has exited: the checks say so
        if full:
            self.pump(settle)
            return
        n = len(self.out)
        end = time.time() + settle
        while self.rc is None and time.time() < end:
            m = len(self.out)
            self.pump(min(QUIET, end - time.time()))
            if len(self.out) == m > n:
                return

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


def shown(v, mark):
    scr = rows(v.out[mark:])
    return "\n".join(scr[k] for k in sorted(scr))


# ---- the card's mentions, newest first (#304, QA 1.62.0 MED-V13) -----------

OSIDS = ["11111111-2222-4333-8444-5555555555%02x" % k for k in (0xa1, 0xa2, 0xa3)]


def fixture_order():
    """xv-o1: four turns ONE..FOUR, each saying PROJ-9, under 1 MiB; xv-o2:
    the same with a second answer per turn and 1.2 MB after each, a window
    each; xv-o3: one turn saying PROJ-9 in 250 lines, so light and full
    keep different lines (a file gives 200)."""
    os.makedirs(os.path.dirname(tl(OSIDS[0])), exist_ok=True)
    for name, sid, big in (("xv-o1", OSIDS[0], 0), ("xv-o2", OSIDS[1], 1)):
        lines = []
        for k, w in enumerate(["ONE", "TWO", "THREE", "FOUR"]):
            ts = "2026-11-10T1%d:00:" % k
            lines += turn("prompt %s" % w, ts + "00Z",
                          at(say("answer %s says PROJ-9" % w), ts + "05Z"))
            if big:
                lines += [at(say("answer %s2 says PROJ-9" % w), ts + "09Z")]
                lines += [say("pad " * 50000)] * 6     # 1.2 MB
        record(name, sid, lines)
    record("xv-o3", OSIDS[2], turn("prompt PIN", "2026-11-10T10:00:00Z") +
           [at(say("note %03d says PROJ-9" % k),
               "2026-11-10T10:%02d:%02dZ" % (k // 60, k % 60))
            for k in range(1, 251)])


def card_rows(v):
    """The card's mention words top down, and the selected one."""
    scr = rows(v.out)
    got = [(scr[k].startswith("\u258c"),
            re.search(r"(?:answer|note) (\w+) says", scr[k]))
           for k in sorted(scr)]
    got = [(s, m.group(1)) for s, m in got if m]
    return [w for _, w in got], [w for s, w in got if s]


def order_card(name, keys, secs=20.0):
    """name's view, PROJ-9's card opened by ^R, settled."""
    v = Pty([DCH, "--xray", name], 40, 100)
    v.wait(b"Prompts ", secs)
    v.pump(0.5)
    v.key(b"/PROJ-9", 0.5)
    mark = len(v.out)
    v.key(keys, 0.5)
    v.wait(b"PROJ-9  light", secs, start=mark)
    settle(v, secs)
    return v


def settle(v, secs=20.0):
    """until the card's scan is done: no row says searching"""
    until(lambda: v.pump(0.2) or not any(
        "searching" in r for r in rows(v.out).values()), secs)
    v.pump(0.5)


# ---- Refs: a project under $TMPDIR, a URL's host (#220) ---------------------

TPD = os.path.join(T, "tmpd")      # the view's $TMPDIR
TPP = os.path.join(TPD, "proj")    # a project inside it
TPL = os.path.join(T, "plink")     # a symlink to it, another session's cwd
# a real project outside every temp dir, and a symlink into it from the temp
# dir: a row under it is never resolved, so it stays scratch. A system
# directory, not this checkout: the view's --memory walks up from the cwd and
# would read the real CLAUDE.md files above a checkout (#325); nothing above
# /usr/bin holds one, and the test dir itself is always in a temp dir
SYSP = "/usr/bin"
HOSTILE = "https://github.com." + "a" * 50 + ".example/github.com/o/r/pull/1"
HUGE = "https://" + "z" * 120 + ".example/x/y/end"
IDN = "https://x" + "\u4f8b" * 25 + "e\u0301" * 6 + ".jp/" + "p/" * 30 + "fin"


def fixture_tmp():
    os.makedirs(os.path.join(TPP, "src"))
    for f in ("main.c", "real.c"):
        open(os.path.join(TPP, "src", f), "w").close()
    os.symlink(TPP, TPL)
    os.symlink(SYSP, os.path.join(TPD, "rlink"))
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
            ("xv-tr", "11111111-2222-4333-8444-5555555555c4", SYSP,
             "see %s/rlink/env" % TPD)):
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
    # Tab twice, by Responses: a managed CLAUDE.md on this machine gives
    # any Claude session a Memory tab (#323), last
    v.key(b"\t\t", 1.0)
    mark = len(v.out)
    v.key(b"a", 1.0)  # files and URLs too (#262)
    scr = rows(v.out[mark:])
    return v, scr, "\n".join(scr[k] for k in sorted(scr))


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


GSID = "11111111-2222-4333-8444-5555555555a8"


def gone_lines(prompts):
    lines = []
    for i, t in enumerate(prompts):
        lines += [{"type": "user", "cwd": P, "message": {"content": t},
                   "timestamp": "2026-11-10T1%d:00:00Z" % i},
                  {"type": "assistant", "message": {"content": [
                      {"type": "text", "text": "reply %d" % i}]}}]
    return "".join(json.dumps(o) + "\n" for o in lines)


# ---- Responses (#290): each turn's answer, newest first ---------------------

ESID = "11111111-2222-4333-8444-5555555555e1"   # xv-rs, a History record
VSID = "11111111-2222-4333-8444-5555555555e2"   # xv-lv, a live Claude
LONGA = "\n".join("line %02d of the long answer" % i for i in range(1, 13))
BIG = "BIG \033]0;pwned\007 evil\u202eend\n" + "y" * 3000


def tl(sid, proj="-home-demo-proj"):
    return os.path.join(HOME, ".claude", "projects", proj, sid + ".jsonl")


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


# ---- Search (#291): where a word or phrase was said ------------------------

SSID = "11111111-2222-4333-8444-5555555555d1"   # xv-se (not c1/c2: xv-tp, xv-tl)
MSID = "11111111-2222-4333-8444-5555555555d2"   # xv-sm: over 50 hits
DEEPL = "\n".join("filler line %02d" % i for i in range(60))


def at(rec, ts):
    return dict(rec, timestamp=ts)


def record(name, sid, lines, proj="-home-demo-proj", cwd=P):
    with open(tl(sid, proj), "w") as f:
        for o in lines:
            f.write(json.dumps(o) + "\n")
    sockdir = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    rec = {"name": name, "cwd": cwd, "end": "exited", "argv": ["claude"],
           "started": 1794299400, "updated": 1794300000,
           "ended": 1794300000, "sock_dir": sockdir,
           "agent": {"session_id": sid, "cwd": cwd,
                     "config_dir": os.path.join(HOME, ".claude")}}
    with open(os.path.join(hdir, name + ".json"), "w") as f:
        f.write(json.dumps(rec) + "\n")


def fixture_search():
    """xv-se: the needle in a prompt, a tool call, its result and two
    answers, one deep in a long chapter; xv-sm: 55 prompts with zebra."""
    os.makedirs(os.path.dirname(tl(SSID)), exist_ok=True)
    ts = "2026-11-10T10:0%d:00Z"
    record("xv-se", SSID,
           turn("first: where is the Needle kept?", ts % 0,
                at(call("n1", "Bash", {"command": "grep -r needle src"}),
                   ts % 1),
                at(result("n1", "src/a.c: needle here"), ts % 2),
                at(say("The needle is in src/a.c."), ts % 3)) +
           turn("an unrelated second prompt", ts % 4,
                at(say("nothing to see"), ts % 5)) +
           turn("a long chapter", ts % 6,
                at(say(DEEPL + "\nthe deep NEEDLE line\nafter it"), ts % 7)))
    record("xv-sm", MSID, sum((turn("zebra prompt %02d" % i,
                                    "2026-11-10T11:%02d:00Z" % i,
                                    say("ok %d" % i))
                               for i in range(55)), []))


def hits(scr):
    """Search's hits on screen, top down, over the rule: (who, title,
    [snippet lines])."""
    out = []
    for k in sorted(x for x in scr if x >= 5):
        r = scr[k]
        if r.startswith(("\u2500", "-")):
            break
        if re.match(r"^.\s\d\d-\d\d \d\d:\d\d", r):
            who, _, title = r[15:].rstrip().partition("  ")
            out.append((who, title.strip(), []))
        elif out and r.strip():
            out[-1][2].append(r.strip())
    return out


def tabbar(v, want, secs=10.0):
    """The tab bar once it holds want (its count faint: its own SGR)."""
    return until(lambda: (v.pump(0.1), want in rows(v.out).get(2, ""))[1],
                 secs)


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


# ---- Tasks (#320): the session's task list ----------------------------------

KSID = "11111111-2222-4333-8444-5555555555f1"   # xv-tk: a task list
NSID = "11111111-2222-4333-8444-5555555555f2"   # xv-nt: none
BSID = "11111111-2222-4333-8444-5555555555f3"   # xv-tb: 20000 tasks
KEVIL = "evil \033]0;pwned\007 bi‮di " + "w" * 300


def iso(ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago))


def made(ts, tid, n, subject, desc="", active=None):
    """A TaskCreate and its result naming task n."""
    inp = {"subject": subject, "description": desc}
    if active:
        inp["activeForm"] = active
    return [at(call(tid, "TaskCreate", inp), ts),
            at(result(tid, "ok", {"toolUseResult": {"task": {"id": n}}}), ts)]


def upd(ts, tid, n, **kw):
    return at(call(tid, "TaskUpdate", dict(taskId=n, **kw)), ts)


def fixture_tasks():
    """xv-tk: six tasks over two prompts, one hostile; xv-nt: none; xv-tb:
    20000 pending tasks."""
    os.makedirs(os.path.dirname(tl(KSID)), exist_ok=True)
    record("xv-tk", KSID,
           turn("plan the work", iso(4 * 86400),
                *(made(iso(4 * 86400), "c1", "1", "Fix the login test",
                       "the CI fails", "Fixing the login test") +
                  made(iso(4 * 86400), "c2", "2", "Write docs") +
                  made(iso(4 * 86400), "c3", "3", "Old done") +
                  made(iso(4 * 86400), "c4", "4", "Recent done") +
                  made(iso(4 * 86400), "c5", "5", "Dropped one") +
                  made(iso(4 * 86400), "c6", "6", KEVIL))) +
           turn("do the work", iso(3 * 86400 + 60),
                upd(iso(3 * 86400), "u3", "3", status="completed")) +
           turn("finish it", iso(3 * 3600),
                upd(iso(2 * 3600), "u1", "1", status="in_progress"),
                upd(iso(2 * 3600), "u2", "2", addBlockedBy=["1"]),
                upd(iso(300), "u4", "4", status="completed"),
                upd(iso(300), "u5", "5", status="deleted")))
    record("xv-nt", NSID, turn("no tasks here", iso(60), say("fine")))
    record("xv-tb", BSID, turn("many", iso(600)))
    with open(tl(BSID), "a") as f:
        for i in range(1, 20001):
            f.write('{"type":"assistant","message":{"content":[{"type":'
                    '"tool_use","id":"t%d","name":"TaskCreate","input":'
                    '{"subject":"bulk task %05d","description":""}}]}}\n'
                    '{"type":"user","toolUseResult":{"task":{"id":"%d"}},'
                    '"message":{"content":[{"type":"tool_result",'
                    '"tool_use_id":"t%d","content":"ok"}]}}\n' % (i, i, i, i))


def tasks_rows(v):
    """Tasks' lines on screen over the rule, top down, blanks folded."""
    out, scr = [], rows(v.out)
    for k in sorted(x for x in scr if x >= 4):
        r = scr[k]
        if r.startswith(("─", "-")):
            break
        if r.strip():
            out.append(re.sub(" +", " ", r).strip())
    return out


# ---- Memory (#323): the session's memories and instruction files ----------

MESID = "11111111-2222-4333-8444-5555555555f6"   # xv-me: memories (not f4: xv-lt)
MBSID = "11111111-2222-4333-8444-5555555555f5"   # xv-mb: 20000 of them
MP = os.path.join(T, "mproj")       # xv-me's cwd: a CLAUDE.md, a .git
MDIR = os.path.join(HOME, ".claude", "projects", "-mem-proj", "memory")


def mfile(name, body, ago, kind=None):
    with open(os.path.join(MDIR, name), "w") as f:
        f.write(("---\nname: %s\ndescription: %s desc\nmetadata:\n  type: %s"
                 "\n---\n" % (name[:-3], name[:-3], kind) if kind else "") +
                body)
    os.utime(os.path.join(MDIR, name), (time.time() - ago,) * 2)


# random per run: no host instruction file can hold it
NEEDLE = "zq" + os.urandom(6).hex()


def fixture_memory():
    """xv-me: four memories (one this session wrote, one it read, one with
    a word only in its text and 40 lines, one over 16 KB) and the project's
    CLAUDE.md. xv-mb: 20000 memories."""
    os.makedirs(MDIR, exist_ok=True)
    os.makedirs(os.path.join(MP, ".git"), exist_ok=True)
    with open(os.path.join(MP, "CLAUDE.md"), "w") as f:
        f.write("project rules here\n")
    with open(os.path.join(MDIR, "MEMORY.md"), "w") as f:
        f.write("- [Alpha rule](alpha.md) \u2014 alpha hook\n"
                "- [Beta notes](beta.md) \u2014 beta hook\n"
                "- [Gamma ref](gamma.md) \u2014 gamma hook\n")
    mfile("alpha.md", "alpha body first line\n", 7200, "feedback")
    mfile("beta.md", "beta body\n", 7200, "project")
    mfile("gamma.md", "".join("gamma line %02d\n" % i for i in range(1, 41))
          + "the zebrafish line\n" + NEEDLE + "\n", 3600, "reference")
    mfile("long.md", "x" * 20000, 2 * 86400)
    tsw = iso(3 * 3600)
    um = lambda prompt, ts: {"type": "user", "cwd": MP, "timestamp": ts,
                             "message": {"content": prompt}}
    record("xv-me", MESID,
           [um("read the notes", iso(5 * 3600)),
            call("r1", "Read", {"file_path": MDIR + "/beta.md"}),
            um("remember the rule", tsw),
            call("w1", "Write", {"file_path": MDIR + "/alpha.md",
                                 "content": "x"})],
           proj="-mem-proj", cwd=MP)
    bdir = os.path.join(HOME, ".claude", "projects", "-mem-big", "memory")
    os.makedirs(bdir, exist_ok=True)
    for i in range(1, 20001):
        with open(os.path.join(bdir, "m%05d.md" % i), "w") as f:
            f.write("bulk memory %05d\n" % i)
    record("xv-mb", MBSID, [um("many", iso(600))], proj="-mem-big")


def mem_tab(v):
    """Memory, from Prompts: Shift-Tab (it is the last tab)."""
    v.key(b"\033[Z", 0.8)
    return tasks_rows(v)


# ---- #325: a Search hit opens at its place; the tab bar's width ------------

PSID = "11111111-2222-4333-8444-5555555555f7"   # xv-sp
TABS = ("Prompts", "Responses", "Refs", "Search", "Tasks", "Memory")


def fixture_place():
    """xv-sp, in xv-me's project (its memories: a Memory tab): one turn
    with a task (a Tasks tab) saying needle in its prompt, in a reply 60
    lines on and in a Bash output 60 lines after that."""
    ts = "2026-11-10T12:0%d:00Z"
    reply = "".join("reply filler %02d\n" % i for i in range(60))
    out = "".join("output filler %02d\n" % i for i in range(60))
    record("xv-sp", PSID,
           turn("PLACE-A where is the needle?", ts % 0,
                *made(ts % 1, "k1", "1", "Find it") +
                [at(say(reply + "PLACE-B the needle in the reply\nreply "
                        "tail"), ts % 2),
                 at(call("b1", "Bash", {"command": "cat log"}), ts % 3),
                 at(result("b1", out + "PLACE-C the needle in the output\n"
                           "output tail"), ts % 4)]),
           proj="-mem-proj", cwd=MP)


QSID = "11111111-2222-4333-8444-5555555555f8"   # xv-sq: twin hits


MLSID = "11111111-2222-4333-8444-5555555555f9"   # xv-ml: a live Claude


def bar_ok(bar, sel, w):
    """The tab bar: whole tab names, counts and marks only, within w
    cells, sel shown selected, a mark when a tab is left out."""
    toks = bar.replace("[", " ").replace("]", " ").split()
    marks = ("\u2039", "\u203a", "<", ">")
    return (toks and all(t in TABS or t in marks or re.fullmatch(r"\d+\+?", t)
                         for t in toks) and "[" + sel in bar and
            cells(bar.rstrip()) <= w and
            (set(TABS) <= set(toks) or any(m in toks for m in marks)))


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


def attached(a):
    """The client's raw mode is on its pty: it is attached. Not its
    repaint: dch-lite has no mirror, so a line printed before the attach
    never comes back and a wait for it runs out its 10 s."""
    return until(lambda: (a.pump(0.05), not termios.tcgetattr(a.fd)[3] &
                          termios.ICANON)[1], 10)


def attach(env=None, extra=()):
    a = Pty([DCH] + list(extra) + ["-a", "xv-rx"], env=env)
    attached(a)
    a.pump(0.3)
    return a


# o, then another after each gap (s), each its own write: a second o
# typed later, a held o's repeats (macOS: about 30 a second); a z 300 ms
# after the last
HELD = (("a second o 10 ms later", [0.01]), ("a second o 50 ms later", [0.05]),
        ("8 o's 33 ms apart", [0.033] * 7))


def o_held(p, gaps):
    """The o's and the z, the pty read meanwhile as a terminal does (see
    o_then). Returns the longest gap between two o's as written."""
    t = [time.monotonic()]
    os.write(p.fd, b"o")
    for g in gaps:
        p.pump(g)
        t.append(time.monotonic())
        os.write(p.fd, b"o")
    p.pump(0.3)
    os.write(p.fd, b"z")
    return max(b - a for a, b in zip(t, t[1:]))


def held_check(name, run):
    """run() -> (ok, detail, gap). The o's are dropped while each follows
    the last within 700 ms (#331, past the view's 100 ms): a failed run
    whose own o's came further apart, the test stalled, never had a held
    o. It is run again, 3 runs at most, and the row says so; a failure
    with the o's within 700 ms is a failure."""
    for n in range(1, 4):
        ok, detail, gap = run()
        if ok or gap <= 0.7:
            break
    if not ok and gap > 0.7:
        print("  skip %s: the test's own o's came %d ms apart in 3 runs, "
              "past the 700 ms window" % (name, gap * 1000))
        return
    check(name + ("" if n == 1 else " (run %d: the test's own o's came "
                  "over 700 ms apart before)" % n), ok, detail)


def chord_case(what, keys, lite):
    a = attach()
    try:
        got = rx()
        mark = len(a.out)
        for k in keys:
            a.key(k, 0.05, full=True)
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
        a.key(b"q", 0.5, full=True)
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
        a.key(b"z", 0.3, full=True)
        check("chord, %s: the attach was never retired (a key after "
              "reaches the session)" % what,
              until(lambda: rx()[len(before):] == "7a", 5) and
              a.reap() is None, (rx()[len(before):], a.rc))
    finally:
        a.close()


def o_then(p, steps):
    """o, then each (gap s, bytes) in a write of its own. The pty is read
    meanwhile, as a terminal does: an attach whose repaint finds the pty
    full blocks on it and reads no key until it drains."""
    os.write(p.fd, b"o")
    for g, b in steps:
        p.pump(g)
        os.write(p.fd, b)


def xv_rx():
    v = Pty([DCH, "--xray", "xv-rx"], env=DTAP)
    ready(v)
    return v


def picker_rx():
    v = Pty([DCH, "-l"], env=DTAP)
    v.wait(b"x-ray")
    for _ in range(4):  # the picker's row of xv-rx
        mark = len(v.out)
        v.key(b"x", 1.0, full=True)
        v.wait(b"X-ray  ", start=mark)
        if b"X-ray  xv-rx" in v.out[mark:]:
            break
        v.key(b"q", 1.0, full=True)
        v.key(b"j", 0.5, full=True)
    ready(v, mark)
    return v


def chord_rx():
    a = attach()
    a.key(b"\x1cx", 0.5, full=True)
    a.wait(b"X-ray  xv-rx")
    mark = len(a.out)
    a.key(b"\t\t", 0.5, full=True)  # Refs, Search, then Commands
    ready(a, mark)
    return a


# o's repeats starting 300 ms on, one every 50 ms for 1 s, then a z
REPEAT = [(0.3, b"o")] + [(0.05, b"o")] * 19 + [(0.3, b"z")]


def run_steps(*steps):
    """Run each fixture or test in turn, then the cleanup; exit 1 on a FAIL."""
    try:
        for f in steps:
            f()
    finally:
        dch("-k", "xv-rx")
        dch("-k", "xv-end")
        dch("-k", "xv-sh")
        dch("-k", "xv-0")
        shutil.rmtree(T, ignore_errors=True)
    if fails[0]:
        print("%d failed" % fails[0])
        sys.exit(1)
