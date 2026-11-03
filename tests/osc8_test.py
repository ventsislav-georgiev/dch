#!/usr/bin/env python3
"""OSC 8 hyperlinks survive every screen dch rebuilds from its mirror.

The VT formatter emits per-cell links but carries an open one across a row
break, so a line taken on its own lost it: `--read --ansi --recent N` cut the
open of a wrapped link off, leaving the kept row unlinked with a stray close.
Every line of an ANSI snapshot must now open and close its own links; the
plain format carries none.

Run:  python3 tests/osc8_test.py            (uses ./dch)
      DCH=/path/to/dch python3 tests/osc8_test.py
"""
import os, re, sys, pty, signal, select, time, errno, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))
SESS = "osc8test-%d" % os.getpid()

A, B, C = "https://a.example/one", "https://b.example/two", "https://c.example/wrap"
# Two links on one line (the second with an id= parameter and a colour),
# plain text between, then a 30-cell link that wraps on a 20-column screen.
CHILD = (
    "import sys,time;w=sys.stdout.write;"
    "w('pre \\x1b]8;;%s\\x07LINKONE\\x1b]8;;\\x07 mid "
    "\\x1b[31m\\x1b]8;id=x;%s\\x07RED2\\x1b]8;;\\x07\\x1b[0m end\\n');"
    "w('\\x1b]8;;%s\\x07' + 'W'*30 + '\\x1b]8;;\\x07 tail\\n');"
    "w('plain line\\n');sys.stdout.flush();time.sleep(60)" % (A, B, C))

OSC8 = re.compile(rb"\x1b\]8;([^;\x07\x1b]*);([^\x07\x1b]*)(?:\x07|\x1b\\)")


def read(*args):
    return subprocess.run([DCH, "--read", SESS] + list(args),
                          capture_output=True, timeout=15).stdout


def link(uri, text, params=b""):
    """Regex: the link opened right before `text` and closed after it (SGR
    may sit between the text and the close)."""
    return re.compile(re.escape(b"\x1b]8;" + params + b";" + uri.encode() +
                                b"\x1b\\" + text) +
                      rb"(?:\x1b\[[0-9;]*m)*" + re.escape(b"\x1b]8;;\x1b\\"))


def unbalanced(blob):
    """Lines that end with a link still open, or close one never opened."""
    bad = []
    for line in blob.split(b"\n"):
        cur = None
        for m in OSC8.finditer(line):
            if m.group(2):
                cur = m.group(2)
            elif cur is None:
                bad.append(line)
                break
            else:
                cur = None
        else:
            if cur is not None:
                bad.append(line)
    return bad


def check(name, blob, fails):
    want = [link(A, b"LINKONE"), link(B, b"RED2", b"id=x"),
            link(C, b"W" * 20), link(C, b"W" * 10)]
    for w in want:
        if not w.search(blob):
            fails.append("%s: missing %r in %r" % (name, w.pattern, blob))
    for line in unbalanced(blob):
        fails.append("%s: unbalanced OSC 8 on line %r" % (name, line))


def drain(fd, seconds, until):
    out = b""
    end = time.time() + seconds
    while time.time() < end and until not in out:
        r, _, _ = select.select([fd], [], [], 0.1)
        if fd in r:
            try:
                chunk = os.read(fd, 65536)
            except OSError as e:
                if e.errno in (errno.EIO, errno.EAGAIN):
                    break
                raise
            if not chunk:
                break
            out += chunk
    return out


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    subprocess.run([DCH, "-k", SESS], capture_output=True)
    subprocess.run([DCH, "--spawn", SESS, "--size", "20x10", sys.executable,
                    "-c", CHILD], capture_output=True, timeout=15)
    pid = None
    fails = []
    try:
        end = time.time() + 15
        while b"plain line" not in read() and time.time() < end:
            time.sleep(0.2)

        plain = read()
        if b"plain line" not in plain:
            print("FAIL: session never printed. --read gave", repr(plain))
            return 1
        if b"\x1b]8" in plain:
            fails.append("plain --read carries OSC 8: %r" % plain)

        check("--read --ansi", read("--ansi"), fails)

        # The tail-cut keeps only the wrapped link's second row: it must
        # still open (and close) the link on its own.
        tail = read("--ansi", "--recent", "2")
        if not link(C, b"W" * 10).match(tail):
            fails.append("--recent 2 lost the wrapped link: %r" % tail)
        for line in unbalanced(tail):
            fails.append("--recent 2: unbalanced OSC 8 on line %r" % line)

        # Attach redraw: a client with no pty size keeps the 20x10 geometry.
        pid, fd = pty.fork()
        if pid == 0:
            os.environ.pop("DCH_SESSION", None)
            os.execv(DCH, [DCH, "-E", "-f", "-n", SESS])
            os._exit(127)
        redraw = drain(fd, 10.0, b"plain line")
        if b"plain line" not in redraw:
            fails.append("attach never redrew: %r" % redraw)
        else:
            check("attach redraw", redraw, fails)
    finally:
        if pid:
            try:
                os.kill(pid, signal.SIGHUP)
            except OSError:
                pass
        subprocess.run([DCH, "-k", SESS], capture_output=True)

    if fails:
        for f in fails:
            print("FAIL:", f)
        return 1
    print("PASS: OSC 8 links survive --read --ansi, --recent and the attach "
          "redraw, one balanced link per line; plain carries none.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
