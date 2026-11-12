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
moving the pin, <file>.json naming exactly one record (#063), and the
usage errors and exit codes. #067: a .json argument that is also a
session's name (a / a.json) refused as ambiguous by --forget, --pin, --unpin
and --revive; --unpin of nothing not_found; the [record <file>] --history
prints for a long name's moved record (and a hashed name's) taken by --pin,
--unpin and --revive, with and without .json, and refused as ambiguous when
it is also another record's name; a master whose link() fails (an
LD_PRELOAD/DYLD interposer, as on a filesystem without hard links) still
moves the record aside, never over a taken name. #162: a name starting
with '-' (an older dch's `--spawn -h`): `--` ends the options of --pin,
--unpin, --forget, --stop and -k, so each reaches it, and no verb makes
such a name (--spawn, -n, --revive --as, a revive of the record). HOME,
the runtime dir and the state dir are all temporary.

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


# link() fails as on a filesystem without hard links: the master's
# rec_aside must fall back to an exclusive rename (#067 LOW-2). NOLINK_MARK
# proves the shim was hit.
NOLINK_C = r'''
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <unistd.h>
static int nolink(const char *a, const char *b)
{
    const char *p = getenv("NOLINK_MARK");
    int fd = p ? open(p, O_WRONLY | O_CREAT | O_APPEND, 0600) : -1;
    (void)a; (void)b;
    if (fd >= 0) { (void)write(fd, "x", 1); close(fd); }
    errno = EPERM;
    return -1;
}
#ifdef __APPLE__
__attribute__((used, section("__DATA,__interpose")))
static struct { const void *n, *o; } ip[] = {
    { (const void *)nolink, (const void *)link } };
#else
int link(const char *a, const char *b) { return nolink(a, b); }
#endif
'''


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
        check("--unpin with no pin is ok; with no record at all it is "
              "not_found, exit 1 (QA #530: never ok on no match)",
              r.returncode == 1 and r.stdout ==
              "ea: unpinned\nnobody: failed — no history for this session\n")
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

        # --- #059: a cold start moves a dead record with a claude id aside -
        def agent(sid):
            # no launch: --revive cannot relaunch it, so it is quick
            return dict(kind="claude", session_id=sid, name="", cwd=tmp,
                        launch=[])

        def asides(n):
            return sorted(f[:-5] for f in os.listdir(recdir)
                          if f.startswith(n + ".") and f.endswith(".json")
                          and f != n + ".json")

        def arec(n):
            a = asides(n)
            return rec(a[0]) if len(a) == 1 else None

        def asid(n):
            return ((rec(n) or {}).get("agent") or {}).get("session_id")

        now = int(time.time())
        plant("hw", end="stopped", agent=agent("sid-hw"), ended=now - 60)
        run("--pin", "hw")
        spawn("hw", "sleep", "60")
        until(lambda: (rec("hw") or {}).get("end") == "" and asides("hw"))
        check("new session under a stopped name: the stopped record moves "
              "to hw.<ended>.json, id kept; hw.json is the new one",
              asides("hw") == ["hw.%d" % (now - 60)] and
              asid("hw.%d" % (now - 60)) == "sid-hw" and
              rec("hw.%d" % (now - 60))["end"] == "stopped" and
              rec("hw")["agent"] is None)
        check("...its pin is copied, the name keeps its own",
              pinned("hw.%d" % (now - 60)) and pinned("hw"))
        e = [x for x in json.loads(run("--history", "--json").stdout or "[]")
             if x["name"] == "hw"]
        check("--history --json: the moved record, not live, with \"file\" "
              "and pinned", len(e) == 1 and e[0]["end"] == "stopped" and
              e[0]["agent"]["session_id"] == "sid-hw" and
              e[0]["file"] == "hw.%d" % (now - 60) and e[0]["pinned"] is True)
        check("text --history: [record hw.<ended>]",
              any(l.startswith("hw ") and
                  l.endswith("[pinned]  [record hw.%d]" % (now - 60))
                  for l in run("--history").stdout.splitlines()))
        r = run("--revive", "hw")
        check("--revive hw while the new hw is live: refused, nothing moved",
              r.returncode == 1 and "is live" in r.stderr and
              "--as <new>" in r.stderr and asides("hw"))
        names.append("hw2")
        r = run("--revive", "hw", "--as", "hw2")
        check("--revive hw --as hw2 takes the stopped record, not the live "
              "one: hw2 carries sid-hw, the moved record and pin retire",
              "hw: revived as hw2" in r.stdout and
              until(lambda: asid("hw2") == "sid-hw") and
              asides("hw") == [] and pinned("hw2") and
              (rec("hw") or {}).get("end") == "")
        r = run("--revive", "hw", "--as", "hw3")
        check("--revive <live name> --as with no ended record left: refused "
              "as live, not revived", r.returncode == 1 and
              "is live" in r.stderr and not os.path.exists(path("hw3")))

        plant("hr", end="stopped", agent=agent("sid-hr"), ended=now - 120)
        spawn("hr", "sleep", "60")
        until(lambda: asides("hr"))
        run("-k", "hr")
        until(lambda: (rec("hr") or {}).get("end") == "killed")
        r = run("--revive", "hr.%d" % (now - 120))
        check("--revive <name>.<ended> under its own name: hr carries "
              "sid-hr, the moved record retires",
              "hr: revived as hr" in r.stdout and
              until(lambda: asid("hr") == "sid-hr") and asides("hr") == [])

        plant("hs", end="stopped", agent=agent("sid-hs"))
        names.append("hs")
        run("--revive", "hs")
        check("--revive hs under its own name: no copy moved aside (the new "
              "record carries the id on)",
              until(lambda: (rec("hs") or {}).get("end") == "") and
              asid("hs") == "sid-hs" and asides("hs") == [])
        plant("hn")
        spawn("hn", "sleep", "60")
        check("a dead record with no claude id is just replaced",
              until(lambda: (rec("hn") or {}).get("end") == "") and
              asides("hn") == [])
        plant("hv", end="", ended=0, agent=agent("sid-hv"))
        spawn("hv", "sleep", "60")
        until(lambda: (rec("hv") or {}).get("updated", 0) > now and
              asides("hv"))
        e = [x for x in json.loads(run("--history", "--json").stdout or "[]")
             if x["name"] == "hv"]
        check("a vanished record with an id moves aside too and reads "
              "vanished beside the live hv",
              len(e) == 1 and e[0]["end"] == "vanished" and
              e[0]["agent"]["session_id"] == "sid-hv")

        plant("hp", end="killed", agent=agent("sid-hp"))
        spawn("hp", "sleep", "60")
        until(lambda: asides("hp"))
        ap = asides("hp")[:1] or ["hp.x"]
        r = run("--pin", "hp")
        check("--pin pins every record of the name, the moved one too",
              r.returncode == 0 and pinned("hp") and pinned(ap[0]))
        t = time.time() - 31 * 86400
        if os.path.exists(path(ap[0])):
            os.utime(path(ap[0]), (t, t))
        spawn("hpc", "sleep", "60")
        time.sleep(0.5)
        check("...a cold start's prune keeps the old moved pinned record",
              rec(ap[0]) is not None)
        r = run("--unpin", "hp")
        check("--unpin unpins every record of the name",
              r.returncode == 0 and not pinned("hp") and not pinned(ap[0]))
        r = run("--forget", "hp", "--json")
        o = ndjson(r.stdout) or []
        check("--forget hp: the live record refused, the moved one forgotten",
              r.returncode == 1 and rec("hp") is not None and
              asides("hp") == [] and
              sorted(x.get("reason") for x in o[:-1]) == ["", "live"])

        # --- #063: <file>.json names exactly one record ------------------
        fa = "fw.%d" % (now - 300)
        plant("fw")
        plant(fa, name="fw", ended=now - 300)
        e = [x for x in json.loads(run("--history", "--json").stdout or "[]")
             if x["name"] == "fw"]
        check("--history --json: \"file\" on every record, the primary too",
              sorted(x.get("file", "") for x in e) == sorted(["fw", fa]))
        r = run("--pin", "fw.json")
        check("--pin fw.json pins the primary only",
              r.returncode == 0 and r.stdout == "fw.json: pinned\n" and
              pinned("fw") and not pinned(fa))
        r = run("--unpin", "fw.json")
        check("--unpin fw.json unpins it", r.returncode == 0 and
              not pinned("fw"))
        run("--pin", fa + ".json")
        check("--pin fw.<ended>.json pins the moved record only",
              pinned(fa) and not pinned("fw"))
        run("--unpin", fa + ".json")
        r = run("--forget", fa + ".json", "--json")
        o = ndjson(r.stdout) or []
        check("--forget fw.<ended>.json forgets that one only",
              r.returncode == 0 and rec(fa) is None and
              rec("fw") is not None and
              o[-1:] == [dict(ok=1, skipped=0, failed=0)])
        plant(fa, name="fw", ended=now - 300)
        r = run("--pin", "fw")
        check("bare --pin fw still pins every record of the name",
              r.returncode == 0 and pinned("fw") and pinned(fa))
        r = run("--forget", "fw", "-f")
        check("bare --forget fw -f still forgets every record of the name",
              r.returncode == 0 and rec("fw") is None and rec(fa) is None)
        r = run("--forget", "nosuch.json", "--json")
        o = ndjson(r.stdout) or []
        check("--forget nosuch.json: not_found, exit 1",
              r.returncode == 1 and len(o) == 2 and
              o[0]["reason"] == "not_found" and
              o[0]["name"] == "nosuch.json")
        r = run("--forget", live + ".json", "--json")
        o = ndjson(r.stdout) or []
        check("--forget <live>.json: failed live, record kept",
              r.returncode == 1 and o and o[0]["reason"] == "live" and
              rec(live) is not None)
        evil = os.path.join(os.path.dirname(recdir), "evil.pin")
        open(evil, "w").close()
        r = run("--unpin", "../evil.json")
        check("--unpin ../evil.json: not_found, nothing outside the dir "
              "touched", r.returncode == 1 and os.path.exists(evil))

        # --- #067: a .json argument that is also a session's name ---------
        plant("aj")
        plant("aj.json")
        for verb in ("--forget", "--pin", "--unpin"):
            r = run(verb, "aj.json", "--json")
            o = ndjson(r.stdout) or []
            check("%s aj.json with sessions aj and aj.json: failed "
                  "ambiguous, exit 1, nothing touched" % verb,
                  r.returncode == 1 and len(o) == 2 and
                  o[0]["reason"] == "ambiguous" and
                  "<file>.json" in o[0]["detail"] and
                  rec("aj") is not None and rec("aj.json") is not None and
                  not pinned("aj") and not pinned("aj.json"))
        r = run("--revive", "aj.json", "--json")
        o = ndjson(r.stdout) or []
        check("--revive aj.json: failed ambiguous, exit 1, nothing started",
              r.returncode == 1 and o and o[-1]["reason"] == "ambiguous" and
              run("--cmd", "aj").returncode != 0 and
              run("--cmd", "aj.json").returncode != 0)
        r = run("--forget", "aj.json.json")
        check("--forget aj.json.json forgets session aj.json's record only",
              r.returncode == 0 and rec("aj.json") is None and
              rec("aj") is not None)
        run("--forget", "aj")

        # --- #067 / QA #530: the file --history prints for a moved-aside
        # record of a long name, which canon_name would hash -------------
        sun = 104 if sys.platform == "darwin" else 108
        budget = sun - len(sockdir) - 1 - 5 - 1

        def canon(n):
            if len(n) <= budget:
                return n
            h = 5381
            for c in n.encode():
                h = (h * 33 + c) & 0xffffffffffffffff
            return "%s-%06x" % (n[:budget - 7], h & 0xffffff)

        ln = ("L%d" % os.getpid()).ljust(budget - 3, "l")
        la = "%s.%d" % (ln, now - 400)
        check("the test's long name fits, its aside file does not",
              canon(ln) == ln and canon(la) != la)
        plant(la, name=ln, ended=now - 400)
        hint = [l for l in run("--history").stdout.splitlines()
                if l.startswith(ln + " ")]
        check("text --history prints [record <name>.<ended>]",
              len(hint) == 1 and hint[0].endswith("[record %s]" % la))
        r = run("--pin", la)
        check("--pin <that file>: pinned, exit 0",
              r.returncode == 0 and pinned(la))
        r = run("--unpin", la)
        check("--unpin <that file>: unpinned", r.returncode == 0 and
              not pinned(la))
        names.append(ln)
        r = run("--revive", la)
        check("--revive <that file>: revived as the long name, the moved "
              "record retired",
              r.returncode == 0 and ("revived as %s" % ln) in r.stdout and
              until(lambda: rec(ln)) and rec(la) is None)
        run("-k", ln)
        until(lambda: (rec(ln) or {}).get("end"))
        run("--forget", ln)
        plant(la, name=ln, ended=now - 400)
        r = run("--revive", la + ".json")
        check("--revive <that file>.json: revived the same way",
              r.returncode == 0 and ("revived as %s" % ln) in r.stdout and
              until(lambda: rec(ln)) and rec(la) is None)
        run("-k", ln)
        until(lambda: (rec(ln) or {}).get("end"))
        run("--forget", ln)

        hn = ("H%d" % os.getpid()).ljust(budget + 20, "h")
        ha = "%s.%d" % (canon(hn), now - 500)
        plant(canon(hn), name=hn)
        plant(ha, name=hn, ended=now - 500)
        r = run("--pin", ha)
        check("a hashed name's moved record: --pin <file --history prints> "
              "pins that one only", r.returncode == 0 and pinned(ha) and
              not pinned(canon(hn)))
        run("--unpin", ha)
        plant(ha, name=hn[:budget + 10])
        plant(canon(ha), name=ha)
        r = run("--pin", ha, "--json")
        o = ndjson(r.stdout) or []
        check("an argument that is one record's file and another's hashed "
              "name: ambiguous, nothing pinned",
              r.returncode == 1 and o and o[0]["reason"] == "ambiguous" and
              not pinned(ha) and not pinned(canon(ha)))
        for f in (canon(hn), ha, canon(ha)):
            run("--forget", f + ".json")

        # --- #067 LOW-2: rec_aside where link() fails (no hard links) ------
        cc = shutil.which("cc")
        if not cc:
            print("  skip no-hard-link aside (no cc)")
        else:
            src, mark = os.path.join(tmp, "nolink.c"), os.path.join(tmp, "nl")
            lib = os.path.join(tmp, "nolink.so")
            with open(src, "w") as f:
                f.write(NOLINK_C)
            ok = subprocess.run([cc, "-shared", "-fPIC", "-o", lib, src] +
                                (["-dynamiclib"] if sys.platform == "darwin"
                                 else []), capture_output=True).returncode == 0
            var = ("DYLD_INSERT_LIBRARIES" if sys.platform == "darwin"
                   else "LD_PRELOAD")
            plant("nl", end="stopped", agent=agent("sid-nl"), ended=now - 90)
            names.append("nl")
            subprocess.run([DCH, "--spawn", "nl", "sleep", "60"],
                           capture_output=True, timeout=60,
                           env=dict(os.environ, **{var: lib,
                                                   "NOLINK_MARK": mark}))
            until(lambda: (rec("nl") or {}).get("end") == "" and
                  asides("nl"))
            check("link() failing EPERM (shim hit: %s): the stopped record "
                  "still moves to nl.<ended>.json, id kept" %
                  os.path.exists(mark),
                  ok and os.path.exists(mark) and
                  asides("nl") == ["nl.%d" % (now - 90)] and
                  asid("nl.%d" % (now - 90)) == "sid-nl" and
                  asid("nl") is None)
            run("-k", "nl")
            until(lambda: (rec("nl") or {}).get("end") == "killed")
            plant("nl.%d" % (now - 89), name="nl", end="stopped",
                  agent=agent("sid-other"), ended=now - 89)
            plant("nl", end="stopped", agent=agent("sid-nl2"), ended=now - 90)
            subprocess.run([DCH, "--spawn", "nl", "sleep", "60"],
                           capture_output=True, timeout=60,
                           env=dict(os.environ, **{var: lib,
                                                   "NOLINK_MARK": mark}))
            until(lambda: (rec("nl") or {}).get("end") == "" and
                  len(asides("nl")) == 3)
            check("...and never over a taken name: the next second, "
                  "nl.<ended+2>, every id kept",
                  asides("nl") == ["nl.%d" % (now - t) for t in
                                   (90, 89, 88)] and
                  asid("nl.%d" % (now - 88)) == "sid-nl2" and
                  asid("nl.%d" % (now - 89)) == "sid-other" and
                  asid("nl.%d" % (now - 90)) == "sid-nl")
            run("-k", "nl")

        # --- #059 M1: --revive X --as Y keeps Y's own dead record ----------
        plant("mk", end="stopped", agent=agent("sid-mk"), ended=now - 120)
        plant("mo", end="stopped", agent=agent("sid-mo"), ended=now - 60)
        names.append("mk")
        run("--revive", "mo", "--as", "mk")
        check("--revive mo --as mk: mk carries sid-mo, mk's own sid-mk "
              "record is moved aside, mo's retired",
              until(lambda: asid("mk") == "sid-mo") and
              asides("mk") == ["mk.%d" % (now - 120)] and
              asid("mk.%d" % (now - 120)) == "sid-mk" and rec("mo") is None)

        # --- L2: --revive --as under DCH_NO_HISTORY keeps the old record ---
        plant("lo", end="stopped", agent=agent("sid-lo"))
        names.append("ln")
        subprocess.run([DCH, "--revive", "lo", "--as", "ln"],
                       capture_output=True, timeout=60,
                       env=dict(os.environ, DCH_NO_HISTORY="1"))
        check("--revive lo --as ln with no record kept for ln: lo's stays",
              asid("lo") == "sid-lo" and rec("ln") is None)
        # The retire is decided by the env, not by whether the new master
        # has written its record yet: checked at once, with no wait.
        plant("lz", end="stopped", agent=agent("sid-lz"))
        names.append("lz2")
        subprocess.run([DCH, "--revive", "lz", "--as", "lz2"],
                       capture_output=True, timeout=60,
                       env=dict(os.environ, DCH_NO_HISTORY="0"))
        check("--revive lz --as lz2 under DCH_NO_HISTORY=0: lz's record is "
              "gone the moment --revive returns", rec("lz") is None)

        # --- #162: a name starting with '-' -------------------------------
        plant("-h", end="exit 0")
        r = run("--forget", "-h")
        check("--forget -h: still a flag, exit 1, record kept",
              r.returncode == 1 and r.stderr ==
              "dch: --forget: unexpected -h"
              " (a name starting with - goes after --)\n" and rec("-h") is not None)
        r = run("--pin", "--", "-h")
        check("--pin -- -h pins it", r.returncode == 0 and
              r.stdout == "-h: pinned\n" and pinned("-h"))
        r = run("--forget", "--", "-h")
        check("--forget -- -h of the pinned record: failed pinned, kept",
              r.returncode == 1 and rec("-h") is not None)
        r = run("--unpin", "--json", "--", "-h")
        check("--unpin --json -- -h unpins it", r.returncode == 0 and
              (ndjson(r.stdout) or [{}])[0].get("result") == "ok" and
              not pinned("-h"))
        r = run("--forget", "--", "-h")
        check("--forget -- -h removes the record",
              r.returncode == 0 and r.stdout == "-h: forgotten\n" and
              rec("-h") is None)
        r = run("--forget", "--", "-h", "--all")
        check("after --, --all is a name too: two not found, exit 1",
              r.returncode == 1 and
              r.stdout.count("no history for this session") == 2)
        r = run("--forget", "--", "../-h", "a/b")
        check("after --, a name with '/' is still refused",
              r.returncode == 1 and r.stdout.count("no history") == 2)
        dash = "dch: a session name cannot start with '-'\n"
        sockdash = os.path.join(sockdir, "-h.sock")
        for args in (["--spawn", "-h", "true"], ["-n", "-h", "true"]):
            r = run(*args)
            check("dch %s refused: exit 2, one line, no session" %
                  " ".join(args), r.returncode == 2 and r.stderr == dash and
                  not os.path.exists(sockdash))
        plant("rv", end="stopped")
        r = run("--revive", "rv", "--as", "-h")
        check("--revive rv --as -h refused, exit 1",
              r.returncode == 1 and not os.path.exists(sockdash))
        plant("-h", end="stopped")
        r = run("--revive", "-h.json")
        check("--revive -h.json: not taken as a record, no session",
              r.returncode == 1 and not os.path.exists(sockdash))
        run("--forget", "--", "-h", "rv")
        # a live one, as an older dch made it: its master started directly,
        # orphaned so that --stop sees it exit (a child of ours would linger
        # as a zombie)
        for verb in (["-k", "--", "-x"], ["--stop", "--", "-x"]):
            mp = int(subprocess.run(
                ["sh", "-c", '"$@" </dev/null >/dev/null 2>&1 & echo $!',
                 "sh", DCH, "--master-of", os.path.join(sockdir, "-x.sock"),
                 "--", "sleep", "60"], capture_output=True,
                text=True).stdout)
            up = until(lambda: "-x" in run("-ls").stdout.split("\n"))
            r = run(*verb)
            gone = until(lambda: "-x" not in run("-ls").stdout.split("\n"))
            check("dch %s ends a live -x (%r)" % (" ".join(verb),
                                                  r.stdout.strip()),
                  up and r.returncode == 0 and gone)
            if not gone:
                os.kill(mp, 9)  # ours: the pid sh printed
        check("--stop's line names no revive for -x",
              r.stdout == "-x: stopped (a name starting with '-' is not "
              "revived)\n")
        run("--forget", "--", "-x")
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
