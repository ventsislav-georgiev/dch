"""--ls-json "agent" (#677): the live agent under each session's program,
"claude", "codex" or null, read by the client from the kernel.

The fake Claude (tests/fake_claude.py as `claude`) and the fake Codex
(tests/fake_codex.c built as `codex`) each as the session's program and each
typed at a `bash -i` prompt; a plain shell is null; two sibling Claudes
(claude_session_id null) still say claude; nested, the one nearest the
program wins both ways (a Claude run by a Codex: codex; a Codex run from a
Claude's bash mode: claude). Every row has "agent" just before "cwd", cwd
last. No real Claude or Codex runs; HOME and the runtime dir are temporary.

Run:  python3 tests/ls_json_agent_test.py        (uses ./dch)
"""
import json, os, shutil, stat, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
TAG = str(os.getpid())
fails = 0


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
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchag"))
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
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("DCH_", "CLAUDE", "CODEX", "Malloc",
                                "FAKE_"))}
    env.update(HOME=home, XDG_RUNTIME_DIR=tmp,
               XDG_STATE_HOME=os.path.join(tmp, "state"),
               DCH_CONFIG=os.path.join(tmp, "config"), DCH_LOGIN_ENV="0",
               DCH_NO_REMOTE="1", DCH_NO_GITHUB="1", FAKE_CLAUDE_DIR=fc,
               CODEX_HOME=os.path.join(home, ".codex"), PS1="$ ",
               BASH_SILENCE_DEPRECATION_WARNING="1", PROMPT_COMMAND="",
               PATH=bindir + os.pathsep + env.get("PATH", "/usr/bin:/bin"))
    cxlog = os.path.join(tmp, "codex.log")
    env["FAKE_CODEX_LOG"] = cxlog
    names = []

    def run(*args, extra=None):
        return subprocess.run([DCH] + list(args), capture_output=True,
                              text=True, timeout=60, cwd=tmp,
                              env=dict(env, **(extra or {})))

    def spawn(base, *cmd, extra=None):
        n = base + TAG
        names.append(n)
        r = run("--spawn", n, "--size", "100x30", *cmd, extra=extra)
        check("%s: spawned" % base, r.returncode == 0, r.stderr)
        return n

    def rows():
        try:
            return {x["name"]: x for x in json.loads(run("--ls-json").stdout)}
        except ValueError:
            return {}

    def agent_is(n, want):
        return until(lambda: rows().get(n, {}).get("agent", "?") == want)

    def typed(n, text):
        until(lambda: run("--read", n).stdout.rstrip().endswith("$"))
        run("--send", n, text + "\r")

    try:
        cl = spawn("cl", "claude")
        sh_cl = spawn("shcl", "bash", "--norc", "-i")
        cx = spawn("cx", "codex", extra={"FAKE_CODEX_TUI": ""})
        sh_cx = spawn("shcx", "bash", "--norc", "-i")
        sh = spawn("sh", "bash", "--norc", "-i")
        two = spawn("two", "sh", "-c", "claude </dev/tty & claude; wait")
        # the fake Codex runs its TUI script as its child: a Claude below it
        cxcl = spawn("cxcl", "codex",
                     extra={"FAKE_CODEX_TUI": fake_claude})
        typed(sh_cl, "claude")
        typed(sh_cx, "codex")

        check("fake Claude as the program: claude", agent_is(cl, "claude"),
              rows().get(cl))
        check("fake Claude typed at a shell prompt: claude",
              agent_is(sh_cl, "claude"), rows().get(sh_cl))
        check("fake Codex as the program: codex", agent_is(cx, "codex"),
              rows().get(cx))
        check("fake Codex typed at a shell prompt: codex",
              agent_is(sh_cx, "codex"), rows().get(sh_cx))
        until(lambda: run("--read", sh).stdout.rstrip().endswith("$"))
        r = rows().get(sh, {})
        check("a plain shell: null", "agent" in r and r["agent"] is None, r)
        r = until(lambda: (lambda x: x if x.get("agent") == "claude" and
                           x.get("claude_session_id", 1) is None else None)
                  (rows().get(two, {})))
        check("two Claudes (claude_session_id null): still claude",
              bool(r), [rows().get(two, {}).get(k) for k in
                         ("agent", "claude_session_id")])
        check("a Claude below a Codex: codex, the one nearest the program",
              agent_is(cxcl, "codex") and
              until(lambda: rows().get(cxcl, {}).get("claude_session_id")),
              rows().get(cxcl))
        # a Codex run from the Claude's bash mode ("!", then "run <cmd>")
        run("--send", cl, "!")
        until(lambda: "!" in run("--read", cl).stdout)
        run("--send", cl, "run codex\r")
        out = os.path.join(fc, cl + ".run.out")

        def codex_below_claude():
            try:   # one line per fake Codex started: cx, shcx, cxcl, this
                return len(open(cxlog).read().splitlines()) >= 4
            except OSError:
                return False
        check("the Codex under the Claude runs", until(codex_below_claude),
              open(out).read() if os.path.exists(out) else "no run.out")
        check("a Codex below a Claude: claude", agent_is(cl, "claude") and
              rows().get(cl, {}).get("agent") == "claude", rows().get(cl))

        # #679: a Codex behind 70 background jobs of its shell is still seen
        up = os.path.join(tmp, "many.up")
        many = spawn("many", "sh", "-c", "codex </dev/tty & sleep 1; "
                     "i=0; while [ $i -lt 70 ]; do sleep 60 & "
                     "i=$((i+1)); done; : > %s; wait" % up,
                     extra={"FAKE_CODEX_TUI": ""})
        # all 70 jobs started: only then is the Codex past the old 64 cap
        check("the 70 jobs run", until(lambda: os.path.exists(up)))
        check("a Codex behind 70 sibling jobs: codex (--ls-json)",
              rows().get(many, {}).get("agent") == "codex", rows().get(many))
        r = run("--stop", many, "--json")
        check("a Codex behind 70 sibling jobs: --stop sees it (agent codex)",
              '"agent":"codex"' in r.stdout, r.stdout)

        rs = rows()
        bad = [n for n in names
               if n in rs and list(rs[n])[-2:] != ["agent", "cwd"]]
        check("every row: agent just before cwd, cwd last",
              not bad and all(n in rs for n in names),
              [list(rs.get(n, {}))[-3:] for n in names])
    finally:
        for n in names:
            run("-k", n)
        time.sleep(0.3)
        shutil.rmtree(tmp, ignore_errors=True)
    print("FAIL: %d" % fails if fails else "PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
