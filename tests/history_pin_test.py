"""History pin, unpin and forget (#052).

`dch --pin <n>...` drops an empty <file>.pin beside a session's record:
the master never touches it, so it outlives every rewrite, and a cold
start's 30-day prune skips that record. `--unpin` removes it. `--forget
<n>... [-f]` deletes records (never a live session's; a pinned one only
with -f, pin and all); `--forget --all` deletes every record neither live
nor pinned. `--json` prints one result per name, then
{"ok","skipped","failed"}.

Covers: pin/unpin round trip and idempotence, --history's "pinned":true
(also on a vanished record) and [pinned], forget of an ended, a live and a
pinned record, -f, not_found, --forget --all (text and --json, summary
line), the prune exemption, the pin surviving live refreshes, revive --as
moving the pin, and the usage errors and exit codes. HOME, the runtime dir
and the state dir are all temporary.

Run:  python3 tests/history_pin_test.py        (uses ./dch)
"""
import json, os, shutil, subprocess, sys, tempfile, time, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))


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


def ndjson(out):
    try:
        return [json.loads(l) for l in out.splitlines()]
    except ValueError:
        return None


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchpin", dir="/tmp"))
    home, state = os.path.join(tmp, "home"), os.path.join(tmp, "state")
    os.makedirs(home)
    for k in list(os.environ):
        if k.startswith("CLAUDE") or k in ("DCH_SESSION", "DCH_SOCKET_DIR",
                                           "DCH_NO_HISTORY"):
            del os.environ[k]
    os.environ.update(HOME=home, XDG_RUNTIME_DIR=tmp, XDG_STATE_HOME=state,
                      DCH_RECORD_MS="300")
    os.chdir(tmp)
    sockdir = os.path.join(tmp, "dch-%d" % os.getuid())
    recdir = os.path.join(state, "dch", "sessions",
                          sockdir.replace("/", "-"))
    names, fails = [], [0]

    def check(msg, cond):
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails[0] += 1

    def spawn(n, *cmd):
        names.append(n)
        subprocess.run([DCH, "--spawn", n] + list(cmd), capture_output=True,
                       check=True)
        return n

    def path(n, ext=".json"):
        return os.path.join(recdir, n + ext)

    def rec(n):
        try:
            with open(path(n)) as f:
                return json.loads(f.read())
        except (OSError, ValueError):
            return None

    def plant(n, **fields):
        """An ended record, written as the master would (0600, one line)."""
        now = int(time.time())
        r = dict(name=n, sock_dir=sockdir, alias="", cwd=tmp,
                 argv=["sleep", "60"], started=now - 660, updated=now - 60,
                 ended=now - 60, end="exit 3", agent=None)
        r.update(fields)
        os.makedirs(recdir, mode=0o700, exist_ok=True)
        fd = os.open(path(n) + ".t", os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                     0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
        os.rename(path(n) + ".t", path(n))

    def pinned(n):
        return os.path.exists(path(n, ".pin"))

    def history():
        r = run("--history", "--json")
        try:
            return {e["name"]: e for e in json.loads(r.stdout)}
        except ValueError:
            return None

    try:
        # --- usage ----------------------------------------------------------
        for args in (["--pin"], ["--unpin"], ["--forget"],
                     ["--pin", "x", "-f"], ["-f", "--unpin", "x"],
                     ["--forget", "--all", "-f"], ["--pin", "--all"],
                     ["--forget", "--all", "x"], ["--history", "--json",
                                                 "--forget"]):
            r = run(*args)
            check("usage error: dch %s (exit 1, nothing on stdout)" %
                  " ".join(args), r.returncode == 1 and r.stdout == "")

        # --- pin / unpin ----------------------------------------------------
        plant("ea")
        plant("eb")
        r = run("--pin", "ea")
        check("--pin of an ended record: 'ea: pinned', exit 0, 0600 pin",
              r.returncode == 0 and r.stdout == "ea: pinned\n" and
              pinned("ea") and
              os.stat(path("ea", ".pin")).st_mode & 0o777 == 0o600)
        check("--pin again is ok (idempotent)",
              run("--pin", "ea").returncode == 0 and pinned("ea"))
        h = history() or {}
        check("--history --json: \"pinned\":true on the pinned record only",
              h.get("ea", {}).get("pinned") is True and
              "eb" in h and "pinned" not in h["eb"])
        t = run("--history").stdout.splitlines()
        check("text --history: [pinned] on the pinned row only",
              any(l.startswith("ea ") and l.endswith("  [pinned]") for l in t)
              and any(l.startswith("eb ") and "[pinned]" not in l
                      for l in t))
        r = run("--unpin", "ea", "--json")
        o = ndjson(r.stdout)
        check("--unpin --json: ok line, then the summary; pin gone",
              r.returncode == 0 and not pinned("ea") and o == [
                  dict(name="ea", result="ok", reason="", detail="unpinned",
                       session_id=""),
                  dict(ok=1, skipped=0, failed=0)])
        check("...and --history no longer says pinned",
              "pinned" not in (history() or {}).get("ea", {"pinned": 1}))
        r = run("--unpin", "ea", "nobody")
        check("--unpin with no pin, or no record: ok either way",
              r.returncode == 0 and
              r.stdout == "ea: unpinned\nnobody: unpinned\n")
        r = run("--pin", "nobody", "--json")
        o = ndjson(r.stdout) or []
        check("--pin with no record: failed not_found, exit 1, no pin",
              r.returncode == 1 and not pinned("nobody") and len(o) == 2 and
              o[0]["result"] == "failed" and o[0]["reason"] == "not_found"
              and o[1] == dict(ok=0, skipped=0, failed=1))
        plant("ev", end="", ended=0)
        run("--pin", "ev")
        e = (history() or {}).get("ev", {})
        check("a vanished pinned record: end vanished and pinned:true",
              e.get("end") == "vanished" and e.get("pinned") is True)
        run("--unpin", "ev")
        run("--forget", "ev")

        # --- forget -----------------------------------------------------------
        r = run("--forget", "eb")
        check("--forget of an ended record: 'eb: forgotten', gone",
              r.returncode == 0 and r.stdout == "eb: forgotten\n" and
              rec("eb") is None)
        r = run("--forget", "eb", "--json")
        o = ndjson(r.stdout) or []
        check("--forget with no record: not_found, exit 1",
              r.returncode == 1 and len(o) == 2 and
              o[0]["reason"] == "not_found" and o[0]["name"] == "eb" and
              o[1] == dict(ok=0, skipped=0, failed=1))
        check("...text: 'eb: failed — <detail>'",
              run("--forget", "eb").stdout.startswith("eb: failed — "))
        live = spawn("plive", "sleep", "60")
        r0 = until(lambda: rec(live))
        r = run("--forget", live, "--json")
        o = ndjson(r.stdout) or []
        check("--forget of a live session: failed live, record kept",
              r.returncode == 1 and o and o[0]["reason"] == "live" and
              rec(live) is not None)
        r = run("--forget", live, "-f")
        check("...-f does not override live",
              r.returncode == 1 and r.stdout.startswith(live + ": failed")
              and rec(live) is not None)
        run("--pin", "ea")
        r = run("--forget", "ea", "--json")
        o = ndjson(r.stdout) or []
        check("--forget of a pinned record: failed pinned, both kept",
              r.returncode == 1 and o and o[0]["reason"] == "pinned" and
              rec("ea") is not None and pinned("ea"))
        r = run("--forget", "-f", "ea")
        check("--forget -f: record and pin removed",
              r.returncode == 0 and r.stdout == "ea: forgotten\n" and
              rec("ea") is None and not pinned("ea"))

        # --- the pin survives the live master's rewrites ---------------------
        check("--pin of a live session's record", run("--pin", live)
              .returncode == 0 and pinned(live))
        ino = os.stat(path(live)).st_ino
        check("a pinned live record is rewritten by its master, pin kept",
              until(lambda: os.stat(path(live)).st_ino != ino and
                    (rec(live) or {}).get("updated", 0) > r0["updated"], 8)
              and pinned(live))

        # --- forget --all -----------------------------------------------------
        spawn("ulive", "sleep", "60")
        until(lambda: rec("ulive"))
        for n in ("c1", "c2", "cp"):
            plant(n)
        run("--pin", "cp")
        r = run("--forget", "--all")
        out = r.stdout.splitlines()
        check("--forget --all: removes the unpinned ended records",
              r.returncode == 0 and rec("c1") is None and rec("c2") is None
              and sorted(out[:-1]) == ["c1: forgotten", "c2: forgotten"])
        check("...keeps the pinned one and both live ones",
              rec("cp") is not None and pinned("cp") and
              rec(live) is not None and rec("ulive") is not None)
        check("...summary: 'forgot 2 (1 pinned, 2 live kept)'",
              out[-1:] == ["forgot 2 (1 pinned, 2 live kept)"])
        plant("c3")
        r = run("--forget", "--all", "--json")
        o = ndjson(r.stdout) or []
        by = {x["name"]: x for x in o if "name" in x}
        check("--forget --all --json: ok, skipped pinned/live, summary last",
              r.returncode == 0 and
              by.get("c3", {}).get("result") == "ok" and
              by.get("cp", {}).get("reason") == "pinned" and
              by.get("cp", {}).get("result") == "skipped" and
              by.get(live, {}).get("reason") == "live" and
              by.get("ulive", {}).get("result") == "skipped" and
              o[-1] == dict(ok=1, skipped=3, failed=0))

        # --- prune exemption --------------------------------------------------
        plant("pold")
        plant("uold")
        run("--pin", "pold")
        t = time.time() - 31 * 86400
        for n in ("pold", "uold"):
            os.utime(path(n), (t, t))
        spawn("pcold", "sleep", "60")
        check("a cold start prunes the old unpinned record",
              until(lambda: rec("uold") is None))
        check("...and keeps the old pinned one (and its pin)",
              rec("pold") is not None and pinned("pold"))

        # --- revive --as moves the pin ---------------------------------------
        plant("rold")
        run("--pin", "rold")
        names.append("rnew")
        r = run("--revive", "rold", "--as", "rnew")
        check("revive --as: the old record and pin go, the pin moves",
              r.returncode == 0 and until(lambda: rec("rnew")) and
              rec("rold") is None and not pinned("rold") and pinned("rnew"))
    except Exception:
        traceback.print_exc()
        fails[0] += 1
    for n in names:
        subprocess.run([DCH, "-k", n], capture_output=True)
    time.sleep(0.3)
    shutil.rmtree(tmp, ignore_errors=True)
    if fails[0]:
        print("FAIL: %d pin/forget check(s) failed" % fails[0])
        return 1
    print("PASS: history pin, unpin and forget")
    return 0


if __name__ == "__main__":
    sys.exit(main())
