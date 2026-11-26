"""Harness (Claude Code and Codex) name resolution for picker / --ls-json.

Claude Code writes ~/.claude/sessions/<pid>.json for every live process; the
process's DCH_SESSION env names the dch session it runs inside. dch joins the
two and shows the harness name in the picker and as "harness" in --ls-json.

Cases:
  - named Claude/Codex session -> harness name resolved
  - a Codex outside the session's master (the shared app-server daemon)
    carrying its DCH_SESSION      -> no snapshot title for that session (#083)
  - "nameSource":"derived"     -> skipped (auto-titles beat no one)
  - dead, malformed, empty and wrong-session records -> skipped
  - resolution overhead        -> budget (mean CPU time over N runs vs empty
                                  HOME, as a multiple of that base run)

Run:  python3 tests/harness_names_test.py        (uses ./dch)
"""
import json, os, resource, shutil, signal, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))

# Resolution overhead as a multiple of a plain --ls-json's own CPU time. That
# base run already holds one walk of every pid (load_codex_names); each Codex
# session here without a thread ID adds one more (dch_codex_shared), ~0.8x
# base in all. A walk per record or per session multiplies it. A fixed ms
# budget instead tracked the box: CPU contention, slow cores and the pid count
# (25 ms failed at load ~20 with suites in parallel, #146).
OVERHEAD_FACTOR = 2.0
N = 30


