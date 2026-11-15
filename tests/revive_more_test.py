"""Session history and `dch --revive` (#042), second half: see revive_lib.py for what
the two files cover between them (split for run time, the pool runs both).

Run:  python3 tests/revive_more_test.py        (uses ./dch)
"""
import sys
from revive_lib import main

if __name__ == "__main__":
    sys.exit(main("b"))
