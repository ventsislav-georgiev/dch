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
  3. dch -f -n gamma-qa, then the double tap: Sidekicks, gamma-qa selected;
  4. History (#132): Shift+Enter (kitty CSI 13;2u) on h1 revives it and
     attaches; the double tap then opens on Sessions, h1 selected. o (the
     fallback) does the same for h2-qa, and the switch lands on Sidekicks,
     h2-qa selected. xterm's modifyOtherKeys Shift+Enter (CSI 27;2;13~)
     attaches h3. Plain Enter on h4 only resumes it, the picker stays. A
     revive that fails (its directory gone) stays in the picker with its
     line;
  5. a session the picker opened that ends goes back to the picker, on
     History, its record selected (#133): the fake Claude's Ctrl-D twice
     ends c1, and of two records named c1 the one this end wrote is
     selected, not an older pinned one the History tab had selected; e1,
     revived by Shift+Enter, ends the same way; q and Esc there exit to
     the shell; `dch -a g1` (no picker) still exits to the shell;
  6. the kill picker (dch -k, no name) has no open: History hints no
     Shift+Enter/o, and o and Shift+Enter on k1 revive nothing, kill
     nothing and say where open works;
  7. the master drops a client it cannot feed (stopped past 4 MB of
     output) while dr1 lives on: the picker, after its bounded wait for a
     record that never comes, opens on dr1's own row in Sessions.

Run:  python3 tests/picker_place_test.py      (uses ./dch)
"""
import json, os, re, shutil, signal, subprocess, sys, tempfile, time

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
    for _ in range(4):
        s = screen(pk)
        if "[%s" % label in s:
            return s
        dch("--keys", pk, "tab")
        time.sleep(0.3)
    return wait_screen(pk, lambda s: "[%s" % label in s)


def selline(s):
    """The selected row's whole line."""
    for l in body(s):
        m = ROW_RE.match(l)
        if m and m.group(1) != " ":
            return l
    return ""


def live(name):
    return any(l == name for l in dch("-ls").stdout.split("\n"))


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
    dch("--keys", "pb", "q")

    # 4. History: Shift+Enter and o revive and attach; Enter only resumes
    hist = ("h1", "h2-qa", "h3", "h4")
    for n in hist:
        dch("--spawn", n, "--size", "100x30", "sh", "-c",
            "echo %s_SCREEN; sleep 600" % n.upper())
    wait_screen("h4", lambda s: "H4_SCREEN" in s)
    for n in hist:
        dch("-k", n)
    rec = os.path.join(ROOT, "state", "dch", "sessions",
                       ENV["DCH_SOCKET_DIR"].replace("/", "-"))
    now = int(time.time())
    with os.fdopen(os.open(os.path.join(rec, "hgone.json"),
                           os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
        json.dump(dict(name="hgone", sock_dir=ENV["DCH_SOCKET_DIR"],
                       alias="", cwd=os.path.join(ROOT, "gone"),
                       argv=["sleep", "60"], started=now - 660,
                       updated=now - 60, ended=now - 60, end="exit 3"), f,
                  separators=(",", ":"))
    dch("--spawn", "pc", "--size", "100x30", "env", *TAPENV, DCH, "-l")
    wait_screen("pc", lambda s: "History " in s)
    s = to_tab("pc", "History")
    s = wait_screen("pc", lambda s: all(n in rows(s) for n in hist))
    check("History lists h1 h2-qa h3 h4", all(n in rows(s) for n in hist), s)
    check("History's hints show Shift+Enter/o open",
          "\u21e7\u21b5/o open" in s, s)
    check("select h1", select("pc", "h1"), screen("pc"))
    dch("--send", "pc", "\x1b[13;2u")
    s = wait_screen("pc", lambda s: "H1_SCREEN" in s, 20)
    check("Shift+Enter (CSI 13;2u) revives h1 and attaches it",
          "H1_SCREEN" in s and live("h1"), s)
    s = switch("pc")
    check("the double tap opens on Sessions, h1 selected",
          "[Sessions" in s and current(s) == "h1", s)

    s = to_tab("pc", "History")
    check("select h2-qa", select("pc", "h2-qa"), screen("pc"))
    dch("--keys", "pc", "o")
    s = wait_screen("pc", lambda s: "H2-QA_SCREEN" in s, 20)
    check("o revives h2-qa and attaches it", "H2-QA_SCREEN" in s, s)
    s = switch("pc")
    check("the double tap opens on Sidekicks, h2-qa selected",
          "[Sidekicks" in s and current(s) == "h2-qa", s)

    s = to_tab("pc", "History")
    check("select h3", select("pc", "h3"), screen("pc"))
    dch("--send", "pc", "\x1b[27;2;13~")
    s = wait_screen("pc", lambda s: "H3_SCREEN" in s, 20)
    check("modifyOtherKeys Shift+Enter (CSI 27;2;13~) attaches h3",
          "H3_SCREEN" in s, s)
    s = switch("pc")
    check("... and the switch selects h3 on Sessions",
          "[Sessions" in s and current(s) == "h3", s)

    s = to_tab("pc", "History")
    check("select h4", select("pc", "h4"), screen("pc"))
    dch("--keys", "pc", "enter")
    s = wait_screen("pc", lambda s: "Resumed as h4" in s, 20)
    check("plain Enter only resumes h4: the picker stays on History",
          "Resumed as h4" in s and "[History" in s and
          "H4_SCREEN" not in s and live("h4"), s)

    check("select hgone", select("pc", "hgone"), screen("pc"))
    dch("--keys", "pc", "o")
    s = wait_screen("pc", lambda s: "NOT revived" in s, 20)
    check("a failed revive stays in the picker with its line",
          "NOT revived" in s and "[History" in s and not live("hgone"), s)
    dch("--keys", "pc", "q")

    # 5. an end goes back to the picker, on History (#133)
    bindir, fc = os.path.join(ROOT, "bin"), os.path.join(ROOT, "fc")
    for d in (bindir, fc, os.path.join(ROOT, "home", ".claude", "sessions")):
        os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    with open(os.path.join(fc, "version"), "w") as f:
        f.write("1.0.0")
    dch("--spawn", "c1", "--size", "100x30", "env",
        "PATH=%s:%s" % (bindir, os.environ["PATH"]),
        "FAKE_CLAUDE_DIR=" + fc, "claude")
    s = wait_screen("c1", lambda s: "auto mode on" in s)
    check("the fake Claude is up in c1", "auto mode on" in s, s)
    wrap = '"$0" %s; echo SHELL_BACK; sleep 600'
    # an older c1, pinned: the top of History, where its selection starts
    with os.fdopen(os.open(os.path.join(rec, "c1.1000.json"),
                           os.O_WRONLY | os.O_CREAT, 0o600), "w") as f:
        json.dump(dict(name="c1", sock_dir=ENV["DCH_SOCKET_DIR"],
                       alias="", cwd=ROOT, argv=["sleep", "60"],
                       started=now - 7200, updated=now - 3600,
                       ended=now - 3600, end="killed"), f,
                  separators=(",", ":"))
    open(os.path.join(rec, "c1.1000.pin"), "w").close()
    dch("--spawn", "pe", "--size", "100x30", "env", *TAPENV, "sh", "-c",
        wrap % "-l", DCH)
    wait_screen("pe", lambda s: "c1" in rows(s))
    check("select c1", select("pe", "c1"), screen("pe"))
    dch("--keys", "pe", "enter")
    s = wait_screen("pe", lambda s: "auto mode on" in s)
    check("Enter attaches c1", "auto mode on" in s, s)
    dch("--send", "pe", "\x04")
    time.sleep(0.3)
    dch("--send", "pe", "\x04")
    s = wait_screen("pe", lambda s: "switch to session:" in s and rows(s),
                    15)
    check("Claude's Ctrl-D twice: back to the picker, on History",
          "[History" in s and not live("c1"), s)
    check("... the record c1's end wrote selected, not the older c1",
          rows(s).count("c1") == 2 and current(s) == "c1" and
          "exited" in selline(s) and "now" in selline(s), s)
    dch("--keys", "pe", "q")
    s = wait_screen("pe", lambda s: "SHELL_BACK" in s)
    check("q there exits to the shell", "SHELL_BACK" in s, s)

    dch("--spawn", "e1", "--size", "100x30", "sh", "-c",
        "echo E1_SCREEN; read x")
    wait_screen("e1", lambda s: "E1_SCREEN" in s)
    dch("-k", "e1")
    dch("--spawn", "pf", "--size", "100x30", "env", *TAPENV, "sh", "-c",
        wrap % "-l", DCH)
    wait_screen("pf", lambda s: "History " in s)
    to_tab("pf", "History")
    check("select e1 (killed)", select("pf", "e1"), screen("pf"))
    dch("--send", "pf", "\x1b[13;2u")
    s = wait_screen("pf", lambda s: "E1_SCREEN" in s, 20)
    check("Shift+Enter revives e1 and attaches it", "E1_SCREEN" in s, s)
    dch("--keys", "pf", "enter")	# its read returns: e1 ends
    s = wait_screen("pf", lambda s: "switch to session:" in s and rows(s),
                    15)
    check("e1's end: back to the picker, on History",
          "[History" in s and not live("e1"), s)
    check("... its record selected",
          current(s) == "e1" and "exited" in selline(s), s)
    dch("--keys", "pf", "esc")
    s = wait_screen("pf", lambda s: "SHELL_BACK" in s)
    check("Esc there exits to the shell", "SHELL_BACK" in s, s)

    dch("--spawn", "g1", "--size", "100x30", "sh", "-c",
        "echo G1_SCREEN; read x")
    wait_screen("g1", lambda s: "G1_SCREEN" in s)
    dch("--spawn", "pg", "--size", "100x30", "env", *TAPENV, "sh", "-c",
        wrap % "-a g1", DCH)
    s = wait_screen("pg", lambda s: "G1_SCREEN" in s)
    check("dch -a g1 attaches", "G1_SCREEN" in s, s)
    dch("--keys", "pg", "enter")
    s = wait_screen("pg", lambda s: "SHELL_BACK" in s, 15)
    check("g1's end under dch -a: the shell, no picker",
          "SHELL_BACK" in s and "switch to session:" not in s, s)

    # 6. the kill picker never opens what it revives
    dch("--spawn", "k1", "--size", "100x30", "sh", "-c",
        "echo K1_SCREEN; sleep 600")
    wait_screen("k1", lambda s: "K1_SCREEN" in s)
    dch("-k", "k1")
    dch("--spawn", "pk", "--size", "100x30", "env", *TAPENV, "sh", "-c",
        wrap % "-k", DCH)
    wait_screen("pk", lambda s: "kill session:" in s and "History " in s)
    s = to_tab("pk", "History")
    check("select k1 in the kill picker", select("pk", "k1"), screen("pk"))
    s = screen("pk")
    check("the kill picker's History hints no open",
          "resume" in s and "open" not in s and "/o" not in s, s)
    for key in ("o", "\x1b[13;2u"):
        dch("--send", "pk", key)
        s = wait_screen("pk", lambda s: "o opens a session" in s)
        time.sleep(1.5)
        s = screen("pk")
        check("%r in the kill picker: the line, no revive, no kill" % key,
              "o opens a session: from dch -l or the switch" in s and
              "kill session:" in s and not live("k1") and
              "k1" in rows(s), s)
        dch("--keys", "pk", "j")	# a key: the line clears
        wait_screen("pk", lambda s: "o opens a session" not in s)
    dch("--keys", "pk", "q")

    # 7. a dropped client while the session lives: its own row, not History
    dch("--spawn", "dr1", "--size", "100x30", "sh", "-c",
        "echo DR1_SCREEN; read x; yes FLOODFLOODFLOODFLOOD | "
        "head -c 30000000; echo FLOOD_DONE; sleep 600")
    wait_screen("dr1", lambda s: "DR1_SCREEN" in s)
    dch("--spawn", "pd", "--size", "100x30", "env", *TAPENV, "sh", "-c",
        wrap % "-l", DCH)
    wait_screen("pd", lambda s: "dr1" in rows(s))
    check("select dr1", select("pd", "dr1"), screen("pd"))
    dch("--keys", "pd", "enter")
    s = wait_screen("pd", lambda s: "DR1_SCREEN" in s)
    check("Enter attaches dr1", "DR1_SCREEN" in s, s)
    sd = ENV["DCH_SOCKET_DIR"]
    cl = [f for f in os.listdir(sd) if f.startswith("dr1.sock.client.")]
    pid = int(cl[0].rsplit(".", 1)[1]) if len(cl) == 1 else 0
    check("one client pidfile for dr1", pid > 0, cl, os.listdir(sd))
    if pid:
        os.kill(pid, signal.SIGSTOP)
        try:
            dch("--send", "dr1", "\r")
            wait_screen("dr1", lambda s: "FLOOD_DONE" in s, 30)
        finally:
            os.kill(pid, signal.SIGCONT)
        s = wait_screen("pd", lambda s: "switch to session:" in s and
                        rows(s), 30)
        check("a dropped client: the picker, on Sessions, dr1 selected, "
              "dr1 still live",
              "[Sessions" in s and current(s) == "dr1" and live("dr1"), s)
        dch("--keys", "pd", "q")
    return 1 if failures else 0


if __name__ == "__main__":
    rc = 1
    try:
        rc = main()
    finally:
        for n in ("pa", "pb", "pc", "pe", "pf", "pg", "alpha", "beta-qa",
                  "gamma-qa", "h1", "h2-qa", "h3", "h4", "c1", "e1", "g1",
                  "pk", "pd", "k1", "dr1"):
            dch("-k", n)
        shutil.rmtree(ROOT, ignore_errors=True)
    print("PASS" if rc == 0 else "FAILED: %d" % len(failures))
    sys.exit(rc)
