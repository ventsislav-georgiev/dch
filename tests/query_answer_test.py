#!/usr/bin/env python3
"""A session no terminal sees answers the program's start-up queries (#273).

A program with no TERM_PROGRAM to guess from asks the terminal: DECRQM 2026
(CSI ? 2026 $ p, synchronized output) and XTVERSION (CSI > q). With nothing
attached (dch --spawn, a revive) those bytes reach no terminal, so before
#273 nothing answered and the program left synchronized output off. Now the
master's mirror answers those two, and only when no attached client got the
bytes. The probe writes both, plus DA1 (CSI c) and CPR (CSI 6 n) which stay
the attached terminal's to answer, then reads its tty for 2 s.
  (a) --spawn, nothing attached: the mirror's two answers, nothing else;
  (b) started under an attach whose terminal (this test) answers all four:
      exactly the terminal's answers, once, no mirror answer on top;
  (c) --spawn, attached inside the probe's wait: the queries were read
      before the attach and went to no terminal, so the mirror's answers,
      and the attaching terminal is never asked.
dch-lite has no mirror: (a) and (c) get nothing, as before.

Run:  python3 tests/query_answer_test.py            (uses ./dch)
"""
import os, pty, select, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
ROOT = tempfile.mkdtemp(prefix="dchqa", dir="/tmp")
ENV = dict(os.environ, DCH_SOCKET_DIR=os.path.join(ROOT, "s"),
           XDG_STATE_HOME=os.path.join(ROOT, "state"),
           HOME=os.path.join(ROOT, "home"),
           DCH_CONFIG=os.path.join(ROOT, "config"), DCH_LOGIN_ENV="0",
           DCH_NO_REMOTE="1", TERM="xterm-256color")
for k in ("DCH_SESSION", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "COLORTERM",
          "MallocStackLogging", "MallocStackLoggingNoCompact"):
    ENV.pop(k, None)
PROBE = os.path.join(ROOT, "probe.py")
QUERIES = b"\033[?2026$p\033[>q\033[c\033[6n"
OUTER = {b"\033[?2026$p": b"\033[?2026;1$y", b"\033[>q": b"\033P>|outer\033\\",
         b"\033[c": b"\033[?62;22c", b"\033[6n": b"\033[9;9R"}
failures = []
names = []


def check(what, ok, *extra):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)
        for e in extra:
            print("     " + repr(e))


def result(path, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read()
        time.sleep(0.1)
    return None


def client(args):
    pid, fd = pty.fork()
    if pid == 0:
        os.execve(DCH, [DCH, "-E"] + args, ENV)
        os._exit(127)
    os.set_inheritable(fd, False)
    return pid, fd


def outer_terminal(fd, done, timeout=10.0):
    """Answer what the client forwards, as a terminal would; return what it
    was asked."""
    seen, end = b"", time.time() + timeout
    while time.time() < end and not os.path.exists(done):
        if select.select([fd], [], [], 0.05)[0]:
            try:
                b = os.read(fd, 65536)
            except OSError:
                break
            if not b:
                break
            seen += b
            for q, a in OUTER.items():
                if q in b:
                    os.write(fd, a)
    return [q for q in OUTER if q in seen]


def start(case, how, delay):
    out = os.path.join(ROOT, "out_" + case)
    name = "qa273%s-%d" % (case, os.getpid())
    names.append(name)
    argv = [sys.executable, PROBE, out, str(delay)]
    if how == "spawn":
        subprocess.run([DCH, "--spawn", name] + argv, env=ENV,
                       capture_output=True, timeout=30)
        return out, name, None
    return out, name, client(["-n", name] + argv)


def main():
    with open(PROBE, "w") as f:
        f.write('''import os, select, sys, termios, time, tty
fd = os.open("/dev/tty", os.O_RDWR)
tty.setraw(fd)
time.sleep(float(sys.argv[2]))
os.write(fd, %r)
got, end = b"", time.time() + 2.0
while time.time() < end:
    if select.select([fd], [], [], max(0, end - time.time()))[0]:
        got += os.read(fd, 4096)
with open(sys.argv[1] + ".t", "wb") as f:
    f.write(got)
os.rename(sys.argv[1] + ".t", sys.argv[1])
time.sleep(5)
''' % QUERIES)
    build = subprocess.run([DCH, "--build"], capture_output=True,
                           text=True).stdout.strip()
    ver = subprocess.run([DCH, "-V"], capture_output=True,
                         text=True).stdout.split()[1]
    mirror = (b"\033[?2026;2$y\033P>|dch " + ver.encode() + b"\033\\"
              if build == "full" else b"")

    out, name, _ = start("a", "spawn", 0)
    got = result(out)
    check("(a) --spawn, nothing attached: %s" % ("the mirror answers "
          "DECRQM 2026 and XTVERSION, not DA1 or CPR" if mirror else
          "lite, nothing answers"), got == mirror, got)

    out, name, (pid, fd) = start("b", "attach", 0.5)
    asked = outer_terminal(fd, out)
    got = result(out)
    check("(b) attached start: the terminal is asked all four",
          asked == list(OUTER), asked)
    check("(b) attached start: exactly the terminal's answers, no mirror's",
          got == b"".join(OUTER.values()), got)

    out, name, _ = start("c", "spawn", 0.3)
    time.sleep(1.0)
    pid, fd = client(["-a", name])
    asked = outer_terminal(fd, out)
    got = result(out)
    check("(c) attached inside the wait: the attaching terminal is not asked",
          asked == [], asked)
    check("(c) attached inside the wait: the mirror's answers, once",
          got == mirror, got)


try:
    main()
finally:
    for n in names:
        subprocess.run([DCH, "-k", n], env=ENV, capture_output=True,
                       timeout=30)
    shutil.rmtree(ROOT, ignore_errors=True)
print("PASS" if not failures else "FAILED: %d" % len(failures))
sys.exit(1 if failures else 0)
