"""A Claude Code suspended with Ctrl-Z (#282): still the session's Claude,
never the terminal's foreground job.

A session runs `bash -i` (job control with no rc file to read: zsh -i
under a temp HOME would start its new-user menu), the fake Claude
(tests/fake_claude.py as `claude` first on PATH) typed into it. Checks:
--ls-json gives the row claude_session_id and claude_foreground true;
SIGTSTP to the fake's process group (it reads the tty raw, so a typed
Ctrl-Z would not stop it) puts bash in front: claude_session_id stays,
claude_foreground false; with auto_allow on, a permission prompt that the
shell prints is not answered (no "1", no auto_allowed); --say and --stop
fail not_foreground and type nothing; --run keeps the plain burst for the
shell; after `fg`, claude_foreground is true and the fake's own prompt is
answered. Then a second session runs the fake under script(1) (a pty and
process group of its own, #312): claude_foreground true and --say reaches
it; suspended inside script's bash, --say fails not_foreground and the
shell's prompt-shaped text is not answered; after fg, auto-allow answers the
fake's prompt (#318); with script itself stopped, claude_foreground is false
and nothing is answered; after fg, --stop is not refused. On dch-lite (no
mirror) the auto-allow checks are skipped.

Run:  python3 tests/claude_fg_test.py        (uses ./dch)
"""
import json, os, shutil, signal, stat, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
TAG = str(os.getpid())
RULE = "─" * 100
NO = "No, and tell Claude what to do differently (esc)"


def dialog(body):
    """auto_allow_test.py's plain two-option Bash prompt."""
    return [RULE, " Bash command", ""] + list(body) + [
        "", " Do you want to proceed?", " ❯ 1. Yes", "   2. " + NO, "",
        " Esc to cancel · Tab to amend"]


SHELL_DLG = dialog(("   rm -rf build", "   Clean the build tree"))
FAKE_DLG = dialog(("   swift build", "   Build it"))


def run(*args, timeout=60):
    return subprocess.run([DCH] + list(args), capture_output=True, text=True,
                          timeout=timeout)


