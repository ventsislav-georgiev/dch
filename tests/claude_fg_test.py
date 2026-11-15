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
and nothing is answered; after fg claude_foreground follows its terminal's
mode (macOS: bash put its line-buffered modes back and script does not set
raw again, so false, and --stop is refused). Then a pty wrapper that never reads the
keyboard (plain unbuffer's shape, MED-AA1): claude_foreground false, its
prompt not answered, --say and --run fail not_foreground (MED-CW1), and once
it exits the shell finds nothing typed; after `stty -icanon` it is still not
in front (LOW-CW2); the same wrapper forwarding the keyboard (-p, raw) is
in front, answered, and --stop is not refused. On dch-lite (no mirror) the
auto-allow checks are skipped.

Run:  python3 tests/claude_fg_test.py        (uses ./dch)
"""
import json, os, shutil, signal, stat, subprocess, sys, tempfile, termios
import time

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


PTYWRAP = r'''#!/usr/bin/env python3
"""ptywrap [-p] cmd...: cmd on a pty of its own, its output copied out.
-p forwards the keyboard with the terminal raw, as script(1) and unbuffer -p
do; without it nothing reads the keyboard, as plain unbuffer."""
import fcntl, os, pty, select, sys, termios, tty
fwd = sys.argv[1] == "-p"
pid, fd = pty.fork()
if pid == 0:
    os.execvp(sys.argv[1 + fwd], sys.argv[1 + fwd:])
fcntl.ioctl(fd, termios.TIOCSWINSZ,
            fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))
if fwd:
    saved = termios.tcgetattr(0)
    tty.setraw(0)
try:
    while True:
        r = select.select([fd] + [0] * fwd, [], [])[0]
        if fd in r:
            try:
                b = os.read(fd, 4096)
            except OSError:
                break
            if not b:
                break
            os.write(1, b)
        if 0 in r:
            os.write(fd, os.read(0, 4096))
finally:
    if fwd:
        termios.tcsetattr(0, termios.TCSAFLUSH, saved)
    os.waitpid(pid, 0)
'''
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
    fails, fpid, spid, fakes, cooked = 0, None, None, [], False

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

    def line_buffered(p):
        """p's terminal is in canonical mode: a wrapper there reads no key."""
        tt = ps(p, "tt")
        fd = os.open("/dev/" + ("tty" + tt if sys.platform == "darwin"
                                else tt), os.O_RDONLY | os.O_NOCTTY |
                     os.O_NONBLOCK)
        try:
            return bool(termios.tcgetattr(fd)[3] & termios.ICANON)
        finally:
            os.close(fd)

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
        sa = subprocess.run(["stty", "-a", "-f" if mac else "-F",
                             ("/dev/tty" if mac else "/dev/") + tt],
                            capture_output=True, text=True).stdout
        mode = sa.split()
        r = row()
        print("    keys=%r auto_allowed=%r claude_foreground=%r"
              % (keys(), r.get("auto_allowed"), r.get("claude_foreground")))
        print("    fake pid=%s stat=%s tty=%s %s" % (
            pid, st, tt, "-icanon (raw)" if "-icanon" in mode
            else "icanon (cooked)" if "icanon" in mode else "mode ?"))
        # #330: discarded (flusho) or stopped (tostop) output shows here
        for l in sa.split("\n"):
            if any(f in l.split() or "-" + f in l.split() for f in (
                    "flusho", "tostop", "icanon", "isig", "ixon")):
                print("    stty: " + l.strip())
        procs = []
        try:   # the shell above the fake, and script(1) when there is one
            procs.append(int(ps(pid, "ppid")))
            procs.append(script_of(pid))
        except (ValueError, RuntimeError):
            pass
        for p in procs:
            print("    %s pid=%d stat=%s (+: foreground)" % (
                os.path.basename(ps(p, "comm")), p, ps(p, "stat")))
        print("    fake events: %r" % [(e["ev"], e.get("text"))
                                       for e in events()[-12:]])
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
                back = until(lambda: "auto mode on" in screen(), 5)
                setctl(screen=FAKE_DLG)
                if not back and sys.platform == "darwin":
                    # #330: on the macOS 26 GitHub runner (2 runs of 2) the
                    # fake is in front with its tty raw after fg, yet
                    # nothing it paints reaches the screen, even after a
                    # ctl change; the master sees no dialog and types
                    # nothing. Linux and a newer local macOS pass. Until
                    # #330 pins it, these two rows skip there; the fake's
                    # stop/cont/raw/paint events in why() say where it hung.
                    time.sleep(2)
                    why(spid)
                    print("  SKIP under script on this macOS: the fake's "
                          "screen did not come back after fg (see #330)")
                else:
                    check("under script, fg: the fake's screen is back",
                          back)
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
                # bash put its own line-buffered modes back when script
                # stopped, and script does not set raw again on SIGCONT:
                # a "1" would wait in that line for the shell (MED-AA1)
                sc = script_of(spid)
                send("fg")
                until(lambda: "+" in ps(sc, "stat"), 10)
                cooked = line_buffered(sc)
                check("script resumed by fg: claude_foreground %s, its "
                      "terminal %s" % (not cooked, "line-buffered" if cooked
                                       else "raw"),
                      "+" in ps(sc, "stat") and until(
                          lambda: row().get("claude_foreground") is
                          (not cooked), 5))
            r = run("--stop", n, "--json", timeout=90)
            check("under script: --stop %s not_foreground" % (
                  "is refused" if cooked else "is not refused"),
                  (result(r).get("reason") == "not_foreground") == cooked)

        # --- a pty wrapper that never reads the keyboard (MED-AA1: plain
        # unbuffer, expect without interact): its terminal stays
        # line-buffered and what is typed there waits for the shell ---
        wrapper = os.path.join(bindir, "ptywrap")
        with open(wrapper, "w") as f:
            f.write(PTYWRAP)
        os.chmod(wrapper, 0o755)
        run("-k", n)
        n = "fw" + TAG
        setctl()
        subprocess.run([DCH, "--spawn", n, "--size", "100x30", "bash",
                        "--norc", "--noprofile", "-i"], capture_output=True,
                       check=True, env=dict(os.environ, DCH_DEBUG=trace))
        until(lambda: "$" in screen(), 10)
        send("ptywrap claude")
        st = until(lambda: [e for e in events() if e["ev"] == "start"], 15)
        wpid = st[0]["pid"] if st else None
        fakes.append(wpid)
        until(lambda: lite or "auto mode on" in screen(), 10)
        r = until(lambda: row().get("claude_session_id") and row(), 10)
        check("plain wrapper: claude_foreground false",
              wpid and r and r.get("claude_foreground") is False)
        if not lite:
            setctl(screen=FAKE_DLG)
            gone = "auto-allow: claude %s not in front" % wpid

            def refused():
                try:
                    with open(trace) as f:
                        return gone in f.read()
                except OSError:
                    return False
            check("plain wrapper: auto-allow leaves the fake's prompt alone "
                  "(no \"1\" typed, auto_allowed null)",
                  until(refused, 15) and row().get("auto_allowed") is None)
        mark = os.path.join(tmp, "SAYRAN")
        r = run("--say", n, "--json", "--", "say-%s\ntouch %s\n" % (TAG, mark))
        check("plain wrapper: --say fails not_foreground, nothing typed",
              result(r).get("reason") == "not_foreground" and
              "say-" + TAG not in screen())
        # MED-CW1: 1.66.0 sent --run's plain burst here
        rmark = os.path.join(tmp, "RUNRAN")
        r = run("--run", n, "touch", rmark)
        check("plain wrapper: --run fails not_foreground, nothing typed",
              r.returncode == 1 and "not_foreground" in r.stderr)
        os.kill(wpid, signal.SIGTERM)    # the wrapper exits with it
        until(lambda: lite or screen().rstrip().endswith("$"), 10)
        run("--send", n, "echo END-$((6*7))\r")
        check("plain wrapper: once it exits the shell ran nothing typed",
              (lite or until(lambda: "END-42" in screen(), 10)) and
              not os.path.exists(mark) and not os.path.exists(rmark) and
              "say-" + TAG not in screen())

        # LOW-CW2: a terminal an earlier command left non-canonical is not
        # the wrapper forwarding the keyboard
        send("stty -icanon")
        send("ptywrap claude")
        st = until(lambda: [e for e in events() if e["ev"] == "start"
                            and e["pid"] != wpid], 15)
        wpid = st[0]["pid"] if st else None
        fakes.append(wpid)
        r = until(lambda: row().get("claude_session_id") and row(), 10)
        check("plain wrapper after stty -icanon: claude_foreground false",
              wpid and r and r.get("claude_foreground") is False)
        os.kill(wpid, signal.SIGTERM)
        until(lambda: lite or screen().rstrip().endswith("$"), 10)
        send("stty sane")

        # --- the same wrapper forwarding the keyboard (-p: raw, as
        # script and unbuffer -p): in front, verbs reach it ---
        setctl()
        send("ptywrap -p claude")
        st = until(lambda: [e for e in events() if e["ev"] == "start"
                            and e["pid"] not in fakes], 15)
        wpid = st[0]["pid"] if st else None
        fakes.append(wpid)
        until(lambda: lite or "auto mode on" in screen(), 10)
        r = until(lambda: row().get("claude_foreground") is True and row(), 10)
        check("forwarding wrapper: claude_foreground true", wpid and r)
        r = run("--say", n, "--json", "--", "hello-fwd")
        check("forwarding wrapper: --say reaches the running Claude",
              result(r).get("reason") != "not_foreground" and
              (lite or until(lambda: "hello-fwd"
              in [e.get("text") for e in events()
                  if e["ev"] in ("line", "turn")], 5)))
        if not lite:
            setctl(screen=FAKE_DLG)
            answered("forwarding wrapper: auto-allow answers the running "
                     "Claude's prompt with 1", wpid)
            setctl()
        r = run("--stop", n, "--json", timeout=90)
        check("forwarding wrapper: --stop is not refused not_foreground",
              result(r).get("reason") != "not_foreground")
    except Exception:
        import traceback
        traceback.print_exc()
        fails += 1
    run("-k", n)
    time.sleep(0.3)
    for p in [fpid, spid] + fakes:  # safety net: only the fakes started here
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
