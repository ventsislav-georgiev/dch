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

Related hazard (#005): if a master's node vanished long enough for a
replacement to take the same name before either master could heal, the
LOSING master's atexit(unlink_socket) must not delete the WINNER's socket
node -- or its sidecars, which live at the same path. unlink_socket() only
cleans up the node it can verify by device+inode is the one it itself bound;
a losing master's own normal exit still cleans up as always.

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


def wait_until_gone(sock, pid, timeout=5.0):
    """Block until `pid` no longer shows up in masters(sock), or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pid not in masters(sock):
            return True
        time.sleep(0.05)
    return False


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1

    tmp = tempfile.mkdtemp()
    os.environ["XDG_RUNTIME_DIR"] = tmp
    sockdir = os.path.join(tmp, "dch-%d" % os.getuid())
    sock = os.path.join(sockdir, SESSION + ".sock")
    session2 = "healdbl%d" % os.getpid()
    sock2 = os.path.join(sockdir, session2 + ".sock")
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

        # --- #005: a losing master's exit must not delete the winner's ----
        # socket. Give the loser a heal interval far longer than this test
        # so it cannot heal-race its own exit.
        loser_env = dict(os.environ, DCH_HEAL_MS="60000")
        subprocess.run([DCH, "--spawn", session2, "--size", "80x24", "sh"],
                       env=loser_env, capture_output=True, check=True)
        first = masters(sock2)
        check("double-master setup: one master to start (%r)" % first,
              len(first) == 1)
        if len(first) != 1:
            return 1
        loser_pid = first[0]

        # Vanish the node and spawn a second session under the same name --
        # this is the attach-falls-through path (do_spawn_verb sees no live
        # socket and binds a fresh one), leaving two masters on one name.
        os.unlink(sock2)
        subprocess.run([DCH, "--spawn", session2, "--size", "80x24", "sh"],
                       capture_output=True, check=True)
        both = masters(sock2)
        check("double-master setup: two masters serving one name (%r)" % both,
              len(both) == 2 and loser_pid in both)
        if len(both) != 2 or loser_pid not in both:
            return 1
        winner_pid = [p for p in both if p != loser_pid][0]
        winner_ino = os.lstat(sock2).st_ino

        # The LOSER exits (SIGTERM, not SIGKILL: die() must run so atexit
        # fires) -- unlink_socket() must see the node on disk no longer
        # matches what it bound, and touch nothing.
        os.kill(loser_pid, signal.SIGTERM)
        check("loser process actually exits",
              wait_until_gone(sock2, loser_pid))
        survived = os.path.exists(sock2) and stat.S_ISSOCK(os.lstat(sock2).st_mode)
        check("winner's socket node survives the loser's exit", survived)
        check("winner's socket node is untouched (same inode)",
              survived and os.lstat(sock2).st_ino == winner_ino)
        check("only the winner is left serving the name",
              masters(sock2) == [winner_pid])
        check("--status still reaches the winner",
              run("--status", session2).stdout.strip()
              in ("idle", "working", "blocked", "done"))
        check("--run still reaches the winner",
              run("--run", session2, "echo dbl_ping").returncode == 0)

        # Normal-exit (owned) case still cleans up: the winner's own node
        # matches its own recorded identity, so its exit unlinks it as ever.
        os.kill(winner_pid, signal.SIGTERM)
        check("winner process actually exits",
              wait_until_gone(sock2, winner_pid))
        check("owned socket node IS removed on normal exit",
              not os.path.exists(sock2))
    finally:
        for pid in masters(sock) + masters(sock2):
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
