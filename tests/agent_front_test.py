"""--say, --stop and --restart --include-agents pick the agent as --ls-json
"agent" does (#680): with a Claude and a Codex both under the session's
program, the one nearest the program; a tie, Claude.

Codex in front: the fake Codex (tests/fake_codex.c) is the program, its TUI
(tests/fake_codex_tui.py) started by a wrapper that first runs the fake
Claude (tests/fake_claude.py) on a pty of its own, so a live Claude record
sits two generations below. --say pastes into Codex (then Tab); --stop and
--restart --include-agents say "agent":"codex" (no thread id known, so
skipped no_thread with nothing typed). Claude in front: the fake Claude is
the program and runs a fake Codex from its bash mode; --say goes through
Claude's gate, nothing reaches the Codex, and --restart / --stop say
"agent":"claude". No real Claude or Codex runs; HOME and the runtime dir are
temporary.

Run:  python3 tests/agent_front_test.py        (uses ./dch)
"""
import json, os, shutil, stat, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
TAG = str(os.getpid())
fails = 0

# the Codex TUI's wrapper: a fake Claude below it on its own pty, kept open
# across the exec, then the Codex screens
WRAP = """import os, pty, subprocess, sys
m, s = pty.openpty()
os.set_inheritable(m, True)
subprocess.Popen([%r], stdin=s, stdout=s, stderr=s)
os.close(s)
os.execvp("python3", ["python3", %r])
"""


def check(msg, cond, extra=""):
    global fails
    print("  %s %s%s" % ("ok  " if cond else "FAIL", msg,
                         "" if cond else " (%s)" % (extra,)))
    if not cond:
        fails += 1


def until(fn, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.1)
    return fn()


