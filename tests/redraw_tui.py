import os, signal, time

# os.write, not sys.stdout: a buffered write re-entered from a signal handler
# is a hard RuntimeError on newer CPython ("reentrant call inside
# BufferedWriter"), which is exactly what a WINCH mid-print does.
def on_winch(*_):
    os.write(1, b"REPAINT\r\n")

signal.signal(signal.SIGWINCH, on_winch)
while True:
    os.write(1, b"HELLO\r\n")
    time.sleep(0.2)
