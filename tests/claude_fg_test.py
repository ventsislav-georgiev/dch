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
it; suspended inside script's bash, --say fails not_foreground; after fg,
--stop is not refused. On dch-lite (no mirror) the auto-allow checks are skipped.

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

    def result(r):
        try:
            return json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {}

    try:
        run("--config", "auto_allow", "true")
        setctl()
        subprocess.run([DCH, "--spawn", n, "--size", "100x30", "bash",
                        "--norc", "--noprofile", "-i"],
                       capture_output=True, check=True)
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
            # the shell prints a prompt-shaped text: not Claude's to answer
            send("clear; cat %s; sleep 4" % dlg)
            until(lambda: "Tab to amend" in screen(), 5)
            time.sleep(2.5)                  # past AA_QUIET_MS and a check
            scr = screen()
            check("suspended: the shell's prompt-shaped text is not "
                  "answered (no \"1\", no auto_allowed)",
                  "Tab to amend" in scr and
                  "1" not in scr.rstrip().split("\n")[-1] and
                  row().get("auto_allowed", 0) is None)
            until(lambda: screen().rstrip().endswith("$"), 6)

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
            until(lambda: "auto mode on" in screen(), 5)
            setctl(screen=FAKE_DLG)
            check("fg: the fake Claude's prompt is answered with 1",
                  until(lambda: keys() == ["1"], 8) and
                  (row().get("auto_allowed") or {}).get("title") ==
                  "Bash command")

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
                           capture_output=True, check=True)
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
