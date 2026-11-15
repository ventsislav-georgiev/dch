"""Remote client, second half: the #180 robustness checks of
remote_client_test.py (split for run time; that file holds the helpers).

Run:  python3 tests/remote_client_more_test.py        (uses ./dch)
"""
import sys
from remote_client_test import main

if __name__ == "__main__":
    sys.exit(main("b"))
