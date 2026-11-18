#!/usr/bin/env python3
"""The `dch --xray <n>` view, twin names in one project (#493): ended records
sharing a name, a live session renamed over an ended one.

The harness, the fixtures and the list of what the parts cover
are in xray_view_lib.py.

Run:  python3 tests/xray_view_dup_test.py      (uses ./dch)
"""
from xray_view_lib import *  # noqa: F401,F403 (harness, fixtures, T)


DLONG = "xv-db-long-branch-name"
DSIDS = ["11111111-2222-4333-8444-493000000%03d" % k for k in range(1, 6)]


def fixture_dup():
    """#493: xv-da (viewed) and, in its project, two ended records both named
    DLONG (two ids), an ended xv-dc and a live session now named xv-dc."""
    cwd = "/home/demo/dup"
    os.makedirs(os.path.dirname(tl("x", "-home-demo-dup")), exist_ok=True)
    for k, (name, fn, pre) in enumerate((
            ("xv-da", None, "mine"), (DLONG, "xv-db-1", "first"),
            (DLONG, "xv-db-2", "second"), ("xv-dc", "xv-dc-old", "old"))):
        record(name, DSIDS[k], [dict(o, cwd=cwd) if "cwd" in o else o
               for o in turn("%s: see #412" % pre, "2026-11-1%dT10:00:00Z" % k,
                             at(say("ok %s" % pre), "2026-11-1%dT10:01:00Z" % k))],
               "-home-demo-dup", cwd, fn,
               1794300000 + 1000 * (name == "xv-dc"))
    with open(tl(DSIDS[4], "-home-demo-dup"), "w") as f:
        for o in turn("live: see #412", "2026-11-18T10:00:00Z",
                      at(say("ok live"), "2026-11-18T10:01:00Z")):
            f.write(json.dumps(dict(o, cwd=cwd) if "cwd" in o else o) + "\n")
    old = time.time() - 600
    os.utime(tl(DSIDS[4], "-home-demo-dup"), (old, old))
    dch("--spawn", "xv-dc", "/bin/sleep", "300")
    pid = until(lambda: json.loads(dch("--cmd", "xv-dc").stdout or "{}")
                .get("pid"), 5)
    start = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(pid)],
                           capture_output=True, text=True,
                           env={"LC_ALL": "C", "TZ": "UTC"}).stdout.strip()
    sd = os.path.join(HOME, ".claude", "sessions")
    os.makedirs(sd, exist_ok=True)
    # ten minutes back: a live age of "now" is short enough to stay at 30
    # columns, so the 30-column row needs it past "now"
    now = int(time.time() * 1000) - 600000
    with open(os.path.join(sd, "%d.json" % pid), "w") as f:
        json.dump({"pid": pid, "sessionId": DSIDS[4], "cwd": cwd,
                   "procStart": start, "version": "2.1.0",
                   "kind": "interactive", "status": "idle",
                   "updatedAt": now, "statusUpdatedAt": now}, f)


def dup_card(cols):
    """xv-da's card, full scan, at cols columns: the Pty and its screen."""
    v = Pty([DCH, "--xray", "xv-da"], rows=40, cols=cols,
            env={"TMPDIR": TPD + "/", "NO_COLOR": ""})
    # the count has its own SGR: read the screen, not the raw bytes
    until(lambda: (v.pump(0.1), "Prompts 1" in screen(v, 0))[1], 10)
    v.pump(0.5)
    for _ in range(6):
        if "[Refs" in rows(v.out).get(2, ""):
            break
        v.key(b"\t", 0.5)
    mark = len(v.out)
    v.key(b"\r", 1.5)
    v.wait(b"Mentions: this session ", start=mark)
    mark = len(v.out)
    v.key(b"f", 0.5)
    until(lambda: (v.pump(0.1), "in 3 sessions" in screen(v, mark))[1], 10)
    v.pump(0.5)
    return v


