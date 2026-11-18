#!/usr/bin/env python3
"""The `dch --xray <n>` view, narrow Reply rows (#483, #488, #494, #496, #504):
the cut of a mention row at a narrow width over CJK, combining marks and
emoji runs.

The harness, the fixtures and the list of what the parts cover
are in xray_view_lib.py.

Run:  python3 tests/xray_view_narrow_test.py      (uses ./dch)
"""
from xray_view_lib import *  # noqa: F401,F403 (harness, fixtures, T)


NSIDS = ["11111111-2222-4333-8444-5555555555d%s" % k
         for k in ("6", "7", "4", "3", "5", "a", "b", "c")]


def fixture_narrow():
    """xv-nw: one reply with 99 words before #412, alone in its project."""
    solo = "/home/demo/narrow"
    words = " ".join("w%03d" % k for k in range(1, 100))
    os.makedirs(os.path.dirname(tl("x", "-home-demo-narrow")), exist_ok=True)
    record("xv-nw", NSIDS[0], [dict(o, cwd=solo) if "cwd" in o else o
                               for o in turn("go", "2026-11-10T13:00:00Z",
                                  at(say(words + " so #412 holds."),
                                     "2026-11-10T13:01:00Z"))],
           "-home-demo-narrow", solo)
    # CJK with one space before the ref: snapping to it would drop the
    # whole context, so the scalar cut stays (#483's half-room rule)
    solo = "/home/demo/narrowcjk"
    os.makedirs(os.path.dirname(tl("x", "-home-demo-narrowcjk")),
                exist_ok=True)
    record("xv-nwc", NSIDS[1], [dict(o, cwd=solo) if "cwd" in o else o
                                for o in turn("go", "2026-11-10T13:00:00Z",
                                   at(say("\u6f22\u5b57" * 60 + " so #412 holds."),
                                      "2026-11-10T13:01:00Z"))],
           "-home-demo-narrowcjk", solo)
    # decomposed e + U+0301 runs: the row's cut lands on a mark (#488)
    solo = "/home/demo/narrowmark"
    os.makedirs(os.path.dirname(tl("x", "-home-demo-narrowmark")),
                exist_ok=True)
    record("xv-nwm", NSIDS[2], [dict(o, cwd=solo) if "cwd" in o else o
                                for o in turn("go", "2026-11-10T13:00:00Z",
                                   at(say("e\u0301" * 60 + " so #412 holds."),
                                      "2026-11-10T13:01:00Z"))],
           "-home-demo-narrowmark", solo)
    # runs of U+2764 U+FE0F and keycaps: two cells each (#496)
    solo = "/home/demo/narrowemoji"
    os.makedirs(os.path.dirname(tl("x", "-home-demo-narrowemoji")),
                exist_ok=True)
    record("xv-nwe", NSIDS[3], [dict(o, cwd=solo) if "cwd" in o else o
                                for o in turn("go", "2026-11-10T13:00:00Z",
                                   at(say("\u2764\ufe0f" * 30 + "1\ufe0f\u20e3" * 30 +
                                          " so #412 holds."),
                                      "2026-11-10T13:01:00Z"))],
           "-home-demo-narrowemoji", solo)
    # a mark right after the spaces at the cut (#494)
    solo = "/home/demo/narrowspmark"
    os.makedirs(os.path.dirname(tl("x", "-home-demo-narrowspmark")),
                exist_ok=True)
    record("xv-nws", NSIDS[4], [dict(o, cwd=solo) if "cwd" in o else o
                                for o in turn("go", "2026-11-10T13:00:00Z",
                                   at(say("ab \u0301 " * 60 + " so #412 holds."),
                                      "2026-11-10T13:01:00Z"))],
           "-home-demo-narrowspmark", solo)
    # two spaces before the marks (#504 UN1); mark-led word chains after
    # one long word, two letters and one (#504 UN2)
    for n, i, tx in (("xv-nw2s", 5, "c  \u0323\u0301" * 60),
                     ("xv-nwch", 6, "q" * 200 + " \u0301aa \u0301bb "
                                    "\u0301cc \u0301dd"),
                     ("xv-nwch1", 7, "q" * 200 + "".join(
                         " \u0301" + c for c in "abcdefg"))):
        solo = "/home/demo/" + n
        os.makedirs(os.path.dirname(tl("x", "-home-demo-" + n)), exist_ok=True)
        record(n, NSIDS[i], [dict(o, cwd=solo) if "cwd" in o else o
                             for o in turn("go", "2026-11-10T13:00:00Z",
                                at(say(tx + " so #412 holds."),
                                   "2026-11-10T13:01:00Z"))],
               "-home-demo-" + n, solo)


