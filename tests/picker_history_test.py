#!/usr/bin/env python3
"""The picker's History tab: one row per session name, by name (#144).

Records written straight into the record dir: two of alpha, an unpinned
newer and a pinned older mix, two pinned pz-b, one each of pinned pz-a,
Beta and gamma. The picker runs as the command of a dch session (--read shows it,
--keys and --send type into it).

Covers:
  1. one row per name, the tab count the rows': [History 6];
  2. pinned first, each block by name, case folded: mix pz-a pz-b, then
     alpha Beta gamma;
  3. each row shows its name's newest record, pinned (◆) when any record of
     the name is: mix "exited (1)" with ◆, alpha "exited (2)" without;
  4. p on pz-b unpins both its records: no ◆, pz-b sorts among the rest;
  5. the filter keeps that one row: /alpha is one row, [History 1];
  6. open picks the record the row shows: Shift+Enter in the /alpha
     prompt alpha's newest (ALPHA_NEW), o on mix its newest (MIX_NEW),
     not the older pinned one;
  7. d, y on a name that also has a live session removes the row and both
     ended records, keeps the live one's, and says no failure;
  8. the cap counts pinned names: 3 pinned and 60 others are 50 rows, the
     47 newest of the 60.

Run:  python3 tests/picker_history_test.py      (uses ./dch)
"""
import json, os, re, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
ROOT = tempfile.mkdtemp(prefix="dchph", dir="/tmp")
ENV = dict(os.environ, DCH_SOCKET_DIR=os.path.join(ROOT, "s"),
           XDG_STATE_HOME=os.path.join(ROOT, "state"),
           HOME=os.path.join(ROOT, "home"),
           DCH_CONFIG=os.path.join(ROOT, "config"),
           DCH_LOGIN_ENV="0", DCH_NO_REMOTE="1")
for k in ("DCH_SESSION", "MallocStackLogging", "MallocStackLoggingNoCompact",
          "NO_COLOR", "COLORTERM", "COLORFGBG"):
    ENV.pop(k, None)
PKENV = ["-u", "DCH_SESSION", "NO_COLOR=1", "TERM=xterm-256color"]
ROW_RE = re.compile("^([>▌ ]) . (\\S+)")
RULE_RE = re.compile("^[─-]{20,}$")
failures = []


def dch(*args):
    return subprocess.run([DCH] + list(args), env=ENV, capture_output=True,
                          text=True, timeout=60)


def check(what, ok, *extra):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)
        for e in extra:
            print("     " + repr(e))


def screen(name):
    return dch("--read", name).stdout


def wait_screen(name, pred, timeout=10.0):
    end = time.time() + timeout
    s = ""
    while time.time() < end:
        s = screen(name)
        if pred(s):
            return s
        time.sleep(0.2)
    return s


def body(s):
    out = []
    for l in s.split("\n")[3:]:
        if RULE_RE.match(l):
            break
        out.append(l)
    return out


def lines(s):
    return [l for l in body(s) if ROW_RE.match(l)]


def rows(s):
    return [ROW_RE.match(l).group(2) for l in lines(s)]


def line(s, name):
    return next((l for l in lines(s) if ROW_RE.match(l).group(2) == name),
                "")


def current(s):
    for m in map(ROW_RE.match, body(s)):
        if m and m.group(1) != " ":
            return m.group(2)
    return None


