"""Records --history and --revive can always read (#061).

The master writes every string through json_quote, which writes a byte that
is not UTF-8 as U+FFFD: the record always parses. --revive refuses a record
whose command, directory or claude command holds U+FFFD (it would run
something else) and says how to resume the Claude id by hand; --history
still lists it with that id. An older master's raw bytes read the same way.
A long alias is cut on a character boundary by `dch -m` and by the master's
reader, and the JSON reader's tokens grow with the record, so an argv of
1100 words is listed and revived whole. --ls-json quotes the same way.

Every session gets its Claude id the way --revive hands one over
(DCH_REVIVE_AGENT, no launch), so no Claude runs. HOME, the runtime dir and
the state dir are all temporary.

Run:  python3 tests/record_utf8_test.py        (uses ./dch)
"""
import json, os, shutil, subprocess, sys, tempfile, time, traceback

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
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchu8", dir=os.environ.get("DCH_TEST_TMP", "/tmp")))
    home, state = os.path.join(tmp, "home"), os.path.join(tmp, "state")
    os.makedirs(home)
    for k in list(os.environ):
        if k.startswith("CLAUDE") or k in ("DCH_SESSION", "DCH_SOCKET_DIR",
                                           "DCH_NO_HISTORY"):
            del os.environ[k]
    os.environ.update(HOME=home, XDG_RUNTIME_DIR=tmp, XDG_STATE_HOME=state,
                      DCH_RECORD_MS="300", DCH_LOGIN_ENV="0")
    os.chdir(tmp)
    sockdir = os.path.join(tmp, "dch-%d" % os.getuid())
    recdir = os.path.join(state, "dch", "sessions",
                          sockdir.replace("/", "-"))
    names, fails = [], [0]
    sleep = ["/bin/sh", "-c", "sleep 300"]

    def check(msg, cond):
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails[0] += 1

    def agent(sid):
        return dict(kind="claude", session_id=sid, name="", cwd=tmp,
                    launch=[])

    def spawn(n, argv, sid):
        names.append(n)
        env = dict(os.environ, DCH_REVIVE_AGENT=json.dumps(agent(sid)))
        subprocess.run([DCH.encode(), b"--spawn", n.encode()] + argv,
                       capture_output=True, check=True, env=env, timeout=30)

    def path(n):
        return os.path.join(recdir, n + ".json")

    def rec(n):
        """The record as JSON, None when absent or not strict UTF-8 JSON."""
        try:
            with open(path(n), "rb") as f:
                return json.loads(f.read().decode("utf-8"))
        except (OSError, ValueError):
            return None

    def history():
        try:
            return {e["name"]: e for e in
                    json.loads(run("--history", "--json").stdout)}
        except ValueError:
            return None

    def listed(n, sid):
        """--history lists n with its Claude id, text and --json."""
        e = (history() or {}).get(n, {})
        return (e.get("agent") or {}).get("session_id") == sid and any(
            l.startswith(n + " ") and l.endswith("[claude %s]" % sid)
            for l in run("--history").stdout.splitlines())

    def kill(n, sid):
        run("-k", n)
        return until(lambda: (rec(n) or {}).get("end") == "killed" and
                     (rec(n).get("agent") or {}).get("session_id") == sid)

    try:
        # --- an argv byte that is not UTF-8 ---------------------------------
        sid = "11111111-1111-4111-8111-111111111111"
        spawn("bad", [s.encode() for s in sleep] + [b"x\xffy"], sid)
        r = until(lambda: rec("bad"))
        check("argv byte 0xff: the record parses as UTF-8 JSON, the byte "
              "written as U+FFFD", r and r["argv"][-1] == "x�y")
        check("...and it keeps the Claude id once killed", kill("bad", sid))
        check("--history lists bad with its Claude id", listed("bad", sid))
        r = run("--revive", "bad")
        check("--revive bad: refused (exit 1), says why and how to resume "
              "the id by hand, starts nothing",
              r.returncode == 1 and "NOT revived" in r.stdout and
              "not UTF-8" in r.stdout and "command" in r.stdout and
              "claude --resume " + sid in r.stdout and
              not os.path.exists(os.path.join(sockdir, "bad.sock")))
        r = run("--revive", "bad", "--json")
        o = [json.loads(l) for l in r.stdout.splitlines()]
        check("--revive bad --json: failed not_utf8 with the session id",
              r.returncode == 1 and any(
                  e.get("result") == "failed" and
                  e.get("reason") == "not_utf8" and
                  e.get("session_id") == sid for e in o))
        check("...and the record is still there", listed("bad", sid))

        # --- an older master's raw bytes in argv and cwd -----------------------
        sid = "22222222-2222-4222-8222-222222222222"
        now = int(time.time())
        raw = json.dumps(dict(
            name="old", sock_dir=sockdir, alias="", cwd=tmp + "/dXd",
            argv=["/bin/echo", "aXb"], started=now - 660, updated=now - 60,
            ended=now - 60, end="exit 3", agent=agent(sid)),
            separators=(",", ":")).encode().replace(b"X", b"\xfe") + b"\n"
        os.makedirs(recdir, mode=0o700, exist_ok=True)
        fd = os.open(path("old"), os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, raw)
        os.close(fd)
        check("a record with raw 0xfe bytes (dch 1.27.1): --history lists "
              "it with its Claude id", listed("old", sid))
        r = run("--revive", "old")
        check("--revive old: refused for its directory, id given",
              r.returncode == 1 and "its directory had bytes" in r.stdout
              and "claude --resume " + sid in r.stdout)

        # --- an alias of 300 x "é" (600 bytes) --------------------------------
        sid = "33333333-3333-4333-8333-333333333333"
        spawn("al", [s.encode() for s in sleep], sid)
        until(lambda: rec("al"))
        al = "é" * 300
        check("dch -m al <300 x é>: exit 0", run("-m", "al", al).returncode
              == 0)
        with open(os.path.join(sockdir, "al.sock.alias"), "rb") as f:
            side = f.read()
        check("the alias sidecar is cut on a character: 299 x é",
              side == ("é" * 299 + "\n").encode())
        check("the record carries the alias whole",
              until(lambda: (rec("al") or {}).get("alias") == al[:299]))
        # an older dch -m cut it at 599 bytes, mid-character: the master's
        # reader cuts it back to a character
        with open(os.path.join(sockdir, "al.sock.alias"), "wb") as f:
            f.write(("ü" * 300).encode()[:599] + b"\n")
        check("a sidecar cut mid-character: the record still parses, alias "
              "299 x ü", until(lambda: (rec("al") or {}).get("alias") ==
                               "ü" * 299))
        run("-m", "al", al)
        until(lambda: (rec("al") or {}).get("alias") == al[:299])
        check("...killed, it keeps the Claude id", kill("al", sid))
        check("--history lists al with its alias and Claude id",
              listed("al", sid) and
              (history() or {}).get("al", {}).get("alias") == al[:299])
        r = run("--revive", "al")
        check("--revive al: revived, its alias set again",
              "al: revived as al" in r.stdout and until(
                  lambda: os.path.exists(os.path.join(sockdir,
                                                      "al.sock.alias")))
              and open(os.path.join(sockdir, "al.sock.alias"),
                       "rb").read() == side)

        # --- an argv of 1100 words --------------------------------------------
        sid = "44444444-4444-4444-8444-444444444444"
        words = [str(i) for i in range(1100)]
        spawn("many", [s.encode() for s in sleep + words], sid)
        check("1100-word argv: the record holds every word",
              until(lambda: (rec("many") or {}).get("argv") == sleep + words))
        check("...killed, it keeps the Claude id", kill("many", sid))
        check("--history lists many with its Claude id", listed("many", sid))
        r = run("--revive", "many")
        check("--revive many: revived with all 1103 words",
              "many: revived as many" in r.stdout and until(
                  lambda: (rec("many") or {}).get("end") == "" and
                  rec("many")["argv"] == sleep + words))

        # --- --ls-json with an alias that is not UTF-8 -------------------------
        with open(os.path.join(sockdir, "many.sock.alias"), "wb") as f:
            f.write(b"a\xffb\"c\x01\n")
        r = subprocess.run([DCH, "--ls-json"], capture_output=True,
                           timeout=30)
        try:
            ls = {e["name"]: e for e in json.loads(r.stdout.decode("utf-8"))}
        except ValueError:
            ls = {}
        check("--ls-json: an alias with 0xff, a quote and ^A still parses",
              ls.get("many", {}).get("alias") == "a�b\"c\x01")
    except Exception:
        traceback.print_exc()
        fails[0] += 1
    for n in names:
        subprocess.run([DCH, "-k", n], capture_output=True)
    time.sleep(0.3)
    shutil.rmtree(tmp, ignore_errors=True)
    if fails[0]:
        print("FAIL: %d record UTF-8/size check(s) failed" % fails[0])
        return 1
    print("PASS: records with odd bytes, long aliases and long argv read back")
    return 0


if __name__ == "__main__":
    sys.exit(main())
