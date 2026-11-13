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
  7. a name with a session running is decided by the name (#254): pinned
     with no ended record (rp) and pinned with two (rq) are one row each,
     "running"; running and not pinned (lv, two older records) is no row;
     pinned and ended (rn) shows its end; d and m on rq refuse, saying it
     is running; /rp keeps the running row; rn started under the open
     picker turns running, killed turns closed; p on rq unpins every record
     and the row goes; Enter on rp in dch -k kills nothing, in dch -l
     attaches it;
  8. the cap counts pinned names, running ones too: 3 pinned, running
     pinned rp and 60 others are 50 rows, the 46 newest of the 60.

Run:  python3 tests/picker_history_test.py      (uses ./dch)
"""
import json, os, re, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
ROOT = tempfile.mkdtemp(prefix="dchph", dir=os.environ.get("DCH_TEST_TMP", "/tmp"))
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



def below_tabs(s):
    """The lines under the tabs row and the column headers: the tabs row
    is the first on dch -l and the switch picker (#252), the second under
    -k's and -d's title."""
    ls = s.split("\n")
    i = next((k for k, l in enumerate(ls[:2])
              if re.match(r"[\[ ]?(Sessions|Settings)\b", l)), 1)
    return ls[i + 2:]

def body(s):
    out = []
    for l in below_tabs(s):
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


def history(pk, flag="-l"):
    dch("--spawn", pk, "--size", "120x30", "env", *PKENV, DCH, flag)
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
                           alias="", cwd="/",  # a temp dir name could match a filter
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

    # a running name (#254): decided by the name, not by which record is
    # live. rp pinned, running, no ended record; rq pinned, running, two
    # ended records; lv running, not pinned, two ended records; rn pinned,
    # ended, started and ended again while the picker is open
    def run(name, word):
        dch("--spawn", name, "--size", "80x24", "sh", "-c",
            "echo %s; sleep 600" % word)

    run("rp", "RP_LIVE")
    run("rq", "RQ_LIVE")
    run("lv", "LV_LIVE")
    end = time.time() + 10
    while time.time() < end and not all(
            os.path.exists(os.path.join(rec, n + ".json"))
            for n in ("rp", "rq", "lv")):
        time.sleep(0.1)
    dch("--pin", "rp")
    for i in (1, 2):
        record("rq.%d" % (now - 900 * i), "rq", 900 * i, "signal 15", "RQ",
               True)
    # ended "after" the live one's last update (a clock step back): on the
    # base the newest record of the name, the one its row showed
    for i in (1, 2):
        record("lv.%d" % (now + 100 * i), "lv", -100 * i, "exit 0", "LV")
    record("rn.%d" % (now - 600), "rn", 600, "exit 0", "RN", True)
    s = history("pr")
    check("pinned and running with no ended record: rp is a row, ◆, "
          "running", "running" in line(s, "rp") and
          "\u25c6" in line(s, "rp"), s)
    check("pinned and running with two ended records: one rq row, running, "
          "not an old end",
          rows(s).count("rq") == 1 and "running" in line(s, "rq") and
          "ended" not in line(s, "rq"), s)
    check("running and not pinned: lv is not on History (its older records "
          "neither)", "lv" not in rows(s), s)
    check("pinned and ended: rn unchanged, its end (exited)",
          "exited" in line(s, "rn") and "running" not in line(s, "rn"), s)

    check("select rq", select("pr", "rq"), screen("pr"))
    s = screen("pr")
    check("a running row's hints: attach, no resume, no remove",
          "attach" in s and "resume" not in s and "remove" not in s, s)
    dch("--keys", "pr", "d")
    s = wait_screen("pr", lambda s: "rq is running" in s)
    check("d on running rq refuses: says it is running, asks nothing",
          "rq is running; d removes it once it has ended" in s and
          "Remove" not in s, s)
    dch("--keys", "pr", "m")
    s = wait_screen("pr", lambda s: "Enter attaches it" in s)
    check("m on running rq resumes nothing: says it is running",
          "rq is running; Enter attaches it" in s, s)
    time.sleep(1)
    left = os.listdir(rec)
    check("... and every rq record is still there",
          len([f for f in left if f.startswith("rq") and
               f.endswith(".json")]) == 3, left)

    dch("--keys", "pr", "/")
    wait_screen("pr", lambda s: "/█" in s)
    dch("--send", "pr", "rp")
    s = wait_screen("pr", lambda s: "/rp█" in s and rows(s) == ["rp"])
    check("the filter keeps a running row: /rp is rp, running, [History 1]",
          rows(s) == ["rp"] and "running" in line(s, "rp") and
          "[History 1]" in s, s)
    dch("--keys", "pr", "esc")
    s = wait_screen("pr", lambda s: "/rp" not in s and "rn" in rows(s))

    run("rn", "RN_LIVE")
    s = wait_screen("pr", lambda s: "running" in line(s, "rn"), 15)
    check("a session started while the picker is open: rn turns running",
          "running" in line(s, "rn") and rows(s).count("rn") == 1, s)
    dch("-k", "rn")
    s = wait_screen("pr", lambda s: "closed" in line(s, "rn"), 15)
    check("... and ended: rn is an ended row again (closed)",
          "closed" in line(s, "rn") and "running" not in line(s, "rn"), s)

    check("select rq again", select("pr", "rq"), screen("pr"))
    dch("--keys", "pr", "p")
    s = wait_screen("pr", lambda s: "rq" not in rows(s), 15)
    check("p on running rq unpins every record of it: the row goes",
          "rq" not in rows(s) and not [f for f in os.listdir(rec) if
                                       f.startswith("rq") and
                                       f.endswith(".pin")], s)

    s = history("pkk", "-k")
    check("dch -k: select rp", select("pkk", "rp"), screen("pkk"))
    dch("--keys", "pkk", "enter")
    s = wait_screen("pkk", lambda s: "rp is running" in s)
    time.sleep(1)
    check("dch -k: Enter on running rp kills nothing, says where it "
          "attaches", "rp is running; attach it from dch -l or the switch"
          in s and "rp" in dch("-ls").stdout.split(), s)
    dch("-k", "pkk")

    check("select rp", select("pr", "rp"), screen("pr"))
    dch("--keys", "pr", "enter")
    s = wait_screen("pr", lambda s: "RP_LIVE" in s, 15)
    check("Enter on running rp attaches it (its screen), revives nothing",
          "RP_LIVE" in s and not [f for f in os.listdir(rec)
                                  if f.startswith("rp-")], s)

    # the cap: 3 pinned and 60 more names and pinned running rp, 50 rows;
    # the 46 newest of the 60
    for f in os.listdir(rec):        # every record but the live ones'
        if f not in ("pk.json", "pm.json", "pr.json", "lv.json", "mix.json",
                     "alpha.json", "rp.json", "rp.pin", "rq.json"):
            os.unlink(os.path.join(rec, f))
    for i in range(3):
        record("cp%d" % i, "cp%d" % i, 5000 + i, "exit 0", "CP", True)
    for i in range(60):
        record("cu%02d" % i, "cu%02d" % i, 1000 - i * 10, "exit 0", "CU")
    s = history("pc")
    check("3 pinned, running pinned rp and 60 other names: [History 50], "
          "pinned first, rp running among them",
          "[History 50]" in s and
          rows(s)[:4] == ["cp0", "cp1", "cp2", "rp"] and
          "running" in line(s, "rp"), s)
    dch("--keys", "pc", "/")
    wait_screen("pc", lambda s: "/█" in s)
    dch("--send", "pc", "cu")
    s = wait_screen("pc", lambda s: "/cu█" in s and "[History 46]" in s)
    check("... the 46 newest of the 60 (the running unpinned names take no "
          "place): /cu is [History 46], cu14 first",
          "[History 46]" in s and rows(s)[:1] == ["cu14"], s)
    return 1 if failures else 0


if __name__ == "__main__":
    rc = 1
    try:
        rc = main()
    finally:
        for n in ("pk", "pm", "pr", "pkk", "pc", "lv", "rp", "rq", "rn",
                  "probe", "mix", "alpha"):
            dch("-k", n)
        shutil.rmtree(ROOT, ignore_errors=True)
    print("PASS" if rc == 0 else "FAILED: %d" % len(failures))
    sys.exit(rc)
