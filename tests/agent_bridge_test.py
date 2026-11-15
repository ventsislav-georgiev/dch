"""Native-agent bridge regression, first half: see agent_bridge_lib.py (split
for run time; agent_bridge_more_test.py holds the 30 s hung-queue rows).
Run with DCH=/path/to/dch python3 this-file."""
import sys
from agent_bridge_lib import main

if __name__ == "__main__":
    sys.exit(main("a"))
