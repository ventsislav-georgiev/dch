#!/usr/bin/env python3
"""The X-ray view's agent rows (#360, on #359's "peer").

xv-pe, a History Claude record: two prompts the user typed, a cross-session
message from prosper, one whose sender has no readable name, one from a
sender with a long wide name, a subagent hand-back and a task notification.

Covers: Prompts marks a cross-session row with U+1F916 and its sender, the
nameless one with the robot alone, the user's rows with nothing; the mark
where an unmarked row's title starts; Responses marks its items the same;
the reader's header too; a hides the cross-session rows on both tabs (the
selection on its prompt, or the next older one shown), the key help says
"a hide agent rows" / "a agent rows 3 hidden"; with n off and a off only
the user's prompts are left; n shows the hand-back with U+1F4E5 and its
sender, the notification with U+1F514; a filter whose rows a hides says
"a shows them"; at 41 columns no row passes the edge. xv-np, a record with
no "peer" (an older host's): no mark, no a in the key help, a hides nothing.

Run:  python3 tests/xray_peer_view_test.py      (uses ./dch)
"""
import json, os, re, shutil, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from xray_view_lib import DCH, HOME, P, T, Pty, cells, check, dch, fails, head, note, rows, say, screen, tl, turn  # noqa: E402

PSID = "11111111-2222-4333-8444-5555555555c7"   # xv-pe
NSID = "11111111-2222-4333-8444-5555555555c8"   # xv-np
BOT, INBOX, BELL = "\U0001F916", "\U0001F4E5", "\U0001F514"
WIDE = "設計" * 15
WRAP = ("Another Claude session sent a message:\n<cross-session-message "
        "from=\"uds:/tmp/cc-socks/74069.sock\" from-name=\"x\">\n%s\n"
        "</cross-session-message>\n")


def peer(body, ts, name, handback=False):
    o = {"kind": "peer", "from": "uds:/tmp/cc-socks/74069.sock",
         "name": name, "body": body}
    if handback:
        o.update(handback=True, handbackReportText=body)
    return [{"type": "user", "cwd": P, "timestamp": ts, "isMeta": True,
             "message": {"content": WRAP % body}, "origin": o},
            say("ok, " + body.split()[0])]


def record(name, sid, lines):
    os.makedirs(os.path.dirname(tl(sid)), exist_ok=True)
    with open(tl(sid), "w") as f:
        for o in lines:
            f.write(json.dumps(o) + "\n")
    sockdir = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    rec = {"name": name, "cwd": P, "end": "exited", "argv": ["claude"],
           "started": 1794299400, "updated": 1794300000,
           "ended": 1794300000, "sock_dir": sockdir,
           "agent": {"session_id": sid, "cwd": P,
                     "config_dir": os.path.join(HOME, ".claude")}}
    with open(os.path.join(hdir, name + ".json"), "w") as f:
        f.write(json.dumps(rec) + "\n")


def fixture():
    task = turn(note("<task-id>t1</task-id>\n<summary>Build finished: 3 "
                     "passed</summary>"), "2026-11-10T10:40:00Z",
                say("noticed the build"), auto=True)
    record("xv-pe", PSID,
           turn("my own first prompt", "2026-11-10T10:00:00Z",
                say("answer one")) +
           peer("Scrollback answers for entry 895", "2026-11-10T10:10:00Z",
                "prosper") +
           peer("PASS. all rows green", "2026-11-10T10:20:00Z", "worker-a",
                handback=True) +
           peer("nameless note here", "2026-11-10T10:30:00Z",
                "0123456789abcdef") +
           task +
           peer("wide sender long title " + "z" * 80,
                "2026-11-10T10:50:00Z", WIDE) +
           turn("my own second prompt", "2026-11-10T11:00:00Z",
                say("answer two")))
    record("xv-np", NSID,
           turn("plain prompt one", "2026-11-10T10:00:00Z", say("a1")) +
           task +
           turn("plain prompt two", "2026-11-10T11:00:00Z", say("a2")))


def listed(v):
    """The list's rows on screen, top down (a repaint draws only the rows
    that changed)."""
    scr = rows(v.out)
    return [scr[k] for k in sorted(scr) if re.match(r"^.\s{0,3}\d+  ", scr[k])]


def at(rws, text):
    return next((r for r in rws if text in r), "")


def col(r, text):
    return cells(r[:r.find(text)])


