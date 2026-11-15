"""Native-agent bridge regression, second half: see agent_bridge_lib.py (split
for run time; agent_bridge_test.py holds every other row).
Run with DCH=/path/to/dch python3 this-file."""
import sys
from agent_bridge_lib import main

if __name__ == "__main__":
    sys.exit(main("b"))
