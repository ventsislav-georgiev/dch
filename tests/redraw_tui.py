#!/usr/bin/env python3
# Tiny inner "TUI" for the redraw harness: prints HELLO once, then prints a
# REPAINT marker every time it receives SIGWINCH. Stands in for Claude Code's
# "repaint on SIGWINCH" behavior so the harness can assert the signal arrives.
import signal, sys, time

def on_winch(*_):
    sys.stdout.write("REPAINT\r\n")
    sys.stdout.flush()

signal.signal(signal.SIGWINCH, on_winch)
sys.stdout.write("HELLO\r\n")
sys.stdout.flush()
while True:
    time.sleep(0.2)