def select(pk, name, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        s = screen(pk)
        r, cur = rows(s), current(s)
        if cur == name:
            return True
        if cur in r and name in r:
            d = r.index(name) - r.index(cur)
            dch("--keys", pk, *(["down"] * d if d > 0 else ["up"] * -d))
        time.sleep(0.3)
    return False


def history(pk):
    dch("--spawn", pk, "--size", "120x30", "env", *PKENV, DCH, "-l")
    wait_screen(pk, lambda s: "History " in s)
    for _ in range(4):
        if "[History" in screen(pk):
            break
        dch("--keys", pk, "tab")
        time.sleep(0.3)
    return wait_screen(pk, lambda s: "[History" in s and rows(s))


def main():
    dch("--spawn", "probe", "--size", "80x24", "sh", "-c", "sleep 600")
    if dch("--read", "probe").returncode == 3:
        print("skip: no terminal mirror in this build")
        return 0
    rec = os.path.join(ROOT, "state", "dch", "sessions",
                       ENV["DCH_SOCKET_DIR"].replace("/", "-"))
    dch("-k", "probe")
    # -k unlinks the socket and returns; the master writes its end record
    # later, at exit. --forget before that write leaves the record behind.
    end = time.time() + 10
    while time.time() < end:
        try:
            with open(os.path.join(rec, "probe.json")) as f:
                if json.load(f).get("end"):
                    break
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    dch("--forget", "probe")
    left = [f for f in os.listdir(rec) if f.startswith("probe")] \
        if os.path.isdir(rec) else []
    check("the probe session's record is forgotten", not left, left)
    os.makedirs(rec, exist_ok=True)
    now = int(time.time())

    def record(file, name, ago, end, word, pin=False):
        with open(os.path.join(rec, file + ".json"), "w") as f:
            json.dump(dict(name=name, sock_dir=ENV["DCH_SOCKET_DIR"],
                           alias="", cwd=ROOT,
                           argv=["sh", "-c", "echo %s; sleep 600" % word],
                           started=now - ago - 600, updated=now - ago,
                           ended=now - ago, end=end), f,
                      separators=(",", ":"))
        if pin:
            open(os.path.join(rec, file + ".pin"), "w").close()

    record("mix", "mix", 10, "exit 1", "MIX_NEW")
    record("mix.%d" % (now - 7000), "mix", 7000, "killed", "MIX_PIN", True)
    record("pz-b", "pz-b", 50, "exit 0", "PZB_NEW", True)
    record("pz-b.%d" % (now - 5000), "pz-b", 5000, "killed", "PZB_OLD", True)
    record("pz-a", "pz-a", 9000, "exit 0", "PZA", True)
    record("alpha", "alpha", 20, "exit 2", "ALPHA_NEW")
    record("alpha.%d" % (now - 30), "alpha", 30, "killed", "ALPHA_OLD")
    record("Beta", "Beta", 40, "exit 0", "BETA")
    record("gamma", "gamma", 1, "exit 0", "GAMMA")

    s = history("pk")
    want = ["mix", "pz-a", "pz-b", "alpha", "Beta", "gamma"]
    check("one row per name, the count deduped: [History 6]",
          "[History 6]" in s and len(rows(s)) == 6, s)
    check("pinned first, each block by name, case folded: %s" % want,
          rows(s) == want, rows(s))
    check("mix shows its newest record (exited (1)) with ◆, alpha its newest "
          "(exited (2)) without",
          "exited (1)" in line(s, "mix") and "\u25c6" in line(s, "mix") and
          "exited (2)" in line(s, "alpha") and
          "\u25c6" not in line(s, "alpha"), line(s, "mix"), line(s, "alpha"))

    check("select pz-b", select("pk", "pz-b"), screen("pk"))
    dch("--keys", "pk", "p")
    after = ["mix", "pz-a", "alpha", "Beta", "gamma", "pz-b"]
    s = wait_screen("pk", lambda s: rows(s) == after, 15)
    check("p unpins both pz-b records: no ◆, %s" % after,
          rows(s) == after and "\u25c6" not in line(s, "pz-b") and
          not [f for f in os.listdir(rec) if f.startswith("pz-b") and
               f.endswith(".pin")], s)

    dch("--keys", "pk", "/")
    wait_screen("pk", lambda s: "/█" in s)
    dch("--send", "pk", "alpha")
    s = wait_screen("pk", lambda s: "/alpha█" in s and rows(s) == ["alpha"])
    check("/alpha keeps one row, [History 1]",
          rows(s) == ["alpha"] and "[History 1]" in s, s)
    dch("--send", "pk", "\x1b[13;2u")     # Shift+Enter: open the top match
    s = wait_screen("pk", lambda s: "ALPHA_" in s, 20)
    check("Shift+Enter on the filtered alpha opens its newest record (ALPHA_NEW)",
          "ALPHA_NEW" in s and "ALPHA_OLD" not in s, s)

    s = history("pm")
    check("select mix", select("pm", "mix"), screen("pm"))
    dch("--keys", "pm", "o")
    s = wait_screen("pm", lambda s: "MIX_" in s, 20)
    check("o on mix opens the newest record the row shows (MIX_NEW)",
          "MIX_NEW" in s and "MIX_PIN" not in s, s)

    # d on a name with a live session: its two ended records go, the live
    # one's record stays, and no failure is said
    dch("--spawn", "lv", "--size", "80x24", "sh", "-c", "sleep 600")
    # ended "after" the live one's last update (a clock step back): the
    # live record lists last, so a failure on it would be the line shown
    lv = ["lv.%d" % (now + 100), "lv.%d" % (now + 200)]
    for i, file in enumerate(lv):
        record(file, "lv", -100 * (i + 1), "exit 0", "LV")
    s = history("pd")
    check("select lv", select("pd", "lv"), screen("pd"))
    dch("--keys", "pd", "d")
    wait_screen("pd", lambda s: "Remove lv from History?" in s)
    dch("--keys", "pd", "y")
    s = wait_screen("pd", lambda s: "lv" not in rows(s) and "forgotten" in s,
                    15)
    left = os.listdir(rec)
    check("d, y on lv (live too): its row and both ended records gone, the "
          "live record kept, no failure said",
          "lv" not in rows(s) and "forgotten" in s and "failed" not in s and
          not [f for f in left if f.startswith("lv.") and f != "lv.json"] and
          "lv.json" in left,
          s, left)

    # the cap: 3 pinned and 60 more names, 50 rows; the 47 newest of the 60
    for f in os.listdir(rec):        # every record but the live ones'
        if f not in ("pk.json", "pm.json", "pd.json", "lv.json", "mix.json",
                     "alpha.json"):
            os.unlink(os.path.join(rec, f))
    for i in range(3):
        record("cp%d" % i, "cp%d" % i, 5000 + i, "exit 0", "CP", True)
    for i in range(60):
        record("cu%02d" % i, "cu%02d" % i, 1000 - i * 10, "exit 0", "CU")
    s = history("pc")
    check("3 pinned and 60 other names: [History 50], pinned first",
          "[History 50]" in s and rows(s)[:3] == ["cp0", "cp1", "cp2"], s)
    dch("--keys", "pc", "/")
    wait_screen("pc", lambda s: "/█" in s)
    dch("--send", "pc", "cu")
    s = wait_screen("pc", lambda s: "/cu█" in s and "[History 47]" in s)
    check("... the 47 newest of the 60: /cu is [History 47], cu13 first",
          "[History 47]" in s and rows(s)[:1] == ["cu13"], s)
    return 1 if failures else 0


if __name__ == "__main__":
    rc = 1
    try:
        rc = main()
    finally:
        for n in ("pk", "pm", "pd", "pc", "lv", "probe", "mix", "alpha"):
            dch("-k", n)
        shutil.rmtree(ROOT, ignore_errors=True)
    print("PASS" if rc == 0 else "FAILED: %d" % len(failures))
    sys.exit(rc)
