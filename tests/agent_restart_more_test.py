"""Agent-aware `dch --restart --include-agents`, second half: see agent_restart_lib.py
for what the two files cover between them (split for run time).

Run:  python3 tests/agent_restart_more_test.py        (uses ./dch)
"""
import sys
from agent_restart_lib import main

if __name__ == "__main__":
    sys.exit(main("b"))
