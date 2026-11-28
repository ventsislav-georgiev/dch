#!/usr/bin/env python3
"""The interactive session picker (#075), driven inside dch sessions.

The picker runs as the command of a dch session; --read shows its screen
and --keys types into it. Covers:
  1. the Sidekicks tab shows only once a session matches sidekick_pattern
     (default "-qa"), live, and that session leaves the Sessions tab;
  2. the sort toggle: activity (working first) vs name (s2 before s10);
  3. rename through the picker sets the alias;
  4. X kills and asks first: n keeps the session, y kills it; x opens
     the row's X-ray view and never kills (OQ-6), the hints say so;
  5. the History tab lists the killed session and Enter resumes it, and
     titles a record by its agent.name (#097);
  6. NO_COLOR and TERM=dumb draw no ESC[3x color, a colored one does;
  7. at 40 columns a long title is cut and no row wraps;
  8. s persists the sort (picker_sort) and a fresh picker starts in it;
  9. the settings view (,): lists every key, Enter cycles picker_sort and
     prompts for sidekick_pattern (the Sidekicks split follows at once), u
     resets to the default, auto_allow on asks y/N first and off does not;
 10. the layout (#086): column headers on every list, the STATE column at
     one x on every row, the selected row's state keeping its own colour on
     a tinted bar (no reverse block), the key hints grouped, all of them at
     200 columns and whole groups dropped at 80;
 11. a session whose name lacks the pattern but whose harness (Claude)
     title has it is a sidekick (#086);
 12. the "/" filter (#096): typing narrows the tab (title, dch name or
     directory, case folded), the tab counts follow, Backspace and Ctrl-U
     edit, no row says No match and its actions do nothing, the filter
     survives a tab switch,
     Esc restores every row and a later Esc quits, Backspace on nothing
     leaves the prompt, Enter attaches the top match; Down moves among
     the kept rows, closing the prompt and keeping the filter so a letter
     action (X) works on the filtered row, j/k type into the prompt, and a
     reload keeps the filter and the selection (#108);
 13. the kitty keyboard protocol (#132), on a real pty: the picker pushes
     the disambiguate flag (CSI > 1 u) after entering the alternate screen
     and pops it (CSI < u) before leaving it, on quit (by a kitty Esc,
     CSI 27 u) and on attach; Esc and Ctrl-C with Caps or Num Lock on
     (CSI 27;65u, 27;129u, 99;69u) quit too (#137); the attached
     session's own push comes back after the pop and nothing pops it
     again; History hints Shift+Enter/o;
 14. a name starting with '-' (#162), as an older dch made it: on a live
     row X kills (`-k -- <name>`) and r says what works; on a History row
     p pins and unpins, d removes (`--` before the name), Enter says it is
     not resumed;
 15. the -k and -d pickers' hints name what Enter does there, kill or
     detach, never attach (QA 1.59.0 LOW-V8), the filter's too; so do their
     status lines: r on a '-' name says what Enter does there, a on a
     question form sends the user to dch -l, never to Enter (#247);
 16. dch -l has no title row (#252): the tabs row is the first, the
     session count and dch version at its right edge, the frame fills the
     screen; at 40 columns that text gives way, the tabs stay whole; the -k
     and -d pickers keep "kill session:" / "detach session:" with the count
     on that row;
 17. session groups (#544): `group = <name> = <regex>` lines make a tab
     each, in their order, between Sessions and History, a session in the
     first that matches; they win over sidekick_pattern; a bad line is
     skipped, the rest still apply; a group no session matches has no tab;
     at most 8; with none, sidekick_pattern (default -qa) is Sidekicks, and
     `sidekick_pattern =` empties the list; dch --config keeps the lines.
 18. bare dch, no arguments, opens the same picker as dch -l (#590).

Run:  python3 tests/picker_test.py            (uses ./dch)
      DCH=/path/to/dch python3 tests/picker_test.py
"""
import errno, json, os, pty, re, shutil, signal, subprocess, sys, tempfile, time
from select import select as fdselect

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
ROOT = tempfile.mkdtemp(prefix="dchpk", dir=os.environ.get("DCH_TEST_TMP", "/tmp"))
SOCKDIR = os.path.join(ROOT, "s")
ENV = dict(os.environ, DCH_SOCKET_DIR=SOCKDIR,
           XDG_STATE_HOME=os.path.join(ROOT, "state"),
           HOME=os.path.join(ROOT, "home"),
           CLAUDE_CONFIG_DIR=os.path.join(ROOT, "home", ".claude"),
           DCH_CONFIG=os.path.join(ROOT, "config"), DCH_LOGIN_ENV="0")
for k in ("DCH_SESSION", "MallocStackLogging", "MallocStackLoggingNoCompact",
          "NO_COLOR", "COLORTERM", "COLORFGBG"):
    ENV.pop(k, None)
os.makedirs(os.path.join(ENV["CLAUDE_CONFIG_DIR"], "sessions"))
RULE = "\u2500" * 60
PERM = [RULE, " Bash command", "", "   rm -rf build", "   Clean the build tree",
        "", " Do you want to proceed?", " \u276f 1. Yes",
        "   2. No, and tell Claude what to do differently (esc)", "",
        " Esc to cancel \u00b7 Tab to amend"]
FORM = [RULE, " \u2610 Proceed", "", "Do you want to proceed?", "",
        "\u276f 1. Yes", "  2. No", "", "Enter to select \u00b7 Esc to cancel"]
COLOR_RE = re.compile(r"\x1b\[(?:[0-9;]*;)?3[0-9][;m]")
# any foreground or background colour, or reverse video
ANYCOLOR_RE = re.compile(r"\x1b\[(?:[0-9;]*;)?(?:[34][0-9]|9[0-7]|10[0-7]|7)[;m]")
SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
ACCENT_TC, BAR_TC = "38;2;217;119;87", "48;2;38;44;56"
STATES = ("Needs input", "Working", "Done", "Idle")
procs = []
failures = []


def dch(*args):
    return subprocess.run([DCH] + list(args), env=ENV, capture_output=True,
                          text=True, timeout=60)


def check(what, ok, *extra):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)
        for e in extra:
            print("     " + repr(e))


def screen(name, ansi=False):
    return dch("--read", name, *(["--ansi"] if ansi else [])).stdout


def wait_screen(name, pred, timeout=10.0):
    end = time.time() + timeout
    s = ""
    while time.time() < end:
        s = screen(name)
        if pred(s):
            return s
        time.sleep(0.2)
    return s


def live():
    return dch("-ls").stdout.split("\n")