def test_narrow_2space():
    """#504 UN1: a cut between two spaces leads with neither a space nor a mark."""
    bad = []
    for cols, (row, text) in zip((40, 41, 42),
                                 narrow_rows("xv-nw2s", (40, 41, 42))):
        m = re.search(ELL + "(.)", row)
        if not (m and m.group(1) == "c" and "so #412" in row):
            bad.append((cols, row))
    check("#504 UN1: over two spaces then marks the narrow row starts on a "
          "letter at 40-42 columns and #412 shows", not bad, bad)


def test_narrow_chain():
    """#504 UN2: the word snap runs once, so mark-led words before the ref stay."""
    bad = []
    for name, want in (("xv-nwch", "\u0301cc \u0301dd so #412"),
                       ("xv-nwch1", "\u0301e \u0301f \u0301g so #412")):
        for cols, (row, text) in zip((40, 50, 60),
                                     narrow_rows(name, (40, 50, 60))):
            m = re.search(ELL + "(.)", row)
            if not (m and m.group(1) not in " \u0301" and want in row):
                bad.append((name, cols, row))
    check("#504 UN2: a chain of mark-led words before the ref keeps its last "
          "words at 40, 50 and 60 columns, no space or mark leading", not bad,
          bad)


def test_narrow_cjk():
    """NIT-UH2 with #483's rule: a CJK row keeps its context."""
    row, text = narrow_row("xv-nwc")
    check("NIT-UH2: a CJK sentence's narrow row keeps CJK before the ref "
          "(no snap to the one space)", re.search(ELL + "\u6f22|" + ELL +
          "\u5b57", row) is not None and "so #412" in row, text)


def test_narrow_mark():
    """#488: a narrow row's cut never leaves a lone combining mark."""
    row, text = narrow_row("xv-nwm")
    m = re.search(ELL + "(.)", row)
    check("#488: the narrow row's first scalar after the ellipsis is not a "
          "combining mark and #412 shows", m is not None and
          not ("\u0300" <= m.group(1) <= "\u036f") and "so #412" in row,
          text)



def emoji_cells(s):
    """Cells as terminals draw: VS16 or U+20E3 widens a one-cell base."""
    n, one = 0, False
    for c in s:
        w = (0 if unicodedata.category(c) in ("Mn", "Me", "Cf") else
             2 if unicodedata.east_asian_width(c) in "WF" else 1)
        if w == 0 and c in "\ufe0f\u20e3" and one:
            w, one = 1, False
        elif w:
            one = w == 1
        n += w
    return n


def test_narrow_emoji():
    """#496: VS16 emoji and keycaps take two cells; no row passes the width."""
    for cols, (row, text) in zip((40, 50), narrow_rows("xv-nwe", (40, 50))):
        wide = [l for l in text.split("\n") if emoji_cells(l) > cols]
        check("#496: at %d columns no row of an emoji run is wider than the "
              "window and #412 shows" % cols, not wide and "so #412" in row,
              (wide, text))


def test_narrow_spmark():
    """#494: nor a mark that follows the spaces at the cut."""
    row, text = narrow_row("xv-nws")
    m = re.search(ELL + "(.)", row)
    check("#494: the narrow row's first scalar after the ellipsis is not a "
          "combining mark after the spaces and #412 shows", m is not None and
          not ("\u0300" <= m.group(1) <= "\u036f") and "so #412" in row,
          text)


def test_narrow():
    """NIT-UH2: a mention row cut to 50 columns starts on a whole word."""
    row, text = narrow_row("xv-nw")
    check("NIT-UH2: the narrow row starts on a whole word after the "
          "ellipsis and keeps #412", re.search(ELL + r"w\d\d\d ", row)
          is not None and "so #412" in row, text)


def main():
    run_steps(fixture_narrow, test_narrow, test_narrow_cjk, test_narrow_mark,
              test_narrow_emoji, test_narrow_spmark, test_narrow_2space,
              test_narrow_chain)


if __name__ == "__main__":
    main()
