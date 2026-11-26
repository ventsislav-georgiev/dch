"""`dch --stop` and `--restart --include-agents` on a Codex (#659).

A native `codex` (tests/fake_codex.c) runs tests/fake_codex_tui.py, which
paints Codex 0.167's screens (parity spec A1-A5), logs every key it reads
and its argv, answers /quit with Codex's exit summary (`codex resume <id>`,
spec B) and marks any Ctrl-C byte. HOME and the runtime dir are temporary;
no real Codex runs. Covers: --stop on an idle Codex (the pause message
pasted and Tab, /quit then Enter, the record ended "stopped" with the thread
id, --revive relaunching `codex resume <id> -C <cwd>`); the exit line's id
over the live one, and the live one with no exit line; a busy Codex waited
out until idle; a dialog (failed/prompt, no key); a daemon-attached and a
--remote Codex (skipped/codex_daemon, no key); --restart --include-agents
relaunching it `codex resume <id> -C <cwd> ... <resume message>`; no Ctrl-C
byte, ever.

Run:  python3 tests/stop_codex_test.py        (uses ./dch)
"""
import json, os, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
TAG = str(os.getpid())
T1 = "019a5555-1111-7222-8333-444455556601"
T2 = "019a5555-1111-7222-8333-444455556602"
T3 = "019a5555-1111-7222-8333-444455556603"
PAUSE, RESUME = "pause now please", "carry on now"
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
        time.sleep(0.05)
    return fn()