def wait_live(name, present, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if any(l == name or l.endswith("(%s)" % name) for l in live()) \
                == present:
            return True
        time.sleep(0.2)
    return False


ROW_RE = re.compile("^([>\u258c ]) . (\\S+)")  # marker, glyph or pin, title
RULE_RE = re.compile("^[\u2500-]{20,}$")



def below_tabs(s):
    """The lines under the tabs row and the column headers: the tabs row
    is the first on dch -l and the switch picker (#252), the second under
    -k's and -d's title."""
    ls = s.split("\n")
    i = next((k for k, l in enumerate(ls[:2])
              if re.match(r"[\[ ]?(Sessions|Settings)\b", l)), 1)
    return ls[i + 2:]

def body(s):
    """The list's lines: below the title, tabs and column headers, above
    the rule."""
    out = []
    for l in below_tabs(s):
        if RULE_RE.match(l):
            break
        out.append(l)
    return out


def footer(s):
    """The key hint lines: below the rule and the status line."""
    ls = s.split("\n")
    for i, l in enumerate(ls):
        if RULE_RE.match(l):
            return [x for x in ls[i + 2:] if x.strip()]
    return []


def rows(s):
    """Row titles in drawn order."""
    return [m.group(2) for m in map(ROW_RE.match, body(s)) if m]


def current(s):
    for m in map(ROW_RE.match, body(s)):
        if m and m.group(1) != " ":
            return m.group(2)
    return None


def sgr_before(line, text):
    """The SGR parameters in force where text starts on an --ansi line."""
    i = line.find(text)
    if i < 0:
        return None
    on = []
    for m in SGR_RE.finditer(line[:i]):
        ps = m.group(1)
        on = [] if ps in ("", "0") else on + [ps]
    return ";".join(on)


def harness(session, title):
    """A stand-in Claude Code titled `title` inside `session`: a live
    process carrying DCH_SESSION (our own dch: macOS hides a platform
    binary's env) and its ~/.claude/sessions record."""
    pr = subprocess.Popen([DCH, "--wait", session, "--state", "done",
                           "--timeout", "120000"],
                          env=dict(ENV, DCH_SESSION=session),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(pr)
    with open(os.path.join(ENV["CLAUDE_CONFIG_DIR"], "sessions",
                           "%d.json" % pr.pid), "w") as f:
        json.dump({"pid": pr.pid, "sessionId": "x", "cwd": "/",
                   "name": title}, f, separators=(",", ":"))


def select(pk, name, timeout=20.0):
    """Move the selection to the row titled `name`."""
    end = time.time() + timeout
    while time.time() < end:
        s = screen(pk)
        r, cur = rows(s), current(s)
        if cur == name:
            return True
        if cur in r and name in r:
            # one step, then wait for it: keys sent ahead of a slow picker
            # overshoot and the loop chases its own moves (#154)
            dch("--keys", pk, "down" if r.index(name) > r.index(cur) else "up")
            wait_screen(pk, lambda s: current(s) != cur, 3.0)
        else:
            time.sleep(0.3)
    return False


def to_tab(pk, label):
    for _ in range(3):
        s = screen(pk)
        if "[%s" % label in s:
            return s
        dch("--keys", pk, "right")
        time.sleep(0.3)
    return wait_screen(pk, lambda s: "[%s" % label in s)


def config():
    try:
        with open(ENV["DCH_CONFIG"]) as f:
            return f.read()
    except OSError:
        return ""

def picker(name, size, *env):
    r = dch("--spawn", name, "--size", size, "env", *env, DCH, "-l")
    return r.returncode == 0


def drain(fd, timeout, until=None):
    out = b""
    end = time.time() + timeout
    while time.time() < end and not (until and until in out):
        if fdselect([fd], [], [], 0.1)[0]:
            try:
                chunk = os.read(fd, 65536)
            except OSError as e:
                if e.errno in (errno.EIO, errno.EAGAIN):
                    break
                raise
            if not chunk:
                break
            out += chunk
    return out


def pty_picker():
    pid, fd = pty.fork()
    if pid == 0:
        e = dict(ENV, TERM="xterm-256color", NO_COLOR="1",
                 DCH_DOUBLE_TAP_MS="0")
        os.execve(DCH, [DCH, "-l"], e)
        os._exit(127)
    # inheritable by default: the next pty_picker's child would hold it open
    os.set_inheritable(fd, False)
    return pid, fd


def reap(pid, fd, secs=5):
    """Drain fd until pid exits or secs pass, close fd, then SIGKILL and
    reap a pid still running (the fd first, #215). True if it exited."""
    end, done = time.time() + secs, 0
    while time.time() < end and not done:
        drain(fd, 0.2)
        done = os.waitpid(pid, os.WNOHANG)[0]
    os.close(fd)
    if not done:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    return bool(done)


def kitty():
    """13. The picker's kitty push and pop, as bytes on a pty (#132)."""
    PUSH, POP = b"\x1b[>1u", b"\x1b[<u"
    ALT_ON, ALT_OFF = b"\x1b[?1049h", b"\x1b[?1049l"
    dch("--spawn", "zkbd", "--size", "80x24", "sh", "-c",
        "printf '\\033[>3u'; echo ZKBD_SCREEN; sleep 600")
    wait_screen("zkbd", lambda s: "ZKBD_SCREEN" in s)

    pid, fd = pty_picker()
    out = drain(fd, 10, b"q quit")
    on = out.find(ALT_ON)
    check("the picker pushes the kitty flag on the alternate screen",
          on >= 0 and out.find(PUSH, on) > on, out[:200])
    os.write(fd, b"\x1b[27u")  # kitty's Esc: quit
    out += drain(fd, 5)
    reap(pid, fd)
    off = out.rfind(ALT_OFF)
    check("a kitty Esc quits; the flag is popped before 1049l",
          off > 0 and out.rfind(POP, 0, off) > out.rfind(PUSH),
          out[-120:])

    # Caps Lock (64) and Num Lock (128) ride in the mods (#137)
    for key, what in ((b"\x1b[27;65u", "Caps Lock Esc"),
                      (b"\x1b[27;129u", "Num Lock Esc"),
                      (b"\x1b[99;69u", "Caps Lock Ctrl-C")):
        pid, fd = pty_picker()
        drain(fd, 10, b"q quit")
        os.write(fd, key)
        done = reap(pid, fd)
        check("a kitty %s (%r) quits the picker" % (what, key), done, key)

    pid, fd = pty_picker()
    out = drain(fd, 10, b"q quit")
    os.write(fd, b"/zkbd")
    out += drain(fd, 3, b"/zkbd")
    os.write(fd, b"\r")
    out += drain(fd, 10, b"ZKBD_SCREEN")
    out += drain(fd, 1)
    os.write(fd, b"\x1c")  # DCH_DOUBLE_TAP_MS=0: one tap detaches
    drain(fd, 5)
    reap(pid, fd)
    off = out.find(ALT_OFF, out.find(PUSH))
    after = out[off:] if off > 0 else b""
    check("on attach the flag is popped before the picker leaves 1049h",
          off > 0 and out.rfind(POP, 0, off) > out.find(PUSH), out[-300:])
    check("the session's own push is replayed, the picker's never",
          b"ZKBD_SCREEN" in after and b"\x1b[>3u" in after and
          PUSH not in after, after)
    check("... and nothing pops the session's push after it",
          after.find(b"\x1b[<", after.rfind(b"\x1b[>3u")) < 0, after)
    dch("-k", "zkbd")


def dashname():
    """14. Rows named with a leading '-' (#162). No dch makes such a name
    now, so the live one is a master started directly and the History one
    a record written by hand, as an older dch left them."""
    rec = os.path.join(ROOT, "state", "dch", "sessions",
                       SOCKDIR.replace("/", "-"))
    os.makedirs(rec, mode=0o700, exist_ok=True)
    now = int(time.time())
    hf = os.path.join(rec, "-h.json")
    with os.fdopen(os.open(hf, os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
        json.dump(dict(name="-h", sock_dir=SOCKDIR, alias="", cwd=os.getcwd(),
                       argv=["/bin/zsh"], started=now - 660, updated=now - 60,
                       ended=now - 60, end="exit 0", agent=None), f,
                  separators=(",", ":"))
    procs.append(subprocess.Popen(
        [DCH, "--master-of", os.path.join(SOCKDIR, "-x.sock"), "--",
         "sleep", "600"], env=ENV, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True))
    check("a live session named -x is up", wait_live("-x", True), live())
    picker("pk9", "100x30", "NO_COLOR=1", "TERM=xterm-256color")
    wait_screen("pk9", lambda s: "-x" in rows(s))
    check("select -x", select("pk9", "-x"), screen("pk9"))
    dch("--keys", "pk9", "r")
    s = wait_screen("pk9", lambda s: "Enter attaches, X kills" in s)
    check("r on -x says what works",
          "-x: a name starting with '-'; Enter attaches, X kills" in s, s)
    dch("--keys", "pk9", "X")
    s = wait_screen("pk9", lambda s: "Kill -x? [y/N]" in s)
    check("X on -x asks", "Kill -x? [y/N]" in s, s)
    dch("--keys", "pk9", "y")
    check("y kills -x", wait_live("-x", False), live())

    s = to_tab("pk9", "History")
    wait_screen("pk9", lambda s: "-h" in rows(s))
    check("select -h in History", select("pk9", "-h"), screen("pk9"))
    dch("--keys", "pk9", "enter")
    s = wait_screen("pk9", lambda s: "can't be resumed" in s)
    check("Enter on -h: not resumed, p and d named",
          "-h: a name starting with '-' can't be resumed; p pins, d removes"
          in s, s)
    pin = os.path.join(rec, "-h.pin")
    dch("--keys", "pk9", "p")
    s = wait_screen("pk9", lambda s: "-h: pinned" in s)
    check("p pins -h", os.path.exists(pin), s)
    dch("--keys", "pk9", "p")
    s = wait_screen("pk9", lambda s: "-h: unpinned" in s)
    check("p again unpins -h", not os.path.exists(pin), s)
    dch("--keys", "pk9", "d")
    s = wait_screen("pk9", lambda s: "Remove -h from History? [y/N]" in s)
    check("d asks to remove -h", "Remove -h from History? [y/N]" in s, s)
    dch("--keys", "pk9", "y")
    s = wait_screen("pk9", lambda s: "-h" not in rows(s))
    check("y removes -h: record gone, row gone",
          not os.path.exists(hf) and "-h" not in rows(s), s)
    dch("--keys", "pk9", "q")
    wait_live("pk9", False)


ZW_RE = re.compile("[\u200b-\u200d\u2060-\u2064\ufeff\u05b8\u05b9\u05c1"
                   "\u094d\u0947\u20d0\u20d1\u180e\u206a\u206f\u115f"
                   "\u1160\u3164]")
WIDE_RE = re.compile("[\U00016ff0\U00016ff1]")


def zerowidth():
    """15. A name holding a zero-width format character (U+FEFF, U+2060)
    takes no cell, so its row pads to the same columns as any other
    (#225): the state starts under STATE. So do combining marks past
    U+036F (Hebrew points, Devanagari signs, U+20D0), the format
    characters U+180E and U+206A-206F, and the Hangul fillers U+115F,
    U+1160 and U+3164, none drawn in a cell by Ghostty (QA 1.60.0
    LOW-V2). U+16FF0-16FF1 (wide Mc) take two (QA 1.63.0 NIT-V8)."""
    names = {"zwbom": "zw\ufeffbom", "zwj": "zw\u2060wj",
             "zwheb": "zw\u05e9\u05b8\u05c1\u05dc\u05d5\u05b9\u05dd",
             "zwdev": "zw\u0928\u092e\u0938\u094d\u0924\u0947",
             "zwsym": "zwa\u20d0\u20d1b",
             "zwfmt": "zw\u180ex\u206ay\u206fz",
             "zwhf": "zw\u115fx\u1160y\u3164z",
             "zwmc": "zw\U00016ff0x\U00016ff1y"}
    for n, alias in names.items():
        dch("--spawn", n, "--size", "80x24", "sh", "-c", "sleep 600")
        dch("-m", n, alias)
    picker("pk10", "100x30", "NO_COLOR=1", "TERM=xterm-256color")
    s = wait_screen("pk10", lambda s: all(a in rows(s)
                                          for a in names.values()))
    cell = lambda l, i: (i - len(ZW_RE.findall(l[:i])) +
                         len(WIDE_RE.findall(l[:i])))
    hdr = [l for l in s.split("\n") if re.match(r"^ {4}NAME +STATE", l)]
    col = cell(hdr[0], hdr[0].find("STATE")) if hdr else -1
    for n, alias in names.items():
        rl = [l for l in body(s) if alias in l]
        check("U+%04X in a name: the row's state starts under STATE"
              % ord((ZW_RE.search(alias) or WIDE_RE.search(alias)).group()),
              len(rl) == 1 and cell(rl[0], rl[0].find("Idle")) == col,
              col, rl)
    dch("--keys", "pk10", "q")
    wait_live("pk10", False)


def verbs():
    """15. dch -k and dch -d with no name: Enter kills or detaches the
    row, and the hints say so (QA 1.59.0 LOW-V8); the status lines never
    say Enter attaches (#247)."""
    f = os.path.join(ROOT, "vform.txt")
    with open(f, "w") as fh:
        fh.write("\n".join(FORM) + "\n")
    dch("--spawn", "vform", "--size", "100x30", "sh", "-c",
        "cat '%s'; sleep 600" % f)
    procs.append(subprocess.Popen(
        [DCH, "--master-of", os.path.join(SOCKDIR, "-v.sock"), "--",
         "sleep", "600"], env=ENV, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True))
    check("a live session named -v is up", wait_live("-v", True), live())
    for pk, flag, verb, dash in (
            ("pk11", "-k", "kill", "Enter kills, X asks first"),
            ("pk12", "-d", "detach", "Enter detaches, X kills")):
        dch("--spawn", pk, "--size", "200x30", "env", "NO_COLOR=1",
            "TERM=xterm-256color", DCH, flag)
        s = wait_screen(pk, lambda s: "q quit" in s)
        l = s.split("\n")
        check("dch %s: keeps its title row, the count and version on it, "
              "the tabs below (#252)" % flag,
              l[0].startswith(verb + " session:") and
              " dch " in l[0] and l[1].startswith("[Sessions "), l[:3])
        dch("--keys", pk, "/")
        f = wait_screen(pk, lambda s: "esc clear" in s)
        k, fk = " ".join(footer(s)), " ".join(footer(f))
        check("dch %s: the hints say \u21b5 %s, not attach, the filter's "
              "too" % (flag, verb), "\u21b5 %s " % verb in k and "\u21b5 %s "
              % verb in fk and "attach" not in k + fk, k, fk)
        dch("--keys", pk, "esc")
        wait_screen(pk, lambda s: "q quit" in s)
        check("dch %s: select -v" % flag, select(pk, "-v"), screen(pk))
        dch("--keys", pk, "r")
        s = wait_screen(pk, lambda s: "-v: a name starting" in s)
        check("dch %s: r on -v says %s" % (flag, dash),
              "-v: a name starting with '-'; " + dash in s and
              "Enter attaches" not in s, s)
        wait_screen(pk, lambda s: any("vform" in l and "Needs input" in l
                                      for l in body(s)))
        check("dch %s: select vform" % flag, select(pk, "vform"), screen(pk))
        dch("--keys", pk, "a")
        s = wait_screen(pk, lambda s: "not a plain Yes/No" in s)
        check("dch %s: a on a question form sends to dch -l, not Enter"
              % flag, "vform: not a plain Yes/No permission prompt; answer "
              "it from dch -l" in s and "(Enter)" not in s, s)
        dch("--keys", pk, "q")
        wait_live(pk, False)
    check("-v and vform outlive both pickers",
          wait_live("-v", True) and wait_live("vform", True), live())
    dch("-k", "vform")
    dch("-k", "--", "-v")


def footshells():
    """17. The footer counts as Prosper's does, "5/13 active · 🤖 2 · dch X"
    (the sessions working or needing input, over all of them; the agents
    summed, left out at 0; "- 2 agents -" without UTF-8), and a SHELLS
    column shows once a session has a live background shell and hides
    again when none has."""
    ver = dch("--version").stdout.split()[-1]
    first = lambda s: s.split("\n")[0].rstrip()
    ls = lambda: json.loads(dch("--ls-json").stdout)
    picker("pkf0", "100x30", "NO_COLOR=1", "TERM=xterm-256color")
    s = wait_screen("pkf0", lambda s: "active" in first(s))
    check("footer: no agents, no robot; no shells, no SHELLS column",
          "\U0001f916" not in first(s) and "agents" not in first(s) and
          "SHELLS" not in s, s)
    dch("--keys", "pkf0", "q")
    dch("--spawn", "pkagents", "--size", "80x24", "sh", "-c",
        "printf '\\342\\217\\272 main\\n\\342\\227\\257 general-purpose  a\\n"
        "\\342\\227\\257 general-purpose  b\\n'; sleep 600")
    wait_live("pkagents", True)
    picker("pkf1", "100x30", "NO_COLOR=1", "TERM=xterm-256color")
    s = wait_screen("pkf1", lambda s: "\U0001f916 2" in first(s))
    # states move under us: the same footer as a --ls-json just read
    end = time.time() + 15
    while True:
        j = ls()
        busy = sum(r["state"] in ("working", "blocked") for r in j)
        want = "%d/%d active \u00b7 \U0001f916 2 \u00b7 dch %s" % (
            busy, len(j), ver)
        s = wait_screen("pkf1", lambda s: first(s).endswith(want), 1.0)
        if first(s).endswith(want) or time.time() > end:
            break
    check("footer: busy/total active, the agents summed, the robot 2 cells "
          "wide, flush right (%s)" % want, first(s).endswith(want) and
          busy > 0 and len(first(s)) == 99, first(s))
    dch("--keys", "pkf1", "q")
    picker("pkf2", "100x30", "NO_COLOR=1", "TERM=dumb")
    s = wait_screen("pkf2", lambda s: "agents" in first(s))
    check("footer without UTF-8: '- 2 agents -'", re.search(
          r"\d+/\d+ active - 2 agents - dch " + re.escape(ver) + "$",
          first(s)), first(s))
    dch("--keys", "pkf2", "q")
    # a Claude with two background shells, as transcript_test fakes one: the
    # transcript's result line is what marks a shell background (#557)
    # mon: commands run as Monitor tasks (#725), the transcript naming each;
    # fg: the same wrapper with no Monitor call (a foreground Bash call)
    def fake_claude(name, sid, shs, bg, mon=(), fg=()):
        d = os.path.join(ROOT, "claude-%d" % os.getuid(), "-p", sid, "tasks")
        os.makedirs(d)
        pd = os.path.join(ENV["CLAUDE_CONFIG_DIR"], "projects", "-p")
        os.makedirs(pd, exist_ok=True)
        iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))
        with open(os.path.join(pd, sid + ".jsonl"), "w") as f:
            for k in bg:
                f.write('{"type":"user","toolUseResult":{"backgroundTaskId":'
                        '"%s"}}\n' % k)
            for k, c in enumerate(mon):
                f.write(json.dumps({"type": "assistant", "timestamp": iso(
                    time.time() - 1), "message": {"content": [{
                        "type": "tool_use", "id": "toolu_m%d" % k,
                        "name": "Monitor", "input": {"command": c}}]}},
                    separators=(",", ":")) + "\n")
                f.write(json.dumps({"type": "user", "timestamp": iso(
                    time.time() + 5), "message": {"content": [{
                        "type": "tool_result", "tool_use_id": "toolu_m%d" % k,
                        "content": "Monitor started (task bm%d, timeout "
                                   "1000ms)." % k}]}},
                    separators=(",", ":")) + "\n")
        wrap = ["true && eval '%s' < /dev/null && pwd -P >| /dev/null" % c
                for c in tuple(mon) + tuple(fg)]
        dch("--spawn", name, "--size", "80x24", sys.executable, "-c",
            "import json,os,subprocess as sp,time\n"
            "p=os.getpid()\n"
            "st=sp.run(['/bin/ps','-o','lstart=','-p',str(p)],"
            "capture_output=True,text=True,"
            "env={'LC_ALL':'C','TZ':'UTC'}).stdout.strip()\n"
            "json.dump({'pid':p,'sessionId':%r,'cwd':'/','procStart':st,"
            "'version':'2.1.0','kind':'interactive','status':'idle',"
            "'statusUpdatedAt':1},open(%r%%p,'w'))\n"
            "for k in %r:\n"
            "    sp.Popen(['sleep','600'],stdout=open(%r+'/'+k+'.output','ab'),"
            "stdin=sp.DEVNULL)\n"
            "for w in %r:\n"
            "    sp.Popen(['/bin/sh','-c',w],stdout=sp.DEVNULL,"
            "stdin=sp.DEVNULL)\n"
            "time.sleep(600)\n" % (sid, os.path.join(
                ENV["CLAUDE_CONFIG_DIR"], "sessions", "%d.json"), shs, d,
                wrap))
    fake_claude("pkshell", "53353353-3533-4533-8533-533533533533",
                ("b1", "b2"), ("b1", "b2"))
    fake_claude("pkfg", "53353353-3533-4533-8533-533533533534",
                ("f1", "b3"), ("b3",))
    picker("pkf3", "100x30", "NO_COLOR=1", "TERM=xterm-256color")
    def shells(s):
        hdr = next((l for l in s.split("\n") if "SHELLS" in l), "")
        cell = lambda l: l[hdr.find("SHELLS"):hdr.find("SHELLS") + 6].strip()
        return hdr, {m.group(2): cell(l) for l in body(s)
                     for m in [ROW_RE.match(l)] if m}
    # the column shows once the first shell is live: wait for the second
    s = wait_screen("pkf3", lambda s: shells(s)[1].get("pkshell") == "2")
    hdr, got = shells(s)
    check("SHELLS column shows beside AGENTS: 2 on the row with two live "
          "shells, blank on the rest (%s)" % got,
          hdr.find("AGENTS") < hdr.find("SHELLS") and
          got.get("pkshell") == "2" and
          set(k for k, v in got.items() if v) <= {"pkshell", "pkfg"},
          hdr, s)
    s = wait_screen("pkf3", lambda s: shells(s)[1].get("pkfg") == "1")
    check("#557: SHELLS counts background shells only: 1 for a foreground "
          "and a background shell (%s)" % shells(s)[1],
          shells(s)[1].get("pkfg") == "1", s)
    # a shell with no result line yet is unknown, not foreground: asked again
    # after 10 s, so a line landing after the first draw still counts (20 s:
    # room for a loaded runner; mretry never asks again)
    fake_claude("pkrl", "53353353-3533-4533-8533-533533533535",
                ("l1",), ())
    s = wait_screen("pkf3", lambda s: "pkrl" in shells(s)[1])
    first_draw = shells(s)[1].get("pkrl")
    with open(os.path.join(ENV["CLAUDE_CONFIG_DIR"], "projects", "-p",
              "53353353-3533-4533-8533-533533533535.jsonl"), "a") as f:
        f.write('{"type":"user","toolUseResult":{"backgroundTaskId":'
                '"l1"}}\n')
    s = wait_screen("pkf3", lambda s: shells(s)[1].get("pkrl") == "1", 20.0)
    check("#557: a shell whose result line lands after the first draw is "
          "counted within 20 s (first draw %r, then %s)" %
          (first_draw, shells(s)[1]), not first_draw and
          shells(s)[1].get("pkrl") == "1", s)
    # a Monitor task counts as a background shell; a foreground-style child
    # of the same wrapper that no Monitor call names does not (#725)
    fake_claude("pkmon", "53353353-3533-4533-8533-533533533536", ("b5",),
                ("b5",), mon=("sleep 600.0725",), fg=("sleep 601.0725",))
    s = wait_screen("pkf3", lambda s: shells(s)[1].get("pkmon") == "2")
    check("#725: SHELLS counts a monitor beside a background shell, not a "
          "foreground-style child: 2 (%s)" % shells(s)[1],
          shells(s)[1].get("pkmon") == "2", s)
    dch("-k", "pkmon")
    dch("-k", "pkrl")
    dch("-k", "pkshell")
    dch("-k", "pkfg")
    # #556: a stopped master holds only the screen queries, not a third
    dch("--spawn", "pkstop", "--size", "80x24", "sh", "-c", "sleep 600")
    wait_live("pkstop", True)
    mp = [int(l.split()[0]) for l in subprocess.run(
        ["/bin/ps", "-axo", "pid=,command="], capture_output=True,
        text=True).stdout.splitlines() if "--master-of" in l and
        "/pkstop.sock" in l and ROOT in l]
    os.kill(mp[0], signal.SIGSTOP)
    try:
        t0 = time.time()
        picker("pkf4", "100x30", "NO_COLOR=1", "TERM=xterm-256color")
        s = wait_screen("pkf4", lambda s: " dch " in first(s), 20.0)
        dt = time.time() - t0
    finally:
        os.kill(mp[0], signal.SIGCONT)
    # 1.118 drew in 1.1 s (two 0.5 s screen queries); a third, 2 s STAT
    # query made it 3.1 s
    check("#556: a stopped master's session costs no shells query: the "
          "first draw in under 2.4 s (%.1f s)" % dt,
          len(mp) == 1 and " dch " in first(s) and dt < 2.4, s)
    dch("--keys", "pkf4", "q")
    dch("-k", "pkstop")
    s = wait_screen("pkf3", lambda s: "SHELLS" not in s, 15.0)
    check("SHELLS column hides when no row has a shell", "SHELLS" not in s, s)
    dch("--keys", "pkf3", "q")
    dch("-k", "pkagents")
    # #558: agents on a group tab count in the footer, as Sessions' do
    with open(ENV["DCH_CONFIG"], "w") as f:
        f.write("group = QA = -qa\n")
    dch("--spawn", "pkag-qa", "--size", "80x24", "sh", "-c",
        "printf '\\342\\217\\272 main\\n\\342\\227\\257 general-purpose  a\\n"
        "\\342\\227\\257 general-purpose  b\\n'; sleep 600")
    wait_live("pkag-qa", True)
    picker("pkf5", "100x30", "NO_COLOR=1", "TERM=xterm-256color")
    s = wait_screen("pkf5", lambda s: "\U0001f916 2" in first(s))
    check("#558: a group tab's 2 agents are in the footer's "
          "robot count", "QA" in first(s) and
          "\U0001f916 2" in first(s), first(s))
    dch("--keys", "pkf5", "q")
    dch("-k", "pkag-qa")


def groups():
    """17. session groups (#544) in one picker, the config rewritten under
    it (read again on every reload)."""
    def setcfg(text):
        with open(ENV["DCH_CONFIG"], "w") as f:
            f.write(text)

    def tabs(s):
        return re.findall(r"([A-Za-z][\w-]*) \d+",
                          s.split("\n")[0].split(" active")[0])

    dch("--spawn", "gr-api-qa", "--size", "80x24", "sh", "-c", "sleep 600")
    dch("--spawn", "gr-web", "--size", "80x24", "sh", "-c", "sleep 600")
    setcfg("sidekick_pattern = gr-\ngroup = API = gr-api\n"
           "group = Empty = ^no-such-session$\ngroup  =  QA  =  -qa \n")
    picker("pk13", "160x30", "NO_COLOR=1", "TERM=xterm-256color")
    s = wait_screen("pk13", lambda s: tabs(s)[1:3] == ["API", "QA"])
    check("#544: two group lines make two tabs named by them, in their "
          "order, between Sessions and History",
          tabs(s) == ["Sessions", "API", "QA", "History"], s.split("\n")[0])
    check("#544: group lines win over sidekick_pattern: no Sidekicks tab",
          "Sidekicks" not in s, s.split("\n")[0])
    check("#544: a group no session matches has no tab",
          tabs(s)[1:3] == ["API", "QA"] and "Empty" not in s,
          s.split("\n")[0])
    n = sum(int(x) for x in re.findall(
        r"(?:Sessions|API|QA) (\d+)", s.split("\n")[0]))
    check("#544: the footer's count is every live session, Sessions and "
          "every group's", re.search(r" \d+/%d active" % n, s.split("\n")[0]),
          n, s.split("\n")[0])
    s = to_tab("pk13", "API")
    check("#544: a session two groups match is on the first one's tab only",
          rows(s) == ["gr-api-qa"], s)
    s = to_tab("pk13", "QA")
    check("#544: the second group's tab has its own, not the first's",
          "[QA" in s and "helper-qa" in rows(s) and
          "gr-api-qa" not in rows(s), s)
    dch("--keys", "pk13", "right")
    s = wait_screen("pk13", lambda s: "[History" in s)
    dch("--keys", "pk13", "right")
    s = wait_screen("pk13", lambda s: "[Sessions" in s)
    check("#544: right goes QA, History, then back to Sessions",
          "[Sessions" in s, s.split("\n")[0])
    dch("--keys", "pk13", "left")
    s = wait_screen("pk13", lambda s: "[History" in s)
    dch("--keys", "pk13", "left")
    s = wait_screen("pk13", lambda s: "[QA" in s)
    check("#544: left from Sessions goes History, then the last group",
          "[QA" in s, s.split("\n")[0])

    # #714: a manual group (dch --group-set) moves a session to its tab,
    # ahead of every pattern; --auto puts it back by pattern
    r = dch("--group-set", "gr-web", "qa")
    s = wait_screen("pk13", lambda s: "gr-web" in rows(s))
    check("#714: --group-set gr-web qa: gr-web on the QA tab",
          r.returncode == 0 and "[QA" in s and "gr-web" in rows(s), s)
    r = dch("--group-set", "gr-api-qa", "QA")
    s = wait_screen("pk13", lambda s: "gr-api-qa" in rows(s))
    check("#714: a manual QA beats API's matching pattern: gr-api-qa on QA",
          "gr-api-qa" in rows(s) and "API" not in tabs(s), s)
    dch("--group-set", "gr-web", "--auto")
    dch("--group-set", "gr-api-qa", "--auto")
    s = wait_screen("pk13", lambda s: "API" in tabs(s) and
                    "gr-web" not in rows(s))
    check("#714: --auto: back by pattern, gr-web off QA, API's tab back",
          "gr-web" not in rows(s) and "gr-api-qa" not in rows(s) and
          "API" in tabs(s), s)
    s = to_tab("pk13", "Sessions")
    check("#714: ... and gr-web on Sessions again", "gr-web" in rows(s), s)
    s = to_tab("pk13", "QA")

    # a bad line is skipped, the later ones still count
    setcfg("group = %s = gr-api\ngroup = Long = (gr-api|%s)\n"
           "group = = gr-api\ngroup = gr-api\n"
           "group = E\x1b[31m = gr-api\n"
           "group = Empty = ^no-such-session$\ngroup = QA = -qa\n"
           % ("N" * 33, "x" * 250))
    s = wait_screen("pk13", lambda s: tabs(s) == ["Sessions", "QA",
                                                  "History"])
    check("#544: bad group lines (a name over 32 bytes, a regex over 255, "
          "no name, no second =, a control character in the name) are skipped, the next applies",
          tabs(s) == ["Sessions", "QA", "History"], s.split("\n")[0])
    s = to_tab("pk13", "QA")
    check("#544: ... and the skipped lines' sessions go to the next match",
          "gr-api-qa" in rows(s) and "helper-qa" in rows(s), s)

    # a group named like a built-in tab, or like an earlier group, is skipped
    setcfg("group = HISTORY = gr-api\ngroup = sessions = gr-web\n"
           "group = Dup = gr-api\ngroup = Dup = gr-web\n"
           "group = Other = -qa\n")
    s = wait_screen("pk13", lambda s: "Dup" in tabs(s))
    check("#568: groups named Sessions or History (any case) or a name an "
          "earlier group has are skipped: one tab each",
          tabs(s) == ["Sessions", "Dup", "Other", "History"],
          s.split("\n")[0])
    s = to_tab("pk13", "Sessions")
    check("#568: ... the skipped lines match nothing: gr-web stays on "
          "Sessions", "gr-web" in rows(s), s)
    s = to_tab("pk13", "Dup")
    check("#568: ... the first Dup line is the group's pattern",
          rows(s) == ["gr-api-qa"], s)

    # a repeat is compared ignoring case: the first spelling wins (#572)
    setcfg("group = Dup = gr-api\ngroup = dup = gr-web\n")
    s = wait_screen("pk13", lambda s: tabs(s) == ["Sessions", "Dup",
                                                  "History"])
    check("#572: group = Dup and group = dup (any case) give one Dup tab",
          tabs(s) == ["Sessions", "Dup", "History"], s.split("\n")[0])
    s = to_tab("pk13", "Sessions")
    check("#572: ... the lowercase repeat matches nothing: gr-web stays on "
          "Sessions", "gr-web" in rows(s), s)

    # only a whole-name repeat is skipped: a prefix or an extension keeps its tab
    setcfg("group = Dog = gr-api\ngroup = Do = gr-web\ngroup = Dogs = -qa\n")
    s = wait_screen("pk13", lambda s: "Dogs" in tabs(s))
    check("#572: group = Do and group = Dogs after group = Dog keep their "
          "own tabs (a prefix is not a repeat)",
          tabs(s) == ["Sessions", "Dog", "Do", "Dogs", "History"],
          s.split("\n")[0])

    # #721: an empty (or blank) regex is a manual-only group: it matches no
    # session by itself, --group-set places one, colour and rename work
    def grp(n):
        return {r["name"]: r.get("group") for r in
                json.loads(dch("--ls-json").stdout)}.get(n)

    for blank in ("", "   \t"):
        setcfg("group = Man =%s\ngroup_color = Man = #112233\n" % blank)
        r = dch("--group-set", "gr-web", "Man")
        s = wait_screen("pk13", lambda s: "Man" in tabs(s))
        check("#721: group = Man =%r: --group-set resolves it, Man tab "
              "holds only gr-web" % blank, r.returncode == 0 and
              grp("gr-web") == "Man" and grp("gr-api-qa") in (None, "") and
              tabs(s) == ["Sessions", "Man", "History"], r.stderr,
              s.split("\n")[0])
        s = to_tab("pk13", "Man")
        check("#721: ... an empty pattern captures no other session",
              rows(s) == ["gr-web"], s)
        dch("--group-set", "gr-web", "--auto")
        s = wait_screen("pk13", lambda s: "Man" not in tabs(s))
        check("#721: ... --auto empties it: no Man tab",
              tabs(s) == ["Sessions", "History"], s.split("\n")[0])
    setcfg("group = Man =\ngroup_color = Man = #112233\n")
    dch("--group-set", "gr-web", "Man")
    r = dch("--group-rename", "Man", "Hand")
    with open(ENV["DCH_CONFIG"]) as f:
        cfg = f.read()
    check("#721: --group-rename renames the empty-pattern line, colour "
          "line and the session's group", r.returncode == 0 and
          cfg == "group = Hand =\ngroup_color = Hand = #112233\n" and
          grp("gr-web") == "Hand", r.stdout, r.stderr, cfg)
    dch("--group-set", "gr-web", "--auto")

    # a regex every name matches is plain text: it takes no session (#569)
    setcfg("group = All = .*\n")
    s = wait_screen("pk13", lambda s: tabs(s) == ["Sessions", "History"])
    s = to_tab("pk13", "Sessions")
    check("#544: group = All = .* takes no session: no All tab, Sessions "
          "keeps them", tabs(s) == ["Sessions", "History"] and
          "gr-web" in rows(s), s.split("\n")[0])

    # a regex whose repeats multiply past 1024 copies is plain text (#616):
    # glibc's regcomp copies per {n,m} count, stacked + doubled into GBs
    # (Web's tab shows the picker has read this config)
    setcfg("group = Big = gr-api(.{0,99}){0,99}\ngroup = Web = gr-web\n")
    s = wait_screen("pk13", lambda s: "Web" in tabs(s))
    check("#616: group = Big = gr-api(.{0,99}){0,99} (9801 copies) is text: "
          "no Big tab", tabs(s) == ["Sessions", "Web", "History"],
          s.split("\n")[0])
    s = to_tab("pk13", "Sessions")
    check("#616: ... gr-api-qa stays on Sessions", "gr-api-qa" in rows(s), s)

    # {,m} is {0,m} to glibc, so its copies count too (#626). The pattern
    # matches gr-api-qa as a regex (the outer * takes zero repeats; macOS
    # reads {,99} as a literal, so the * is what matches), as text it does
    # not: macOS's regcomp never blows up, so no Big tab shows pk_rebig's
    # verdict was "text"
    setcfg("group = Big = gr-api(((a{,99}){,99}){,99})*-qa\ngroup = Docs = gr-web\n")
    s = wait_screen("pk13", lambda s: "Docs" in tabs(s))
    check("#626: group = Big = gr-api(((a{,99}){,99}){,99})*-qa is text: "
          "no Big tab", tabs(s) == ["Sessions", "Docs", "History"],
          s.split("\n")[0])
    s = to_tab("pk13", "Sessions")

    # every session in a group: the picker opens on it, not on empty Sessions
    setcfg("group = Everyone = .\n")
    picker("pk19", "160x30", "NO_COLOR=1", "TERM=xterm-256color")
    s = wait_screen("pk19", lambda s: "Everyone" in s)
    check("#544: every session in a group: the picker opens on that "
          "group's tab", "[Everyone" in s and "[Sessions" not in s,
          s.split("\n")[0])
    dch("-k", "pk19")

    # at most 8: a ninth line is ignored; a 32-byte name is whole
    big = "Web" + "x" * 29
    setcfg("".join("group = g%d = ^none-%d$\n" % (k, k) for k in range(7))
           + "group = %s = gr-web\ngroup = Nine = gr-api\n" % big)
    s = wait_screen("pk13", lambda s: big in tabs(s))
    check("#544: the eighth group, a 32-byte name, is a tab; the ninth "
          "line is ignored", tabs(s) == ["Sessions", big, "History"],
          s.split("\n")[0])
    s = to_tab("pk13", "Sessions")
    check("#544: ... so the ninth's session stays on Sessions",
          "gr-api-qa" in rows(s), s)

    # no group line: sidekick_pattern, default -qa, is Sidekicks
    setcfg("group = NoEq\npicker_sort = name\n")
    s = wait_screen("pk13", lambda s: "Sidekicks" in tabs(s))
    check("#544: no valid group line: the default -qa makes Sidekicks",
          tabs(s) == ["Sessions", "Sidekicks", "History"],
          s.split("\n")[0])
    s = to_tab("pk13", "Sidekicks")
    check("#544: ... listing the -qa sessions",
          "gr-api-qa" in rows(s) and "helper-qa" in rows(s) and
          "gr-web" not in rows(s), s)
    setcfg("sidekick_pattern = gr-web\n")
    s = wait_screen("pk13", lambda s: "Sidekicks 1" in s)
    check("#544: no group line: sidekick_pattern is the Sidekicks group",
          tabs(s) == ["Sessions", "Sidekicks", "History"] and
          "Sidekicks 1" in s, s.split("\n")[0])
    setcfg("sidekick_pattern =\n")
    s = wait_screen("pk13", lambda s: tabs(s) == ["Sessions", "History"])
    check("#544: no group line and an empty sidekick_pattern: no group tab",
          tabs(s) == ["Sessions", "History"], s.split("\n")[0])
    dch("-k", "pk13")

    # dch --config rewrites keep every group line, in order, repeats too
    lines = ["group = API = gr-api", "group = QA = -qa", "group = API = dup"]
    setcfg("# groups\n%s\nsidekick_pattern = -qa\n%s\n%s\n" % (
        lines[0], lines[1], lines[2]))
    r1 = dch("--config", "picker_sort", "name")
    r2 = dch("--config", "sidekick_pattern", "x")
    r3 = dch("--config", "sidekick_pattern", "--unset")
    c = config()
    check("#544: dch --config <key> <value> keeps every group line",
          r1.returncode == r2.returncode == r3.returncode == 0 and
          [l for l in c.split("\n") if l.startswith("group")] == lines and
          c.startswith("# groups\n"), c, r1.stderr, r2.stderr, r3.stderr)
    r = dch("--config", "group", "x")
    check("#544: group is not a --config key: dch --config group refuses",
          r.returncode != 0 and config() == c, r.stdout, r.stderr, config())


def groupcolor():
    """19. group_color = <name> = #rrggbb colours that group's tab (#715)."""
    def websgr(cfg, *env):
        with open(ENV["DCH_CONFIG"], "w") as f:
            f.write("group = Web = gc-web\ngroup = Other = gc-none\n" + cfg)
        dch("-k", "pk19c")
        picker("pk19c", "100x14", *env)
        wait_screen("pk19c", lambda s: "Web" in s)
        a = screen("pk19c", ansi=True).split("\n")[0]
        m = re.search(r"\x1b\[([0-9;]*)m Web", a)
        return m.group(1) if m else a

    dch("--spawn", "gc-web", "--size", "80x24", "sh", "-c", "sleep 600")
    tc = ("TERM=xterm-256color", "COLORTERM=truecolor")
    s = websgr("group_color = web =  #12ab56 \n", *tc)
    check("#715: a group_color line colours the tab (truecolor), name "
          "matched ignoring case", s == "38;2;18;171;86", s)
    s = websgr("group_color = Web = #12ab56\n", "TERM=xterm-256color")
    check("#715: 256 colours: the nearest cube entry", s == "38;5;35", s)
    s = websgr("group_color = Web = #808080\n", "TERM=xterm-256color")
    check("#715: 256 colours: a grey takes the grey ramp", s == "38;5;244", s)
    s = websgr("group_color = Web = #ff0000\n", "TERM=xterm")
    check("#715: basic colours: the nearest ANSI colour", s == "38;5;1", s)
    s = websgr("group_color = Web = #12ab56\n", "NO_COLOR=1", *tc)
    check("#715: NO_COLOR draws no colour", "38;" not in s and s != "38;2;18;171;86", s)
    for bad in ("#12ab5", "#12ab567", "12ab56", "#12ab5g", "red", "",
                "#12ab56 x"):
        s = websgr("group_color = Web = %s\n" % bad, *tc)
        check("#715: invalid colour %r ignored" % bad, "38;2;18" not in s, s)
    s = websgr("group_color = Nope = #12ab56\n", *tc)
    check("#715: an unknown group name is ignored", "38;2;18" not in s, s)
    s = websgr("group_color = Web = #111111\ngroup_color = WEB = #222222\n",
               *tc)
    check("#715: a repeated name: the last line wins",
          s == "38;2;34;34;34", s)
    s = websgr("group_color = Web = #111111\ngroup_color = Web = bad\n", *tc)
    check("#715: a later invalid line does not undo an earlier valid one",
          s == "38;2;17;17;17", s)
    dch("-k", "pk19c")
    dch("-k", "gc-web")


def groupscroll():
    """18. the tab bar keeps the selected tab in view (QA 1.124.0 LOW-VA1):
    8 groups of 14-byte names, Tab through every tab at narrow widths."""
    names = ["LongGroupName%d" % k for k in range(8)]
    with open(ENV["DCH_CONFIG"], "w") as f:
        f.write("".join("group = %s = ^nw%d$\n" % (n, k)
                        for k, n in enumerate(names)))
    for k in range(8):
        dch("--spawn", "nw%d" % k, "--size", "80x24", "sh", "-c",
            "sleep 600")
    for w in (40, 60, 80, 200):
        picker("pk18", "%dx14" % w, "NO_COLOR=1", "TERM=xterm-256color")
        s = wait_screen("pk18", lambda s: "[Sessions" in s)
        bad, hist, mk = [], False, []
        for tab in ["Sessions"] + names + ["History"]:
            s = wait_screen("pk18", lambda s: "[" + tab in s.split("\n")[0])
            l = s.split("\n")[0]
            if "[" + tab not in l:
                bad.append((tab, l))
            hist = hist or "[History" in l
            mk.append((tab, "\u2039" in l, "\u203a" in l))
            dch("--keys", "pk18", "tab")
        check("#567: %d cols: every tab, selected in turn, is on the tab "
              "line and bracketed" % w, not bad, bad)
        check("#567: %d cols: History is reachable and visible" % w, hist)
        if w == 200:
            s = wait_screen("pk18", lambda s: "[Sessions" in s)
            check("#567: 200 cols: all tabs fit, no markers, the line "
                  "unchanged", not any(a or b for _, a, b in mk) and
                  s.split("\n")[0].startswith("[Sessions ") and
                  "  LongGroupName0 1   LongGroupName1 1   LongGroupName2"
                  in s and "LongGroupName7 1   History " in s,
                  s.split("\n")[0])
        else:
            check("#567: %d cols: \u2039 only once tabs are cut on the "
                  "left, \u203a only while some are cut on the right" % w,
                  mk[0] == ("Sessions", False, True) and
                  mk[-1] == ("History", True, False), mk)
        dch("-k", "pk18")
    # 39 cols, tab 2 selected: the first-tab search counts the right marker
    picker("pk18", "39x14", "NO_COLOR=1", "TERM=xterm-256color")
    wait_screen("pk18", lambda s: "[Sessions" in s)
    dch("--keys", "pk18", "tab", "tab")
    s = wait_screen("pk18", lambda s: "[LongGroupName1" in s.split("\n")[0])
    check("#580: 39 cols, tab 2 selected: whole, line 0 ends with \u203a",
          "[LongGroupName1 1]" in s.split("\n")[0] and
          s.split("\n")[0].rstrip().endswith("\u203a"), s.split("\n")[0])
    dch("-k", "pk18")
    for k in range(8):
        dch("-k", "nw%d" % k)
    # a 32-char name (the cap) at 30 cols: the selected tab is cut, not
    # dropped, and keeps the cell of the right marker (NIT-VG1)
    with open(ENV["DCH_CONFIG"], "w") as f:
        f.write("group = %s = ^nw$\ngroup = Other = ^zz$\n" % ("W" * 32))
    dch("--spawn", "nw", "--size", "80x24", "sh", "-c", "sleep 600")
    picker("pk18", "30x14", "NO_COLOR=1", "TERM=xterm-256color")
    wait_screen("pk18", lambda s: "[Sessions" in s)
    dch("--keys", "pk18", "tab")
    s = wait_screen("pk18", lambda s: "[WWW" in s.split("\n")[0])
    l = s.split("\n")[0]
    check("#580: 30 cols, a 32-char group selected: its start and \u203a "
          "are on line 0", "[WWW" in l and "\u203a" in l, l)
    dch("-k", "pk18")
    dch("-k", "nw")


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL dch not executable at " + DCH)
        return 1
    dch("--spawn", "s2", "--size", "80x24", "sh", "-c", "sleep 600")
    dch("--spawn", "s10", "--size", "80x24", "sh", "-c",
        "while :; do echo tick; sleep 0.3; done")
    if dch("--read", "s2").returncode == 3:
        print("skip: no terminal mirror in this build")
        return 0

    # bare dch, no arguments, is dch -l (#590): the picker, not an attach
    dch("--spawn", "pk0", "--size", "100x30", "env", "NO_COLOR=1",
        "TERM=xterm-256color", DCH)
    s = wait_screen("pk0", lambda s: "q quit" in s)
    check("#590: bare dch opens the picker, as dch -l",
          s.startswith("[Sessions ") and "q quit" in s, s)
    dch("-k", "pk0")
    wait_live("pk0", False)

    # bare dch with a stdin that is no tty lists, it does not draw a picker
    # whose Enter could not attach (#593)
    dch("--spawn", "pk0n", "--size", "100x30", "sh", "-c",
        '"$0" </dev/null; echo RC=$?; sleep 600', DCH)
    s = wait_screen("pk0n", lambda s: "RC=" in s)
    check("#593: bare dch with stdin from /dev/null lists, rc 0, no picker",
          "RC=0" in s and "pk0n" in s and "[Sessions " not in s, s)
    dch("-k", "pk0n")
    wait_live("pk0n", False)
    dch("--forget", "pk0n")
    dch("--forget", "pk0")  # no History tab yet: the tab rows below cycle

    # 1. tabs: no Sidekicks tab until a "-qa" session exists
    picker("pk1", "100x30", "NO_COLOR=1", "TERM=xterm-256color")
    s = wait_screen("pk1", lambda s: "s10" in s and "s2" in s)
    check("picker opens with a Sessions tab",
          re.search(r"\[Sessions \d+\]", s), s)
    # 16. no title row on dch -l (#252): the tabs row is the first and
    # carries the count and version at its right edge; the frame still
    # fills the screen (one list row more)
    ver = dch("--version").stdout.split()[-1]
    lines = s.split("\n")
    check("#252: no 'attach session'/'switch to session' title",
          "attach session" not in s and "switch to session" not in s, s)
    check("#252: the first row is the tabs, the count and dch version "
          "at its right edge",
          lines[0].startswith("[Sessions ") and re.search(
              r"\d+/\d+ active \u00b7 dch " + re.escape(ver) + "$",
              lines[0].rstrip()) and lines[0].rstrip().endswith(ver) and
          len(lines[0].rstrip()) == 100, lines[:2])
    check("#252: the frame fills all 30 rows, the hints on the last",
          len(lines) >= 30 and "q quit" in lines[29], s)
    check("no Sidekicks tab without a matching session",
          "Sidekicks" not in s, s)
    dch("--spawn", "helper-qa", "--size", "80x24", "sh", "-c", "sleep 600")
    s = wait_screen("pk1", lambda s: "Sidekicks 1" in s)
    check("Sidekicks 1 appears live", "Sidekicks 1" in s, s)
    check("a sidekick is not in Sessions", "helper-qa" not in rows(s), s)
    check("Sessions: column headers",
          re.search(r"^ {4}NAME +STATE +ACTIVE\b", s, re.M), s)
    dch("--keys", "pk1", "tab")
    s = wait_screen("pk1", lambda s: "[Sidekicks 1]" in s)
    check("tab switches to Sidekicks, listing the sidekick only",
          rows(s) == ["helper-qa"], s)
    check("Sidekicks: column headers",
          re.search(r"^ {4}NAME +STATE\b", s, re.M), s)

    # 11. the pattern matches the harness title the row shows, too: a4k-2
    # is "kodi-qa" only to Claude
    dch("--spawn", "a4k-2", "--size", "80x24", "sh", "-c", "sleep 600")
    harness("a4k-2", "kodi-qa")
    s = wait_screen("pk1", lambda s: "[Sidekicks 2]" in s and
                    "kodi-qa" in rows(s))
    check("a harness title matching sidekick_pattern makes a sidekick",
          sorted(rows(s)) == ["helper-qa", "kodi-qa"], s)
    dch("--keys", "pk1", "tab")
    s = wait_screen("pk1", lambda s: "[Sessions" in s)
    check("... and keeps it out of Sessions",
          "kodi-qa" not in rows(s) and "a4k-2" not in rows(s), s)

    # 2. sort: activity puts the working s10 first; name puts s2 before s10
    s = wait_screen("pk1", lambda s: "s sort activity" in s and
                    "s10" in rows(s) and "s2" in rows(s) and
                    rows(s).index("s10") < rows(s).index("s2"))
    check("activity sort: working s10 above idle s2",
          rows(s).index("s10") < rows(s).index("s2"), s)
    check("states are labelled", "Working" in s and "Idle" in s, s)
    xs = {l.find(w) for l in body(s) for w in STATES if w in l}
    head = [l for l in s.split("\n") if l.startswith("    NAME")]
    check("the STATE column starts at one x on every row, under its header",
          len(xs) == 1 and head and head[0].find("STATE") in xs, s)
    dch("--keys", "pk1", "s")
    s = wait_screen("pk1", lambda s: "s sort name" in s)
    check("name sort is numeric-aware: s2 above s10",
          "s sort name" in s and rows(s).index("s2") < rows(s).index("s10"),
          s)
    check("s persists the sort: picker_sort = name in the config",
          "picker_sort = name\n" in config(), config())
    picker("pk4", "100x30", "NO_COLOR=1", "TERM=xterm-256color")
    s = wait_screen("pk4", lambda s: "s10" in s and "s sort" in s)
    check("a fresh picker starts in the saved sort", "s sort name" in s, s)
    dch("-k", "pk4")

    # 3. rename
    check("select s10", select("pk1", "s10"), screen("pk1"))
    dch("--keys", "pk1", "r")
    wait_screen("pk1", lambda s: "new name for s10" in s)
    dch("--send", "pk1", "nick")
    dch("--keys", "pk1", "enter")
    check("rename sets the alias",
          wait_live("s10", True) and "nick (s10)" in live(), live())

    # 4. kill asks first; x is the X-ray view, X the kill (OQ-6)
    check("select s2", select("pk1", "s2"), screen("pk1"))
    f = " ".join(footer(screen("pk1")))
    check("the hints name x x-ray and X kill",
          "x x-ray" in f and "X kill" in f, f)
    dch("--keys", "pk1", "x")
    s = wait_screen("pk1", lambda s: "X-ray  s2" in s or "[y/N]" in s)
    check("x opens the row's X-ray view and never asks to kill",
          "X-ray  s2" in s and "[y/N]" not in s, s)
    dch("--keys", "pk1", "q")
    s = wait_screen("pk1", lambda s: "X-ray  s2" not in s and "s10" in s)
    check("q in the view returns to the picker, s2 still live",
          "X-ray  s2" not in s and wait_live("s2", True), s)
    dch("--keys", "pk1", "X")
    s = wait_screen("pk1", lambda s: "Kill s2? [y/N]" in s)
    check("X asks before killing", "Kill s2? [y/N]" in s, s)
    dch("--keys", "pk1", "n")
    s = wait_screen("pk1", lambda s: "cancelled" in s)
    check("n cancels and keeps the session",
          "cancelled" in s and wait_live("s2", True), s)
    dch("--keys", "pk1", "X")
    wait_screen("pk1", lambda s: "Kill s2? [y/N]" in s)
    dch("--keys", "pk1", "y")
    check("y kills it", wait_live("s2", False), live())
    s = wait_screen("pk1", lambda s: "killed: s2" in s)
    check("the result line says so", "killed: s2" in s, s)

    # 5. History: the killed session, resumed by Enter
    # #097: an ended record with agent.name is titled by it, not its dch name
    rec = os.path.join(ROOT, "state", "dch", "sessions",
                       SOCKDIR.replace("/", "-"))
    os.makedirs(rec, mode=0o700, exist_ok=True)
    now = int(time.time())
    with os.fdopen(os.open(os.path.join(rec, "hx.json"),
                           os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
        json.dump(dict(name="hx", sock_dir=SOCKDIR, alias="", cwd=os.getcwd(),
                       argv=["sleep", "60"], started=now - 660,
                       updated=now - 60, ended=now - 60, end="exit 3",
                       agent=dict(kind="claude", session_id="x",
                                  name="claude-title")), f,
                  separators=(",", ":"))
    wait_screen("pk1", lambda s: "History " in s)
    s = to_tab("pk1", "History")
    check("History tab lists s2 as closed",
          "s2" in rows(s) and re.search(r"s2\s+closed", s) is not None, s)
    check("History: column headers",
          re.search(r"^ {4}NAME +ENDED +WHEN +DIRECTORY", s, re.M), s)
    s = wait_screen("pk1", lambda s: "claude-title" in rows(s))
    check("History: agent.name titles the record (#097)",
          "claude-title" in rows(s) and "hx" not in rows(s), s)
    check("History: its own key hints only",
          "resume" in s and "rename" not in s and "s sort" not in s, s)
    check("History: Shift+Enter/o open is hinted (#132)",
          "\u21e7\u21b5/o open" in s, s)
    check("select s2 in History", select("pk1", "s2"), screen("pk1"))
    dch("--keys", "pk1", "enter")
    s = wait_screen("pk1", lambda s: "Resumed as s2" in s, 20)
    check("Enter resumes it: Resumed as s2", "Resumed as s2" in s, s)
    check("s2 is live again", wait_live("s2", True), live())

    # 5b. a (allow) presses 1 only on a plain Yes/No permission prompt
    for n, lines in (("perm", PERM), ("form", FORM)):
        f = os.path.join(ROOT, n + ".txt")
        with open(f, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        dch("--spawn", n, "--size", "100x30", "sh", "-c",
            "cat '%s'; sleep 600" % f)
    to_tab("pk1", "Sessions")
    s = wait_screen("pk1", lambda s: "perm" in rows(s) and
                    "form" in rows(s) and s.count("Needs input") >= 2)
    check("select form", select("pk1", "form"), screen("pk1"))
    dch("--keys", "pk1", "a")
    s = wait_screen("pk1", lambda s: "not a plain Yes/No" in s)
    check("a on a question form refuses to press 1, attach (Enter) answers",
          "form: not a plain Yes/No permission prompt; attach (Enter) to "
          "answer" in s and "[y/N]" not in s, s)
    check("select perm", select("pk1", "perm"), screen("pk1"))
    dch("--keys", "pk1", "a")
    s = wait_screen("pk1", lambda s: "[y/N]" in s)
    check("a on a permission prompt asks, naming it",
          "Allow Bash command (presses 1, Yes)? [y/N]" in s, s)
    dch("--keys", "pk1", "n")

    # 6. color: none under NO_COLOR or TERM=dumb, some otherwise
    a = screen("pk1", ansi=True)
    check("NO_COLOR: no color codes, no reverse video",
          not ANYCOLOR_RE.search(a), a)
    picker("pk2", "100x30", "TERM=xterm-256color", "COLORTERM=truecolor")
    # s10 shows as its alias by now (3.)
    wait_screen("pk2", lambda s: "nick" in s)
    a = screen("pk2", ansi=True)
    check("color on by default: ESC[3x present", COLOR_RE.search(a), a)

    # 10. the selected Working row: the tinted bar, the state in its own
    # accent on it, never a reverse block
    check("select nick (s10) in the color picker", select("pk2", "nick"),
          screen("pk2"))
    a = screen("pk2", ansi=True)
    sl = [l for l in a.split("\n") if "\u258c" in l and "nick" in l]
    on = sgr_before(sl[0], "Working") if sl else None
    check("selected Working row: accent foreground on the bar's tint",
          on is not None and ACCENT_TC in on and BAR_TC in on and
          not re.search(r"(?:^|;)7(?:;|$)", on), sl, on)
    on = sgr_before(sl[0], "nick") if sl else None
    check("selected row: the title on the same bar",
          on is not None and BAR_TC in on, sl, on)
    check("key hints at 100 columns: whole groups, separated",
          any("\u2502" in l for l in footer(screen("pk2"))),
          footer(screen("pk2")))
    dch("-k", "pk2")
    picker("pk5", "200x30", "TERM=xterm-256color", "COLORTERM=truecolor")
    s = wait_screen("pk5", lambda s: "nick" in s and "quit" in s)
    f = footer(s)
    check("200 columns: every hint group on one line",
          len(f) == 1 and f[0].count("\u2502") == 3 and all(
              w in f[0] for w in ("attach", "rename", "kill", "restart",
                                  "stop", "stale", "force all", "sort",
                                  "settings", "tabs", "quit")), f)
    dch("-k", "pk5")
    picker("pk6", "80x24", "TERM=xterm-256color", "COLORTERM=truecolor")
    s = wait_screen("pk6", lambda s: "nick" in s and "quit" in s)
    f = " ".join(footer(s))
    check("80 columns: the agents' group dropped whole, actions and quit "
          "kept", "attach" in f and "quit" in f and "restart" not in f and
          "stale" not in f and re.search(r"\bstop\b", f) is None, footer(s))
    dch("-k", "pk6")

    # 7. 40 columns, TERM=dumb: a long title is cut, nothing wraps
    dch("-m", "s10", "a-very-long-alias-that-cannot-fit-in-forty-cols")
    picker("pk3", "40x20", "TERM=dumb")
    s = wait_screen("pk3", lambda s: "a-very-long" in s)
    long_rows = [l for l in s.split("\n") if "a-very-long" in l]
    check("narrow: the long title is cut on its own row",
          len(long_rows) == 1 and "~" in long_rows[0] and
          "forty-cols" not in s, s)
    check("narrow: the state still shows on that row",
          long_rows and re.search(r"Working|Idle", long_rows[0]), s)
    check("narrow: the tabs row leads, whole; the count and version give "
          "way, never overlap (#252)", re.match(
              r"\[Sessions \d+\]( +Sidekicks \d+)?( +History \d+)?"
              r"( {4,}\d+/\d+ active - dch \S+)?$", s.split("\n")[0].rstrip()),
          s.split("\n")[:2])
    check("narrow: no line wider than 40",
          all(len(l) <= 40 for l in s.split("\n")), s)
    a = screen("pk3", ansi=True)
    check("TERM=dumb: no ESC[3x color codes", not COLOR_RE.search(a), a)

    # q quits and leaves the alternate screen (the session's command ends)
    # 9. the settings view, on pk1 (100x30, NO_COLOR)
    dch("--keys", "pk1", ",")
    s = wait_screen("pk1", lambda s: "[Settings]" in s)
    check(", opens the settings view, every key listed",
          "[Settings]" in s and all(k in s for k in (
              "auto_allow", "sidekick_pattern", "picker_sort",
              "detach_key", "focus_resize")), s)
    check("settings: column headers",
          re.search(r"^ {4}SETTING +VALUE +ABOUT", s, re.M), s)
    check("settings: values, (default) when unset, a description each",
          re.search("auto_allow +\u25cb off +\\(default\\) +Answer", s)
          and re.search(r"picker_sort +activity \[name\] +How", s) and
          re.search(r"detach_key +\[detach\] switch +\(default\)", s)
          and re.search("focus_resize +\u25cf on +\\(default\\)", s), s)
    dch("--keys", "pk1", "down", "down", "enter")
    s = wait_screen("pk1", lambda s: "picker_sort = activity" in s)
    check("Enter cycles picker_sort: name -> activity, saved",
          "picker_sort = activity\n" in config(), s, config())
    dch("--keys", "pk1", "u")
    s = wait_screen("pk1", lambda s: re.search(
        r"picker_sort +\[activity\] name +\(default\)", s))
    check("u resets to the default: the key leaves the file",
          "picker_sort" not in config() and "(default)" in s, s, config())
    dch("--keys", "pk1", "up", "enter")
    wait_screen("pk1", lambda s: "new sidekick_pattern" in s)
    dch("--send", "pk1", "^nomatch$")
    dch("--keys", "pk1", "enter")
    s = wait_screen("pk1", lambda s: "sidekick_pattern = ^nomatch$" in s)
    check("Enter prompts for sidekick_pattern, saved",
          "sidekick_pattern = ^nomatch$\n" in config(), s, config())
    dch("--keys", "pk1", "up", "enter")
    s = wait_screen("pk1", lambda s: "[y/N]" in s)
    check("auto_allow on asks first", "Turn auto_allow on?" in s and
          "[y/N]" in s and "auto_allow" not in config(), s)
    dch("--keys", "pk1", "n")
    s = wait_screen("pk1", lambda s: "cancelled" in s)
    check("n keeps it off", "auto_allow" not in config(), s, config())
    dch("--keys", "pk1", "enter")
    wait_screen("pk1", lambda s: "[y/N]" in s)
    dch("--keys", "pk1", "y")
    s = wait_screen("pk1", lambda s: "auto_allow = true" in s)
    check("y turns it on", "auto_allow = true\n" in config(), s, config())
    dch("--keys", "pk1", "enter")
    s = wait_screen("pk1", lambda s: "auto_allow = false" in s)
    check("off needs no confirm", "[y/N]" not in s and
          "auto_allow = false\n" in config(), s, config())
    dch("--keys", "pk1", "esc")
    s = wait_screen("pk1", lambda s: "[Sessions" in s)
    check("esc goes back to the sessions; the new sidekick_pattern "
          "split them at once: no Sidekicks tab, helper-qa in Sessions",
          "[Sessions" in s and "Sidekicks" not in s and
          "helper-qa" in rows(s) and "kodi-qa" in rows(s), s)

    # 12. the "/" filter (#096), in fresh pickers. Sessions: helper-qa,
    # kodi-qa (a4k-2), nick (s10), s2, perm, form and the pickers; History:
    # the pickers killed above.
    CARET = "/\u2588"
    picker("pk7", "100x30", "NO_COLOR=1", "TERM=xterm-256color")
    s = wait_screen("pk7", lambda s: "kodi-qa" in rows(s) and
                    "History " in s)
    full = sorted(rows(s))
    check("select s2 before filtering", select("pk7", "s2"), screen("pk7"))
    dch("--keys", "pk7", "/")
    s = wait_screen("pk7", lambda s: CARET in s)
    check("/ opens the filter prompt on the status line, its own hints",
          CARET in s and "esc clear" in s and "q quit" not in s, s)
    dch("--send", "pk7", "KOD")
    s = wait_screen("pk7", lambda s: rows(s) == ["kodi-qa"])
    check("typing narrows the list, case folded: /KOD keeps kodi-qa",
          rows(s) == ["kodi-qa"] and "/KOD\u2588" in s, s)
    check("the tab counts are the filtered ones",
          "[Sessions 1]" in s and "History 0" in s, s)
    check("the selection starts on the top match",
          current(s) == "kodi-qa", s)
    dch("--keys", "pk7", "ctrl+u")
    s = wait_screen("pk7", lambda s: sorted(rows(s)) == full)
    check("Ctrl-U clears the text: every row back, the prompt open",
          sorted(rows(s)) == full and CARET in s, s)
    dch("--send", "pk7", "a4k")
    s = wait_screen("pk7", lambda s: rows(s) == ["kodi-qa"])
    check("the dch name matches too: /a4k keeps a4k-2 (kodi-qa)",
          rows(s) == ["kodi-qa"], s)
    dch("--send", "pk7", "z")
    s = wait_screen("pk7", lambda s: "No match" in s)
    check("nothing kept: a quiet No match line, Sessions 0",
          "    No match" in s and "[Sessions 0]" in s, s)
    dch("--keys", "pk7", "backspace")
    s = wait_screen("pk7", lambda s: rows(s) == ["kodi-qa"])
    check("Backspace edits: /a4k again",
          rows(s) == ["kodi-qa"] and "/a4k\u2588" in s, s)
    dch("--keys", "pk7", "ctrl+u")
    dch("--send", "pk7", "pk")
    s = wait_screen("pk7", lambda s: "/pk\u2588" in s and
                    len(rows(s)) >= 2)
    # The pk rows are all Working "now": activity sort reorders them between
    # reloads, so compare with the selection, never a remembered order.
    was = current(s)
    check("the prompt's hints say the arrows keep the filter",
          "\u2191\u2193 keep" in s, s)
    dch("--keys", "pk7", "down")
    s = wait_screen("pk7", lambda s: "/pk\u2588" not in s and
                    current(s) not in (None, was), 3.0)
    # A reload can sort `was` to the edge the arrow pushes against, so the
    # arrow has nowhere to go: try the other way until the selection moves.
    key, end = "up", time.time() + 15
    while current(s) == was and time.time() < end:
        dch("--keys", "pk7", key)
        key = "down" if key == "up" else "up"
        s = wait_screen("pk7", lambda s: current(s) not in (None, was), 3.0)
    sel = current(s)
    check("Down moves within the filtered list, closes the prompt and keeps "
          "the filter (#108)",
          sel not in (None, was) and sel.startswith("pk") and
          all(x.startswith("pk") for x in rows(s)) and
          "/pk" in s and "/pk\u2588" not in s and "q quit" in s, s)
    dch("--keys", "pk7", "X")
    s = wait_screen("pk7", lambda s: "[y/N]" in s)
    check("a letter action works on the filtered row: X asks about it",
          "Kill %s? [y/N]" % sel in s, s)
    dch("--keys", "pk7", "n")
    wait_screen("pk7", lambda s: "[y/N]" not in s)
    time.sleep(2.5)  # two reloads
    s = screen("pk7")
    check("reloads keep the filter and the selection",
          current(s) == sel and "/pk" in s and
          all(x.startswith("pk") for x in rows(s)), s)
    dch("--keys", "pk7", "/")
    s = wait_screen("pk7", lambda s: "/pk\u2588" in s)
    check("/ reopens the prompt on the kept text", "/pk\u2588" in s, s)
    dch("--send", "pk7", "jk")
    s = wait_screen("pk7", lambda s: "/pkjk\u2588" in s)
    check("j and k type into the prompt, they do not move",
          "/pkjk\u2588" in s and "No match" in s, s)
    dch("--keys", "pk7", "backspace")
    dch("--keys", "pk7", "backspace")
    wait_screen("pk7", lambda s: "/pk\u2588" in s and len(rows(s)) >= 2)
    dch("--keys", "pk7", "tab")
    s = wait_screen("pk7", lambda s: "[History" in s)
    hr = rows(s)
    check("the filter survives a tab switch: History keeps the pk rows",
          "/pk\u2588" in s and len(hr) >= 4 and
          all(r.startswith("pk") for r in hr) and
          "[History %d]" % len(hr) in s, s)
    dch("--send", "pk7", "zzz")
    wait_screen("pk7", lambda s: "No match" in s)
    dch("--keys", "pk7", "enter")
    s = wait_screen("pk7", lambda s: "/pkzzz" in s and
                    "/pkzzz\u2588" not in s)
    check("Enter on no row: nothing runs, the prompt closes, the filter "
          "stays", "No match" in s and "/pkzzz" in s and "q quit" in s and
          "Resum" not in s, s)
    dch("--keys", "pk7", "d")
    time.sleep(0.5)
    s = screen("pk7")
    check("d on no row asks nothing", "[y/N]" not in s, s)
    dch("--keys", "pk7", "esc")
    s = wait_screen("pk7", lambda s: "/pkzzz" not in s)
    check("Esc clears the filter: every History row back",
          "/pkzzz" not in s and "No match" not in s and
          len(rows(s)) >= len(hr) and "[History %d]" % len(rows(s)) in s,
          s)
    # the records made in this directory: its name keeps those History rows
    # and no others
    base = os.path.basename(os.getcwd())
    nh = sum(1 for l in body(s) if ROW_RE.match(l) and base in l)
    dch("--keys", "pk7", "/")
    wait_screen("pk7", lambda s: CARET in s)
    dch("--send", "pk7", base)
    s = wait_screen("pk7", lambda s: "/%s\u2588" % base in s)
    check("the directory matches too: /%s keeps the History rows run there"
          % base, nh >= 4 and len(rows(s)) == nh and "No match" not in s,
          nh, s)
    dch("--keys", "pk7", "ctrl+u")
    wait_screen("pk7", lambda s: CARET in s)
    dch("--keys", "pk7", "backspace")
    s = wait_screen("pk7", lambda s: CARET not in s)
    check("Backspace on an empty filter leaves the prompt",
          CARET not in s and "q quit" in s, s)
    dch("--keys", "pk7", "esc")
    check("then Esc quits, as before", wait_live("pk7", False), live())

    # attached from inside a session: DCH_SESSION would refuse the nesting
    picker("pk8", "100x30", "-u", "DCH_SESSION", "NO_COLOR=1",
           "TERM=xterm-256color")
    wait_screen("pk8", lambda s: "perm" in rows(s))
    check("select s2 in pk8", select("pk8", "s2"), screen("pk8"))
    dch("--keys", "pk8", "/")
    wait_screen("pk8", lambda s: CARET in s)
    dch("--send", "pk8", "PERM")
    wait_screen("pk8", lambda s: rows(s) == ["perm"])
    dch("--keys", "pk8", "enter")
    s = wait_screen("pk8", lambda s: "rm -rf build" in s)
    check("Enter attaches the top match (perm), not the row selected "
          "before", "rm -rf build" in s, s)

    dch("--keys", "pk3", "q")
    check("q quits the picker", wait_live("pk3", False), live())
    kitty()
    dashname()
    zerowidth()
    verbs()
    footshells()
    groups()
    groupscroll()
    groupcolor()
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        for n in ("pk1", "pk2", "pk3", "pk4", "pk5", "pk6", "pk7", "pk8",
                  "pk9", "pk10", "pk11", "pk12", "s2", "s10", "zwbom", "zwj",
                  "zwheb", "zwdev", "zwsym", "zwfmt", "zwhf", "zwmc", "helper-qa", "a4k-2", "perm", "form", "zkbd",
                  "pk13", "gr-api-qa", "gr-web", "pk19c", "gc-web"):
            dch("-k", n)
        for pr in procs:
            pr.kill()
        shutil.rmtree(ROOT, ignore_errors=True)
    print("PASS" if rc == 0 else "FAILED: %d" % len(failures))
    sys.exit(rc)
