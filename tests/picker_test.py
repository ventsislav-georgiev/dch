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
     detach, never attach (QA 1.59.0 LOW-V8), the filter's too.

Run:  python3 tests/picker_test.py            (uses ./dch)
      DCH=/path/to/dch python3 tests/picker_test.py
"""
import errno, json, os, pty, re, shutil, signal, subprocess, sys, tempfile, time
from select import select as fdselect

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
ROOT = tempfile.mkdtemp(prefix="dchpk", dir="/tmp")
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


def body(s):
    """The list's lines: below the title, tabs and column headers, above
    the rule."""
    out = []
    for l in s.split("\n")[3:]:
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


ZW_RE = re.compile("[\u200b-\u200d\u2060-\u2064\ufeff]")


def zerowidth():
    """15. A name holding a zero-width format character (U+FEFF, U+2060)
    takes no cell, so its row pads to the same columns as any other
    (#225): the state starts under STATE."""
    names = {"zwbom": "zw\ufeffbom", "zwj": "zw\u2060wj"}
    for n, alias in names.items():
        dch("--spawn", n, "--size", "80x24", "sh", "-c", "sleep 600")
        dch("-m", n, alias)
    picker("pk10", "100x30", "NO_COLOR=1", "TERM=xterm-256color")
    s = wait_screen("pk10", lambda s: all(a in rows(s)
                                          for a in names.values()))
    cell = lambda l, i: i - len(ZW_RE.findall(l[:i]))
    hdr = [l for l in s.split("\n") if re.match(r"^ {4}NAME +STATE", l)]
    col = cell(hdr[0], hdr[0].find("STATE")) if hdr else -1
    for n, alias in names.items():
        rl = [l for l in body(s) if alias in l]
        check("U+%04X in a name: the row's state starts under STATE"
              % ord(ZW_RE.search(alias).group()),
              len(rl) == 1 and cell(rl[0], rl[0].find("Idle")) == col,
              col, rl)
    dch("--keys", "pk10", "q")
    wait_live("pk10", False)


def verbs():
    """15. dch -k and dch -d with no name: Enter kills or detaches the
    row, and the hints say so (QA 1.59.0 LOW-V8)."""
    for pk, flag, verb in (("pk11", "-k", "kill"), ("pk12", "-d", "detach")):
        dch("--spawn", pk, "--size", "200x30", "env", "NO_COLOR=1",
            "TERM=xterm-256color", DCH, flag)
        s = wait_screen(pk, lambda s: "q quit" in s)
        dch("--keys", pk, "/")
        f = wait_screen(pk, lambda s: "esc clear" in s)
        k, fk = " ".join(footer(s)), " ".join(footer(f))
        check("dch %s: the hints say \u21b5 %s, not attach, the filter's "
              "too" % (flag, verb), "\u21b5 %s " % verb in k and "\u21b5 %s "
              % verb in fk and "attach" not in k + fk, k, fk)
        dch("--keys", pk, "esc")
        wait_screen(pk, lambda s: "q quit" in s)
        dch("--keys", pk, "q")
        wait_live(pk, False)


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

    # 1. tabs: no Sidekicks tab until a "-qa" session exists
    picker("pk1", "100x30", "NO_COLOR=1", "TERM=xterm-256color")
    s = wait_screen("pk1", lambda s: "s10" in s and "s2" in s)
    check("picker opens with the caller's header and a Sessions tab",
          "attach session (mirror):" in s and
          re.search(r"\[Sessions \d+\]", s), s)
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
    check("a on a question form refuses to press 1",
          "form: not a plain Yes/No permission prompt" in s and
          "[y/N]" not in s, s)
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
    wait_screen("pk2", lambda s: "s10" in s)
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
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        for n in ("pk1", "pk2", "pk3", "pk4", "pk5", "pk6", "pk7", "pk8",
                  "pk9", "pk10", "pk11", "pk12", "s2", "s10", "zwbom", "zwj",
                  "helper-qa", "a4k-2", "perm", "form", "zkbd"):
            dch("-k", n)
        for pr in procs:
            pr.kill()
        shutil.rmtree(ROOT, ignore_errors=True)
    print("PASS" if rc == 0 else "FAILED: %d" % len(failures))
    sys.exit(rc)
