#!/usr/bin/env python3
"""The picker keeps its place across a double-tap switch (#098).

A picker reopened by the detach key's double tap, in the same dch process,
keeps the last one's tab, sort and filter (its prompt closed) and selects
the session just left; the first switch after a plain attach opens on that
session's tab with it selected. Each dch runs as the command of a dch
session (env -u DCH_SESSION: attaching from inside one is refused as
nesting); --read shows its screen, --keys and --send type into it.

Covers:
  1. s (name sort), Tab to Sidekicks, Down, Enter attaches gamma-qa; the
     double tap reopens the picker on Sidekicks, sorted by name (kept in
     memory: the config says activity by then), gamma-qa selected;
  2. a filter set there (/qa, Enter attaches its top match beta-qa) is kept
     by the next switch, its prompt closed, beta-qa selected;
  3. dch -f -n gamma-qa, then the double tap: Sidekicks, gamma-qa selected.

Run:  python3 tests/picker_place_test.py      (uses ./dch)
"""
import os, re, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
ROOT = tempfile.mkdtemp(prefix="dchpp", dir="/tmp")
CONF = os.path.join(ROOT, "config")
ENV = dict(os.environ, DCH_SOCKET_DIR=os.path.join(ROOT, "s"),
           XDG_STATE_HOME=os.path.join(ROOT, "state"),
           HOME=os.path.join(ROOT, "home"), DCH_CONFIG=CONF,
           DCH_LOGIN_ENV="0", DCH_NO_REMOTE="1")
for k in ("DCH_SESSION", "MallocStackLogging", "MallocStackLoggingNoCompact",
          "NO_COLOR", "COLORTERM", "COLORFGBG"):
    ENV.pop(k, None)
# a slow tap is still a double tap: two --send calls apart, under load
TAPENV = ["-u", "DCH_SESSION", "DCH_DOUBLE_TAP_MS=3000", "NO_COLOR=1",
          "TERM=xterm-256color"]
ROW_RE = re.compile("^([>▌ ]) . (\\S+)")
RULE_RE = re.compile("^[─-]{20,}$")
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


def screen(name):
    return dch("--read", name).stdout


def wait_screen(name, pred, timeout=10.0):
    end = time.time() + timeout
    s = ""
    while time.time() < end:
        s = screen(name)
        if pred(s):
            return s
        time.sleep(0.2)
    return s


def body(s):
    out = []
    for l in s.split("\n")[3:]:
        if RULE_RE.match(l):
            break
        out.append(l)
    return out


def rows(s):
    return [m.group(2) for m in map(ROW_RE.match, body(s)) if m]


def current(s):
    for m in map(ROW_RE.match, body(s)):
        if m and m.group(1) != " ":
            return m.group(2)
    return None


def switch(name):
    """The double tap, then the reopened picker's screen."""
    before = screen(name)
    dch("--send", name, "\x1c")
    time.sleep(0.1)
    dch("--send", name, "\x1c")
    return wait_screen(name, lambda s: "switch to session:" in s and
                       s != before and rows(s), 15)


def main():
    for n, word in (("alpha", "ALPHA"), ("beta-qa", "BETA"),
                    ("gamma-qa", "GAMMA")):
        dch("--spawn", n, "--size", "100x30", "sh", "-c",
            "echo %s_SCREEN; sleep 600" % word)
    if dch("--read", "alpha").returncode == 3:
        print("skip: no terminal mirror in this build")
        return 0

    # 1. tab, sort and the session just left
    dch("--spawn", "pa", "--size", "100x30", "env", *TAPENV, DCH, "-l")
    s = wait_screen("pa", lambda s: "gamma-qa" in s and "Sidekicks 2" in s)
    check("picker up, Sidekicks 2", "Sidekicks 2" in s, s)
    dch("--keys", "pa", "s")
    wait_screen("pa", lambda s: "s sort name" in s)
    dch("--keys", "pa", "tab")
    s = wait_screen("pa", lambda s: "[Sidekicks 2]" in s and
                    rows(s) == ["beta-qa", "gamma-qa"])
    check("Sidekicks by name: beta-qa, gamma-qa",
          rows(s) == ["beta-qa", "gamma-qa"], s)
    dch("--keys", "pa", "down")
    s = wait_screen("pa", lambda s: current(s) == "gamma-qa")
    dch("--keys", "pa", "enter")
    s = wait_screen("pa", lambda s: "GAMMA_SCREEN" in s)
    check("Enter attaches gamma-qa (row 2)", "GAMMA_SCREEN" in s, s)
    # the sort the next picker keeps is this process's, not the file's
    with open(CONF, "w") as f:
        f.write("picker_sort = activity\n")
    s = switch("pa")
    check("the double tap reopens on Sidekicks", "[Sidekicks 2]" in s, s)
    check("... sorted by name, kept in memory",
          "s sort name" in s and rows(s) == ["beta-qa", "gamma-qa"], s)
    check("... the session just left selected", current(s) == "gamma-qa", s)

    # 2. a filter is kept, its prompt closed
    dch("--keys", "pa", "/")
    wait_screen("pa", lambda s: "/█" in s)
    dch("--send", "pa", "qa")
    wait_screen("pa", lambda s: "/qa█" in s and current(s) == "beta-qa")
    dch("--keys", "pa", "enter")
    s = wait_screen("pa", lambda s: "BETA_SCREEN" in s)
    check("/qa, Enter attaches the top match beta-qa", "BETA_SCREEN" in s, s)
    s = switch("pa")
    check("the next switch keeps the filter, its prompt closed",
          "/qa" in s and "/qa█" not in s and "q quit" in s, s)
    check("... on Sidekicks, beta-qa selected",
          "[Sidekicks 2]" in s and current(s) == "beta-qa", s)
    dch("--keys", "pa", "q")

    # 3. the first switch after a plain attach: that session's tab and row
    dch("--spawn", "pb", "--size", "100x30", "env", *TAPENV, DCH, "-f",
        "-n", "gamma-qa")
    s = wait_screen("pb", lambda s: "GAMMA_SCREEN" in s)
    check("dch -f -n gamma-qa attaches", "GAMMA_SCREEN" in s, s)
    s = switch("pb")
    check("its first switch opens on Sidekicks, gamma-qa selected",
          "[Sidekicks" in s and current(s) == "gamma-qa", s)
    return 1 if failures else 0


if __name__ == "__main__":
    rc = 1
    try:
        rc = main()
    finally:
        for n in ("pa", "pb", "alpha", "beta-qa", "gamma-qa"):
            dch("-k", n)
        shutil.rmtree(ROOT, ignore_errors=True)
    print("PASS" if rc == 0 else "FAILED: %d" % len(failures))
    sys.exit(rc)
