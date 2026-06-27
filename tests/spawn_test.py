#!/usr/bin/env python3
"""Headless end-to-end test of the spawn-vs-attach decision (dch).

Covers the leftover-socket bug and the attach-first hot path:

  1. spawn over a STRAY regular file at <name>.sock — used to fail with
     "failed to spawn session" because the master's bind() hit EADDRINUSE.
  2. spawn over a DEAD socket file (crashed master, atexit unlink never ran)
     — used to fail the attach with "Connection refused".
  3. attach to a LIVE master (hot path) must ATTACH, not respawn: the inner
     program must run exactly once across two sequential clients.

Run:  python3 tests/spawn_test.py            (uses ./dch)
      DCH=/path/to/dch python3 tests/spawn_test.py
"""
import os, sys, pty, signal, select, time, errno, socket, tempfile, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))


def drain(fd, seconds):
    out = b""
    end = time.time() + seconds
    while time.time() < end:
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
    return out.decode("utf-8", "replace")


def run_client(name, argv_tail, drain_s=2.0):
    """forkpty a dch client; return (exit_status, output)."""
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.pop("DCH_SESSION", None)  # bypass nesting guard
        os.execv(DCH, [DCH, "-E", "-n", name] + argv_tail)
        os._exit(127)
    out = drain(fd, drain_s)
    # Reap; client should have exited when its inner cmd finished.
    for _ in range(20):
        wpid, status = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            break
        time.sleep(0.05)
    else:
        os.kill(pid, signal.SIGHUP)
        _, status = os.waitpid(pid, 0)
    try:
        os.close(fd)
    except OSError:
        pass
    return status, out


def make_dead_socket(path):
    """Leave a real socket node on disk with no listener (bound, then closed)."""
    if os.path.exists(path):
        os.unlink(path)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    s.close()  # file persists as a socket node, nothing listening


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH); return 1

    tmp = tempfile.mkdtemp()
    os.environ["XDG_RUNTIME_DIR"] = tmp
    sockdir = os.path.join(tmp, "dch-%d" % os.getuid())
    os.makedirs(sockdir, mode=0o700, exist_ok=True)
    fails = 0

    def ok(msg):   print("  ok   " + msg)
    def bad(msg):
        nonlocal fails
        fails += 1
        print("  FAIL " + msg)

    try:
        # 1. stray regular file at the socket path -> must still spawn.
        name = "stray"
        open(os.path.join(sockdir, name + ".sock"), "w").close()
        st, out = run_client(name, ["sh", "-c", "printf STRAY_OK; exit 0"])
        if "STRAY_OK" in out and "failed to spawn" not in out:
            ok("spawn over stray regular file")
        else:
            bad("spawn over stray regular file (out=%r)" % out)

        # 2. dead socket node at the path -> must still spawn.
        name = "deadsock"
        make_dead_socket(os.path.join(sockdir, name + ".sock"))
        st, out = run_client(name, ["sh", "-c", "printf DEAD_OK; exit 0"])
        if "DEAD_OK" in out and "Connection refused" not in out \
           and "failed to spawn" not in out:
            ok("spawn over dead socket node")
        else:
            bad("spawn over dead socket node (out=%r)" % out)

        # 3. hot path: a live master must be ATTACHED, not respawned.
        #    Inner program records each start; two sequential clients must
        #    yield exactly one start.
        name = "hot"
        marker = os.path.join(tmp, "starts.txt")
        inner = ["sh", "-c", "echo RUN >> '%s'; sleep 10" % marker]

        # Client A: spawns the master, then we detach it (master stays alive).
        pidA, fdA = pty.fork()
        if pidA == 0:
            os.environ.pop("DCH_SESSION", None)
            os.execv(DCH, [DCH, "-E", "-n", name] + inner)
            os._exit(127)
        drain(fdA, 1.0)  # let inner start
        os.kill(pidA, signal.SIGUSR1)  # clean detach -> client A exits, master lives
        for _ in range(20):
            wpid, _ = os.waitpid(pidA, os.WNOHANG)
            if wpid == pidA:
                break
            time.sleep(0.05)
        os.close(fdA)

        # Master should still be listed.
        listed = os.popen("'%s' -ls 2>/dev/null" % DCH).read().split()
        if name not in listed:
            bad("hot path: master not alive after detach (listed=%r)" % listed)
        else:
            # Client B: must attach to the SAME live master (hot path).
            st, out = run_client(name, inner, drain_s=1.0)
            try:
                with open(marker) as f:
                    runs = f.read().count("RUN")
            except FileNotFoundError:
                runs = 0
            if runs == 1:
                ok("hot path attaches to live master (inner ran once)")
            else:
                bad("hot path respawned inner (ran %d times, want 1)" % runs)

        # 4. cwd trap: invoked as a bare PATH name from a directory that
        #    contains a "dch" subdir. realpath(argv[0]) used to resolve the
        #    bare name relative to cwd -> the ./dch DIRECTORY, so the master
        #    execvp'd a directory and the spawn failed ("failed to spawn").
        bindir = tempfile.mkdtemp()
        os.symlink(os.path.abspath(DCH), os.path.join(bindir, "dch"))
        trap = tempfile.mkdtemp()
        os.makedirs(os.path.join(trap, "dch"))  # the ./dch directory trap
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(trap)
            os.environ.pop("DCH_SESSION", None)
            os.environ["PATH"] = bindir + ":" + os.environ.get("PATH", "")
            os.execvp("dch", ["dch", "-n", "cwdtrap",
                              "sh", "-c", "printf TRAP_OK; exit 0"])
            os._exit(127)
        out = drain(fd, 2.0)
        for _ in range(20):
            wpid, _ = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                break
            time.sleep(0.05)
        os.close(fd)
        shutil.rmtree(bindir, ignore_errors=True)
        shutil.rmtree(trap, ignore_errors=True)
        if "TRAP_OK" in out and "failed to spawn" not in out:
            ok("spawn as bare name from a dir containing ./dch")
        else:
            bad("cwd ./dch trap (out=%r)" % out)
    finally:
        os.system("'%s' -kl >/dev/null 2>&1" % DCH)
        shutil.rmtree(tmp, ignore_errors=True)

    if fails == 0:
        print("PASS: spawn-vs-attach (stray file, dead socket, hot path).")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