def main():
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchstc"))
    home, bindir = os.path.join(tmp, "home"), os.path.join(tmp, "bin")
    state = os.path.join(tmp, "state")
    os.makedirs(home)
    os.makedirs(bindir)
    b = subprocess.run(["cc", "-o", os.path.join(bindir, "codex"),
                        os.path.join(HERE, "fake_codex.c")],
                       capture_output=True, text=True, timeout=60)
    if b.returncode:
        print("FAIL: the fake codex does not build:", b.stderr)
        return
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("DCH_", "CLAUDE", "CODEX", "Malloc"))}
    env.update(HOME=home, XDG_RUNTIME_DIR=tmp, XDG_STATE_HOME=state,
               DCH_CONFIG=os.path.join(tmp, "config"), DCH_LOGIN_ENV="0",
               DCH_NO_REMOTE="1", DCH_NO_GITHUB="1", DCH_RECORD_MS="300",
               DCH_AGENT_SETTLE_MS="600", DCH_AGENT_TIMEOUT_MS="30000",
               DCH_PAUSE_MSG=PAUSE, DCH_RESUME_MSG=RESUME,
               CODEX_HOME=os.path.join(home, ".codex"),
               FAKE_CODEX_TUI=os.path.join(HERE, "fake_codex_tui.py"),
               PATH=bindir + os.pathsep + env.get("PATH", "/usr/bin:/bin"))
    recdir = os.path.join(state, "dch", "sessions",
                          os.path.join(tmp, "dch-%d" % os.getuid())
                          .replace("/", "-"))
    names, dirs = [], []

    def run(*args, extra=None, timeout=120):
        return subprocess.run([DCH] + list(args), capture_output=True,
                              text=True, timeout=timeout,
                              env=dict(env, **(extra or {})), cwd=tmp)

    def sess(base, mode, argv=("--no-daemon",), extra=None, prog="codex"):
        n = "%s%s" % (base, TAG)
        d = os.path.join(tmp, n)
        os.mkdir(d)
        with open(os.path.join(d, "mode"), "w") as f:
            f.write(mode)
        names.append(n)
        dirs.append(d)
        e = dict(FAKE_CODEX_DIR=d, **(extra or {}))
        r = run("--spawn", n, "--size", "100x30", prog, *argv, extra=e)
        check("%s: spawned" % n, r.returncode == 0, r.stderr)
        want = {"idle": "Ask Codex", "busy": "to interrupt)",
                "dialog": "Press enter to confirm"}[mode]
        until(lambda: want in run("--read", n).stdout)
        return n, d, e

    def keys(d):
        try:
            with open(os.path.join(d, "keys.log")) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def argvs(d):
        try:
            with open(os.path.join(d, "argv.log")) as f:
                return [l.rstrip("\n").split("\x1f") for l in f]
        except OSError:
            return []

    def rec(n):
        try:
            return json.load(open(os.path.join(recdir, n + ".json")))
        except (OSError, ValueError):
            return None

    def final(r):
        for l in reversed(r.stdout.splitlines()):
            try:
                o = json.loads(l)
            except ValueError:
                continue
            if "result" in o:
                return o
        return {"raw": r.stdout + r.stderr}

    def sent_pause_then_quit(k):
        s = "".join(k)
        p = s.find("\x1b[200~%s\x1b[201~" % PAUSE)
        q = s.find("/quit")
        return p >= 0 and s[p:].count("\t") >= 1 and q > p and \
            s[q:].startswith("/quit") and "\r" in s[q:]

    try:
        p = "probe%s" % TAG
        run("--spawn", p, "sh")
        lite = run("--read", p).returncode == 3
        run("-k", p)
        if lite:
            print("build: lite (no mirror): nothing to test")
            return

        # 1. idle: pause pasted + Tab, /quit + Enter, ended stopped with
        # the exit line's id (over the live one), revived resumed
        n, d, e = sess("idle", "idle", extra={"FAKE_CODEX_TID": T1,
                                               "FAKE_CODEX_SNAP_TID": T2})
        until(lambda: ((rec(n) or {}).get("codex") or {})
              .get("thread_id") == T2, 10)
        r = run("--stop", n, "--json")
        j = final(r)
        k = keys(d)
        check("idle: --stop ok, exit 0", r.returncode == 0 and
              j.get("result") == "ok", (r.returncode, r.stdout, r.stderr))
        check("idle: the pause message pasted, Tab, then /quit and Enter",
              sent_pause_then_quit(k), k)
        check("idle: --stop reports the exit line's thread id",
              j.get("session_id") == T1, j)
        rc = until(lambda: (rec(n) or {}).get("end") and rec(n), 10) or {}
        check("idle: the record ended stopped with the codex id",
              rc.get("end") == "stopped" and
              (rc.get("codex") or {}).get("thread_id") == T1, rc)
        r = run("--revive", n, extra=e)
        got = until(lambda: len(argvs(d)) > 1 and argvs(d)[1])
        check("idle: --revive relaunches codex resume <id> -C <cwd>",
              got and got[1:5] == ["resume", T1, "-C", tmp] and
              "--no-daemon" in got, (r.stdout, r.stderr, got))
        run("-k", n)

        # 2. no exit line: the thread id seen live
        n, d, e = sess("live", "idle", extra={"FAKE_CODEX_SNAP_TID": T3})
        r = run("--stop", n, "--json")
        j = final(r)
        check("live: --stop ok with the live thread id",
              r.returncode == 0 and j.get("result") == "ok" and
              j.get("session_id") == T3, (r.stdout, r.stderr))
        rc = until(lambda: (rec(n) or {}).get("end") and rec(n), 10) or {}
        check("live: the record ended stopped with it",
              rc.get("end") == "stopped" and
              (rc.get("codex") or {}).get("thread_id") == T3, rc)

        # 3. busy: the pause queues (Tab); /quit waits for idle
        n, d, e = sess("busy", "busy", extra={"FAKE_CODEX_TID": T1})
        pr = subprocess.Popen([DCH, "--stop", n, "--json"], env=env, cwd=tmp,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True)
        q = until(lambda: "Queued follow-up inputs" in
                  run("--read", n).stdout, 10)
        time.sleep(2.5)
        k = keys(d)
        check("busy: the pause message queued, no Enter, no /quit yet",
              q and pr.poll() is None and "/quit" not in "".join(k) and
              "\r" not in "".join(k), (q, k))
        with open(os.path.join(d, "mode"), "w") as f:
            f.write("idle")
        try:
            out, err = pr.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            pr.kill()
            out, err = pr.communicate()
        j = final(subprocess.CompletedProcess([], pr.returncode, out, err))
        check("busy: once idle, /quit and stopped ok",
              pr.returncode == 0 and j.get("result") == "ok" and
              j.get("session_id") == T1 and
              sent_pause_then_quit(keys(d)), (out, err, keys(d)))

        # 4. a dialog: failed/prompt, not one key
        n, d, e = sess("dlg", "dialog")
        r = run("--stop", n, "--json")
        j = final(r)
        time.sleep(0.5)
        check("dialog: failed/prompt, nothing typed",
              r.returncode == 1 and j.get("result") == "failed" and
              j.get("reason") == "prompt" and keys(d) == [],
              (r.stdout, keys(d)))
        run("-k", n)

        # 5. daemon-attached and --remote: skipped, left running
        for base, argv in (("dmn", ()),
                           ("rmt", ("--no-daemon", "--remote", "ws://x"))):
            n, d, e = sess(base, "idle", argv)
            r = run("--stop", n, "--json")
            j = final(r)
            time.sleep(0.5)
            check("%s: --stop skipped/codex_daemon, nothing typed" % base,
                  r.returncode == 0 and j.get("result") == "skipped" and
                  j.get("reason") == "codex_daemon" and keys(d) == [] and
                  run("--read", n).returncode == 0, (r.stdout, keys(d)))
            r = run("--restart", n, "--include-agents", "--json")
            j = final(r)
            check("%s: --include-agents skipped/codex_daemon" % base,
                  j.get("result") == "skipped" and
                  j.get("reason") == "codex_daemon" and keys(d) == [],
                  (r.stdout, r.stderr, keys(d)))
            run("-k", n)

        # 6. --restart --include-agents: a Codex whose binary is unchanged
        # since it started is current: skipped without -f, nothing typed;
        # -f: /quit, then codex resume with the resume message as PROMPT
        n, d, e = sess("rst", "idle", extra={"FAKE_CODEX_TID": T1})
        r = run("--restart", n, "--include-agents", "--json")
        j = final(r)
        time.sleep(0.5)
        check("current: skipped/current without -f, nothing typed",
              r.returncode == 0 and j.get("result") == "skipped" and
              j.get("reason") == "current" and "installed codex" in
              j.get("detail", "") and keys(d) == [] and len(argvs(d)) == 1,
              (r.stdout, r.stderr, keys(d)))
        r = run("--restart", n, "--include-agents", "-f", "--json")
        j = final(r)
        got = until(lambda: len(argvs(d)) > 1 and argvs(d)[1])
        check("restart: ok, exit 0", r.returncode == 0 and
              j.get("result") == "ok", (r.stdout, r.stderr))
        check("restart: the pause message, then /quit",
              sent_pause_then_quit(keys(d)), keys(d))
        check("restart: relaunched codex resume <id> -C <cwd>, its options "
              "kept, the resume message as PROMPT",
              got and got[1:] == ["resume", T1, "-C", tmp, "--no-daemon",
                                  RESUME], got)
        check("restart: the session runs on",
              until(lambda: "Ask Codex" in run("--read", n).stdout), "")
        run("-k", n)

        # 7. stale: its binary touched after it started restarts, no -f
        own = os.path.join(tmp, "own")
        os.mkdir(own)
        shutil.copy2(os.path.join(bindir, "codex"), own)
        exe = os.path.join(own, "codex")
        n, d, e = sess("stale", "idle", extra={"FAKE_CODEX_TID": T2},
                       prog=exe)
        time.sleep(1.2)
        os.utime(exe)  # an upgrade wrote the binary after the start
        r = run("--restart", n, "--include-agents", "--json")
        j = final(r)
        got = until(lambda: len(argvs(d)) > 1 and argvs(d)[1])
        check("stale: restarted without -f, codex resume <id>",
              r.returncode == 0 and j.get("result") == "ok" and got and
              got[0] == exe and got[1:4] == ["resume", T2, "-C"] and
              got[-1] == RESUME, (r.stdout, r.stderr, got))
        run("-k", n)

        bad = [x for x in dirs if os.path.exists(os.path.join(x, "CTRL-C"))
               or any("\x03" in s for s in keys(x))]
        check("no Ctrl-C byte ever reached a Codex", not bad, bad)
    finally:
        for n in names:
            run("-k", n)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
    if fails:
        print("%d failed" % fails)
    sys.exit(1 if fails else 0)
