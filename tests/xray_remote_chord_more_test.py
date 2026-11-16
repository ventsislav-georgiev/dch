#!/usr/bin/env python3
"""The X-ray view of a remote session from the in-attach chord over
`dch -a host:name`, second half: 7 on of xray_remote_chord_test.py (split
for run time; that file runs 6, the chord itself, and lists what both cover).

Run:  python3 tests/xray_remote_chord_more_test.py      (uses ./dch)
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from xray_remote_chord_test import *  # noqa: E402,F401,F403

if __name__ == "__main__":
    sys.exit(run(lambda port: main(port, "b"), sh=True))
