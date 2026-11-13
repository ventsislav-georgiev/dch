#!/usr/bin/env python3
"""Kitty keyboard protocol vs the alternate screen.

Ghostty and kitty keep the kitty-keyboard flag stack PER SCREEN and do not
copy it across 1049h/l. Two consequences dch has to respect:

  1. On attach the master must re-arm 1049h BEFORE it replays the child's
     ESC[>..u push, so the flags land on the alt screen the child reads keys
     from (and where the detaching client can pop them).
  2. On detach the client must pop on BOTH screens: once before leaving the
     alt screen and once after, so neither screen keeps stale flags. This is
     the "ctrl-d types 0;5u in the bare shell after detach" bug.
  3. A local ctrl-z suspend restores the terminal as a detach does, before
     the client stops (#211): the main screen, mouse and bracketed paste off,
     the kitty stacks empty. The resume re-arms each mode once and pops once
     before its one push (#200): over three suspend/resume cycles a
     per-screen stack model of the whole attach ends at 1 on the alternate
     screen, 0 on the main one. The first attach pops nothing.
  4. A busy alternate-screen child and a terminal that stalls before the
     ctrl-z: what the master queued or sent before it read the suspend's
     MSG_DETACH is dropped (the master's queue there, the socket's by the
     client on the resume), so none of it lands on the main screen the
     suspend left before the resume's 1049h (#211).

Run:  python3 tests/kbd_altscreen_test.py            (uses ./dch)
"""
import errno, os, pty, re, select, signal, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))
SESS = "kbdaltscreen-%d" % os.getpid()
POP = re.compile(rb"\x1b\[<\d*u")
PUSH = b"\x1b[>1u"
ALT_ON, ALT_OFF = b"\x1b[?1049h", b"\x1b[?1049l"
KSEQ = re.compile(rb"\033\[(\?1049[hl]|>\d*u|<\d*u)")
MODES = (b"\x1b[?1000h", b"\x1b[?2004h", ALT_ON)   # the child's, re-armed
RESET = (b"\x1b[?25h", b"\x1b[?1000l", b"\x1b[?2004l", b"\x1b[?1004l")
STOPPED = b"<<stopped>>"


def kitty_stacks(out):
    """The kitty flag stack depths (main, alt) a terminal ends with after
    out: a stack per screen, 1049h/l switch, ESC[<Nu pops N (default 1).
    The model of tests/xray_remote_view_test.py."""
    st, alt = [0, 0], 0
    for m in KSEQ.finditer(out):
        t = m.group(1)
        if t[:1] == b"?":
            alt = int(t[-1:] == b"h")
        elif t[:1] == b">":
            st[alt] += 1
        else:
            st[alt] = max(0, st[alt] - int(t[1:-1] or 1))
    return tuple(st)


def spawn(args):
    pid, fd = pty.fork()
    if pid == 0:
        # No -E: the detach key must stay live.
        os.environ.pop("DCH_SESSION", None)
        os.execv(DCH, [DCH] + args)
        os._exit(127)
    # inheritable by default: a later client would hold this pty open
    os.set_inheritable(fd, False)
    return pid, fd


def spawn_job(args):
    """spawn, but as a shell runs a job: dch in its own foreground process
    group under a parent in its session, so its ctrl-z SIGTSTP really stops
    it. The parent writes STOPPED to the terminal then, and continues it."""
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.pop("DCH_SESSION", None)
        job = os.fork()
        if job == 0:
            os.setpgid(0, 0)
            signal.signal(signal.SIGTTOU, signal.SIG_IGN)
            os.tcsetpgrp(0, os.getpgrp())
            signal.signal(signal.SIGTTOU, signal.SIG_DFL)
            os.execv(DCH, [DCH] + args)
            os._exit(127)
        while True:
            _, st = os.waitpid(job, os.WUNTRACED)
            if not os.WIFSTOPPED(st):
                os._exit(0)
            os.write(1, STOPPED)
            time.sleep(0.3)
            os.kill(job, signal.SIGCONT)
    os.set_inheritable(fd, False)
    return pid, fd


def drain(fd, timeout, until=None):
    out = b""
    end = time.time() + timeout
    while time.time() < end:
        if until and until in out:
            break
        ready, _, _ = select.select([fd], [], [], 0.1)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.EAGAIN):
                    break
                raise
            if not chunk:
                break
            out += chunk
    return out


