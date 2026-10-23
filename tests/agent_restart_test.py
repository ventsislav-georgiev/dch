"""Agent-aware `dch --restart --include-agents`: pause Claude, wait, relaunch
with --resume.

Drives tests/fake_claude.py installed as `claude` first on PATH, with HOME
pointing at a temp dir so its session records never touch a real ~/.claude.
Covers the end-to-end restart under a wrapper (`fakewrap wrap claude ...`),
argv rewriting, concurrency (a slow agent does not hold up the rest), the
holds (shell status, subagent panel, a pause queued while busy), the gate
(dialog, draft: nothing typed), timeout, /exit that never comes, a crash,
self-restart, shell-launched Claude, an old master, -f, a decoy resume line
and a mangled (SGR-split, hard-wrapped) exit line. Also: Enter only once the
text shows in the composer (a prompt raised by the paste gets no Enter, a
"[Pasted text #1]" placeholder counts), SIGTERM while waiting (resume typed),
a pending respawn, a crash's last output in the report. The agent step is
opt-in: without --include-agents nothing is typed, and `--restart <name>`
touches only that session's agent. On dch-lite it checks the step is skipped
with a note, only when a session runs claude and the flag is given.

Run:  python3 tests/agent_restart_test.py        (uses ./dch)
"""
import json, os, shutil, socket, stat, subprocess, sys, tempfile, threading
import time, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
TAG = str(os.getpid())
PAUSE = "pause all work, i need to restart you"
RESUME = "resume all work"


def run(*args, timeout=60, env=None):
    return subprocess.run([DCH] + list(args), capture_output=True, text=True,
                          timeout=timeout, env=env)


