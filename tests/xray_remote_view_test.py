#!/usr/bin/env python3
"""The X-ray view of a remote session (#188, XR5c): `dch --xray host:name`
on the host's record, its live shell, a hostile host, o.

The host, the fake tailscale and the harness: xray_remote_view_lib.py.

Covers:
  1. srv:xv-cl, a History Claude record on the host with a hostile corpus:
     the title names srv:, the tabs carry the host's counts (Prompts, not
     Chapters, #295; a shell's is Commands), the chapter
     list, the Refs list, the card (light, `f` full scan), the reader, q
     exits 0; the corpus's ESC, BEL and bidi bytes never reach the
     terminal; every query ran on the host as its own ctl (follow, ref,
     ref --full, chapter); k, Up, PgUp and Enter in its card under a
     too-small window neither crash nor read outside the mentions (QA
     1.56.0 MED-V1);
  2. Refs of srv:xv-sh, a live shell on the host (#294): no On screen
     tab; the screen read with ctl --read xv-sh --ansi, o/r#41 on it
     under the faint "on screen now" line, not repeated below, Enter
     opens its card (full build); on dch-lite no group line;
  3. a hostile host: raw ESC, BEL, C1 and bidi bytes in its snapshot are
     drawn as ?; its --read screen, with OSC, CSI, BEL, C1, bidi, invalid
     UTF-8 and NUL bytes, still gives its o/r#41 to Refs' group (#294) and
     none of those bytes reach the terminal; a later failed read takes
     the group away and its stderr is never drawn (QA 1.57.0 LOW-R1,
     NIT-R1);
  5. the picker: x on the remote Sessions row srv:xv-sh opens its view, q
     returns to the picker, X (kill) there opens no view (OQ-6); x on the
     remote History row srv:xv-cl opens that record's view; next to a local
     session literally named srv:xv-sh, x on each row opens that row's
     session (QA 1.57.0 LOW-R2), and Enter on the remote row, o in its
     view and o in a dch --xray srv:xv-sh view opened before the local
     one came attach the host's, o in the local row's view the local one
     (#210);
  9. o (#194): on srv:xv-sh's Commands the view is left as q leaves it and
     the client attaches to xv-sh on the host (the host's log says so);
     on the ended srv:xv-cl it says the session has ended and stays;
  12. the lookup (#263) on srv:xv-cl: OQ-32, which no row has, and #831,
      which a chapter title holds, typed in the filter: ^R opens each card
      from the host, Enter reads a mention's chapter there, Esc back to the
      card; each a ctl --xray of its own. The hostile host refuses --ref
      as an older dch's grammar would (exit 2): the status says the host's
      dch takes no such ref, with its line, and the view stays.
  13. Responses (#290) of srv:xv-cl: the host's answers newest first (the
      text, or the tool line of a turn that ended on its result), Enter
      reads the item's prompt from the host at 2 of 2, q back to the item,
      each a ctl --xray of its own. A fake host whose chapters lack #296's
      fields (a dch before 1.63.0, or no answer yet): No responses yet,
      never "older than 1.63.0" (LOW-V19), keys do no harm. One
      with them: the running turn working, an answer the cap dropped
      (response_offset, no response) says Enter reads it, and Enter reads
      it from the host. Every Prompts-to-Refs move is Shift-Tab now
      (twice since #291: Search is the last tab).
  14. Search (#291) of srv:xv-cl: PROJ-5 searched on the host, its hits
      newest first (the prompt as You, the tool result as Bash under the
      hostile title, with ?), Enter reads the hit's prompt from the host at
      1 of 2, q back to the hit; the follow, the search and the chapter
      each a ctl --xray. A fake host before 1.63.0 refuses --search (0x13):
      the list says its dch does not know --search, no count. A fake answer:
      a null chapter's hit says it is outside the prompts shown and Enter
      on it says so, a tool hit by its tool, partial with why time said,
      ESC and BEL in a snippet shown as ?; Enter on the tool hit reads its
      prompt from the host.
  15. the card's mentions newest first (#304, QA 1.62.0 MED-V13), read
      on the host: a transcript under 1 MiB, one spanning several 1 MiB
      windows (no zigzag), and after f (oldest first from the engine); the
      selected mention stays selected while f's lines arrive; Enter on a
      mention reads its prompt from the host.
  Also: the fake hosts of 13 and 14: xray_remote_fakes_test.py.

Run:  python3 tests/xray_remote_view_test.py      (uses ./dch)
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from xray_remote_view_lib import *  # noqa: E402,F401,F403


# ---- 1: the view on the host's record --------------------------------------

def test_view():
    n0 = {w: ctls(w) for w in ("--xray", "--read")}
    late = []
    v = Pty([DCH, "--xray", "srv:xv-cl"])
    try:
        check("view: the title names the host's session as srv:xv-cl",
              v.wait(b"X-ray  srv:xv-cl"), v.out[-600:])
        check("view: the tabs carry the host's counts",
              v.wait(b"Prompts 2") and v.wait(b"Refs 3"), v.out[-600:])
        check("#295: a Claude record's first tab is Prompts, no Chapters",
              b"Chapters" not in v.out, v.out[-600:])
        check("view: the chapter list shows both titles",
              v.wait(b"second prompt about PROJ-5") and
              v.wait(b"look at #831"), v.out[-600:])
        check("hostile host text: the chapter title's OSC, BEL, U+202E and "
              "U+061C show as ?", v.wait(b"o/r#7 ?]0;pwned? evil?title al?m"),
              v.out[-600:])
        v.key(b"\033[Z\033[Z")  # back to Search, then Refs (#291)
        check("Refs: the host's keys", v.wait(b"#831") and
              v.wait(b"PROJ-5") and v.wait(b"o/r#7"), v.out[-600:])
        v.key(b"\033[B", 0.3)  # #831: PROJ-5, said last, is on top (#262)
        mark = len(v.out)
        v.key(b"\r", 0.05)
        check("card: #831 opens light with its definition and counts",
              v.wait(b"light", start=mark) and
              v.wait(b"Definition", start=mark) and
              v.wait(b"Mentions: this session ", start=mark), v.out[mark:])
        check("card: the definition title's SGR shows as ?",
              v.wait(b"Canary ?[31mred?[0m passage", start=mark),
              v.out[mark:])
        host_ran(v, n0["--xray"] + 4, "follow, task list, memory list, ref",
                 late)
        mark = len(v.out)
        v.key(b"f", 0.05)
        check("card: f runs the full scan on the host",
              v.wait(b"full scan", start=mark), v.out[mark:])
        # "full scan" is drawn at the key, before its query leaves (#374)
        host_ran(v, n0["--xray"] + 5, "f's ref --full", late)
        # #227: p is the scope toggle (o until 1.60.x); o opens, as in the list
        mark = len(v.out)
        v.key(b"p", 0.05)
        check("card: p scans other projects on the host",
              v.wait("full scan \u00b7 other projects".encode(), start=mark)
              and v.wait(b"p this project", start=mark), v.out[mark:])
        host_ran(v, n0["--xray"] + 6, "p's ref --full --scope all", late)
        mark = len(v.out)
        v.key(b"o", 0.5)
        check("card: o on the ended srv:xv-cl: said, the view stays",
              v.wait(b"xv-cl has ended: no session to open", start=mark) and
              v.reap() is None, v.out[mark:])
        v.key(b"\033", 0.5)
        v.key(b"\t\t")  # Refs, Search, then Prompts: Tab wraps (#291)
        mark = len(v.out)
        v.key(b"\r", 0.05)
        check("reader: Enter reads the newest chapter from the host",
              v.wait(b"You", start=mark) and v.wait(b"noted", start=mark),
              v.out[mark:])
        check("reader: the header says 2 of 2", v.wait(b"2 of 2", start=mark),
              v.out[mark:])
        host_ran(v, n0["--xray"] + 7, "chapter", late)
        mark = len(v.out)
        v.key(b"[", 0.05)
        check("reader: [ reads the previous chapter from the host, 1 of 2 "
              "(#288)", v.wait(b"1 of 2", start=mark) and
              v.wait(b"gh issue view 831", start=mark), v.out[mark:])
        host_ran(v, n0["--xray"] + 8, "['s chapter", late)
        mark = len(v.out)
        v.key(b"q")
        check("reader: q goes back to the list",
              v.wait(b"Prompts 2", start=mark), v.out[mark:])
        mark = len(v.out)
        v.key(b"o", 0.5)
        check("o on the ended srv:xv-cl: said, the view stays",
              v.wait(b"xv-cl has ended: no session to open", start=mark) and
              v.reap() is None, v.out[mark:])
        v.key(b"q")
        rc = v.exit()
        check("quit: q exits 0 and leaves the alternate screen",
              rc == 0 and RESTORE in v.out[-60:], (rc, v.out[-80:]))
        bad = [b for b in (b"\033]0;pwned", b"\007 evil", b"\xe2\x80\xae",
                           b"\033[31mred", b"\xd8\x9c", b"\xe2\x80\xa8")
               if b in v.out]
        check("hostile host text: no corpus ESC, BEL or bidi byte reached "
              "the terminal", not bad, bad)
    finally:
        v.close()
    # follow, ref (light), ref --full, p's ref --full --scope all, chapter,
    # ['s chapter, the task list (#320), the memory list (#323): eight,
    # each its own ctl
    until(lambda: ctls("--xray") - n0["--xray"] >= 8, 5)
    check("every query ran on the host as its own ctl --xray (8), no --read "
          "for an ended session", ctls("--xray") - n0["--xray"] == 8 and
          ctls("--read") == n0["--read"],
          (ctls("--xray") - n0["--xray"], ctls("--read") - n0["--read"],
           "not on the host in time: %s" % late, refused()))


def test_lookup():
    """#263: a ref typed in the filter opens its card on the host with ^R."""
    n0 = ctls("--xray")
    late = []
    v = Pty([DCH, "--xray", "srv:xv-cl"])
    try:
        v.wait(b"Prompts 2")
        v.pump(0.5)
        mark = len(v.out)
        v.key(b"/OQ-32", 1.0)
        text = shown(v, mark)
        check("lookup #263: a ref no row has: the empty line and the footer "
              "name ^R", "No match: ^R looks it up" in text and
              "^R look up" in text, text)
        mark = len(v.out)
        v.key(b"\x12", 0.05)
        check("lookup #263: ^R opens its card from the host",
              v.wait(b"OQ-32  light", start=mark) and
              v.wait(b"Mentions: this session ", start=mark), v.out[mark:])
        # that line is drawn at the key, before the query leaves (#374)
        host_ran(v, n0 + 4, "OQ-32's ref", late)
        v.key(b"\033", 0.5)
        v.key(b"/\x15", 0.3)
        mark = len(v.out)
        v.key(b"#831\x12", 1.0)
        check("lookup #263: #831, a chapter title's: ^R opens its card, "
              "not the chapter", v.wait(b"#831  light", start=mark) and
              v.wait(b"Definition", start=mark), v.out[mark:])
        host_ran(v, n0 + 5, "#831's ref", late)
        # its mentions come after the definition: Enter needs one
        until(lambda: v.pump(0.05) or re.search(
            rb"Mentions: this session \D*[1-9]", v.out[mark:]), 30)
        mark = len(v.out)
        v.key(b"\r", 1.0)
        rd = v.wait(b"You", start=mark)
        host_ran(v, n0 + 6, "chapter", late)
        mark = len(v.out)
        v.key(b"\033", 0.05)
        check("lookup #263: Enter on its mention reads the chapter from the "
              "host, Esc back to the card", rd and
              v.wait(b"#831  light", start=mark), v.out[mark:])
        v.key(b"\033", 0.5)
        v.key(b"q")
        v.exit()
    finally:
        v.close()
    until(lambda: ctls("--xray") - n0 >= 6, 5)
    check("lookup #263: the cards and the chapter ran on the host, each its "
          "own ctl --xray (follow, task list, memory list, 2 refs, chapter)",
          ctls("--xray") - n0 == 6,
          (ctls("--xray") - n0, "not on the host in time: %s" % late,
           refused()))


