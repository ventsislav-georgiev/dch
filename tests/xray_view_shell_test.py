#!/usr/bin/env python3
"""The `dch --xray <n>` view, part 4 of 4 (#365): resize
and the small card, a shell session, signals, On screen, the screen
group, Search on a shell, the picker's x, o, the chord, held o.

The harness, the fixtures and the list of what the four parts cover
are in xray_view_lib.py.

Run:  python3 tests/xray_view_shell_test.py      (uses ./dch)
"""
from xray_view_lib import *  # noqa: F401,F403 (harness, fixtures, T)


def test_resize():
    v = Pty([DCH, "--xray", "xv-cl"])
    try:
        v.wait(b"Prompts 2")
        mark = len(v.out)
        v.resize(6, 40)
        check("resize: a narrow window says it is too small",
              v.wait(b"the window is too small", start=mark), v.out[mark:])
        mark = len(v.out)
        v.resize(30, 100)
        check("resize: back to 100x30 redraws the list",
              v.wait(b"Prompts 2", start=mark), v.out[mark:])
        v.key(b"\003")
        check("Ctrl-C quits with 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()


def test_small_card():
    """QA 1.56.0 MED-V1: k, Up and PgUp in a too-small window took the
    card's selection below 0 and Enter read v->m[-1] (SIGSEGV)."""
    v = Pty([DCH, "--xray", "xv-cl"])
    try:
        v.wait(b"Prompts 2")
        v.key(b"\033[Z\033[Z")
        v.wait(b"#831")
        mark = len(v.out)
        v.key(b"\r")
        check("small card: #831's card opens with its mentions",
              v.wait(b"Mentions: this session ", start=mark), v.out[mark:])
        mark = len(v.out)
        v.resize(6, 20)
        check("small card: 20 columns says it whole (NIT-V3)",
              v.wait(b"X-ray: too small", start=mark) and
              b"the window is" not in v.out[mark:], v.out[mark:])
        for k in (b"k", b"k", b"k", b"\033[A", b"\033[5~", b"\r"):
            v.key(k, 0.1)
        mark = len(v.out)
        v.resize(30, 100)
        v.wait(b"You", start=mark)
        check("small card: k, Up, PgUp then Enter: no crash, Enter reads "
              "the first mention's chapter", v.reap() is None and
              b"You" in v.out[mark:],
              (v.rc, v.out[mark:][-600:]))
        if v.reap() is not None:
            return
        for cols, want in ((12, b"too small"), (5, b"too")):
            mark = len(v.out)
            v.resize(6, cols)
            v.wait(want, start=mark)
            got = [r for r in rows(v.out[mark:]).values() if r.strip()]
            check("too small at %d columns: one row, within the width"
                  % cols, len(got) == 1 and cells(got[0]) <= cols and
                  want.decode() in got[0], got)
        v.key(b"qqq")
        check("small card: q, q, q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()


def test_signals():
    for sig in (signal.SIGTERM, signal.SIGINT):
        name = signal.Signals(sig).name
        v = Pty([DCH, "--xray", "xv-sh"])
        try:
            v.wait(b"Commands")
            v.pump(0.5)
            ks = kids(v.pid)
            os.kill(v.pid, sig)
            rc = v.exit()
            check("%s: the view exits and restores the terminal" % name,
                  rc is not None and RESTORE in v.out[-60:],
                  (rc, v.out[-80:]))
            check("%s: the view's follow child is gone" % name,
                  ks and until(lambda: not any(alive(k) for k in ks), 5),
                  ks)
        finally:
            v.close()


def test_on_screen():
    """#294: Refs on the live xv-sh: the refs on its screen first."""
    rc = subprocess.run([DCH, "--xray", "xv-sh", "--json"], env=ENV,
                        capture_output=True, text=True, timeout=30)
    o = json.loads(rc.stdout)
    lite = o["corpus"] == "none" and o["reason"] == "lite"
    v = Pty([DCH, "--xray", "xv-sh"])
    try:
        v.wait(b"Commands")
        if lite:
            check("lite: the label says X-ray needs the full dch build",
                  v.wait(b"X-ray needs the full dch build"), v.out[-600:])
            mark = len(v.out)
            v.key(b"\t", 1.5)
            check("lite: Refs has no group line (no mirror)",
                  v.wait(b"[Refs", start=mark) and
                  b"on screen now" not in v.out[mark:], v.out[mark:])
        else:
            u = [x for x in o["refs"] if x["key"] == "pull:o/r#41"]
            check("shell: o/r#41 is underlined in the index (count 2)",
                  u and u[0]["underline"], o["refs"])
            u = [x for x in o["refs"] if x["key"] == "pull:o/r#77"]
            check("shell: o/r#77, printed once, is not underlined",
                  u and not u[0]["underline"], o["refs"])
            mark = len(v.out)
            v.key(b"\t", 1.5)
            ok = v.wait(b"on screen now", start=mark)
            v.pump(0.5)
            text = screen(v, mark)
            g = text.find("on screen now")
            check("Refs #294: the group line, then o/r#77 (the lowest row) "
                  "and o/r#41, with their counts",
                  ok and 0 <= g < text.find("o/r#77") < text.find("o/r#41")
                  and re.search(r"\d pull o/r#41", text), text)
            check("Refs #294: a ref in the group is not repeated below, "
                  "and nothing else is held: no rest line",
                  text.count("o/r#41") == 1 and text.count("o/r#77") == 1
                  and "the rest" not in text, text)
            mark = len(v.out)
            v.key(b"\r", 1.0)
            check("Refs #294: Enter on the first row opens o/r#77's card",
                  v.wait(b"o/r#77  light", start=mark) and
                  v.wait(b"Mentions", start=mark), v.out[mark:])
        v.key(b"\003")
        check("on screen: Ctrl-C quits with 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()


def test_screen_group(lite):
    """#294: Refs' on-screen group on a live shell whose master keeps no
    ring (xray_ring = 0): the index holds nothing, the screen is read all
    the same."""
    cfg = os.path.join(T, "config-noring")
    with open(cfg, "w") as f:
        f.write("xray_ring = 0\n")
    r = subprocess.run([DCH, "--spawn", "xv-0", "--size", "80x24", "sh", "-c",
                        'echo READY0; while read -r x; do '
                        'echo "see $x https://example.com/p"; done'],
                       capture_output=True, text=True, timeout=30,
                       env=dict(ENV, DCH_CONFIG=cfg))
    check("screen group: spawned", r.returncode == 0, r.stderr)
    time.sleep(0.5)
    v = Pty([DCH, "--xray", "xv-0"])
    try:
        v.wait(b"Commands")
        v.pump(0.5)
        check("#294: no On screen tab", b"Refs 0" in v.out and
              b"On screen" not in v.out, v.out[-600:])
        check("#290: a shell has no Responses tab, never drawn",
              b"Responses" not in v.out, v.out[-600:])
        mark = len(v.out)
        v.key(b"\t", 1.5)  # over 1 s: the screen read at least once
        check("Refs: no refs, the empty state says what would show",
              v.wait(b"No refs yet: the files, commits, pull requests, "
                     b"issues and URLs this session touches show here",
                     start=mark), v.out[mark:])
        check("Refs #294: no ref on the screen (READY0), no group line",
              b"on screen now" not in v.out[mark:], v.out[mark:])
        mark = len(v.out)
        dch("--send", "xv-0", "o/r#5\n")
        if lite:
            v.pump(2.5)
            check("lite: no mirror, never a group line",
                  b"on screen now" not in v.out[mark:], v.out[mark:])
        else:
            ok = v.wait(b"on screen now", start=mark) and \
                v.wait(b"o/r#5", start=mark)
            v.pump(0.5)
            text = screen(v, 0)  # rows drawn last: the whole frame
            check("Refs #294: o/r#5 on the screen, the index holding "
                  "nothing, shows under the group line", ok, text)
            check("Refs #294: its row has no count (not the index's)",
                  re.search(r"(?m)^\W*pull o/r#5\b", text) and
                  "Refs 0" in text, text)
            check("Refs #294: the URL on the screen is hidden by the a "
                  "filter, the footer counting it",
                  "example.com" not in text and
                  "a files, commits, URLs 1 hidden" in text, text)
            mark = len(v.out)
            v.key(b"a", 1.0)
            text = screen(v, 0)
            check("Refs #294: a shows the URL in the group, after o/r#5 "
                  "on its row", 0 <= text.find("o/r#5") <
                  text.find("example.com/p"), text)
            v.key(b"a", 0.5)
            mark = len(v.out)
            v.key(b"\r", 1.0)
            check("Refs #294: Enter on the group's row opens its card",
                  v.wait(b"o/r#5  light", start=mark), v.out[mark:])
            # the rows change under the selection each second (#294)
            v.key(b"\033", 0.5)
            mark = len(v.out)
            dch("--send", "xv-0", "o/r#6\n")
            v.wait(b"o/r#6", start=mark)
            v.pump(1.5)
            mark = len(v.out)
            v.key(b"\r", 1.0)
            check("Refs #294: on the first row the selection stays there: "
                  "o/r#6, new on the screen, is under it",
                  v.wait(b"o/r#6  light", start=mark), v.out[mark:])
            v.key(b"\033", 0.5)
            v.key(b"j", 0.5)  # o/r#5, the second row
            mark = len(v.out)
            dch("--send", "xv-0", "o/r#7\n")
            v.wait(b"o/r#7", start=mark)
            v.pump(1.5)
            text = screen(v, 0)
            check("Refs #294: o/r#7 comes on the screen, first in the group",
                  0 <= text.find("o/r#7") < text.find("o/r#6") <
                  text.find("o/r#5"), text)
            mark = len(v.out)
            v.key(b"\r", 1.0)
            check("Refs #294: the selection stays on its ref (o/r#5) when "
                  "a new one comes above it; Enter opens o/r#5's card",
                  v.wait(b"o/r#5  light", start=mark), v.out[mark:])
        v.key(b"\003")
        check("screen group: Ctrl-C quits with 0", v.exit() == 0,
              v.out[-80:])
    finally:
        v.close()
        dch("-k", "xv-0")  # the picker test's first row is xv-sh


def test_search_shell(lite):
    """#291: a shell's Search searches its ring (Commands, Refs, Search);
    dch-lite has no ring: the list's one line says no corpus."""
    v = Pty([DCH, "--xray", "xv-sh"], rows=30, cols=100)
    try:
        v.wait(b"Commands")
        v.pump(0.5)
        tabs = []
        for _ in range(3):
            v.key(b"\t", 0.8)
            tabs.append(rows(v.out).get(2, ""))
        check("Search, shell: Tab cycles Commands, Refs, Search, Commands; "
              "no Responses", re.search(r"\[Refs", tabs[0]) and
              re.search(r"\[Search", tabs[1]) and
              re.search(r"\[Commands", tabs[2]) and
              all("Responses" not in t and "Tasks" not in t for t in tabs),
              tabs)
        mark = len(v.out)
        v.key(b"\033[Z", 0.5)
        v.key(b"see o/r\r", 1.0)
        if lite:
            check("Search, shell on dch-lite: no corpus, said as the list's "
                  "line", v.wait(b"dch: no x-ray corpus", start=mark),
                  v.out[mark:][-600:])
        else:
            ok = v.wait(b"Search ", start=mark) and until(
                lambda: (v.pump(0.1), hits(rows(v.out)))[1], 5)
            h = hits(rows(v.out))
            # the engine answers a ring's lines as "tool" with no chapter:
            # the command line and its output alike
            check("Search, shell: its ring searched, the command line and "
                  "its output both found, each as Output",
                  ok and len(h) == 4 and all(w == "Output" for w, _, _ in h)
                  and any(s == ["$ echo 'see o/r#41'"] for _, _, s in h)
                  and any(s == ["see o/r#41"] for _, _, s in h), h)
        v.key(b"q")
        check("Search, shell: q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()


def test_picker():
    v = Pty([DCH, "-l"])
    try:
        check("picker: the x hint is shown",
              v.wait(b"x-ray"), v.out[-600:])
        mark = len(v.out)
        v.key(b"x", 1.0)
        check("picker: x opens the view of the row's session",
              v.wait(b"X-ray  xv-sh", start=mark) and
              v.wait(b"Commands", start=mark), v.out[mark:])
        mark = len(v.out)
        v.key(b"q", 1.0)
        check("picker: q in the view returns to the picker",
              v.reap() is None and v.wait(b"Sessions", start=mark),
              v.out[mark:])
        mark = len(v.out)
        v.key(b"X", 1.0)
        check("picker: X asks to kill and never opens the view (OQ-6)",
              v.wait(b"Kill xv-sh? [y/N]", start=mark) and
              b"X-ray  xv-sh" not in v.out[mark:], v.out[mark:])
        mark = len(v.out)
        v.key(b"n", 1.0)
        check("picker: n keeps the session",
              v.wait(b"cancelled", start=mark) and clients("xv-sh") == 0,
              v.out[mark:])
        v.key(b"\033")
        check("picker: Esc quits it", v.exit() is not None, v.out[-80:])
    finally:
        v.close()


def test_open(lite):
    # one typed shell block, scrolled into the ring: a chapter (full build)
    prog = ("printf '\\033]133;A\\007$ \\033]133;B\\007'; read cmd; "
            "printf '\\033]133;C\\007'; echo \"ran:$cmd\"; "
            "printf '\\033]133;D;0\\007'; seq 1 40; "
            "printf '\\033]133;A\\007$ '; exec sleep 600")
    r = dch("--spawn", "xv-op", "--size", "80x24", "sh", "-c", prog)
    check("open: spawned", r.returncode == 0, r.stderr)
    # dch-lite: --read exits 3, nothing to wait for
    until(lambda: (lambda r: r.returncode == 3 or "$" in r.stdout)(
        dch("--read", "xv-op")), 5)
    dch("--send", "xv-op", "make the build o/r#41\n")  # a ref: the card's o
    until(lambda: (lambda r: r.returncode == 3 or "40" in r.stdout)(
        dch("--read", "xv-op")), 5)
    time.sleep(0.5)  # dch-lite: no --read; the block's time
    v = Pty([DCH, "--xray", "xv-op"], env=DTAP)
    try:
        ok = ready(v)
        text = screen(v, 0)
        check("Commands: a live session's footer offers o", ok, text)
        check("#295: a shell session's first tab is Commands",
              "Commands" in text and "Prompts" not in text and
              "Chapters" not in text, text)
        mark = len(v.out)
        v.key(b"o", 0.3)
        opened(v, mark, "Commands", "xv-op")
    finally:
        v.close()
    if lite:
        print("  ok   lite: a shell has no chapters to read (no corpus): o "
              "in the reader and on a card is the full build's")
    else:
        v = Pty([DCH, "--xray", "xv-op"], env=DTAP)
        try:
            v.wait(b"make the build")
            v.pump(0.3)
            mark = len(v.out)
            v.key(b"\r", 1.0)
            v.wait(b"ran:make the build", start=mark)
            v.pump(0.3)
            text = screen(v, mark)
            check("#295: a shell's reader footer says ] next command",
                  "] next command" in text and "chapter" not in text, text)
            check("reader: a live session's footer offers o",
                  "o open session" in text and "ran:make the build" in text,
                  text)
            mark = len(v.out)
            v.key(b"G", 0.3)
            v.key(b"j", 0.5)
            text = screen(v, mark)
            check("#295: past its only command's end the status says the "
                  "end of the last command", "end of the last command" in
                  text, text)
            mark = len(v.out)
            v.key(b"o", 0.3)
            opened(v, mark, "reader", "xv-op")
        finally:
            v.close()
        v = Pty([DCH, "--xray", "xv-op"], env=DTAP)
        try:
            ready(v)
            v.key(b"\t", 0.3)
            v.wait(b"o/r#41")
            mark = len(v.out)
            v.key(b"\r", 1.0)
            ok = until(lambda: (v.pump(0.1), "o open session" in
                                screen(v, mark) and "p other projects" in
                                screen(v, mark))[1], 10)
            check("card: a live session's footer offers o and p", ok,
                  screen(v, mark))
            mark = len(v.out)
            v.key(b"o", 0.3)
            opened(v, mark, "card", "xv-op")
        finally:
            v.close()
    dch("-k", "xv-op")  # the pickers below take their first row: xv-sh
    v = Pty([DCH, "-l"], env=DTAP)
    try:
        v.wait(b"x-ray")
        v.key(b"x", 1.0)
        v.wait(b"X-ray  xv-sh")
        ready(v)
        mark = len(v.out)
        v.key(b"o", 0.3)
        opened(v, mark, "picker's x")
    finally:
        v.close()
    v = Pty([DCH, "-k"], env=DTAP)
    try:
        v.wait(b"x-ray")
        v.key(b"x", 1.0)
        v.wait(b"X-ray  xv-sh")
        ready(v)
        mark = len(v.out)
        v.key(b"o", 1.0, full=True)
        check("-k picker: the view's o attaches nothing and kills nothing, "
              "said", v.wait(b"o opens a session: from dch -l or the switch",
                             start=mark) and clients("xv-sh") == 0 and
              v.reap() is None, v.out[mark:])
        v.key(b"\033")
        v.exit()
    finally:
        v.close()
    check("-k picker: xv-sh still runs", clients("xv-sh") == 0, clients("xv-sh"))
    v = Pty([DCH, "--xray", "xv-sh"], env=dict(DTAP, DCH_SESSION="outer"))
    try:
        ready(v)
        mark = len(v.out)
        v.key(b"o", 1.0, full=True)
        check("o inside a dch session: refused as -a refuses it, the view "
              "stays", v.wait(b"already inside session outer; nesting "
                              b"disabled", start=mark) and
              v.reap() is None and clients("xv-sh") == 0, v.out[mark:])
        v.key(b"q")
        v.exit()
    finally:
        v.close()


def test_chord():
    o = json.loads(dch("--xray", "xv-sh", "--json").stdout)
    lite = o["corpus"] == "none" and o["reason"] == "lite"
    r = dch("--spawn", "xv-rx", "--size", "100x30", "python3", "-c", RX,
            RXF)
    check("chord: the probe session spawned", r.returncode == 0, r.stderr)
    # the probe opens RXF after tty.setraw, which flushes input: a --send
    # before it is lost (seen under load, #365)
    until(lambda: os.path.exists(RXF), 10)
    chord_case("x in a later read", [b"\x1c", b"x"], lite)
    chord_case("x in the same burst", [b"\x1cx"], lite)
    chord_case("the kitty CSI form", [b"\x1c", b"\033[120u"], lite)
    chord_case("all kitty, one burst, the release between",
               [b"\033[92;5u\033[92;5:3u\033[120;1:1u"], lite)
    if lite:
        print("  ok   lite: the repaint after the view is the master's "
              "SIGWINCH (no mirror), the screen check is the full build's")

    # o on Prompts (#194): the client is on the session already; o goes
    # back to it, the attach kept; what came in the o's write (a second
    # o, Enter, a held o) is the view's, dropped (LOW-V12)
    for burst in (b"o", b"oo", b"o\r", b"o" * 7):
        a = attach()
        try:
            got = rx()
            a.key(b"\x1cx", 0.5, full=True)
            a.wait(b"X-ray  xv-rx")
            mark = len(a.out)
            a.key(b"\t\t", 0.5, full=True)  # Refs, Search, then Commands (#291)
            ready(a, mark)
            mark = len(a.out)
            a.key(burst, 0.5, full=True)
            check("chord, %r: the view closes, back to the session" % burst,
                  a.wait(RESTORE, start=mark) and a.reap() is None,
                  a.out[mark:][-300:])
            a.key(b"z", 0.3, full=True)
            check("chord, %r: the attach was kept, neither Tab nor the "
                  "burst reached the session, a key after does" % burst,
                  until(lambda: rx()[len(got):] == "7a", 5) and
                  a.reap() is None, (rx()[len(got):], a.rc))
        finally:
            a.close()

    # dch --xray's o (LOW-V12): the burst's rest never reaches the attach
    # it opens; a key typed once the attach is up does
    for burst in (b"oo", b"o\r", b"o" * 7):
        v = Pty([DCH, "--xray", "xv-rx"], env=DTAP)
        try:
            got = rx()
            ready(v)
            v.key(burst, 0.3, full=True)
            ok = until(lambda: (v.pump(0.1), clients("xv-rx") == 1)[1], 10)
            v.pump(0.5)
            v.key(b"z", 0.3, full=True)
            check("dch --xray, %r: o attaches, the rest of the burst never "
                  "reaches the session, a key after does" % burst,
                  ok and until(lambda: rx()[len(got):] == "7a", 5),
                  (ok, rx()[len(got):]))
            v.key(b"\x1c", 0.5, full=True)
            v.exit(5)
        finally:
            v.close()

    # a held o (MED-V20): o's in reads of their own, still coming after the
    # view's exit, dropped until a 100 ms pause, then by the attach (#331);
    # a z 300 ms on reaches it
    def o_attaches(v, gaps):
        got = rx()
        gap = o_held(v, gaps)
        ok = until(lambda: (v.pump(0.1), clients("xv-rx") == 1)[1], 10)
        held = ok and until(lambda: rx()[len(got):] == "7a", 5)
        detail = (ok, rx()[len(got):])
        v.key(b"\x1c", 0.5, full=True)
        v.exit(5)
        return held, detail, gap

    def xv(gaps, search=False):
        v = Pty([DCH, "--xray", "xv-rx"], env=DTAP)
        try:
            ready(v)
            if search:
                v.key(b"\033[Z", 0.5, full=True)  # Search, typing
                v.key(b"\033", 0.5, full=True)    # typing ended, the Search list
            return o_attaches(v, gaps)
        finally:
            v.close()
    for what, gaps in HELD:
        held_check("dch --xray, %s: o attaches, no o reaches the session, a "
                   "z 300 ms after the last does (MED-V20)" % what,
                   lambda: xv(gaps))
    eight = HELD[-1][1]

    def chord_held():
        a = attach()
        try:
            got = rx()
            a.key(b"\x1cx", 0.5, full=True)
            a.wait(b"X-ray  xv-rx")
            mark = len(a.out)
            a.key(b"\t\t", 0.5, full=True)  # Refs, Search, then Commands
            ready(a, mark)
            gap = o_held(a, eight)
            until(lambda: rx()[len(got):] != "", 5)
            a.pump(0.3)
            return (rx()[len(got):] == "7a" and a.reap() is None,
                    (rx()[len(got):], a.rc), gap)
        finally:
            a.close()
    held_check("chord, 8 o's 33 ms apart: back to the session, no o reached "
               "it, a z 300 ms after the last does (MED-V20)", chord_held)
    held_check("dch --xray, Search, 8 o's 33 ms apart: no o reaches the "
               "session, a z 300 ms after the last does (MED-V20)",
               lambda: xv(eight, True))

    def picker_held():
        v = picker_rx()
        try:
            return o_attaches(v, eight)
        finally:
            v.close()
    held_check("picker's x, 8 o's 33 ms apart: no o reaches the session, a z "
               "300 ms after the last does (MED-V20)", picker_held)

    # -E: no detach key, no chord; the session gets both bytes
    a = attach(extra=["-E"])
    try:
        got = rx()
        mark = len(a.out)
        a.key(b"\x1c", 0.05, full=True)
        a.key(b"x", 0.5, full=True)
        until(lambda: rx()[len(got):] == "1c78", 5)
        check("-E: the session receives the detach key and x, no view",
              rx()[len(got):] == "1c78" and b"X-ray" not in a.out[mark:]
              and a.reap() is None, (rx()[len(got):], a.out[mark:]))
    finally:
        a.close()

    # DCH_DOUBLE_TAP_MS=0: the press detaches at once, x after it or in
    # its burst never reaches the session, no view
    for keys in ([b"\x1c", b"x"], [b"\x1cx"]):
        a = attach(env={"DCH_DOUBLE_TAP_MS": "0"})
        try:
            got = rx()
            mark = len(a.out)
            a.key(keys[0], 0.05, full=True)
            rc = a.exit(5)
            if len(keys) > 1:
                try:
                    os.write(a.fd, keys[1])
                except OSError:
                    pass
            time.sleep(0.5)
            check("DCH_DOUBLE_TAP_MS=0, %s: the detach key detaches at once "
                  "(exit 0), the session receives nothing, no view" %
                  ("x after" if len(keys) > 1 else "one burst"),
                  rc == 0 and rx()[len(got):] == "" and
                  b"X-ray" not in a.out[mark:],
                  (rc, rx()[len(got):], a.out[mark:]))
        finally:
            a.close()

    # detach_key = switch: one press opens the picker after the window; a
    # late x there is the picker's view (OQ-6), q comes back to it, X is
    # kill, which asks first; n keeps the session
    cfg = os.path.join(T, "config-switch")
    with open(cfg, "w") as f:
        f.write("detach_key = switch\n")
    a = attach(env={"DCH_CONFIG": cfg})
    try:
        got = rx()
        a.key(b"\x1c", 0.05, full=True)
        check("detach_key = switch: one press opens the picker",
              a.wait(b"Sessions"), a.out[-600:])
        a.pump(0.5)
        mark = len(a.out)
        a.key(b"x", 0.5, full=True)
        check("detach_key = switch: a late x opens the picker's view",
              a.wait(b"X-ray  xv-rx", start=mark), a.out[mark:][-600:])
        mark = len(a.out)
        a.key(b"q", 0.5, full=True)
        check("detach_key = switch: q in that view returns to the picker",
              a.wait(b"Sessions", start=mark), a.out[mark:][-600:])
        mark = len(a.out)
        a.key(b"X", 0.5, full=True)
        check("detach_key = switch: X reaches the kill confirmation",
              a.wait(b"Kill xv-rx? [y/N]", start=mark), a.out[mark:])
        a.key(b"n", 0.5, full=True)
        a.key(b"\033", 0.5, full=True)
        a.exit(5)
        late = rx()[len(got):]
        dch("--send", "xv-rx", "alive")
        check("detach_key = switch: the late x never ran -k (the session "
              "lives, x never reached it)", late == "" and
              until(lambda: rx()[len(got):] == "616c697665", 5),
              (late, rx()[len(got):]))
    finally:
        a.close()


def test_held_o():
    """#331: the attach a view's o opens drops the leading o's that follow
    within 700 ms of its start or of the last one dropped (a held o's
    repeats start 225 ms or more after the press, past the view's 100 ms
    pause); the first other byte, or an o after a longer gap, goes on."""
    cases = (
        ("dch --xray", xv_rx, "a held o's repeats", REPEAT, "7a"),
        ("dch --xray", xv_rx, "x 300 ms later", [(0.3, b"x")], "78"),
        ("dch --xray", xv_rx, "oox in one write 300 ms later",
         [(0.3, b"oox")], "78"),
        ("dch --xray", xv_rx, "one o 1 s after the attach", None, "6f"),
        ("picker's x", picker_rx, "a held o's repeats", REPEAT, "7a"),
        ("picker's x", picker_rx, "x 300 ms later", [(0.3, b"x")], "78"),
        ("chord", chord_rx, "a held o's repeats", REPEAT, "7a"),
        ("chord", chord_rx, "x 300 ms later", [(0.3, b"x")], "78"),
    )
    for where, start, what, steps, want in cases:
        v = start()
        try:
            got = rx()
            if steps is None:
                o_then(v, [])
                until(lambda: (v.pump(0.1), clients("xv-rx") == 1)[1], 10)
                v.pump(1.0)
                os.write(v.fd, b"o")
            else:
                o_then(v, steps)
            ok = until(lambda: (v.pump(0.1), clients("xv-rx") == 1)[1], 10)
            until(lambda: (v.pump(0.1), rx()[len(got):] != "")[1], 5)
            v.pump(0.3)
            check("%s, o then %s: the session gets %s only (#331)"
                  % (where, what, want), ok and rx()[len(got):] == want and
                  v.reap() is None, (ok, rx()[len(got):], v.rc))
        finally:
            v.close()
    a = Pty([DCH, "-a", "xv-rx"])
    try:
        attached(a)
        got = rx()
        time.sleep(0.1)
        os.write(a.fd, b"o")
        until(lambda: rx()[len(got):] != "", 5)
        check("dch -a, no view: an o 100 ms after attaching reaches the "
              "session (#331)", rx()[len(got):] == "6f", rx()[len(got):])
    finally:
        a.close()


def test_chord_end():
    """The session ends while the view is up: leaving the view ends the
    attach as a session's end does (exit 0, no frame to a closed master).
    `dch -k` while the view is up: the client ends and takes the view with
    it, no orphan left on the terminal (the client is not the pty's session
    leader here, so no hangup does it for us)."""
    r = dch("--spawn", "xv-end", "--size", "100x30", "sh", "-c",
            "echo READY; read x; echo BYE")
    check("chord, end: spawned", r.returncode == 0, r.stderr)
    a = Pty([DCH, "-a", "xv-end"])
    try:
        attached(a)
        mark = len(a.out)
        a.key(b"\x1cx", 0.5, full=True)
        check("chord, end: the view opens", a.wait(b"X-ray  xv-end",
              start=mark), a.out[mark:][-300:])
        dch("--send", "xv-end", "go\n")
        a.pump(1.0)
        check("chord, end: the client waits for the view",
              a.reap() is None, a.out[-200:])
        a.key(b"q", 0.3, full=True)
        rc = a.exit(5)
        check("chord, end: the session ended under the view; leaving it "
              "exits 0", rc == 0, (rc, a.out[-200:]))
    finally:
        a.close()
    r = dch("--spawn", "xv-end", "--size", "100x30", "sh", "-c",
            "echo READY; exec sleep 600")
    a = Pty(["/bin/sh", "-c", "'%s' -a xv-end; echo CLIENT-GONE; "
             "exec sleep 30" % DCH])
    try:
        attached(a)
        mark = len(a.out)
        a.key(b"\x1cx", 0.5, full=True)
        a.wait(b"X-ray  xv-end", start=mark)
        client = kids(a.pid)
        view = [k for c in client for k in kids(c)]
        dch("-k", "xv-end")
        check("chord, -k under the view: the client ends",
              a.wait(b"CLIENT-GONE", start=mark), a.out[mark:][-300:])
        check("chord, -k under the view: the view went with it",
              view and until(lambda: not any(alive(k) for k in view), 5),
              view)
    finally:
        a.close()


def test_chord_detach():
    """#196. dch -d (and SIGTERM, SIGHUP) with the chord's view up: the
    client leaves at once as a plain detach does (exit 0, the terminal's
    stty as before the attach, the reset sequence last, the client pidfile
    gone, the view gone). With kitty event types on, the detach key's
    release in a read of its own neither ends the window nor reaches the
    session: press, release, x opens the view; press, release, press opens
    the switch picker. What follows the x in its read reaches neither the
    view nor the session (a q there leaves the view up)."""
    import re
    reset = b"\033[?1049l\033[<8u"
    for how in ("-d", signal.SIGTERM, signal.SIGHUP):
        what = how if how == "-d" else signal.Signals(how).name
        a = Pty(["/bin/sh", "-c", "stty -g; '%s' -a xv-rx; echo RC=$?; "
                 "stty -g; echo GONE; exec sleep 30" % DCH])
        try:
            attached(a)
            a.pump(0.3)
            before = a.out.split(b"\r\n")[0]
            got = rx()
            mark = len(a.out)
            a.key(b"\x1c", 0.05, full=True)
            a.key(b"x", 0.3, full=True)
            ok = a.wait(b"X-ray  xv-rx", start=mark)
            check("chord, %s: the view opens" % what, ok,
                  a.out[mark:][-300:])
            client = kids(a.pid)
            view = [k for c in client for k in kids(c)]
            pidf = [os.path.join(d, f) for d, _, fs in os.walk(T)
                    for f in fs for c in client
                    if f.endswith(".client.%d" % c)]
            mark = len(a.out)
            t0 = time.time()
            if how == "-d":
                r = dch("-d", "xv-rx")
                check("chord, -d: dch -d says it detached one client",
                      r.returncode == 0 and "detached 1 client" in r.stdout,
                      (r.returncode, r.stdout, r.stderr))
            else:
                for c in client:
                    os.kill(c, how)
            gone = a.wait(b"GONE", secs=5, start=mark)
            tail = a.out[mark:]
            check("chord, %s: the client leaves within 5 s, the view still "
                  "up (%.1f s)" % (what, time.time() - t0), gone,
                  tail[-300:])
            if not gone:
                continue
            lines = tail.split(b"RC=")[-1].split(b"\r\n")
            # PENDIN (macOS 0x20000000): the kernel sets it on any switch
            # back to canonical mode, until the next read
            pend = lambda g: re.sub(
                rb"lflag=([0-9a-f]+)", lambda m: b"lflag=%x" %
                (int(m.group(1), 16) & ~0x20000000), g)
            check("chord, %s: exit 0, the terminal's stty as before the "
                  "attach" % what, lines[0] == b"0" and
                  pend(lines[1]) == pend(before), (lines[:2], before))
            if how != signal.SIGHUP:
                check("chord, %s: the reset sequence comes last" % what,
                      tail.split(b"RC=")[0].endswith(reset),
                      tail.split(b"RC=")[0][-120:])
            check("chord, %s: the client pidfile and the view are gone" %
                  what, pidf and not any(os.path.exists(f) for f in pidf)
                  and view and until(lambda: not any(alive(k)
                                                    for k in view), 5),
                  (pidf, view))
            check("chord, %s: the session got nothing" % what,
                  rx()[len(got):] == "", rx()[len(got):])
        finally:
            a.close()

    # kitty event types on: each report in a read of its own
    for keys, want, what in (
            ([b"\033[92;5u", b"\033[92;5:3u", b"\033[120u"],
             b"X-ray  xv-rx", "press, release, x"),
            ([b"\033[92;5u", b"\033[92;5:3u", b"\033[92;5u"],
             b"Sessions", "press, release, press")):
        a = attach(env={"DCH_DOUBLE_TAP_MS": "2000"})
        try:
            got = rx()
            mark = len(a.out)
            for k in keys:
                try:
                    a.key(k, 0.15, full=True)
                except OSError:	# the client left: the pty is gone
                    break
            check("kitty events, %s, three reads: %s opens, the client "
                  "stays" % (what, want.decode()),
                  a.wait(want, start=mark) and a.reap() is None,
                  (a.rc, a.out[mark:][-300:]))
            check("kitty events, %s: the session got nothing" % what,
                  rx()[len(got):] == "", rx()[len(got):])
        finally:
            a.close()

    # the rest of the x's read is dropped: a q in it never closes the view
    a = attach(env={"DCH_DOUBLE_TAP_MS": "2000"})
    try:
        got = rx()
        mark = len(a.out)
        a.key(b"\x1c", 0.05, full=True)
        a.key(b"xq", 0.3, full=True)
        ok = a.wait(b"X-ray  xv-rx", start=mark)
        a.pump(1.0)
        # the overlay's own reset leaves the alternate screen first
        up = a.out.find(b"X-ray  xv-rx", mark)
        check("chord, x then q in one read: the view opens and stays up, "
              "the session got nothing", ok and
              RESTORE not in a.out[up:] and a.reap() is None and
              rx()[len(got):] == "", (a.out[mark:][-200:], rx()[len(got):]))
        mark = len(a.out)
        a.key(b"q", 0.5, full=True)
        check("chord, x then q in one read: a q of its own closes it",
              a.wait(RESTORE, start=mark), a.out[mark:][-200:])
    finally:
        a.close()


def main():
    def rest():
        o = json.loads(dch("--xray", "xv-sh", "--json").stdout)
        lite = o["corpus"] == "none" and o["reason"] == "lite"
        test_screen_group(lite)
        test_search_shell(lite)
        test_picker()
        test_open(lite)
        test_chord()
        test_held_o()
        test_chord_end()
        test_chord_detach()
    run_steps(fixture, test_resize, test_small_card, shell, test_signals,
              test_on_screen, rest)


if __name__ == "__main__":
    main()