def until(fn, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchv", dir=os.environ.get("DCH_TEST_TMP", "/tmp")))
    home, fc, bindir = (os.path.join(tmp, d) for d in ("home", "fc", "bin"))
    for d in (home, fc, bindir):
        os.mkdir(d)
    os.makedirs(os.path.join(home, ".claude", "sessions"), mode=0o700)
    p = os.path.join(bindir, "claude")
    shutil.copy(os.path.join(HERE, "fake_claude.py"), p)
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
    trace = os.path.join(tmp, "trace")  # the masters' DCH_DEBUG, for why()
    dlg = os.path.join(tmp, "dlg.txt")
    with open(dlg, "w") as f:
        f.write("\n".join(SHELL_DLG) + "\n")
    for k in list(os.environ):
        if k.startswith("CLAUDE") or k in ("DCH_SESSION", "DCH_SOCKET_DIR",
                                           "DCH_NO_DETECT", "DCH_CONFIG",
                                           "XDG_CONFIG_HOME", "PROMPT_COMMAND"):
            del os.environ[k]
    os.environ.update(HOME=home, XDG_RUNTIME_DIR=tmp,
                      XDG_STATE_HOME=os.path.join(tmp, "state"),
                      DCH_CONFIG=os.path.join(tmp, "config"),
                      FAKE_CLAUDE_DIR=fc, PS1="$ ",
                      BASH_SILENCE_DEPRECATION_WARNING="1",
                      DCH_AGENT_TIMEOUT_MS="5000",
                      PATH=bindir + os.pathsep + os.environ["PATH"])
    os.chdir(tmp)
    n = "fg" + TAG
    fails, fpid, spid = 0, None, None

    def check(msg, cond):
        nonlocal fails
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails += 1

    def setctl(**ctl):
        with open(os.path.join(fc, n + ".ctl.tmp"), "w") as f:
            json.dump(ctl, f)
        os.rename(os.path.join(fc, n + ".ctl.tmp"),
                  os.path.join(fc, n + ".ctl"))

    def events():
        try:
            with open(os.path.join(fc, n + ".log")) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def keys():
        return [e["text"] for e in events() if e["ev"] == "key"]

    def row():
        for r in json.loads(run("--ls-json").stdout or "[]"):
            if r["name"] == n:
                return r
        return {}

    def screen():
        return run("--read", n).stdout

    def send(text):
        run("--send", n, "\x15" + text + "\r")   # Ctrl-U: an empty line

    def ps(p, field):
        return subprocess.run(["ps", "-o", field + "=", "-p", str(p)],
                              capture_output=True, text=True).stdout.strip()

    def script_of(p):
        """The script(1) above p (Linux puts an sh -c in between)."""
        for _ in range(8):
            p = int(ps(p, "ppid"))
            if os.path.basename(ps(p, "comm")) == "script":
                return p
        raise RuntimeError("no script(1) above the fake")

    def shell_dlg_unanswered(what):
        """The shell prints a prompt-shaped text: not Claude's to answer."""
        before = row().get("auto_allowed", 0)
        send("clear; cat %s; sleep 4" % dlg)
        until(lambda: "Tab to amend" in screen(), 5)
        time.sleep(2.5)                  # past AA_QUIET_MS and a check
        scr = screen()
        check("%s: the shell's prompt-shaped text is not answered (no "
              "\"1\", auto_allowed unchanged)" % what,
              "Tab to amend" in scr and
              "1" not in scr.rstrip().split("\n")[-1] and
              row().get("auto_allowed", 0) == before)
        until(lambda: screen().rstrip().endswith("$"), 6)

    def why(pid):
        """An answer row failed: what the fake, the row and the master saw."""
        st = ps(pid, "stat") if pid else "?"
        tt = ps(pid, "tt") if pid else ""
        mac = sys.platform == "darwin"  # macOS ps prints ttys003 as s003
        mode = subprocess.run(["stty", "-f" if mac else "-F",
                               ("/dev/tty" if mac else "/dev/") + tt],
                              capture_output=True, text=True).stdout.split()
        r = row()
        print("    keys=%r auto_allowed=%r claude_foreground=%r"
              % (keys(), r.get("auto_allowed"), r.get("claude_foreground")))
        print("    fake pid=%s stat=%s tty=%s %s" % (
            pid, st, tt, "-icanon (raw)" if "-icanon" in mode
            else "icanon (cooked)" if "icanon" in mode else "mode ?"))
        print("    fake events: %r" % [(e["ev"], e.get("text"))
                                       for e in events()[-4:]])
        print("    screen (last 8):")
        for l in screen().rstrip().split("\n")[-8:]:
            print("      | " + l)
        try:
            with open(trace) as f:
                aa = [l.strip() for l in f if "auto-allow" in l]
        except OSError:
            aa = []
        print("    trace auto-allow (last 6 of %d):" % len(aa))
        for l in aa[-6:]:
            print("      " + l)

    def answered(what, pid):
        ok = (until(lambda: keys() == ["1"], 8) and
              (row().get("auto_allowed") or {}).get("title") ==
              "Bash command")
        check(what, ok)
        if not ok:
            why(pid)

    def result(r):
        try:
            return json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {}

    try:
        run("--config", "auto_allow", "true")
        setctl()
        subprocess.run([DCH, "--spawn", n, "--size", "100x30", "bash",
                        "--norc", "--noprofile", "-i"], capture_output=True,
                       check=True, env=dict(os.environ, DCH_DEBUG=trace))
        until(lambda: "$" in screen(), 10)
        lite = run("--read", n).returncode == 3
        send("claude")
        st = until(lambda: [e for e in events() if e["ev"] == "start"], 15)
        fpid, sid = (st[0]["pid"], st[0]["sid"]) if st else (None, None)
        until(lambda: lite or "auto mode on" in screen(), 10)
        r = until(lambda: row().get("claude_session_id") == sid and row(), 10)
        check("Claude in front: claude_session_id, claude_foreground true",
              fpid and r and r.get("claude_foreground") is True)

        # --- suspended: bash in front -------------------------------------
        os.killpg(os.getpgid(fpid), signal.SIGTSTP)
        check("suspended: bash says Stopped",
              lite or until(lambda: "Stopped" in screen(), 10))
        r = until(lambda: row().get("claude_foreground") is False and row(), 5)
        check("suspended: claude_session_id kept, claude_foreground false",
              r and r.get("claude_session_id") == sid)

        # the frozen composer is still on screen above bash's prompt
        r = run("--say", n, "--json", "--", "hello")
        time.sleep(0.5)
        check("suspended: --say fails not_foreground, nothing typed into "
              "the shell", r.returncode == 1 and
              result(r).get("reason") == "not_foreground" and
              "hello" not in screen())

        if not lite:
            shell_dlg_unanswered("suspended")

        r = run("--stop", n, "--json", timeout=90)
        check("suspended: --stop fails not_foreground, the session kept",
              r.returncode == 1 and
              result(r).get("reason") == "not_foreground" and
              run("--cmd", n).returncode == 0)
        r = run("--run", n, "echo", "RUN-OK")
        check("suspended: --run types the plain burst into the shell",
              r.returncode == 0 and
              (lite or until(lambda: "RUN-OK\n" in screen(), 5)))

        # --- fg: Claude in front again ------------------------------------
        send("fg")
        r = until(lambda: row().get("claude_foreground") is True and row(), 10)
        check("fg: claude_foreground true again",
              r and r.get("claude_session_id") == sid)
        if not lite:
            check("fg: the fake's screen is back",
                  until(lambda: "auto mode on" in screen(), 5))
            setctl(screen=FAKE_DLG)
            answered("fg: the fake Claude's prompt is answered with 1", fpid)

        # --- under script(1), a pty of its own: in front, verbs reach it --
        # (#312: 1.63.0 read script's group off the outer pty and refused)
        SH = "bash --norc --noprofile -i"
        if sys.platform == "darwin":
            wrap = "script -q /dev/null " + SH
        elif "util-linux" in subprocess.run(
                ["script", "--version"], capture_output=True,
                text=True).stdout:
            wrap = "script -q -c '%s' /dev/null" % SH
        else:
            wrap = None
            print("  skip under script(1): not util-linux or macOS script")
        if wrap:
            run("-k", n)
            n = "fs" + TAG
            setctl()
            subprocess.run([DCH, "--spawn", n, "--size", "100x30", "bash",
                            "--norc", "--noprofile", "-i"],
                           capture_output=True, check=True,
                           env=dict(os.environ, DCH_DEBUG=trace))
            until(lambda: "$" in screen(), 10)
            send(wrap)
            time.sleep(1)                   # the inner bash's prompt
            send("claude")
            st = until(lambda: [e for e in events() if e["ev"] == "start"],
                       15)
            spid = st[0]["pid"] if st else None
            r = until(lambda: row().get("claude_session_id") and row(), 10)
            until(lambda: lite or "auto mode on" in screen(), 10)
            check("under script: claude_foreground true",
                  spid and r and r.get("claude_foreground") is True)
            r = run("--say", n, "--json", "--", "hello-under-script")
            check("under script: --say reaches the running Claude",
                  result(r).get("reason") != "not_foreground" and
                  (lite or until(lambda: "hello-under-script"
                  in [e.get("text") for e in events()
                      if e["ev"] in ("line", "turn")], 5)))
            os.killpg(os.getpgid(spid), signal.SIGTSTP)
            r = until(lambda: row().get("claude_foreground") is False and
                      row(), 5)
            check("under script, suspended: claude_foreground false", r)
            r = run("--say", n, "--json", "--", "hello")
            check("under script, suspended: --say fails not_foreground",
                  result(r).get("reason") == "not_foreground")
            if not lite:
                shell_dlg_unanswered("under script, suspended")
            send("fg")
            check("under script, fg: claude_foreground true again",
                  until(lambda: row().get("claude_foreground") is True, 10))
            if not lite:
                # #318: the master read script's group off the outer pty
                check("under script, fg: the fake's screen is back",
                      until(lambda: "auto mode on" in screen(), 5))
                setctl(screen=FAKE_DLG)
                answered("under script: auto-allow answers the running "
                         "Claude's prompt with 1", spid)
                setctl()
                # script itself stopped: the outer bash is in front
                # (SIGSTOP: util-linux script blocks SIGTSTP)
                k = keys()
                os.killpg(os.getpgid(script_of(spid)), signal.SIGSTOP)
                check("script suspended: claude_foreground false",
                      until(lambda: row().get("claude_foreground") is False,
                            5))
                shell_dlg_unanswered("script suspended")
                check("script suspended: the fake got no key", keys() == k)
                send("fg")
                until(lambda: row().get("claude_foreground") is True, 10)
            r = run("--stop", n, "--json", timeout=90)
            check("under script: --stop is not refused not_foreground",
                  result(r).get("reason") != "not_foreground")
    except Exception:
        import traceback
        traceback.print_exc()
        fails += 1
    run("-k", n)
    time.sleep(0.3)
    for p in (fpid, spid):  # safety net: only the fakes this test started
        try:
            if p:
                os.kill(p, signal.SIGKILL)
        except OSError:
            pass
    shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print("FAIL: %d suspended-Claude check(s) failed" % fails)
        return 1
    print("PASS: suspended Claude")
    return 0


if __name__ == "__main__":
    sys.exit(main())
