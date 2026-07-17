#!/usr/bin/env python3
# Inner "TUI" for the attach-redraw harness. Puts its tty in raw no-echo mode
# (ECHO|ICANON off, VMIN=1) — the exact state in which dch's master would type
# a ^L for a CTRL_L-method redraw — then reports everything it receives:
#   KEY:<hex>  for every stdin byte (there must be NONE during an attach)
#   WINCH      for every SIGWINCH   (there must be one — the redraw)
import os, select, signal, sys, termios, time

fd = sys.stdin.fileno()
attrs = termios.tcgetattr(fd)
attrs[3] &= ~(termios.ECHO | termios.ICANON)   # lflags
attrs[6][termios.VMIN] = 1
attrs[6][termios.VTIME] = 0
termios.tcsetattr(fd, termios.TCSANOW, attrs)

signal.signal(signal.SIGWINCH, lambda *_: (sys.stdout.write("WINCH\r\n"), sys.stdout.flush()))

# HELLO repeats: output emitted before a slow client finishes attaching is
# lost (attach redraw is WINCH-based, no replay), so a one-shot marker races.
while True:
    sys.stdout.write("HELLO\r\n")
    sys.stdout.flush()
    r, _, _ = select.select([fd], [], [], 0.2)
    if fd in r:
        data = os.read(fd, 256)
        if not data:
            break
        sys.stdout.write("KEY:%s\r\n" % data.hex())
        sys.stdout.flush()