def test_small_card():
    """QA 1.56.0 MED-V1 over a host:name view: k, Up and PgUp in a
    too-small window took the card's selection below 0 and Enter read
    v->m[-1] (SIGSEGV)."""
    v = Pty([DCH, "--xray", "srv:xv-cl"])
    try:
        v.wait(b"Prompts 2")
        v.key(b"\033[Z\033[Z")
        v.wait(b"#831")
        mark = len(v.out)
        v.key(b"\r")
        check("small card: srv:xv-cl's #831 card opens with its mentions",
              v.wait(b"Mentions: this session ", start=mark), v.out[mark:])
        mark = len(v.out)
        v.resize(6, 20)
        small = v.wait(b"too small", start=mark)
        n0 = ctls("--xray")
        for k in (b"k", b"k", b"k", b"\033[A", b"\033[5~", b"\r"):
            v.key(k, 0.1)
        mark = len(v.out)
        v.resize(30, 100)
        v.wait(b"You", start=mark)
        check("small card: k, Up, PgUp then Enter on srv:xv-cl: no crash, "
              "Enter reads the first mention's chapter from the host",
              v.reap() is None and b"You" in v.out[mark:],
              (v.rc, small, ctls("--xray") - n0, refused(),
               v.out[mark:][-600:]))
        v.key(b"qqq")
        check("small card: q, q, q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()


def fixture_order():
    """srv:xv-o1: four turns ONE..FOUR, each saying PROJ-9, under 1 MiB;
    srv:xv-o2: the same with a second answer per turn and 1.2 MB after
    each, a window each; srv:xv-o3: one turn saying PROJ-9 in 250 lines,
    so light and full keep different lines (a file gives 200)."""
    sids = ["11111111-2222-4333-8444-5555555555%02x" % k
            for k in (0xb1, 0xb2, 0xb3)]
    for name, sid, big in (("xv-o1", sids[0], 0), ("xv-o2", sids[1], 1)):
        lines = []
        for k, w in enumerate(["ONE", "TWO", "THREE", "FOUR"]):
            ts = "2026-11-10T1%d:00:" % k
            lines += [{"type": "user", "cwd": "/home/demo/proj",
                       "timestamp": ts + "00Z",
                       "message": {"content": "prompt %s" % w}},
                      say_o("answer %s says PROJ-9" % w, ts + "05Z")]
            if big:
                lines += [say_o("answer %s2 says PROJ-9" % w, ts + "09Z")]
                lines += [say_o("pad " * 50000)] * 6     # 1.2 MB
        record_o(name, sid, lines)
    record_o("xv-o3", sids[2], [
        {"type": "user", "cwd": "/home/demo/proj",
         "timestamp": "2026-11-10T10:00:00Z",
         "message": {"content": "prompt PIN"}}] +
        [say_o("note %03d says PROJ-9" % k,
               "2026-11-10T10:%02d:%02dZ" % (k // 60, k % 60))
         for k in range(1, 251)])


def card_rows(v):
    """The card's mention words top down, and the selected one."""
    scr = screen_rows(v)
    got = [(scr[k].startswith("\u258c"),
            re.search(r"(?:answer|note) (\w+) says", scr[k]))
           for k in sorted(scr)]
    got = [(s, m.group(1)) for s, m in got if m]
    return [w for _, w in got], [w for s, w in got if s]


def order_card(name, secs=30.0):
    """srv:name's view, PROJ-9's card opened by ^R, settled."""
    v = Pty([DCH, "--xray", "srv:" + name], 40, 100)
    v.wait(b"Prompts ", secs)
    v.pump(0.5)
    v.key(b"/PROJ-9", 0.5)
    mark = len(v.out)
    v.key(b"\x12", 0.05)
    v.wait(b"PROJ-9  light", secs, start=mark)
    settle(v, secs)
    return v


def full_scan(v):
    mark = len(v.out)
    v.key(b"f", 0.05)
    v.wait(b"full scan", 30, start=mark)
    settle(v)


def test_order():
    v = order_card("xv-o1")
    try:
        got, _ = card_rows(v)
        check("card #304: srv:xv-o1, under 1 MiB: PROJ-9's mentions newest "
              "first", got == ["FOUR", "THREE", "TWO", "ONE"], got)
        mark = len(v.out)
        v.key(b"j\r", 1.0)
        check("card #304: Enter on the second mention reads its prompt "
              "from the host", v.wait(b"prompt THREE", start=mark) and
              b"prompt FOUR" not in v.out[mark:], v.out[mark:][-600:])
        v.key(b"q")
    finally:
        v.close()
    want = ["FOUR2", "FOUR", "THREE2", "THREE", "TWO2", "TWO", "ONE2", "ONE"]
    v = order_card("xv-o2")
    try:
        got, _ = card_rows(v)
        check("card #304: srv:xv-o2, several 1 MiB windows: newest first, "
              "no zigzag", got == want, got)
        v.key(b"jj", 0.5)
        full_scan(v)
        got, sel = card_rows(v)
        # f reads the project: this session's lines first, then the others'
        check("card #304: srv:xv-o2 after f (oldest first from the engine): "
              "still newest first, the selection on its mention",
              got[:8] == want and sel == ["THREE2"], (got, sel))
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("card #304: Enter on it reads its prompt from the host",
              v.wait(b"prompt THREE", start=mark) and
              b"prompt FOUR" not in v.out[mark:], v.out[mark:][-600:])
        v.key(b"q")
    finally:
        v.close()
    v = order_card("xv-o3")
    try:
        got, _ = card_rows(v)
        check("card #304: srv:xv-o3, 250 mentions in one window: newest "
              "first", len(got) > 20 and got == sorted(got, reverse=True),
              got)
        v.key(b"jjjjj", 0.5)
        _, before = card_rows(v)
        full_scan(v)
        got, sel = card_rows(v)
        check("card #304: srv:xv-o3, the selected mention stays selected "
              "while f's lines arrive, newest first", len(before) == 1 and
              sel == before and got == sorted(got, reverse=True),
              (before, sel, got))
        v.key(b"q")
    finally:
        v.close()


# ---- 2: Refs of a live shell on the host: its screen's refs (#294) ---------

def test_on_screen():
    o = json.loads(dch(SENV, "--xray", "xv-sh", "--json").stdout)
    lite = o["corpus"] == "none" and o["reason"] == "lite"
    n0 = ctls("--read")
    v = Pty([DCH, "--xray", "srv:xv-sh"])
    try:
        check("the view of srv:xv-sh opens",
              v.wait(b"X-ray  srv:xv-sh") and v.wait(b"Commands"),
              v.out[-600:])
        check("#294: no On screen tab", b"On screen" not in v.out,
              v.out[-600:])
        check("#295: a shell's first tab is Commands, never Prompts or "
              "Chapters", b"Prompts" not in v.out and
              b"Chapters" not in v.out, v.out[-600:])
        mark = len(v.out)
        v.key(b"\t", 2.0)
        if lite:
            check("lite: Refs has no group line (no mirror)",
                  v.wait(b"[Refs", start=mark) and
                  b"on screen now" not in v.out[mark:], v.out[mark:])
        else:
            ok = v.wait(b"on screen now", start=mark) and \
                v.wait(b"o/r#41", start=mark)
            v.pump(0.5)
            text = shown(v, 0)  # rows drawn last: the whole frame
            check("Refs #294: the host's screen's o/r#41 under the group "
                  "line, with its count, not repeated below",
                  ok and 0 <= text.find("on screen now") <
                  text.find("o/r#41") and text.count("o/r#41") == 1 and
                  re.search(r"\d +pull +o/r#41", text), text)
            mark = len(v.out)
            v.key(b"\r", 0.05)
            check("Refs #294: Enter opens the card of o/r#41",
                  v.wait(b"o/r#41  light", start=mark) and
                  v.wait(b"Mentions", start=mark), v.out[mark:])
        v.key(b"\003")
        check("srv:xv-sh: Ctrl-C quits with 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()
    with open(SLOG) as f:
        log = f.read()
    check("Refs #294: the screen read on the host with ctl --read xv-sh "
          "--ansi",
          ctls("--read") > n0 and " ctl --read xv-sh " in log,
          ctls("--read") - n0)


# ---- 6: o attaches to the host's session (#194) ----------------------------

def test_open():
    with open(SLOG) as f:
        n0 = f.read().count(" attach xv-sh (")
    CENV["DCH_DOUBLE_TAP_MS"] = "0"  # one Ctrl-\ detaches at once
    v = Pty([DCH, "--xray", "srv:xv-sh"])
    try:
        v.wait(b"Commands")
        v.pump(0.5)
        check("o: srv:xv-sh's footer offers it", v.wait(b"o open session"),
              v.out[-600:])
        mark = len(v.out)
        v.key(b"o", 0.3)
        attached = lambda: open(SLOG).read().count(" attach xv-sh (") > n0
        end = time.time() + 10
        while not attached() and time.time() < end:
            v.pump(0.1)
        check("o: the view left as q leaves it, then the host attaches "
              "xv-sh for us", attached() and RESTORE in v.out[mark:] and
              v.reap() is None, (v.out[mark:][-400:], open(SLOG).read()[-400:]))
        v.key(b"\x1c", 0.5)
        check("o: a real attach, the detach key ends it", v.exit(5) == 0,
              v.out[-200:])
    finally:
        del CENV["DCH_DOUBLE_TAP_MS"]
        v.close()


# ---- 3: a hostile host -------------------------------------------------------

HOSTILE_TITLE = "t\x1b]0;pwned\x07 a\u009bb c‮d e⁦f"
HOSTILE_SCREEN = (b"$ echo hi\x1b]0;pwned\x07\r\n"
                  b"see o/r#41 \x1b[31mred\x1b[0m\x1b[5Cgap\x07bel"
                  b" c1\xc2\x9b2J bidi\xe2\x80\xaex \x1b[2J\r\n"
                  b"sur[\xed\xa0\x80] f8[\xf8\x88\x80\x80] "
                  b"trunc[\xe4\xb8A] f4[\xf4\x90\x80\x80] nul[\x00]after\r\n")
# a later --read fails: the host's stderr, raw, is the line said
HOSTILE_ERR = b"err\x1b]0;pwned\x07 sur\xed\xa0\x80 tr\xe4\xb8\n"
BAD_UTF8 = (b"\xed\xa0\x80", b"\xf8\x88", b"\xe4\xb8", b"\xf4\x90", b"\x00")


class Hostile(Fake):
    """A host with ctl, ctl-cancel and xray: --follow answers a snapshot
    with raw control and bidi bytes and holds the stream; the first --read
    answers a screen with OSC, CSI, BEL, C1, bidi, invalid UTF-8 and NUL
    bytes, every later one fails (exit 1) with such a stderr; --ref fails
    (exit 2) as an older dch's grammar does; anything else ends 0."""

    def __init__(self):
        super().__init__("ok", ops=["ctl", "ctl-cancel", "xray"])
        self.reads = 0

    def serve(self, c):
        buf = b""
        try:
            while True:
                d = c.recv(65536)
                if not d:
                    return
                buf += d
                while len(buf) >= 5:
                    t, n = struct.unpack(">BI", buf[:5])
                    if len(buf) < 5 + n:
                        break
                    body, buf = buf[5:5 + n], buf[5 + n:]
                    if t == 0x08:
                        c.sendall(frame(0x18, {"proto": "1.0",
                                               "server": "fake",
                                               "ops": self.ops}))
                    elif t == 0x30:
                        argv = json.loads(body)["argv"]
                        self.bodies.append(argv)
                        if "--follow" in argv:
                            snap = ('{"schema":1,"snapshot":{"session":"fk",'
                                    '"session_id":"m0000000000000001",'
                                    '"corpus":"shell","live":true,'
                                    '"lines":3,"project_label":"/x",'
                                    '"chapters":[{"anchor":"s1","title":"'
                                    + HOSTILE_TITLE + '","kind":"shell",'
                                    '"at":1794300000,"offset":0}],'
                                    '"refs":[{"key":"pull:o/r#41",'
                                    '"shape":"pull","text":"o/r#41",'
                                    '"title":"' + HOSTILE_TITLE + '",'
                                    '"count":2,"underline":true}]}}\n')
                            c.sendall(frame(0x31, snap.encode()))
                            continue    # a stream: held until the close
                        if "--ref" in argv:
                            # an older dch whose grammar lacks the shape
                            c.sendall(frame(0x32, b"dch: not a ref\n") +
                                      frame(0x33, {"exit": 2}))
                            continue
                        if argv[0] == "--read":
                            self.reads += 1
                            if self.reads > 1:
                                c.sendall(frame(0x32, HOSTILE_ERR) +
                                          frame(0x33, {"exit": 1}))
                                continue
                            c.sendall(frame(0x31, HOSTILE_SCREEN) +
                                      frame(0x32, b"cursor 2 1 1 0\n"))
                        c.sendall(frame(0x33, {"exit": 0}))
        except OSError:
            return


def test_hostile():
    h = Hostile()
    hosts("q=127.0.0.1:%d" % h.port)
    v = Pty([DCH, "--xray", "q:fk"])
    try:
        check("hostile: the view opens on the snapshot",
              v.wait(b"X-ray  q:fk") and v.wait(b"Commands 1"),
              v.out[-600:])
        check("hostile: the snapshot's ESC, BEL, C1 and bidi bytes show as ?",
              v.wait(b"t?]0;pwned? a?b c?d e?f"), v.out[-600:])
        mark = len(v.out)
        v.key(b"\t", 0.05)
        check("hostile, an older host (no last_offset, #262): Refs lists "
              "its pull by count, the faint line claims no latest-first order",
              v.wait(b"o/r#41", start=mark) and
              v.wait(b"issues and tickets this session touched;", start=mark),
              v.out[mark:])
        check("hostile #294: its screen (OSC, CSI, BEL, C1, bidi, invalid "
              "UTF-8, NUL) still gives its o/r#41 to the group",
              v.wait(b"on screen now", start=mark), v.out[mark:])
        mark = len(v.out)
        v.key(b"/OQ-32\x12", 2.0)
        check("hostile, an older host whose dch takes no such ref (#263): ^R "
              "says its line, the view stays",
              v.wait(b"the host's dch takes no such ref (an older dch?): "
                     b"dch: not a ref", start=mark) and v.reap() is None,
              v.out[mark:])
        v.key(b"\033", 0.5)
        v.key(b"\033", 0.5)
        v.pump(2.0)  # the next reads fail
        text = shown(v, 0)
        check("hostile #294: a later failed read takes the group away and "
              "its stderr is never drawn", "on screen now" not in text and
              b"pwned? sur" not in v.out and b"No screen" not in v.out,
              text[-600:])
        v.key(b"q")
        rc = v.exit()
        check("hostile: q exits 0", rc == 0, (rc, v.out[-80:]))
        bad = [b for b in (b"\x1b]0;pwned", b"\x07 a", b"\x07bel",
                           b"\xc2\x9b", b"\xe2\x80\xae", b"\xe2\x81\xa6")
               + BAD_UTF8 if b in v.out]
        check("hostile: none of the host's control, bidi or invalid UTF-8 "
              "bytes reached the terminal", not bad, bad)
        check("hostile: the screen was asked as --read fk --ansi (no "
              "--cursor)", ["--read", "fk", "--ansi"] in h.bodies, h.bodies)
    finally:
        v.close()
        h.close()


def test_responses():
    n0 = ctls("--xray")
    v = Pty([DCH, "--xray", "srv:xv-cl"])
    try:
        v.wait(b"Prompts 2")
        v.pump(0.5)
        mark = len(v.out)
        v.key(b"\t", 0.05)
        ok = v.wait(b"Responses 2", start=mark)
        it = items(v)
        check("Responses, remote: the host's answers, newest first: its "
              "text; a turn that ended on its tool result its tool line",
              ok and len(it) == 2 and
              it[0] == ("second prompt about PROJ-5", ["noted"]) and
              it[1][1] == ["Bash 1"], (it, v.out[mark:][-600:]))
        mark = len(v.out)
        v.key(b"\r", 0.05)
        check("Responses, remote: Enter reads the item's prompt from the "
              "host, 2 of 2, its Reply", v.wait(b"2 of 2", start=mark) and
              v.wait(b"Reply", start=mark) and v.wait(b"noted", start=mark),
              v.out[mark:][-600:])
        mark = len(v.out)
        v.key(b"q", 0.05)
        check("Responses, remote: q back to the item",
              v.wait(b"Responses 2", start=mark) and
              "second prompt about PROJ-5" in head(v), head(v))
        v.key(b"q")
        check("Responses, remote: q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()
    until(lambda: ctls("--xray") - n0 >= 4, 5)
    check("Responses, remote: the follow, the task and memory lists and the "
          "chapter, each its own ctl --xray on the host",
          ctls("--xray") - n0 == 4,
          ctls("--xray") - n0)


def test_search():
    n0 = ctls("--xray")
    late = []
    v = Pty([DCH, "--xray", "srv:xv-cl"])
    try:
        v.wait(b"Prompts 2")
        v.pump(0.5)
        mark = len(v.out)
        v.key(b"\033[Z", 0.5)   # Shift-Tab: Prompts back to Search
        v.key(b"PROJ-5\r", 1.0)
        ok = tabbar(v, "Search 2")
        v.pump(0.3)
        h = shits(v)
        check("Search, remote: the host's hits, newest first: the prompt as "
              "You, the tool result under the first prompt by its tool",
              ok and [(w, t[:12]) for w, t, _ in h] ==
              [("You", "second promp"), ("Bash", "look at #831")],
              (h, v.out[mark:][-600:]))
        check("Search, remote: a hostile title under a hit shows with ?",
              h and len(h) > 1 and "?]0;pwned?" in h[1][1], h)
        host_ran(v, n0 + 4, "follow, task list, memory list, search", late)
        mark = len(v.out)
        v.key(b"j\r", 0.05)
        check("Search, remote: Enter reads the hit's prompt from the host, "
              "1 of 2, at the text", v.wait(b"1 of 2", start=mark) and
              v.wait(b"PROJ-5", start=mark), v.out[mark:][-600:])
        host_ran(v, n0 + 5, "chapter", late)
        mark = len(v.out)
        v.key(b"q", 1.0)
        check("Search, remote: q back to the hit",
              tabbar(v, "Search 2") and "Bash" in head(v), head(v))
        v.key(b"q")
        check("Search, remote: q exits 0", v.exit() == 0, v.out[-80:])
        bad = [b for b in (b"\033]0;pwned", b"\007 evil", b"\xe2\x80\xae")
               if b in v.out]
        check("Search, remote: no host ESC, BEL or bidi byte reached the "
              "terminal", not bad, bad)
    finally:
        v.close()
    until(lambda: ctls("--xray") - n0 >= 5, 5)
    check("Search, remote: the follow, the task and memory lists, the search "
          "and the chapter, each its own ctl --xray on the host",
          ctls("--xray") - n0 == 5,
          (ctls("--xray") - n0, "not on the host in time: %s" % late))


# ---- 5: the picker's x on a remote row --------------------------------------

def test_picker(port):
    hosts("srv=127.0.0.1:%d" % port)
    v = Pty([DCH, "-l"], cols=140)
    try:
        check("picker: the remote row srv:xv-sh is listed",
              v.wait(b"xv-sh", 15), v.out[-600:])
        v.pump(1.0)
        mark = len(v.out)
        v.key(b"x", 1.0)
        check("picker: x on the remote row opens its view",
              v.wait(b"X-ray  srv:xv-sh", start=mark) and
              v.wait(b"Commands", start=mark), v.out[mark:][-600:])
        mark = len(v.out)
        v.key(b"q", 1.0)
        check("picker: q in the view returns to the picker",
              v.reap() is None and v.wait(b"Sessions", start=mark),
              v.out[mark:][-600:])
        n0 = ctls("--xray")
        mark = len(v.out)
        v.key(b"X", 1.0)
        check("picker: X on the remote row opens no view and kills nothing "
              "(OQ-6)", v.wait(b"Enter attaches and x opens its X-ray",
                                start=mark)
              and b"X-ray  srv:xv-sh" not in v.out[mark:] and
              ctls("--xray") == n0, v.out[mark:][-600:])
        # History: the host's record srv:xv-cl, its only row
        for _ in range(3):
            mark = len(v.out)
            v.key(b"\t", 0.05)
            if v.wait(b"[History", 1, start=mark):
                break
        check("picker: History lists the host's record srv:xv-cl",
              v.wait(b"xv-cl", 15, start=mark), v.out[mark:][-600:])
        v.pump(0.5)
        mark = len(v.out)
        v.key(b"x", 0.05)
        check("picker: x on the remote History row opens its record's view",
              v.wait(b"X-ray  srv:xv-cl", start=mark) and
              v.wait(b"Prompts 2", start=mark), v.out[mark:][-600:])
        v.key(b"q", 1.0)
        v.key(b"\033")
        check("picker: Esc quits it", v.exit() is not None, v.out[-80:])
    finally:
        v.close()


def host_attaches():
    with open(SLOG) as f:
        return f.read().count(" attach xv-sh (")


def same_attach(what, v, keys, remote):
    """keys on the picker or view v attach a srv:xv-sh: the host's when
    remote (the host logs an attach), else the local one (its LOCAL-MARK
    drawn, the host asked nothing); the detach key ends it, exit 0."""
    n0 = host_attaches()
    if keys == b"o":
        # the view drops o until its first answer: wait for the footer
        # that offers it, past the view's newest title (#249)
        check("same name: %s: the view offers o" % what,
              v.wait(b"o open session",
                     start=max(0, v.out.rfind(b"X-ray  srv:xv-sh"))),
              (refused(), v.out[-300:]))
    mark = len(v.out)
    v.key(keys, 0.3)
    if remote:
        ok = until(lambda: host_attaches() > n0)
        v.pump(0.5)
    else:
        ok = v.wait(b"LOCAL-MARK", start=mark) and \
            (v.pump(1.5) or host_attaches() == n0)
    check("same name: %s attaches the %s srv:xv-sh" %
          (what, "host's" if remote else "local"), ok and v.reap() is None,
          (host_attaches() - n0, v.rc, v.out[mark:][-300:]))
    v.key(b"\x1c", 0.5)
    check("same name: %s: a real attach, the detach key ends it" % what,
          v.exit(5) == 0, v.out[-200:])


def row_view(v, remote):
    """In the picker v, x on the srv:xv-sh row that is remote (or not):
    the view is left open on it. False when neither row is."""
    for keys in (b"kkk", b"jjj"):
        n0 = ctls("--xray")
        mark = len(v.out)
        v.key(keys + b"x", 0.05)
        # its first answer: a remote view's ctl is logged by then
        v.wait(b"o open session", start=mark)
        if (ctls("--xray") > n0) == remote:
            return True
        mark = len(v.out)
        v.key(b"q", 0)
        v.wait(b"[Sessions 2]", start=mark)     # the picker is back
    return False


def test_same_name():
    """QA 1.57.0 LOW-R2: a local session literally named srv:xv-sh next to
    the host's xv-sh; x on each row opens that row's session (only the
    remote one asks the host). #210: Enter on the remote row, o in the
    remote view (dch --xray srv:xv-sh opened before the local one came,
    the picker's x) attach the host's; o in the local row's view the
    local one."""
    # its own client dirs, the same remote_hosts: the History record the
    # local srv:xv-sh leaves would route srv:xv-sh here for the next tests
    lenv = side("same", DCH_CONFIG=CENV["DCH_CONFIG"],
                DCH_REMOTE_BOX_MS="5000", DCH_DOUBLE_TAP_MS="0")
    try:
        v = Pty([DCH, "--xray", "srv:xv-sh"], env=lenv)
        try:
            ok = v.wait(b"Commands")
            r = dch(dict(lenv, DCH_NO_REMOTE="1"), "--spawn", "srv:xv-sh",
                    "--size", "80x24", "sh", "-c",
                    "while :; do echo LOCAL-MARK; sleep 1; done")
            check("same name: a local session named srv:xv-sh spawned "
                  "under the view of the host's", ok and r.returncode == 0,
                  r.stderr)
            v.pump(0.5)
            same_attach("o in dch --xray srv:xv-sh's view", v, b"o", True)
        finally:
            v.close()
        v = Pty([DCH, "-l"], cols=140, env=lenv)
        try:
            # the host's row comes with its probe: both rows listed
            check("same name: the picker lists both srv:xv-sh rows",
                  v.wait(b"2 sessions", 15), (refused(), v.out[-300:]))
            asked = []
            # the top row, then the bottom one: the two srv:xv-sh rows
            for keys in (b"kkk", b"jjj"):
                n0 = ctls("--xray")
                mark = len(v.out)
                v.key(keys + b"x", 0.05)
                ok = v.wait(b"X-ray  srv:xv-sh", start=mark) and \
                    v.wait(b"o open session", start=mark)
                asked.append((ok, ctls("--xray") - n0, keys))
                mark = len(v.out)
                v.key(b"q", 0)
                v.wait(b"[Sessions 2]", start=mark)     # the picker is back
            check("same name: x on the remote row opens the host's "
                  "session, x on the local row the local one",
                  all(a[0] for a in asked) and
                  sorted(a[1] > 0 for a in asked) == [False, True], asked)
            check("same name: the remote row found for Enter",
                  row_view(v, True))
            mark = len(v.out)
            v.key(b"q", 0)
            v.wait(b"[Sessions 2]", start=mark)     # the picker is back
            same_attach("Enter on the remote row", v, b"\r", True)
        finally:
            v.close()
        for what, remote in (("o in the remote row's view", True),
                             ("o in the local row's view", False)):
            v = Pty([DCH, "-l"], cols=140, env=lenv)
            try:
                v.wait(b"2 sessions", 15)
                check("same name: x on the %s row opens its view" %
                      ("remote" if remote else "local"), row_view(v, remote))
                same_attach(what, v, b"o", remote)
            finally:
                v.close()
    finally:
        dch(dict(lenv, DCH_NO_REMOTE="1"), "-k", "srv:xv-sh")


def main(port):
    test_view()
    test_lookup()
    test_responses()
    test_search()
    test_small_card()
    fixture_order()
    test_order()
    test_on_screen()
    test_picker(port)
    test_same_name()
    test_open()
    test_hostile()


if __name__ == "__main__":
    sys.exit(run(main, sh=True))
