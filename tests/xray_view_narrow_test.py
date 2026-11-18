#!/usr/bin/env python3
"""The `dch --xray <n>` view, narrow Reply rows (#483, #488, #494, #496):
the cut of a mention row at a narrow width over CJK, combining marks and
emoji runs.

The harness, the fixtures and the list of what the parts cover
are in xray_view_lib.py.

Run:  python3 tests/xray_view_narrow_test.py      (uses ./dch)
"""
from xray_view_lib import *  # noqa: F401,F403 (harness, fixtures, T)


NSIDS = ["11111111-2222-4333-8444-5555555555d%d" % k for k in (6, 7, 4, 3, 5)]


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
    for cols in (40, 50):
        row, text = narrow_row("xv-nwe", cols)
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
              test_narrow_emoji, test_narrow_spmark)


if __name__ == "__main__":
    main()
