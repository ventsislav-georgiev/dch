#!/usr/bin/env python3
"""The interactive session picker (#075), driven inside dch sessions.

The picker runs as the command of a dch session; --read shows its screen
and --keys types into it. Covers:
  1. the Sidekicks tab shows only once a session matches sidekick_pattern
     (default "-qa"), live, and that session leaves the Sessions tab;
  2. the sort toggle: activity (working first) vs name (s2 before s10);
  3. rename through the picker sets the alias;
  4. kill asks first: n keeps the session, y kills it;
  5. the History tab lists the killed session and Enter resumes it;
  6. NO_COLOR and TERM=dumb draw no ESC[3x color, a colored one does;
  7. at 40 columns a long title is cut and no row wraps.

Run:  python3 tests/picker_test.py            (uses ./dch)
      DCH=/path/to/dch python3 tests/picker_test.py
"""
import os, re, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
ROOT = tempfile.mkdtemp(prefix="dchpk", dir="/tmp")
SOCKDIR = os.path.join(ROOT, "s")
ENV = dict(os.environ, DCH_SOCKET_DIR=SOCKDIR,
           XDG_STATE_HOME=os.path.join(ROOT, "state"),
           HOME=os.path.join(ROOT, "home"),
           DCH_CONFIG=os.path.join(ROOT, "config"), DCH_LOGIN_ENV="0")
for k in ("DCH_SESSION", "MallocStackLogging", "MallocStackLoggingNoCompact",
          "NO_COLOR"):
    ENV.pop(k, None)
os.makedirs(ENV["HOME"])
RULE = "\u2500" * 60
PERM = [RULE, " Bash command", "", "   rm -rf build", "   Clean the build tree",
        "", " Do you want to proceed?", " \u276f 1. Yes",
        "   2. No, and tell Claude what to do differently (esc)", "",
        " Esc to cancel \u00b7 Tab to amend"]
FORM = [RULE, " \u2610 Proceed", "", "Do you want to proceed?", "",
        "\u276f 1. Yes", "  2. No", "", "Enter to select \u00b7 Esc to cancel"]
COLOR_RE = re.compile(r"\x1b\[(?:[0-9;]*;)?3[0-9][;m]")
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


ROW_RE = re.compile(r"^([> ]) . (\S+)")   # marker, glyph or pin, title


def rows(s):
    """Row titles in drawn order, below the header and tab lines."""
    return [m.group(2) for m in map(ROW_RE.match, s.split("\n")[2:]) if m]


def current(s):
    for m in map(ROW_RE.match, s.split("\n")[2:]):
        if m and m.group(1) == ">":
            return m.group(2)
    return None


def select(pk, name, timeout=10.0):
    """Move the selection to the row titled `name`."""
    end = time.time() + timeout
    while time.time() < end:
        s = screen(pk)
        r, cur = rows(s), current(s)
        if cur == name:
            return True
        if cur in r and name in r:
            d = r.index(name) - r.index(cur)
            dch("--keys", pk, *(["down"] * d if d > 0 else ["up"] * -d))
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


def picker(name, size, *env):
    r = dch("--spawn", name, "--size", size, "env", *env, DCH, "-l")
    return r.returncode == 0


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
          "attach session (mirror):" in s and "[Sessions]" in s, s)
    check("no Sidekicks tab without a matching session",
          "Sidekicks" not in s, s)
    dch("--spawn", "helper-qa", "--size", "80x24", "sh", "-c", "sleep 600")
    s = wait_screen("pk1", lambda s: "Sidekicks (1)" in s)
    check("Sidekicks (1) appears live", "Sidekicks (1)" in s, s)
    check("a sidekick is not in Sessions", "helper-qa" not in rows(s), s)
    dch("--keys", "pk1", "tab")
    s = wait_screen("pk1", lambda s: "[Sidekicks (1)]" in s)
    check("tab switches to Sidekicks, listing the sidekick only",
          rows(s) == ["helper-qa"], s)
    dch("--keys", "pk1", "tab")
    s = wait_screen("pk1", lambda s: "[Sessions]" in s)

    # 2. sort: activity puts the working s10 first; name puts s2 before s10
    s = wait_screen("pk1", lambda s: "sort: activity" in s and
                    "s10" in rows(s) and "s2" in rows(s) and
                    rows(s).index("s10") < rows(s).index("s2"))
    check("activity sort: working s10 above idle s2",
          rows(s).index("s10") < rows(s).index("s2"), s)
    check("states are labelled", "Working" in s and "Idle" in s, s)
    dch("--keys", "pk1", "s")
    s = wait_screen("pk1", lambda s: "sort: name" in s)
    check("name sort is numeric-aware: s2 above s10",
          "sort: name" in s and rows(s).index("s2") < rows(s).index("s10"),
          s)

    # 3. rename
    check("select s10", select("pk1", "s10"), screen("pk1"))
    dch("--keys", "pk1", "r")
    wait_screen("pk1", lambda s: "new name for s10" in s)
    dch("--send", "pk1", "nick")
    dch("--keys", "pk1", "enter")
    check("rename sets the alias",
          wait_live("s10", True) and "nick (s10)" in live(), live())

    # 4. kill asks first
    check("select s2", select("pk1", "s2"), screen("pk1"))
    dch("--keys", "pk1", "x")
    s = wait_screen("pk1", lambda s: "Kill s2? [y/N]" in s)
    check("x asks before killing", "Kill s2? [y/N]" in s, s)
    dch("--keys", "pk1", "n")
    s = wait_screen("pk1", lambda s: "cancelled" in s)
    check("n cancels and keeps the session",
          "cancelled" in s and wait_live("s2", True), s)
    dch("--keys", "pk1", "x")
    wait_screen("pk1", lambda s: "Kill s2? [y/N]" in s)
    dch("--keys", "pk1", "y")
    check("y kills it", wait_live("s2", False), live())
    s = wait_screen("pk1", lambda s: "killed: s2" in s)
    check("the result line says so", "killed: s2" in s, s)

    # 5. History: the killed session, resumed by Enter
    wait_screen("pk1", lambda s: "History (" in s)
    s = to_tab("pk1", "History (")
    check("History tab lists s2 as closed",
          "s2" in rows(s) and re.search(r"s2\s+closed", s) is not None, s)
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
    check("NO_COLOR: no ESC[3x color codes", not COLOR_RE.search(a), a)
    picker("pk2", "100x30", "TERM=xterm-256color")
    wait_screen("pk2", lambda s: "s10" in s)
    a = screen("pk2", ansi=True)
    check("color on by default: ESC[3x present", COLOR_RE.search(a), a)

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
    dch("--keys", "pk3", "q")
    check("q quits the picker", wait_live("pk3", False), live())
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        for n in ("pk1", "pk2", "pk3", "s2", "s10", "helper-qa", "perm",
                  "form"):
            dch("-k", n)
        shutil.rmtree(ROOT, ignore_errors=True)
    print("PASS" if rc == 0 else "FAILED: %d" % len(failures))
    sys.exit(rc)