def spawn_env_proc(session, watch, executable=None, **extra_env):
    """Long-lived stand-in for a harness process carrying DCH_SESSION.

    Must be a NON-platform binary: macOS redacts the env of platform
    binaries (/bin/sleep etc.) from other processes' KERN_PROCARGS2, which
    is exactly the read dch does. Our own dch binary (--wait, blocking)
    is guaranteed present and readable."""
    env = dict(os.environ)
    env["DCH_SESSION"] = session
    env.pop("CODEX_THREAD_ID", None)
    env.pop("CODEX_SESSION_ID", None)
    env.update(extra_env)
    return subprocess.Popen(
        [executable or DCH, "--wait", watch, "--state", "done", "--timeout", "60000"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write_session_json(sessdir, pid, name, derived=False):
    j = {"pid": pid, "sessionId": "x", "cwd": "/", "name": name}
    if derived:
        j["nameSource"] = "derived"
    with open(os.path.join(sessdir, "%d.json" % pid), "w") as f:
        json.dump(j, f, separators=(",", ":"))


def write_codex_index(home, records):
    path = os.path.join(home, ".codex", "session_index.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for record in records:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
        f.write('{"id":"codex-malformed","thread_name":\n')


def write_codex_snapshot(home, thread_id, session):
    path = os.path.join(home, ".codex", "shell_snapshots",
                        thread_id + ".123.sh")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("# snapshot\nexport DCH_SESSION=%s\n" % session)


def picker_output(home):
    env = dict(os.environ)
    env["HOME"] = home
    return subprocess.run([DCH, "-l"], env=env, capture_output=True,
                          text=True, preexec_fn=os.setsid).stdout


def ls_json(home):
    env = dict(os.environ)
    env["HOME"] = home
    out = subprocess.run([DCH, "--ls-json"], env=env,
                         capture_output=True, text=True).stdout
    return {e["name"]: e for e in json.loads(out)}


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1

    tmp = tempfile.mkdtemp()
    os.environ["XDG_RUNTIME_DIR"] = tmp
    home = os.path.join(tmp, "home")
    sessdir = os.path.join(home, ".claude", "sessions")
    os.makedirs(sessdir)
    procs = []
    fails = 0

    def check(msg, cond):
        nonlocal fails
        if cond:
            print("  ok   %s" % msg)
        else:
            fails += 1
            print("  FAIL %s" % msg)

    try:
        codex = os.path.join(tmp, "codex")
        codex_host = os.path.join(tmp, "codex-code-mode-host")
        shutil.copy2(os.path.abspath(DCH), codex)
        shutil.copy2(os.path.abspath(DCH), codex_host)

        # dch sessions to resolve against.
        for name in ("hn-named", "hn-derived",
                     "hn-codex-empty", "hn-codex-wrong", "hn-codex-stale",
                     "hn-codex-malformed", "hn-inherited", "hn-esc",
                     "hn-spaced", "hn-nul", "hn-osc", "hn-bidi", "hn-long"):
            subprocess.run([DCH, "--spawn", name, "sleep", "60"], check=True)
        # A Codex TUI runs under its session's master: that is what makes
        # its snapshot its own (#083).
        # hn-codex-inh (#665) also inherits a parent Codex's thread ID.
        for name in ("hn-codex", "hn-codex-shared", "hn-codex-old",
                     "hn-codex-inh"):
            subprocess.run([DCH, "--spawn", name, codex_host, "--wait", name,
                            "--state", "done", "--timeout", "60000"],
                           check=True,
                           env=dict(os.environ, CODEX_THREAD_ID="codex-parent")
                           if name == "hn-codex-inh" else None)

        p1 = spawn_env_proc("hn-named", "hn-named")
        procs.append(p1)
        write_session_json(sessdir, p1.pid, "my-claude")

        p2 = spawn_env_proc("hn-derived", "hn-derived")
        procs.append(p2)
        write_session_json(sessdir, p2.pid, "hn-derived-7", derived=True)

        p3 = spawn_env_proc("hn-named", "hn-named")
        p3.terminate()
        p3.wait()
        # dead pid json pointing at the same session; must not crash or match
        write_session_json(sessdir, 99999999, "ghost")

        # Current Codex processes carry DCH_SESSION but no thread ID. The
        # shell snapshot filename supplies the live thread's ID. The TUI
        # (codex_host) runs inside hn-codex; p4 is the shared app-server
        # daemon some TUI of hn-codex-shared started: no master above it,
        # its shells snapshot that session's env for any TUI's thread.
        p4 = spawn_env_proc("hn-codex-shared", "hn-codex-shared",
                            executable=codex)
        p5 = spawn_env_proc("hn-codex-empty", "hn-codex-empty",
                            executable=codex,
                            CODEX_THREAD_ID="codex-empty")
        p6 = spawn_env_proc("not-a-session", "hn-codex-wrong",
                            CODEX_THREAD_ID="codex-wrong")
        p7 = spawn_env_proc("hn-codex-malformed", "hn-codex-malformed",
                            CODEX_THREAD_ID="codex-malformed")
        p9 = spawn_env_proc("hn-inherited", "hn-inherited",
                            CODEX_THREAD_ID="codex-inherited")
        procs.extend((p4, p5, p6, p7, p9))
        # #661: the record is parsed as JSON: escapes decoded, \" kept,
        # spaced "name" : form matched, \u0000 is refused by the parser.
        for sess, raw in (
                ("hn-esc", r'{"pid":%d,"name":"caf\u00e9 \"q\" \u001b[0m"}'),
                ("hn-spaced", '{"pid":%d, "name" : "spaced x" }'),
                ("hn-nul", r'{"pid":%d,"name":"ab\u0000cd"}'),
                ("hn-osc", r'{"pid":%d,"name":"t\u001b]0;pwned\u0007\u001b[2Jx"}'),
                ("hn-bidi", r'{"pid":%d,"name":"a\u202eb\u2067c\u0085d"}'),
                ("hn-long", '{"pid":%d,"name":"' + "\u00e9" * 750 + '"}')):
            pe = spawn_env_proc(sess, sess)
            procs.append(pe)
            with open(os.path.join(sessdir, "%d.json" % pe.pid), "w") as f:
                f.write(raw % pe.pid)
        write_codex_snapshot(home, "codex-live", "hn-codex")
        write_codex_snapshot(home, "codex-foreign", "hn-codex-shared")
        write_codex_snapshot(home, "codex-old", "hn-codex-old")
        write_codex_snapshot(home, "codex-own", "hn-codex-inh")
        write_codex_index(home, [
            {"id": "codex-live", "thread_name": "my-codex"},
            {"id": "codex-own", "thread_name": "own-codex"},
            {"id": "codex-parent", "thread_name": "parent-codex"},
            {"id": "codex-old", "thread_name": "old-codex"},
            {"id": "codex-inherited", "thread_name": "not-codex"},
            {"id": "codex-empty", "thread_name": ""},
            {"id": "codex-wrong", "thread_name": "wrong-session"},
            {"id": "codex-stale", "thread_name": "ghost-codex"},
            {"id": "codex-foreign", "thread_name": "foreign-thread"},
        ])

        sl = ls_json(home)
        check("named harness session resolved",
              sl.get("hn-named", {}).get("harness") == "my-claude")
        check("escaped name decoded (u00e9, \\\", ESC)",
              sl.get("hn-esc", {}).get("harness") == 'caf\u00e9 "q"  [0m')
        check("controls become spaces in --ls-json",
              sl.get("hn-osc", {}).get("harness") == "t ]0;pwned  [2Jx")
        check("bidi/C1 controls replaced by one space each",
              sl.get("hn-bidi", {}).get("harness") == "a b c d")
        lng = sl.get("hn-long", {}).get("harness", "")
        check("1500-byte name cut at a UTF-8 boundary, not dropped",
              0 < len(lng.encode()) <= 1000 and lng.strip("\u00e9") == "")
        check("spaced \"name\" : record matched",
              sl.get("hn-spaced", {}).get("harness") == "spaced x")
        check("NUL in name: record rejected by the parser, no name",
              sl.get("hn-nul", {}).get("harness") == "")
        check("derived harness name skipped",
              sl.get("hn-derived", {}).get("harness") == "")
        check("named Codex session resolved",
              sl.get("hn-codex", {}).get("harness") == "my-codex")
        check("shared-daemon snapshot names no session",
              sl.get("hn-codex-shared", {}).get("harness") == "")
        check("Codex snapshot thread resolved (no env ID)",
              sl.get("hn-codex-old", {}).get("harness") == "old-codex")
        check("inherited env thread ID ignored: own snapshot thread named",
              sl.get("hn-codex-inh", {}).get("harness") == "own-codex")
        check("inherited Codex ID on non-Codex process skipped",
              sl.get("hn-inherited", {}).get("harness") == "")
        plain = subprocess.run([DCH, "-ls"], env=dict(os.environ, HOME=home),
                               capture_output=True, text=True).stdout
        check("plain list shows Codex title and real name",
              "my-codex (hn-codex)" in plain)
        plain_osc = [l for l in plain.splitlines() if "hn-osc" in l]
        check("-ls row for the escape name has no ESC/BEL",
              plain_osc and "\x1b" not in plain_osc[0] and
              "\x07" not in plain_osc[0])
        check("-ls output has no bidi override",
              "\u202e" not in plain and "\u2067" not in plain)
        picker = picker_output(home)
        check("picker shows Codex title and real name",
              "my-codex" in picker and "hn-codex" in picker)
        check("empty Codex title skipped",
              sl.get("hn-codex-empty", {}).get("harness") == "")
        check("wrong-session Codex record skipped",
              sl.get("hn-codex-wrong", {}).get("harness") == "")
        check("stale Codex record skipped",
              sl.get("hn-codex-stale", {}).get("harness") == "")
        check("malformed Codex record skipped",
              sl.get("hn-codex-malformed", {}).get("harness") == "")
        check("alias field untouched",
              sl.get("hn-named", {}).get("alias") == "")
        subprocess.run([DCH, "-m", "hn-codex", "my-alias"],
                       check=True, capture_output=True)
        plain = subprocess.run([DCH, "-ls"], env=dict(os.environ, HOME=home),
                               capture_output=True, text=True).stdout
        check("alias stays above harness title",
              "my-alias (hn-codex)" in plain and
              "my-codex (hn-codex)" not in plain)

        # --- resolution overhead: --ls-json with jsons vs without ---
        empty_home = os.path.join(tmp, "home-empty")
        os.makedirs(empty_home)

        # CPU time (user+sys) of the dch child, not wall time: waiting for a
        # core is not resolution work. Interleaved so drift hits both sides.
        def cost_ms(h):
            r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
            t0 = time.perf_counter()
            ls_json(h)
            t1 = time.perf_counter()
            r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
            cpu = (r1.ru_utime + r1.ru_stime) - (r0.ru_utime + r0.ru_stime)
            return cpu * 1000.0, (t1 - t0) * 1000.0

        runs = [(cost_ms(empty_home), cost_ms(home)) for _ in range(N)]
        base = sum(b[0] for b, _ in runs) / N
        withres = sum(w[0] for _, w in runs) / N
        overhead = withres - base
        wall = (sum(w[1] for _, w in runs) - sum(b[1] for b, _ in runs)) / N
        print("  info resolution overhead: %.3f ms CPU/run, %.2fx base "
              "(%.3f with, %.3f without; wall %.3f ms)"
              % (overhead, overhead / base if base else 0, withres, base,
                 wall))
        if os.environ.get("DCH_TEST_NO_BUDGET"):  # sanitizer build
            print("  skip resolution overhead budget (sanitizer build)")
        else:
            check("resolution overhead <= %.1fx a plain --ls-json"
                  % OVERHEAD_FACTOR, overhead <= OVERHEAD_FACTOR * base)
    finally:
        for p in procs:
            try:
                p.send_signal(signal.SIGKILL)
                p.wait()
            except OSError:
                pass
        os.system("'%s' -kl >/dev/null 2>&1" % DCH)
        shutil.rmtree(tmp, ignore_errors=True)

    if fails == 0:
        print("PASS: harness names resolved.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
