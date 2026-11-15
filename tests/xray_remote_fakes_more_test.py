#!/usr/bin/env python3
"""The X-ray view of a remote session against fake hosts, second part: 10,
stopping a remote child, of xray_remote_fakes_test.py (split for run time;
that file runs the rest and lists what both cover).

Run:  python3 tests/xray_remote_fakes_more_test.py      (uses ./dch)
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from xray_remote_fakes_test import *  # noqa: E402,F401,F403

if __name__ == "__main__":
    sys.exit(run(lambda port: main(port, "b")))
