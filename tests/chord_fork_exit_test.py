#!/usr/bin/env python3
"""The in-attach X-ray view's query children leave the client alone (#25).

A query child of the chord's view is a fork of the attach client
(xray_overlay forks the view, the view's xv_spawn forks each query). When
the master dies under its socket write, the child leaves through exit(1),
which runs the atexit handlers the client registered. Before #25 those ran
in the child: client_pidfile_unlink removed the client's marker while the
client lived, and restore_term put the terminal back in cooked mode under
the view.

The window is a race no timing loop hits, so tests/sockfail.c, built here
with the system cc and loaded into the client (DYLD_INSERT_LIBRARIES on
macOS, LD_PRELOAD on Linux), fails every socket write in the client's
forks. Every query child takes that exit path, deterministically. The shim
logs each failing child's exit after the client's handlers have run in it,
so the checks wait on that line, not on a sleep.

Covers: with the view open and a query child gone through exit(1), the
client lives, its marker is still there, and the terminal is still raw;
q leaves the view, the detach key then ends the client (exit 0) and only
then is the marker gone. Skips, with the reason, where the shim cannot be
built or loaded (no cc, a SIP-protected or hardened dch, a sanitizer
runtime that refuses the preload).

Run:  python3 tests/chord_fork_exit_test.py      (uses ./dch)
"""
import os, pty, select, shutil, signal, subprocess, sys, tempfile
import termios, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
NAME = "cfx"
fails = [0]


def check(msg, cond, detail=""):
    print("  %s %s" % ("ok  " if cond else "FAIL", msg))
    if not cond:
        fails[0] += 1
        if detail:
            print("       " + repr(detail)[-600:])


T = os.path.realpath(tempfile.mkdtemp(prefix="cfx"))
HOME = os.path.join(T, "home")
os.makedirs(HOME)
ENV = {k: v for k, v in os.environ.items()
       if not k.startswith(("DCH_SESSION", "DCH_SOCKET_DIR", "DCH_DEBUG",
                            "CLAUDE", "Malloc", "DYLD_INSERT", "LD_PRELOAD"))}
ENV.update(XDG_RUNTIME_DIR=T, XDG_STATE_HOME=os.path.join(T, "state"),
           HOME=HOME, DCH_CONFIG=os.path.join(T, "config"),
           DCH_LOGIN_ENV="0", DCH_NO_REMOTE="1", TERM="xterm-256color")
SOCK = os.path.join(T, "dch-%d" % os.getuid())
VAR = "DYLD_INSERT_LIBRARIES" if sys.platform == "darwin" else "LD_PRELOAD"


def dch(*a, env=None):
    return subprocess.run([DCH] + list(a), capture_output=True, text=True,
                          timeout=30, env=env or ENV)


def until(fn, secs=10.0):
    end = time.time() + secs
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


class Pty:
    """dch exec'd straight on a pty (no shell between: a SIP-protected
    /bin/sh would drop DYLD_INSERT_LIBRARIES); out holds what it wrote."""

    def __init__(self, argv, env):
        self.out = b""
        self.rc = None
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execve(argv[0], argv, env)

    def pump(self, secs=0.05):
        end = time.time() + secs
        while True:
            if time.time() >= end:
                return
            r, _, _ = select.select([self.fd], [], [],
                                    max(0, end - time.time()))
            if not r:
                return
            try:
                b = os.read(self.fd, 65536)
            except OSError:
                b = b""
            if not b:
                self.reap()
                return
            self.out += b

    def reap(self):
        if self.rc is None:
            p, st = os.waitpid(self.pid, os.WNOHANG)
            if p:
                self.rc = os.waitstatus_to_exitcode(st)
        return self.rc

    def wait(self, what, secs=10.0, start=0):
        end = time.time() + secs
        while what not in self.out[start:] and time.time() < end:
            self.pump(0.1)
            if self.rc is not None:
                break
        return what in self.out[start:]

    def key(self, b, settle=0.3):
        os.write(self.fd, b)
        self.pump(settle)

    def close(self):
        # the fd first: on macOS a killed pty leader with output unread
        # cannot finish exiting, so the waitpid would never return
        os.close(self.fd)
        if self.reap() is None:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)


def events(mark):
    """The shim's lines: (event, pid, what)."""
    try:
        with open(mark) as f:
            return [(w[0], int(w[1]), w[2] if len(w) > 2 else "")
                    for w in (l.split() for l in f.read().splitlines())]
    except (OSError, ValueError, IndexError):
        return []