def detach(pid, fd):
    """Ctrl-\\ once; the double-tap window expires and the client detaches.
    Returns everything the client wrote to the terminal on its way out."""
    os.write(fd, b"\x1c")
    out = drain(fd, 5.0)
    reap(pid, fd)
    return out


def reap(pid, fd, secs=5):
    """Drain fd until pid exits or secs pass, close fd, then SIGKILL and
    reap a pid still running (the fd first, #215)."""
    end, done = time.time() + secs, 0
    while time.time() < end and not done:
        drain(fd, 0.2)
        done = os.waitpid(pid, os.WNOHANG)[0]
    os.close(fd)
    if not done:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)


def suspend(fd):
    """Ctrl-z under spawn_job: the client sends MSG_DETACH and SIGTSTPs
    itself, its parent continues it, then MSG_ATTACH and a redraw on the same
    connection. Returns (bytes before the stop, bytes of the resume)."""
    os.write(fd, b"\x1a")
    out = drain(fd, 5.0, STOPPED)
    out += drain(fd, 5.0, PUSH)
    pre, _, post = (out + drain(fd, 0.5)).partition(STOPPED)
    return pre, post


def check_detach(who, out):
    off = out.rfind(ALT_OFF)
    if off < 0:
        print("FAIL: %s detach did not leave the alt screen: %r" % (who, out))
        return 1
    if not POP.search(out, off):
        print("FAIL: %s detach popped kitty flags only on the alt screen: %r"
              % (who, out[off - 40:]))
        return 1
    if not POP.search(out, 0, off):
        print("FAIL: %s detach never popped kitty flags on the alt screen: %r"
              % (who, out[:off + len(ALT_OFF)]))
        return 1
    return 0