def test_marks():
    o = json.loads(dch("--xray", "xv-pe", "--json").stdout)
    check("engine: peer on the cross-session rows and the hand-back",
          [c.get("peer") for c in o["chapters"]] ==
          [None, "prosper", "worker-a", "", None, WIDE, None], o["chapters"])
    v = Pty([DCH, "--xray", "xv-pe"], rows=40, cols=100)
    try:
        check("Prompts: 5 shown by default", v.wait(b"Prompts 5"), v.out)
        v.pump(0.5)
        r = listed(v)
        pro, own = at(r, "Scrollback"), at(r, "my own first")
        check("Prompts: the cross-session row: robot, sender, title",
              BOT + " prosper  Scrollback answers for entry 895" in pro, r)
        check("Prompts: no readable name: the robot alone",
              BOT + " nameless note here" in at(r, "nameless"), r)
        check("Prompts: the user's rows unmarked",
              all(m not in at(r, t) for m in (BOT, INBOX, BELL)
                  for t in ("my own first", "my own second")), r)
        check("Prompts: the mark where an unmarked title starts, the "
              "title 3 cells on (the robot 2 cells wide)",
              col(pro, BOT) == col(own, "my own first") and
              col(pro, "prosper") == col(own, "my own first") + 3,
              (pro, own))
        check("Prompts: auto rows hidden by default",
              not at(r, "PASS.") and not at(r, "Build finished"), r)
        s = screen(v, 0)
        check("key help: a hide agent rows", "a hide agent rows" in s, s)
        # select nameless (3rd from the top), a: it hides, the next older
        # one shown takes the selection
        v.key(b"jj", 0.5)
        check("selection on nameless", "nameless" in head(v), head(v))
        mark = len(v.out)
        v.key(b"a", 0.8)
        r = listed(v)
        check("a: only the user's prompts left (n off, a off)",
              len(r) == 2 and at(r, "my own second") and
              at(r, "my own first"), r)
        check("a: the selection on the next older one shown",
              "my own first" in head(v), head(v))
        s = screen(v, mark)
        check("key help: a agent rows 3 hidden",
              "a agent rows 3 hidden" in s, s)
        check("a: Prompts 2 in the tab bar", "Prompts 2" in s, s)
        mark = len(v.out)
        v.key(b"a", 0.8)
        check("a again: Prompts 5 in the tab bar",
              "Prompts 5" in screen(v, mark), screen(v, mark))
        check("a again: they are back, the selection kept",
              at(listed(v), "Scrollback") and
              "my own first" in head(v), head(v))
        # the reader's header
        v.key(b"k", 0.3)
        mark = len(v.out)
        v.key(b"\r", 1.0)
        v.wait(b"Scrollback", start=mark)
        hd = rows(v.out[mark:]).get(2, "")
        check("reader: its header marked as its row",
              BOT + " prosper  Scrollback" in hd, hd)
        v.key(b"\033", 0.6)
        # n: the hand-back and the notification, marked
        mark = len(v.out)
        v.key(b"n", 0.8)
        r = listed(v)
        check("n: the hand-back: inbox, sender, its report",
              INBOX + " worker-a  PASS. all rows green" in at(r, "PASS."), r)
        check("n: the notification: the bell, its summary",
              BELL + " " in at(r, "Build finished"), r)
        v.key(b"n", 0.5)
        # Responses
        mark = len(v.out)
        v.key(b"\t", 1.0)
        r = listed(v)
        check("Responses: the items marked as their prompts",
              BOT + " prosper  Scrollback" in at(r, "Scrollback") and
              BOT + " nameless" in at(r, "nameless") and
              BOT not in at(r, "my own second"), r)
        check("Responses: the newest selected", "my own second" in head(v),
              head(v))
        nresp = re.search(r"Responses (\d+)", screen(v, 0))
        mark = len(v.out)
        v.key(b"a", 0.8)
        r, s = listed(v), screen(v, mark)
        check("Responses: a drops the tab count by 3",
              nresp and "Responses %d" % (int(nresp.group(1)) - 3) in s, s)
        check("Responses: a hides them, the selection kept, the key help "
              "counts them",
              all(BOT not in x for x in r) and len(r) == 2 and
              "my own second" in head(v) and "a agent rows 3 hidden" in s,
              (r, s))
        mark = len(v.out)
        v.key(b"a", 0.8)
        check("Responses: a shows them", at(listed(v), "Scrollback"),
              v.out[mark:])
        check("Responses: the tab count is back",
              nresp and "Responses %s" % nresp.group(1) in screen(v, mark),
              screen(v, mark))
        v.key(b"\033[Z", 0.6)       # Shift-Tab back to Prompts
        # a filter only cross-session rows match, then a: the empty line
        v.key(b"/Scrollback", 0.5)
        v.key(b"\033[A", 0.8)     # Up ends the filter; Enter would read
        mark = len(v.out)
        v.key(b"a", 0.8)
        s = screen(v, mark)
        check("filter + a: the empty line says a shows them",
              "Only agent rows: a shows them" in s, (s, v.out[mark:]))
        v.key(b"a", 0.4)
        v.key(b"/\033", 0.6)      # the filter cleared
        # narrow: no row past the edge, the wide name cut
        mark = len(v.out)
        v.resize(30, 41)
        v.pump(1.0)
        scr = rows(v.out[mark:])
        over = [x for x in scr.values() if cells(x) > 41]
        check("41 columns: no row past the edge, the wide sender cut",
              not over and any("設" in x for x in scr.values()),
              over or scr)
        v.key(b"q", 0.3)
        check("q exits 0", v.exit() == 0, v.rc)
    finally:
        v.close()


def test_no_peer():
    v = Pty([DCH, "--xray", "xv-np"], rows=30, cols=100)
    try:
        check("no peer: Prompts 2", v.wait(b"Prompts 2"), v.out)
        v.pump(0.5)
        r, s = listed(v), screen(v, 0)
        check("no peer: no mark, no a in the key help",
              len(r) == 2 and not any(m in "".join(r)
                                      for m in (BOT, INBOX, BELL)) and
              "agent rows" not in s, (r, s))
        mark = len(v.out)
        v.key(b"a", 0.8)
        r = listed(v)
        check("no peer: a hides nothing", len(r) == 2, r)
        v.key(b"n", 0.8)
        check("no peer: n's notification gets the bell",
              BELL + " " in at(listed(v), "Build finished"),
              v.out[mark:])
        v.key(b"q", 0.3)
        v.exit()
    finally:
        v.close()


def main():
    try:
        fixture()
        test_marks()
        test_no_peer()
    finally:
        shutil.rmtree(T, ignore_errors=True)
    if fails[0]:
        print("%d failed" % fails[0])
        sys.exit(1)


if __name__ == "__main__":
    main()