def kids(pid):
    r = subprocess.run(["/bin/ps", "-A", "-o", "pid=,ppid="],
                       capture_output=True, text=True)
    return [int(a) for a, b in (l.split() for l in r.stdout.splitlines())
            if int(b) == pid]


def shim():
    """Build tests/sockfail.c; return (path, None) or (None, why not)."""
    cc = shutil.which("cc")
    if not cc:
        return None, "no cc"
    lib = os.path.join(T, "sockfail.so")
    argv = [cc, "-o", lib, os.path.join(HERE, "sockfail.c")]
    argv += (["-dynamiclib"] if sys.platform == "darwin"
             else ["-shared", "-fPIC", "-ldl"])
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode:
        return None, "cc could not build the shim: " + r.stderr.strip()[-300:]
    # loaded at all? A SIP-protected or hardened binary drops the variable,
    # a sanitizer runtime may refuse to start with another library first
    mark = os.path.join(T, "probe")
    r = dch("-ls", env=dict(ENV, **{VAR: lib, "SOCKFAIL_MARK": mark}))
    if not events(mark):
        return None, ("%s is not honoured for this dch (SIP-protected or "
                      "hardened binary, or a sanitizer runtime that refuses "
                      "it): rc %d%s" % (VAR, r.returncode,
                                         (" " + r.stderr.strip()[-200:])
                                         if r.stderr.strip() else ""))
    return lib, None


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    lib, why = shim()
    if not lib:
        print("  skip a query child's exit(1) under the chord's view (%s)"
              % why)
        return 0
    r = dch("--spawn", NAME, "--size", "100x30", "sleep", "600")
    check("spawned", r.returncode == 0, r.stderr)
    mark = os.path.join(T, "events")
    a = Pty([DCH, "-a", NAME], dict(ENV, **{VAR: lib, "SOCKFAIL_MARK": mark}))
    try:
        # attached once the client has the pty raw (dch-lite replays
        # nothing to wait on; a cooked ^\ would be SIGQUIT)
        check("the attach is up (the terminal raw)", until(
            lambda: a.pump(0.05) or not termios.tcgetattr(a.fd)[3] &
            termios.ICANON), a.out[-300:])
        marker = os.path.join(SOCK, "%s.sock.client.%d" % (NAME, a.pid))
        check("the client's marker exists", until(
            lambda: os.path.exists(marker), 5), sorted(os.listdir(SOCK)))
        check("the shim is loaded in the client, nothing failed yet",
              events(mark) == [("l", a.pid, "")], events(mark))
        m = len(a.out)
        a.key(b"\x1cx", 0.3)
        # the view's first query is the one that fails: it never draws a
        # header, only its alternate screen
        check("the chord opens the view (its alternate screen)",
              a.wait(b"\033[?1049h", 5, m), a.out[m:][-300:])
        view = kids(a.pid)
        # a query child failed its socket write and has run its atexit
        # handlers: the shim's exit line comes after the client's ones
        gone = until(lambda: [e for e in events(mark)
                              if e[0] == "x" and e[1] != a.pid], 15)
        check("a query child failed its socket write and exit()ed",
              gone, events(mark))
        check("the client's restore_term did not run in it: the terminal "
              "was still raw after its exit handlers",
              gone and all(e[2] == "raw" for e in gone), gone)
        a.pump(0.2)
        check("the client lives on", a.reap() is None, a.out[-300:])
        check("the client's marker is still there", os.path.exists(marker),
              sorted(os.listdir(SOCK)))
        lflag = termios.tcgetattr(a.fd)[3]
        check("the terminal is raw (no ICANON, no ECHO)",
              not lflag & (termios.ICANON | termios.ECHO), hex(lflag))
        # the view says why it stopped and waits for a key, or is still up
        a.key(b"q", 0.3)
        check("a key leaves the view", view and until(
            lambda: not any(k in kids(a.pid) for k in view), 5),
            (view, a.out[-300:]))
        check("back on the attach: the client and its marker are there",
              a.reap() is None and os.path.exists(marker))
        a.key(b"\x1c", 0.3)
        end = time.time() + 10
        while a.reap() is None and time.time() < end:
            a.pump(0.1)
        check("the detach key ends the client, exit 0", a.rc == 0,
              (a.rc, a.out[-300:]))
        check("only then is the marker gone", not os.path.exists(marker))
    finally:
        a.close()
        dch("-k", NAME)
        shutil.rmtree(T, ignore_errors=True)
    if fails[0]:
        print("%d failed" % fails[0])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
