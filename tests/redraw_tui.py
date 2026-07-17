#!/usr/bin/env python3
# Tiny inner "TUI" for the redraw harness: prints HELLO periodically, plus a
# REPAINT marker every time it receives SIGWINCH. Stands in for Claude Code's
# "repaint on SIGWINCH" behavior so the harness can assert the signal arrives.
# HELLO repeats because output emitted before a slow client finishes attaching
# is lost (attach redraw is WINCH-based, no replay) — a one-shot marker races.
import signal, sys, time

def on_winch(*_):
    sys.stdout.write("REPAINT\r\n")
    sys.stdout.flush()

signal.signal(signal.SIGWINCH, on_winch)
while True:
    sys.stdout.write("HELLO\r\n")
    sys.stdout.flush()
    time.sleep(0.2)