def main():
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchaf"))
    home, bindir, fc = (os.path.join(tmp, d) for d in ("home", "bin", "fc"))
    for d in (home, bindir, fc):
        os.makedirs(d)
    os.makedirs(os.path.join(home, ".claude", "sessions"), mode=0o700)
    b = subprocess.run(["cc", "-o", os.path.join(bindir, "codex"),
                        os.path.join(HERE, "fake_codex.c")],
                       capture_output=True, text=True, timeout=60)
    if b.returncode:
        print("FAIL: the fake codex does not build:", b.stderr)
        return 1
    fake_claude = os.path.join(bindir, "claude")
    shutil.copy(os.path.join(HERE, "fake_claude.py"), fake_claude)
    os.chmod(fake_claude, os.stat(fake_claude).st_mode | stat.S_IXUSR)
    wrap = os.path.join(tmp, "wrap_tui.py")
    with open(wrap, "w") as f:
        f.write(WRAP % (fake_claude, os.path.join(HERE, "fake_codex_tui.py")))
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("DCH_", "CLAUDE", "CODEX", "Malloc",
                                "FAKE_"))}
    env.update(HOME=home, XDG_RUNTIME_DIR=tmp,
               XDG_STATE_HOME=os.path.join(tmp, "state"),
               DCH_CONFIG=os.path.join(tmp, "config"), DCH_LOGIN_ENV="0",
               DCH_NO_REMOTE="1", DCH_NO_GITHUB="1", FAKE_CLAUDE_DIR=fc,
               DCH_RECORD_MS="300", DCH_AGENT_SETTLE_MS="600",
               DCH_AGENT_TIMEOUT_MS="20000",
               CODEX_HOME=os.path.join(home, ".codex"),
               PATH=bindir + os.pathsep + env.get("PATH", "/usr/bin:/bin"))
    names = []

    def run(*args, extra=None):
        return subprocess.run([DCH] + list(args), capture_output=True,
                              text=True, timeout=120, cwd=tmp,
                              env=dict(env, **(extra or {})))

    def rows():
        try:
            return {x["name"]: x for x in json.loads(run("--ls-json").stdout)}
        except ValueError:
            return {}

    def final(r):
        for l in reversed(r.stdout.splitlines()):
            try:
                o = json.loads(l)
            except ValueError:
                continue
            if "result" in o:
                return o
        return {"raw": r.stdout + r.stderr}

    def keys(d):
        try:
            with open(os.path.join(d, "keys.log")) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    try:
        p = "probe" + TAG
        run("--spawn", p, "sh")
        lite = run("--read", p).returncode == 3
        run("-k", p)
        print("build:", "lite" if lite else "full")

        # Codex in front, a live Claude two generations below it
        n = "cxcl" + TAG
        d = os.path.join(tmp, n)
        os.mkdir(d)
        with open(os.path.join(d, "mode"), "w") as f:
            f.write("idle")
        names.append(n)
        r = run("--spawn", n, "--size", "100x30", "codex", "--no-daemon",
                extra=dict(FAKE_CODEX_DIR=d, FAKE_CODEX_TUI=wrap))
        check("codex-front: spawned", r.returncode == 0, r.stderr)
        check("codex-front: --ls-json says codex, a Claude joined below",
              until(lambda: (lambda x: x.get("agent") == "codex" and
                             x.get("claude_session_id"))(rows().get(n, {}))),
              rows().get(n))
        if not lite:
            until(lambda: "Ask Codex" in run("--read", n).stdout)
        r = run("--say", n, "--json", "--", "hello codex")
        j = final(r)
        k = keys(d)
        if lite:
            check("lite codex-front --say: skipped/no_screen, nothing typed",
                  j.get("reason") == "no_screen" and k == [], (j, k))
        else:
            check("codex-front --say: sent, pasted into Codex, then Tab",
                  j.get("result") == "ok" and
                  "\x1b[200~hello codex\x1b[201~" in "".join(k) and
                  k[-1:] == ["\t"], (j, k))
        nk = len(keys(d))
        j = final(run("--restart", n, "--include-agents", "--json"))
        check("codex-front --restart --include-agents: agent codex",
              j.get("agent") == "codex", j)
        j = final(run("--stop", n, "--json"))
        check("codex-front --stop: agent codex", j.get("agent") == "codex", j)
        check("codex-front: restart/stop typed nothing (no thread id)",
              len(keys(d)) == nk, keys(d)[nk:])

        # Claude in front, a Codex run from its bash mode below it
        n = "clcx" + TAG
        names.append(n)
        r = run("--spawn", n, "--size", "100x30", "claude")
        check("claude-front: spawned", r.returncode == 0, r.stderr)
        check("claude-front: the Claude's record",
              until(lambda: rows().get(n, {}).get("claude_session_id")),
              rows().get(n))
        cxd = os.path.join(tmp, n + "cx")
        os.mkdir(cxd)
        if not lite:
            until(lambda: "❯" in run("--read", n).stdout)
        run("--send", n, "!")
        if not lite:
            until(lambda: "!" in run("--read", n).stdout)
        run("--send", n, "run FAKE_CODEX_DIR=%s codex\r" % cxd)
        check("claude-front: the Codex below runs",
              until(lambda: os.path.exists(os.path.join(cxd, "argv.log"))))
        check("claude-front: --ls-json says claude",
              until(lambda: rows().get(n, {}).get("agent") == "claude"),
              rows().get(n))
        j = final(run("--say", n, "--json", "--", "hello claude"))
        if lite:
            check("lite claude-front --say: skipped/no_screen",
                  j.get("reason") == "no_screen", j)
        else:
            check("claude-front --say: through Claude's gate, sent",
                  j.get("result") == "ok", j)
        check("claude-front --say: nothing reached the Codex",
              keys(cxd) == [], keys(cxd))
        j = final(run("--restart", n, "--include-agents", "--json"))
        check("claude-front --restart --include-agents: agent claude",
              j.get("agent") == "claude", j)
        j = final(run("--stop", n, "--json"))
        check("claude-front --stop: agent claude", j.get("agent") == "claude",
              j)
    finally:
        for x in names:
            run("-k", x)
        time.sleep(0.3)
        shutil.rmtree(tmp, ignore_errors=True)
    print("FAIL: %d" % fails if fails else "PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
