"""Master self-heal tick: re-bind a deleted socket, keep the sidecars alive.

macOS runs /usr/libexec/tmp_cleaner daily: `find /tmp -type f -atime +3
-mtime +3 -ctime +3 -delete`. The sidecars (.ver/.act/.state/.alias) are
regular files and age out on any 3-day-idle session; the socket node itself
can go too (other cleaners, a stray rm). A master whose socket is unlinked
keeps running but is unreachable -- and the next attach starts a SECOND
master on the same name, orphaning the first.

The master's select() now times out (DCH_HEAL_MS, ms) and heals:

  - socket node gone       -> re-bound, S_ISSOCK, --status/--ls-json see it,
                              and a control verb round-trips to the pty
  - .ver gone              -> re-stamped
  - a regular file on the  -> unlinked and re-bound
    socket's name
  - a healthy socket node  -> left strictly alone (never clobber a node that
                              may belong to a replacement master)
  - throughout             -> exactly one master process for the session

Run:  python3 tests/socket_heal_test.py        (uses ./dch)
"""
import os, shutil, signal, stat, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))

HEAL_MS = 300
SESSION = "heal%d" % os.getpid()
# Two-and-a-bit tick intervals, plus slack for a loaded CI box.
SETTLE = HEAL_MS / 1000.0 * 3 + 0.6


def run(*args, **kw):
    return subprocess.run([DCH] + list(args), capture_output=True,
                          text=True, **kw)


def masters(sock):
    """pids of live masters serving `sock` (argv carries --master-of <path>)."""
    out = subprocess.run(["ps", "-eo", "pid,command"],
                         capture_output=True, text=True).stdout
    pids = []
    for line in out.splitlines():
        if "--master-of" in line and sock in line:
            pids.append(int(line.split()[0]))
    return pids


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1

    tmp = tempfile.mkdtemp()
    os.environ["XDG_RUNTIME_DIR"] = tmp
    sockdir = os.path.join(tmp, "dch-%d" % os.getuid())
    sock = os.path.join(sockdir, SESSION + ".sock")
    fails = 0

    def check(msg, cond):
        nonlocal fails
        if cond:
            print("  ok   %s" % msg)
        else:
            fails += 1
            print("  FAIL %s" % msg)

    try:
        env = dict(os.environ, DCH_HEAL_MS=str(HEAL_MS))
        subprocess.run([DCH, "--spawn", SESSION, "--size", "80x24", "sh"],
                       env=env, capture_output=True, check=True)
        check("session spawned with a socket",
              stat.S_ISSOCK(os.lstat(sock).st_mode))
        started = masters(sock)
        check("exactly one master at start (%r)" % started, len(started) == 1)

        # --- the socket node and .ver vanish (tmp cleaner / stray rm) -----
        os.unlink(sock)
        os.unlink(sock + ".ver")
        time.sleep(SETTLE)

        healed = os.path.exists(sock) and stat.S_ISSOCK(os.lstat(sock).st_mode)
        check("deleted socket is re-bound as a socket", healed)
        if not healed:
            return 1
        check("--status works again after the heal",
              run("--status", SESSION).stdout.strip()
              in ("idle", "working", "blocked", "done"))
        check("--ls-json lists the session again",
              '"%s"' % SESSION in run("--ls-json").stdout)
        ver = ""
        if os.path.exists(sock + ".ver"):
            with open(sock + ".ver") as f:
                ver = f.read().strip()
        check("`.ver` re-stamped with the live version (%r)" % ver,
              ver != "" and ver == run("--version").stdout.strip().split()[-1])
        check("still exactly one master -- no second one spawned",
              masters(sock) == started)

        # A real round trip through the re-bound node: the heal never touches
        # .act's mtime (UTIME_OMIT), so an advancing mtime can only be the
        # shell's own output coming back off the pty.
        act = sock + ".act"
        was = os.stat(act).st_mtime if os.path.exists(act) else 0
        rc = run("--run", SESSION, "echo heal_ping").returncode
        check("a control verb reaches the master over the new socket", rc == 0)
        deadline = time.time() + 5
        while time.time() < deadline:
            if os.path.exists(act) and os.stat(act).st_mtime > was:
                break
            time.sleep(0.05)
        check("the command reached the pty (activity mtime advanced)",
              os.path.exists(act) and os.stat(act).st_mtime > was)

        # --- a stray regular file takes the socket's name -----------------
        os.unlink(sock)
        with open(sock, "w") as f:
            f.write("not a socket\n")
        time.sleep(SETTLE)
        check("a regular file on the socket's name is replaced",
              stat.S_ISSOCK(os.lstat(sock).st_mode))

        # --- a healthy socket node is never clobbered ---------------------
        before = os.lstat(sock)
        time.sleep(SETTLE)
        after = os.lstat(sock)
        check("a healthy socket node is left alone (inode unchanged)",
              before.st_ino == after.st_ino)
        check("still exactly one master at the end",
              masters(sock) == started)

        # --- surviving sidecars are kept young ----------------------------
        with open(act, "a"):
            pass
        old = time.time() - 10 * 86400
        os.utime(act, (old, old))
        mtime_before = os.stat(act).st_mtime
        time.sleep(SETTLE)
        st = os.stat(act)
        check("`.act` atime refreshed by the tick",
              st.st_atime > old + 86400)
        check("`.act` mtime untouched (it IS the activity signal)",
              st.st_mtime == mtime_before)
    finally:
        for pid in masters(sock):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        subprocess.run([DCH, "-kl"], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)

    if fails == 0:
        print("PASS: master self-heal re-binds the socket and keeps sidecars.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
