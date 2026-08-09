import os, select, signal, sys, termios, time

fd = sys.stdin.fileno()
attrs = termios.tcgetattr(fd)
attrs[3] &= ~(termios.ECHO | termios.ICANON)   # lflags
attrs[6][termios.VMIN] = 1
attrs[6][termios.VTIME] = 0
termios.tcsetattr(fd, termios.TCSANOW, attrs)

# os.write, not sys.stdout: a buffered write re-entered from a signal handler
# is a hard RuntimeError on newer CPython ("reentrant call inside
# BufferedWriter"), which is exactly what a WINCH mid-print does.
signal.signal(signal.SIGWINCH, lambda *_: os.write(1, b"WINCH\r\n"))

while True:
    os.write(1, b"HELLO\r\n")
    r, _, _ = select.select([fd], [], [], 0.2)
    if fd in r:
        data = os.read(fd, 256)
        if not data:
            break
        os.write(1, b"KEY:%s\r\n" % data.hex().encode())
