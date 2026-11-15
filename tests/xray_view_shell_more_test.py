#!/usr/bin/env python3
"""The `dch --xray <n>` view, part 4b: the chord and held o of
xray_view_shell_test.py (split for run time; the harness, the fixtures and
the list of what the parts cover are in xray_view_lib.py).

Run:  python3 tests/xray_view_shell_more_test.py      (uses ./dch)
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from xray_view_shell_test import *  # noqa: E402,F401,F403

if __name__ == "__main__":
    main("b")
