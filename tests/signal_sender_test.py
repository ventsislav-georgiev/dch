"""A master's history end names who signalled it (#113).

A master ended by SIGTERM/SIGINT records "signal N from <pid> <command>":
the sender's pid (siginfo) and its command line, NULs as spaces, anything
but printable ASCII as '?', cut at 120 bytes, JSON-quoted in the record.
Covers: a helper process that SIGTERMs the master, its pid and argv in the
record (quote, backslash, control and non-ASCII bytes in that argv), the
record and --history --json still parse, text --history shows it, and
--revive --all still takes it as died. HOME, the runtime dir and the state
dir are all temporary.

Run:  python3 tests/signal_sender_test.py        (uses ./dch)
"""
import json, os, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))


def run(*args, timeout=60):
    return subprocess.run([DCH] + list(args), capture_output=True, text=True,
                          errors="surrogateescape", timeout=timeout)


def until(fn, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def main():
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchsg", dir="/tmp"))
    home, state = os.path.join(tmp, "home"), os.path.join(tmp, "state")
    os.makedirs(home)
    for k in list(os.environ):
        if k.startswith("CLAUDE") or k.startswith("DCH_"):
            del os.environ[k]
    os.environ.update(HOME=home, XDG_RUNTIME_DIR=tmp, XDG_STATE_HOME=state,
                      DCH_LOGIN_ENV="0", DCH_NO_REMOTE="1")
    sockdir = os.path.join(tmp, "dch-%d" % os.getuid())
    recdir = os.path.join(state, "dch", "sessions",
                          sockdir.replace("/", "-"))
    fails, names, helpers = [], [], []

    def check(msg, cond):
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails.append(msg)

    def master(n):
        sock = os.path.join(sockdir, n + ".sock")
        out = subprocess.run(["ps", "-eo", "pid,command"],
                             capture_output=True, text=True).stdout
        for l in out.splitlines():
            if "--master-of" in l and l.rstrip().split(" -- ")[0].endswith(
                    " " + sock):
                return int(l.split()[0])
        return 0

    def rec(n):
        try:
            with open(os.path.join(recdir, n + ".json"), "rb") as f:
                return json.loads(f.read())
        except (OSError, ValueError):
            return None

    try:
        n = "sig"
        r = run("--spawn", n, "--size", "80x24", "sleep", "300")
        names.append(n)
        mpid = until(lambda: rec(n) and master(n))
        check("session up, master found", r.returncode == 0 and mpid)

        # the helper: SIGTERM the master, then wait to be resolved; its
        # script ends in a comment with the bytes the record must not trust.
        # /bin/sh, not python: a framework python re-execs under a long
        # Python.app path, so its argv is neither predictable nor short.
        code = 'kill -TERM %d; read x #q"b\\\x01\xe9' % mpid
        argv = ["/bin/sh", "-c", code]
        h = subprocess.Popen(argv, stdin=subprocess.PIPE)
        helpers.append(h)
        until(lambda: (rec(n) or {}).get("ended", 0) > 0)
        cmd = "".join(c if " " <= c <= "~" else "?" * len(c.encode())
                      for c in " ".join(argv))[:120]
        want = "signal 15 from %d %s" % (h.pid, cmd)
        end = (rec(n) or {}).get("end")
        print("     | end: %r" % end)
        check("record parses, end starts \"signal 15 from <helper pid> \"",
              isinstance(end, str) and
              end.startswith("signal 15 from %d " % h.pid))
        check("...with the helper's command line, made printable", end == want)
        check("...quote and backslash survive the JSON round trip",
              len(" ".join(argv)) > 120 or ('"b\\' in (end or "")))
        hj = run("--history", "--json")
        try:
            hist = {e["name"]: e for e in json.loads(hj.stdout)}
        except ValueError:
            hist = {}
        check("--history --json parses and carries it",
              hist.get(n, {}).get("end") == want)
        ht = run("--history")
        check("--history prints it", want in ht.stdout)
        h.stdin.close()
        h.wait(10)

        # --revive --all takes a signalled session that ran 5+ minutes
        p = os.path.join(recdir, n + ".json")
        r0 = rec(n)
        r0["started"] = r0["ended"] - 600
        with open(p, "w") as f:
            f.write(json.dumps(r0, separators=(",", ":")) + "\n")
        os.chmod(p, 0o600)
        rv = run("--revive", "--all", "--json", timeout=120)
        objs = [json.loads(l) for l in rv.stdout.splitlines() if l.strip()]
        fin = [o for o in objs if o.get("name") == n and "result" in o]
        check("--revive --all takes it as died",
              rv.returncode == 0 and fin and fin[0]["result"] == "ok")
    finally:
        for h in helpers:
            if h.poll() is None:
                h.kill()
                h.wait()
        for n in names:
            run("-k", n)
        shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print("FAIL: %d check(s)" % len(fails))
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
