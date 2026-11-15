"""`dch --stop`, first half: see stop_lib.py for what the two files cover
between them (split for run time).

Run:  python3 tests/stop_test.py        (uses ./dch)
"""
import sys
from stop_lib import main

if __name__ == "__main__":
    sys.exit(main("a"))
