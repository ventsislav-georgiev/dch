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
  (d) attached, the chord's X-ray view up over the attach (Ctrl-\\ x) when
      the program asks: the view's terminal shows no session output, so the
      mirror's answers and the terminal is never asked (QA 1.63.0 LOW-V15).
  (e) (d) with a `dch --restart` while the view is up: the new master image
      keeps the view's state, so the mirror still answers (QA 1.65.0
      LOW-V23), and the view does not take the restart for the session's
      end (LOW-V27).
  (f) `dch --xray <n> --follow --json` on an idle session (no output, so no
      frame from the master) across three `--restart`s: one `restarted`
      reset and a snapshot each, and the stream goes on (QA 1.66.0 MED-A1:
      it ended at the second), each right after the last snapshot (QA
      1.67.0 LOW-A3: under 1 s apart it ended).
  (g) a fake master that answers and hangs up 1.5 s after each snapshot
      (no new image): one reset and snapshot, then exit 0 (QA 1.67.0
      LOW-A4: past 1 s it looped).
dch-lite has no mirror: (a), (c) and (d) get nothing, as before.

Run:  python3 tests/query_answer_test.py            (uses ./dch)
"""
import json, os, pty, select, shutil, socket, subprocess, sys, tempfile
import threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
ROOT = tempfile.mkdtemp(prefix="dchqa", dir=os.environ.get("DCH_TEST_TMP", "/tmp"))
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


def client(args, esc=False):
    pid, fd = pty.fork()
    if pid == 0:
        os.execve(DCH, [DCH] + ([] if esc else ["-E"]) + args, ENV)
        os._exit(127)
    os.set_inheritable(fd, False)
    return pid, fd


def outer_terminal(fd, done, timeout=10.0, screen=None):
    """Answer what the client forwards, as a terminal would; return what it
    was asked (and append what it showed to screen)."""
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
    if screen is not None:
        screen.append(seen)
    return [q for q in OUTER if q in seen]


def start(case, how, delay, go=None):
    out = os.path.join(ROOT, "out_" + case)
    name = "qa273%s-%d" % (case, os.getpid())
    names.append(name)
    argv = [sys.executable, PROBE, out, str(delay)] + ([go] if go else [])
    if how == "spawn":
        subprocess.run([DCH, "--spawn", name] + argv, env=ENV,
                       capture_output=True, timeout=30)
        return out, name, None
    return out, name, client(["-n", name] + argv, go is not None)


def main():
    with open(PROBE, "w") as f:
        f.write('''import os, select, sys, termios, time, tty
fd = os.open("/dev/tty", os.O_RDWR)
tty.setraw(fd)
time.sleep(float(sys.argv[2]))
while len(sys.argv) > 3 and not os.path.exists(sys.argv[3]):
    time.sleep(0.05)
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

    for case in ("d", "e"):
        go = os.path.join(ROOT, "go_" + case)
        out, name, (pid, fd) = start(case, "attach", 0, go)
        seen = b""
        for keys in (b"", b"\034x"):
            os.write(fd, keys)
            end = time.time() + (1.0 if not keys else 20)
            while time.time() < end and (not keys or b"X-ray  " not in seen):
                if select.select([fd], [], [], 0.05)[0]:
                    seen += os.read(fd, 65536)
        check("(%s) the chord's X-ray view opens over the attach" % case,
              b"X-ray  " in seen, seen[-200:])
        up = "the view up"
        if case == "e":
            up += " across a --restart"
            r = subprocess.run([DCH, "--restart", name], env=ENV,
                               capture_output=True, text=True, timeout=60)
            check("(e) --restart with the view up: exit 0", r.returncode == 0,
                  r.stdout, r.stderr)
        open(go, "w").close()
        screen = [seen]
        asked = outer_terminal(fd, out, screen=screen)
        got = result(out)
        check("(%s) %s: the terminal is not asked" % (case, up), asked == [],
              asked)
        check("(%s) %s: the mirror's answers, once" % (case, up),
              got == mirror, got)
        if case == "e":
            # the restart dropped the view's follow connection before
            # --restart returned, and the probe's 2 s read came after: the
            # old view said it within that time, on its 500 ms tick
            check("(e) the view does not say the session ended",
                  b"session ended" not in b"".join(screen), screen[-1][-300:])
        os.write(fd, b"q")

    name = "qa273f-%d" % os.getpid()
    names.append(name)
    subprocess.run([DCH, "--spawn", name, "sleep", "600"], env=ENV,
                   capture_output=True, timeout=30)
    fl = subprocess.Popen([DCH, "--xray", name, "--follow", "--json"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=ENV)
    got = []
    threading.Thread(target=lambda: [got.append(json.loads(l))
                                     for l in fl.stdout], daemon=True).start()

    def kinds():
        return [next(k for k in o if k != "schema") for o in list(got)]

    def wait_kinds(want, secs=30.0):
        end = time.time() + secs
        while time.time() < end and kinds() != want and fl.poll() is None:
            time.sleep(0.05)
        return kinds() == want

    want = ["snapshot"]
    ok = wait_kinds(want)
    for i in range(3):
        # at once: a new image is a restart however soon (LOW-A3)
        r = subprocess.run([DCH, "--restart", name], env=ENV,
                           capture_output=True, text=True, timeout=60)
        want += ["reset", "snapshot"]
        ok = ok and r.returncode == 0 and wait_kinds(want)
    check("(f) --follow --json, idle session, three --restarts: a reset "
          "restarted and a snapshot each, still running",
          ok and fl.poll() is None and
          all(o["reset"] == {"why": "restarted"} for o in got if "reset" in o),
          kinds(), fl.poll())
    fl.terminate()
    fl.wait(10)

    # (g) LOW-A4: a master that answers MSG_CMD (a live shell), answers the
    # follower's MSG_RING_READ with READ_END UNSUP (a master before #119)
    # and hangs that connection up 1.5 s later, writing no .ver
    stop = threading.Event()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(os.path.join(ROOT, "s", "qa-a4.sock"))
    srv.listen(16)
    srv.settimeout(0.2)

    def pkt(t, n, body=b""):
        return bytes([t, n & 255, n >> 8]) + body

    def conn(c):
        buf, hang = b"", None
        c.settimeout(0.05)
        while not stop.is_set() and not (hang and time.time() >= hang):
            try:
                b = c.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not b:
                break
            buf += b
            while len(buf) >= 3:
                t, n = buf[0], buf[1] | buf[2] << 8
                n = n if t in (6, 7, 13, 14, 15, 18, 22) else (
                    8 if t in (3, 4) else 0)
                if len(buf) < 3 + n:
                    break
                buf = buf[3 + n:]
                if t == 15:     # MSG_CMD
                    body = b"0\0running\0-1\0" + b"0\0" + b"1\0/bin/sh\0"
                    c.sendall(pkt(8, len(body), body) + pkt(9, 0))
                else:           # MSG_RING_READ and the rest: UNSUP
                    c.sendall(pkt(9, 5))
                    hang = hang or (time.time() + 1.5 if t == 18 else None)
        c.close()

    def accept():
        while not stop.is_set():
            try:
                c, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:  # srv closed by the finally: the end
                return
            threading.Thread(target=conn, args=(c,), daemon=True).start()

    acc = threading.Thread(target=accept, daemon=True)
    acc.start()
    try:
        fl = subprocess.Popen([DCH, "--xray", "qa-a4", "--follow", "--json"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              stdin=subprocess.DEVNULL, env=ENV)
        got = []
        rd = threading.Thread(target=lambda: [got.append(json.loads(l))
                                              for l in fl.stdout],
                              daemon=True)
        rd.start()
        end = time.time() + 60
        while fl.poll() is None and kinds().count("reset") < 2 and \
                time.time() < end:
            time.sleep(0.05)
        rc = fl.poll()
        if rc is None:
            fl.terminate()
            fl.wait(10)
        rd.join(10)
        check("(g) a master that hangs up 1.5 s after each snapshot: one "
              "reset and snapshot, then exit 0",
              rc == 0 and kinds().count("reset") == 1, kinds(), rc)
    finally:
        stop.set()
        srv.close()
        acc.join(5)


try:
    main()
finally:
    for n in names:
        subprocess.run([DCH, "-k", n], env=ENV, capture_output=True,
                       timeout=30)
    shutil.rmtree(ROOT, ignore_errors=True)
print("PASS" if not failures else "FAILED: %d" % len(failures))
sys.exit(1 if failures else 0)