def end_session(name):
    """dch -k name, bounded: run() kills it at the timeout (NIT-T4)."""
    try:
        subprocess.run([DCH, "-k", name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)
    except subprocess.TimeoutExpired:
        pass


def stop(master):
    """End our --master-of: SIGTERM, 10 s, then SIGKILL (NIT-T4)."""
    master.terminate()
    try:
        master.wait(10)
    except subprocess.TimeoutExpired:
        master.kill()
        master.wait()


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH); return 1
    end_session(SESS)

    # Make the child write before its first client attaches. A no-mirror
    # master cannot replay this startup output, so first attach must forward it.
    tmp = tempfile.mkdtemp(prefix="dch-kbd-pending-")
    pending = os.path.join(tmp, "ready")
    os.environ["DCH_SOCKET_DIR"] = tmp
    sock = os.path.join(tmp, SESS + ".sock")
    child = ("import sys,time;sys.stdout.write('\\x1b[?1049h\\x1b[?1000h"
             "\\x1b[?2004h\\x1b[>1uREADY\\n');"
             "sys.stdout.flush();open(%r,'w').close();time.sleep(60)" % pending)
    env = dict(os.environ, DCH_NO_VT="1")
    master = subprocess.Popen([DCH, "--master-of", sock, "--",
                               sys.executable, "-c", child], env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(100):
        if os.path.exists(sock):
            break
        time.sleep(0.05)
    if not os.path.exists(sock):
        stop(master)
        shutil.rmtree(tmp, ignore_errors=True)
        print("FAIL: no master socket"); return 1
    for _ in range(100):
        if os.path.exists(pending):
            break
        time.sleep(0.05)
    if not os.path.exists(pending):
        end_session(SESS)
        stop(master)
        shutil.rmtree(tmp, ignore_errors=True)
        print("FAIL: inner program did not queue startup output"); return 1
    pid_a, fd_a = spawn(["-f", "-n", SESS])
    pid_b = pid_c = pid_d = None
    try:
        first = drain(fd_a, 15.0, b"READY")
        if b"READY" not in first:
            print("FAIL: inner program never started:", repr(first)); return 1
        if first.count(ALT_ON) != 1 or first.count(PUSH) != 1:
            print("FAIL: first attach duplicated child modes:", repr(first)); return 1
        if check_detach("first", detach(pid_a, fd_a)):
            return 1
        pid_a = None

        pid_b, fd_b = spawn(["-f", "-n", SESS])
        replay = drain(fd_b, 10.0, PUSH)
        alt, push = replay.find(ALT_ON), replay.find(PUSH)
        if alt < 0 or push < 0 or replay.count(ALT_ON) != 1 \
           or replay.count(PUSH) != 1:
            print("FAIL: reattach did not re-arm alt screen + kitty push:", repr(replay))
            return 1
        if push < alt:
            print("FAIL: kitty push replayed before 1049h, lands on the primary screen:",
                  repr(replay))
            return 1
        if check_detach("reattach", detach(pid_b, fd_b)):
            return 1
        pid_b = None

        pid_c, fd_c = spawn_job(["-f", "-n", SESS])
        out = drain(fd_c, 10.0, PUSH) + drain(fd_c, 0.5)
        if POP.search(out) or out.count(PUSH) != 1:
            print("FAIL: the attach before the suspends is not push-only:",
                  repr(out))
            return 1
        bad = 0
        for i in range(1, 4):
            pre, got = suspend(fd_c)
            out += pre
            if not got:
                print("FAIL: ctrl-z %d never stopped the client:" % i,
                      repr(pre))
                return 1
            if kitty_stacks(out) != (0, 0) or out.rfind(ALT_OFF) < \
               out.rfind(ALT_ON) or any(r not in pre for r in RESET):
                print("FAIL: ctrl-z %d stopped without a detach's reset "
                      "(kitty stacks %r):" % (i, kitty_stacks(out)), repr(pre))
                bad = 1
            out += got
            pops = [m.start() for m in POP.finditer(got)]
            if got.count(PUSH) != 1 or len(pops) != 1 \
               or pops[0] > got.find(PUSH):
                print("FAIL: ctrl-z resume %d is not one kitty pop before "
                      "its one push:" % i, repr(got))
                bad = 1
            if any(got.count(m) != 1 for m in MODES):
                print("FAIL: ctrl-z resume %d does not re-arm each mode "
                      "once:" % i, repr(got))
                bad = 1
        if kitty_stacks(out) != (0, 1):
            print("FAIL: three ctrl-z resumes leave the kitty stacks at "
                  "(main, alt) =", kitty_stacks(out), "not (0, 1)")
            bad = 1
        if bad:
            return 1
        if check_detach("suspended", detach(pid_c, fd_c)):
            return 1
        pid_c = None

        flood = ("import sys\nsys.stdout.write('\\x1b[?1049h')\nwhile 1:\n"
                 " sys.stdout.write('\\x1b[5;1HTICK ');sys.stdout.flush()")
        pid_d, fd_d = spawn_job(["-n", SESS + "-busy", sys.executable, "-c",
                                 flood])
        # the attach is up (raw mode, output relayed) before the first key:
        # a ctrl-z sent earlier is dropped by the cooked tty and nothing stops
        up = drain(fd_d, 20.0, b"TICK")
        if b"TICK" not in up:
            print("FAIL: the busy attach never showed the session:",
                  repr(up[-200:]))
            return 1
        drain(fd_d, 0.5)
        for i in range(1, 4):
            time.sleep(0.1)     # not read: the master queues output
            os.write(fd_d, b"\x1a")
            got, end = b"", time.time() + 10
            while ALT_ON not in got.partition(STOPPED)[2] and \
                  time.time() < end:
                got += drain(fd_d, 0.2)
            post = got.partition(STOPPED)[2]
            if ALT_ON not in post or b"TICK" in post[:post.find(ALT_ON)]:
                print("FAIL: busy ctrl-z resume %d writes output on the main "
                      "screen before its 1049h:" % i, repr(post[:200]),
                      "stopped:", STOPPED in got, "read:", len(got),
                      repr(got[-120:]))
                return 1
        print("PASS: kitty push follows 1049h on attach; detach pops on both "
              "screens; ctrl-z resets as a detach does, a resume re-arms once and "
              "drops the output sent before the suspend.")
        return 0
    finally:
        for pid in (pid_d, pid_c, pid_b, pid_a):
            if pid:
                try:
                    os.kill(pid, signal.SIGHUP)
                except OSError:
                    pass
        end_session(SESS)
        end_session(SESS + "-busy")
        if master.poll() is None:
            stop(master)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
