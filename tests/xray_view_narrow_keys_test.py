#!/usr/bin/env python3
"""The `dch --xray <n>` view, narrow Reply rows over keycaps and flags (#495):
the cut at 40-60 columns never splits a keycap or re-pairs a flag.

The harness, the fixtures and the list of what the parts cover
are in xray_view_lib.py.

Run:  python3 tests/xray_view_narrow_keys_test.py      (uses ./dch)
"""
from xray_view_lib import *  # noqa: F401,F403 (harness, fixtures, T)


NSIDS = ["11111111-2222-4333-8444-5555555555d%d" % k for k in (8, 9)]


def fixture_narrow_keys():
    """xv-nwk, xv-nwf: keycap and flag runs before #412, each alone in its project."""
    # keycaps and flags (#495)
    for n, i, unit in (("xv-nwk", 0, "1\ufe0f\u20e3"),
                       ("xv-nwf", 1, "\U0001f1e7\U0001f1ec")):
        solo = "/home/demo/" + n
        os.makedirs(os.path.dirname(tl("x", "-home-demo-" + n)), exist_ok=True)
        record(n, NSIDS[i], [dict(o, cwd=solo) if "cwd" in o else o
                             for o in turn("go", "2026-11-10T13:00:00Z",
                                at(say(unit * 60 + " so #412 holds."),
                                   "2026-11-10T13:01:00Z"))],
               "-home-demo-" + n, solo)


def test_narrow_keyflag():
    """#495: no lone keycap mark, no flag re-paired by the cut."""
    bad = []
    for name, first in (("xv-nwk", "1"), ("xv-nwf", "\U0001f1e7")):
        for cols in (40, 47, 54, 60):
            row, text = narrow_row(name, cols)
            m = re.search(ELL + "(.)", row)
            if not (m and m.group(1) == first and "so #412" in row):
                bad.append((name, cols, m and m.group(1)))
    check("#495: the narrow row starts on the keycap's digit / the flag's "
          "first indicator at 40-60 columns and #412 shows", not bad, bad)


def main():
    run_steps(fixture_narrow_keys, test_narrow_keyflag)


if __name__ == "__main__":
    main()