def dup_resize(v, cols):
    """Draw xv-da's filtered card again at cols columns, on the same Pty."""
    v.resize(40, cols)
    mark = len(v.out)
    v.wait(b"also discussed in", start=mark)
    v.pump(0.5)
    return mark


def test_dup():
    """#493: LOW-UK1 groups by id, LOW-UK2 keeps the name, NIT-UK4 Enter on
    an ended session opens it by id."""
    v = dup_card(100)
    try:
        text = screen(v, 0)
        hs = re.findall(r"also discussed in ([^,]+), (\w+)( ago)?$", text,
                        re.M)
        names = [h[0] for h in hs]
        check("LOW-UK1: two ended records named alike get two headers",
              names.count(DLONG) == 2, text)
        dc = [h for h in hs if h[0] == "xv-dc"]
        check("LOW-UK1: an ended xv-dc and the live xv-dc get two headers, "
              "each its own age (one in days)", len(dc) == 2 and
              any(re.fullmatch(r"\d+d", h[1]) for h in dc) and
              any(not re.fullmatch(r"\d+d", h[1]) for h in dc), (hs, text))
        # Enter on the ended xv-dc's header: its reader, not the live one's
        for _ in range(12):
            if any("\u258c" in l and re.search(r"discussed in xv-dc, \d+d", l)
                   for l in rows(v.out).values()):
                break
            v.key(b"j", 0.4)
        mark = len(v.out)
        v.key(b"\r", 1.0)
        ok = until(lambda: (v.pump(0.1), "ok old" in screen(v, mark))[1], 5)
        text = screen(v, mark)
        check("NIT-UK4: Enter on the ended xv-dc opens the ended session "
              "(by id), not the live one", ok and "ok live" not in text, text)
        v.key(b"q", 0.5)
        v.key(b"q")
        v.key(b"q")
        v.exit()
    finally:
        v.close()
    # one Pty for the narrow cards and the last wide one, resized; opened at
    # 30 columns: a resized card keeps the live header's age there (a fresh
    # one drops it), so the 30-column row needs the first draw at 30
    v = dup_card(30)
    try:
        # #493: the age goes first, the name stays whole; a header whose
        # transcript is gone says so, never the engine's "no x-ray corpus"
        text = screen(v, 0)
        check("#493: at 30 columns xv-dc's ended header keeps its whole name "
              "and drops the age", re.search(r"discussed in xv-dc$", text,
              re.M) and "xv-dc," not in text, text)
        for cols in (34, 36, 40):
            text = screen(v, dup_resize(v, cols))
            m = re.search(r"also discussed in (xv-db\S*?)(\u2026)?(,|\s*$)",
                          text, re.M)
            check("LOW-UK2: the header at %d columns shows a name, a cut "
                  "one marked with an ellipsis" % cols, m is not None and
                  DLONG.startswith(m.group(1)) and len(m.group(1)) > 3 and
                  (m.group(1) == DLONG or m.group(2) == "\u2026"), text)
        # a status line of 30 columns cuts the sentence: the wide card
        dup_resize(v, 100)
        os.unlink(tl(DSIDS[3], "-home-demo-dup"))
        for _ in range(12):
            if any("\u258c" in l and re.search(r"discussed in xv-dc, \d+d", l)
                   for l in rows(v.out).values()):
                break
            v.key(b"j", 0.4)
        mark = len(v.out)
        v.key(b"\r", 1.0)
        ok = until(lambda: (v.pump(0.1), "transcript is gone" in
                            screen(v, mark))[1], 5)
        text = screen(v, mark)
        check("#493: Enter on a header whose transcript is gone says so, not "
              "'no x-ray corpus'", ok and "no x-ray corpus" not in text, text)
        v.key(b"q")
        v.key(b"q")
        v.exit()
    finally:
        v.close()


def main():
    run_steps(fixture_dup, test_dup)


if __name__ == "__main__":
    main()