def until(fn, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    tmp = tempfile.mkdtemp()
    home = os.path.join(tmp, "home")
    fc = os.path.join(tmp, "fc")
    bindir = os.path.join(tmp, "bin")
    proj = os.path.join(tmp, "proj")
    for d in (home, fc, bindir, proj):
        os.mkdir(d)
    os.makedirs(os.path.join(home, ".claude", "sessions"), mode=0o700)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    with open(os.path.join(bindir, "fakewrap"), "w") as f:
        # headroom-like: stays the session's child, claude its grandchild
        f.write('#!/bin/sh\n[ "$1" = wrap ] && shift\n"$@"\nexit $?\n')
    for b in ("claude", "fakewrap"):
        p = os.path.join(bindir, b)
        os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
    with open(os.path.join(fc, "version"), "w") as f:
        f.write("1.0.0")
    os.environ.update(HOME=home, XDG_RUNTIME_DIR=tmp, FAKE_CLAUDE_DIR=fc,
                      PATH=bindir + os.pathsep + os.environ["PATH"],
                      DCH_AGENT_SETTLE_MS="500", DCH_AGENT_STEP_MS="10000",
                      DCH_AGENT_TIMEOUT_MS="30000")
    for k in ("DCH_SESSION", "DCH_SOCKET_DIR", "CLAUDE_CONFIG_DIR",
              "DCH_PAUSE_MSG", "DCH_RESUME_MSG", "DCH_NO_DETECT"):
        os.environ.pop(k, None)
    sockdir = os.path.join(tmp, "dch-%d" % os.getuid())
    os.chdir(proj)
    names, pids = [], set()
    fails = 0

    def check(msg, cond):
        nonlocal fails
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails += 1

    def sess(base, *cmd, **ctl):
        n = "%s%s" % (base, TAG)
        names.append(n)
        if ctl:
            setctl(n, **ctl)
        subprocess.run([DCH, "--spawn", n, "--size", "100x30"] + list(cmd),
                       capture_output=True, check=True)
        return n

    def setctl(n, **ctl):
        with open(os.path.join(fc, n + ".ctl.tmp"), "w") as f:
            json.dump(ctl, f)
        os.rename(os.path.join(fc, n + ".ctl.tmp"),
                  os.path.join(fc, n + ".ctl"))

    def events(n):
        try:
            with open(os.path.join(fc, n + ".log")) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def starts(n):
        return [e for e in events(n) if e["ev"] == "start"]

    def typed(n, pid=None):
        return [e["text"] for e in events(n)
                if e["ev"] == "line" and (pid is None or e["pid"] == pid)]

    def up(n, count=1, t=10.0):
        """The fake's count-th start, once it has painted its screen."""
        s = until(lambda: len(starts(n)) >= count and starts(n)[count - 1], t)
        if s:
            pids.add(s["pid"])
            until(lambda: lite or "auto mode on" in run("--read", n).stdout)
        return s

    def cmd(n):
        r = run("--cmd", n)
        return json.loads(r.stdout) if r.returncode == 0 else None

    def masters(sock):
        out = subprocess.run(["ps", "-eo", "pid,command"],
                             capture_output=True, text=True).stdout
        return [int(l.split()[0]) for l in out.splitlines()
                if "--master-of" in l and sock in l]

    def restart_all(*extra, env=None):
        return run("--restart", "--all", "--include-agents", *extra, env=env,
                   timeout=120)

    lite = False
    try:
        probe = sess("probe", "sh")
        lite = run("--read", probe).returncode == 3
        run("-k", probe)
        names.remove(probe)
        r = run("-ls", "--include-agents")
        check("--include-agents without --restart: usage error",
              r.returncode == 1 and
              "--include-agents only works with --restart" in r.stderr)

        if lite:
            sh = sess("litesh", "sh")
            r = restart_all()
            check("lite: no note when no session runs claude",
                  "agents:" not in r.stdout)
            run("-k", sh)
            names.remove(sh)
            n = sess("lite", "claude")
            up(n)
            with open(os.path.join(fc, "version"), "w") as f:
                f.write("2.0.0")
            r = restart_all()
            check("lite: agent step skipped with a one-line note",
                  "dch-lite has no terminal mirror" in r.stdout)
            r = run("--restart", "--all")
            check("lite: no note without --include-agents",
                  "agents:" not in r.stdout)
            check("lite: nothing typed into the agent", typed(n) == [])
            return finish(fails, tmp, names, pids)

        # --- end to end: a wrapper, two agents, holds, concurrency -------
        a = sess("aga", "fakewrap", "wrap", "claude", "--chrome",
                 "--continue", "--model", "sonnet", "--fork-session",
                 decoy=True)
        b = sess("agb", "claude", "-r", "--session-id=1234", "--", "hi",
                 shell=True, mangle=True)
        c = sess("agc", "claude", subagents=1)
        d = sess("agd", "claude", busy=True)
        sa, sb, sc, sd = up(a), up(b), up(c), up(d)
        asock = os.path.join(sockdir, a + ".sock")
        m0, ino0 = masters(asock), os.lstat(asock).st_ino
        # a turn before the pause puts a decoy resume line on a's screen
        run("--send", a, "hello")
        run("--keys", a, "enter")
        until(lambda: any(e["ev"] == "turn" for e in events(a)))
        time.sleep(1.0)
        with open(os.path.join(fc, "version"), "w") as f:
            f.write("2.0.0")

        proc = subprocess.Popen([DCH, "--restart", "--all", "--include-agents"],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        # four pauses go out one by one first; seen to take ~14s on a busy host
        a2 = up(a, 2, 45.0)
        check("a relaunched while b (shell), c (subagent), d (busy) wait",
              a2 and not any(t == "/exit" for n in (b, c, d)
                             for t in typed(n)))
        check("a got the resume message after the relaunch",
              until(lambda: any(t.startswith(RESUME)
                                for t in typed(a, a2 and a2["pid"]))))
        check("pause queued while busy: d took nothing yet",
              not any(e["ev"] == "turn" for e in events(d)))
        time.sleep(1.5)
        check("shell status holds b", "/exit" not in typed(b))
        check("subagent panel holds c", "/exit" not in typed(c))
        setctl(b, mangle=True)
        setctl(c)
        setctl(d)
        out, _ = proc.communicate(timeout=90)
        print("     | " + out.rstrip().replace("\n", "\n     | "))
        check("--restart --all exits 0", proc.returncode == 0)

        # a: the whole flow
        ta = typed(a, sa["pid"])
        check("a received the pause text, then /exit",
              ta[-2:-1] and ta[-2].startswith(PAUSE) and "TaskStop" in ta[-2]
              and ta[-1] == "/exit")
        want = ["fakewrap", "wrap", "claude", "--resume", sa["sid"],
                "--chrome", "--model", "sonnet"]
        check("a relaunched as wrapper + original args + --resume <id>",
              (cmd(a) or {}).get("argv") == want)
        check("a's new claude got --resume <id> right after the token",
              a2 and a2["argv"] == want[3:])
        check("a resumed the real id, not the decoy",
              a2 and a2["sid"] == sa["sid"] and
              "%s: claude 1.0.0 -> 2.0.0, resumed %s" % (a, sa["sid"]) in out)
        check("a relaunched in the same cwd",
              a2 and os.path.realpath(a2["cwd"]) == os.path.realpath(proj))
        check("a: same master pid, same socket inode",
              masters(asock) == m0 and os.lstat(asock).st_ino == ino0)
        # b: -r with no value, --session-id=, positional after --
        b2 = up(b, 2)
        check("b relaunched: -r (no value) and --session-id= dropped, "
              "--resume before --",
              (cmd(b) or {}).get("argv") ==
              ["claude", "--resume", sb["sid"], "--", "hi"])
        check("b: SGR-split, wrapped exit line still gives the real id",
              b2 and b2["sid"] == sb["sid"] and "exit line said" not in out
              and "no resume line" not in out)
        check("c restarted once its subagent panel cleared",
              up(c, 2) and RESUME in " ".join(typed(c)))
        ev = [(e["ev"], e.get("text", "")) for e in events(d)]
        tp = [i for i, e in enumerate(ev)
              if e[0] == "turn" and e[1].startswith(PAUSE)]
        tx = [i for i, e in enumerate(ev) if e == ("line", "/exit")]
        check("d: the queued pause was taken before /exit",
              tp and tx and tp[0] < tx[0])
        check("every agent got the resume message",
              all(any(t.startswith(RESUME) for t in typed(n))
                  for n in (a, b, c, d)))

        # --- current agents are left alone without -f -------------------
        counts = {n: len(typed(n)) for n in (a, b, c, d)}
        r = restart_all()
        check("all current: one summary line, nothing typed",
              "all claude agents already on claude 2.0.0" in r.stdout and
              all(len(typed(n)) == counts[n] for n in (a, b, c, d)) and
              r.returncode == 0)

        # --- opt-in, and one session at a time ---------------------------
        def snap():
            return {n: (len(starts(n)), len(typed(n))) for n in (a, b, c, d)}
        with open(os.path.join(fc, "version"), "w") as f:
            f.write("2.5.0")
        z = sess("agz", "sh")
        s0 = snap()
        r = run("--restart", "--all", timeout=120)
        check("--restart --all without --include-agents: masters only, no "
              "agent lines, nothing typed",
              r.returncode == 0 and "claude" not in r.stdout and
              snap() == s0)
        r = run("--restart", c, timeout=120)
        check("--restart <name> without the flag: agent untouched",
              r.returncode == 0 and "claude" not in r.stdout and
              snap() == s0)
        r = run("--restart", c, "--include-agents", timeout=120)
        s1 = snap()
        check("--restart <name> --include-agents: only that stale agent",
              r.returncode == 0 and up(c, 3) and
              "%s: claude 2.0.0 -> 2.5.0" % c in r.stdout and
              all(s1[n] == s0[n] for n in (a, b, d)))
        r = run("--restart", c, "--include-agents", timeout=120)
        check("--restart <name> --include-agents, current: one line for it",
              r.returncode == 0 and snap() == s1 and
              "%s: already on claude 2.5.0" % c in r.stdout and
              "all claude agents" not in r.stdout)
        r = run("-f", "--restart", c, "--include-agents", timeout=120)
        check("-f --restart <name> --include-agents restarts a current agent",
              r.returncode == 0 and up(c, 4) and
              "%s: claude 2.5.0 -> 2.5.0" % c in r.stdout and
              all(snap()[n] == s0[n] for n in (a, b, d)))
        r = run("--restart", z, "--include-agents", timeout=120)
        check("--restart <sh session> --include-agents: no agent line",
              r.returncode == 0 and len(r.stdout.splitlines()) == 1 and
              r.stdout.startswith("%s: restarted on dch" % z))
        with open(os.path.join(fc, "version"), "w") as f:
            f.write("2.0.0")
        for n in (b, c, d, z):
            run("-k", n)
            names.remove(n)
        # a long pause text: the composer shows Claude's paste placeholder
        longp = PAUSE + ". " + "please " * 150
        r = restart_all("-f", env=dict(os.environ, DCH_PAUSE_MSG=longp))
        check("-f restarts a current agent; a collapsed paste counts as "
              "landed", r.returncode == 0 and up(a, 3) and
              "%s: claude 2.0.0 -> 2.0.0" % a in r.stdout and
              longp.strip() in [t.strip() for t in typed(a)])

        # --- dch -k after an agent restart leaves no claude alive ---------
        a3 = up(a, 3) or {"pid": 0}
        wrapper = (cmd(a) or {}).get("pid")
        run("-k", a)
        names.remove(a)
        check("dch -k after an agent restart leaves no claude behind",
              until(lambda: not alive(a3["pid"]) and
                    not (wrapper and alive(wrapper)), 5.0))

        # --- failure paths: each reported, exit 1 ------------------------
        # Pauses go out one after another and a paste that never lands
        # takes DCH_AGENT_LAND_MS, so this batch gets a pause timeout with real
        # headroom; the timeout case runs on its own below.
        g = sess("agg", "claude", dialog=True)
        h = sess("agh", "sh", "-c", "claude; sleep 60")
        dr = sess("agr", "claude", draft="half typed")
        x = sess("agx", "claude", noexit=True)
        k = sess("agk", "claude", crash=True)
        pp = sess("agp", "claude", dialog_on_paste=True, pasted_decoy=True)
        q = sess("agq", "claude")
        for n in (g, h, dr, x, k, pp, q):
            up(n)
        run("--respawn", q, "--", "true")
        with open(os.path.join(fc, "version"), "w") as f:
            f.write("3.0.0")
        old = os.path.join(sockdir, "old%s.sock" % TAG)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(old)
        srv.listen(8)

        def oldmaster():
            # predates MSG_CMD: answers every verb with DCH_ST_UNSUP
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                conn.settimeout(2)
                try:
                    if conn.recv(4096):
                        conn.sendall(bytes([11, 5, 0]))
                except OSError:
                    pass
                conn.close()
        threading.Thread(target=oldmaster, daemon=True).start()
        xsock = os.path.join(sockdir, x + ".sock")
        r = restart_all(env=dict(os.environ, DCH_AGENT_TIMEOUT_MS="60000",
                                 DCH_AGENT_STEP_MS="3000"))
        srv.close()
        print("     | " + r.stdout.rstrip().replace("\n", "\n     | "))
        check("failures exit 1", r.returncode == 1)
        check("dialog up: reported, nothing typed",
              "%s: NOT restarted — a prompt or dialog" % g in r.stdout and
              typed(g) == [])
        check("draft in the composer: reported, nothing typed",
              "%s: NOT restarted — the composer holds an unsent draft" % dr
              in r.stdout and typed(dr) == [])
        check("claude started in a shell: skipped",
              "%s: skipped — claude was started inside the session's shell"
              % h in r.stdout and typed(h) == [])
        cx = cmd(x)
        check("/exit never happens: hold cancelled, claude still running",
              "%s: NOT restarted — /exit did not end claude" % x in r.stdout
              and cx and cx["state"] == "running" and len(starts(x)) == 1)
        check("crash on /exit: reported with its last output, not "
              "relaunched, session ended",
              "%s: NOT restarted — claude exited with status 3" % k
              in r.stdout and "Error: fake claude crashed / at teardown "
              "(fake.js:42))" in r.stdout and len(starts(k)) == 1 and
              until(lambda: not os.path.exists(
                  os.path.join(sockdir, k + ".sock"))))
        check("prompt raised by the paste: no Enter reaches it, reported",
              "%s: NOT restarted — the pause message did not land in the "
              "composer; Enter not pressed" % pp in r.stdout and
              not any(e["ev"] in ("enter_dialog", "line")
                      for e in events(pp)))
        check("pending respawn: skipped as such",
              "%s: skipped — a respawn is pending (armed)" % q in r.stdout
              and typed(q) == [])
        run("--respawn", q, "--cancel")
        check("old master: skipped with a note",
              "old%s: skipped — its master predates agent restart" % TAG
              in r.stdout)
        if os.path.exists(old):
            os.unlink(old)
        for n in (g, h, dr, x, k, pp, q):
            if n in names:
                run("-k", n)
                names.remove(n)

        # --- pause timeout, alone so nothing else eats its clock ----------
        t = sess("agt", "claude", shell=True)
        up(t)
        with open(os.path.join(fc, "version"), "w") as f:
            f.write("3.5.0")
        r = restart_all(env=dict(os.environ, DCH_AGENT_TIMEOUT_MS="3000"))
        print("     | " + r.stdout.rstrip().replace("\n", "\n     | "))
        check("timeout: reported, resume message typed, no /exit",
              r.returncode == 1 and
              "%s: NOT restarted — not ready within 3s (status shell" % t
              in r.stdout and typed(t)[0].startswith(PAUSE) and
              typed(t)[-1].startswith(RESUME) and "/exit" not in typed(t))
        run("-k", t)
        names.remove(t)

        # --- SIGTERM while waiting: the paused agent is resumed -----------
        i = sess("agi", "claude", shell=True)
        up(i)
        proc = subprocess.Popen([DCH, "--restart", "--all", "--include-agents",
                                 "-f"],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        first = ""
        while "pause message sent" not in first:
            first = proc.stdout.readline()
            if not first:
                break
        until(lambda: typed(i))
        time.sleep(0.5)
        proc.send_signal(15)
        out = first + proc.communicate(timeout=30)[0]
        print("     | " + out.rstrip().replace("\n", "\n     | "))
        ci = cmd(i)
        check("SIGTERM while waiting: resume typed, exit non-zero, session "
              "still running",
              proc.returncode not in (0, None) and
              "%s: NOT restarted — interrupted (signal 15) while waiting for "
              "it to pause; resume message typed" % i in out and
              typed(i)[-1].startswith(RESUME) and "/exit" not in typed(i) and
              ci and ci["state"] == "running" and len(starts(i)) == 1)
        run("-k", i)
        names.remove(i)

        # --- SIGTERM with nobody reading stdout: no SIGPIPE death --------
        i1 = sess("agu", "claude", shell=True)
        i2 = sess("agv", "claude", shell=True)
        up(i1)
        up(i2)
        proc = subprocess.Popen([DCH, "--restart", "--all", "--include-agents",
                                 "-f"],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        seen = 0
        while seen < 2:
            line = proc.stdout.readline()
            if not line:
                break
            seen += "pause message sent" in line
        until(lambda: typed(i1) and typed(i2))
        time.sleep(0.5)
        proc.stdout.close()         # the reader goes away (`| head`)
        proc.send_signal(15)
        proc.wait(timeout=30)
        check("SIGTERM with stdout closed: both waiting agents resumed",
              proc.returncode != 0 and
              until(lambda: all(typed(n)[-1].startswith(RESUME)
                                for n in (i1, i2)), 5.0) and
              all("/exit" not in typed(n) for n in (i1, i2)))
        for n in (i1, i2):
            run("-k", n)
            names.remove(n)

        # --- self-restart: the orchestrator runs under the agent ---------
        s = sess("ags", "claude")
        up(s)
        with open(os.path.join(fc, "version"), "w") as f:
            f.write("4.0.0")
        run("--send", s, "!run '%s' --restart --all --include-agents" % DCH)
        run("--keys", s, "enter")
        so = os.path.join(fc, s + ".run.out")
        check("run under the agent: its own session skipped",
              until(lambda: os.path.exists(so) and
                    "run from outside dch to restart this one" in
                    open(so).read(), 15.0))
        check("run under the agent: nothing typed into it",
              typed(s) == ["!run '%s' --restart --all --include-agents" % DCH])
    except Exception:
        traceback.print_exc()
        fails += 1
    return finish(fails, tmp, names, pids)


def finish(fails, tmp, names, pids):
    for n in names:
        subprocess.run([DCH, "-k", n], capture_output=True)
    time.sleep(0.3)
    for p in pids:          # safety net: only the fakes this test started
        if alive(p):
            try:
                os.kill(p, 9)
            except OSError:
                pass
    shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print("FAIL: %d agent restart check(s) failed" % fails)
        return 1
    print("PASS: agent-aware restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())
