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
import json, os, sys, pty, subprocess, signal, select, time, errno, socket, tempfile, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))


def drain(fd, seconds, until=None):
    # Return early once `until` appears: big deadlines stay cheap locally
    # and absorb slow CI runners.
    out = b""
    end = time.time() + seconds
    while time.time() < end:
        if until and until in out:
            break
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


def run_client(name, argv_tail, drain_s=10.0, until=None):
    """forkpty a dch client; return (exit_status, output)."""
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.pop("DCH_SESSION", None)  # bypass nesting guard
        os.execv(DCH, [DCH, "-E", "-n", name] + argv_tail)
        os._exit(127)
    out = drain(fd, drain_s, until)
    if until and until.decode("utf-8", "replace") in out:
        try:  # marker seen -> detach now instead of waiting out the inner cmd
            os.kill(pid, signal.SIGHUP)
        except OSError:
            pass
    # Reap; client should have exited when its inner cmd finished.
    for _ in range(100):
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
    os.environ["HOME"] = os.path.join(tmp, "home")
    os.environ["CODEX_THREAD_ID"] = "inherited-codex-id"
    os.makedirs(os.path.join(os.environ["HOME"], ".codex"))
    with open(os.path.join(os.environ["HOME"], ".codex", "session_index.jsonl"), "w") as f:
        f.write(json.dumps({"id": "inherited-codex-id",
                            "thread_name": "inherited-child"}) + "\n")
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
        # Marker LOOPS: output printed before a slow attach completes is lost
        # (attach redraw is WINCH-based, no replay), so a one-shot marker
        # races. run_client SIGHUPs the client once the marker is seen.
        st, out = run_client(name,
                             ["sh", "-c", "while :; do printf STRAY_OK; sleep 1; done"],
                             until=b"STRAY_OK")
        if "STRAY_OK" in out and "failed to spawn" not in out:
            ok("spawn over stray regular file")
        else:
            bad("spawn over stray regular file (out=%r)" % out)

        # 2. dead socket node at the path -> must still spawn.
        name = "deadsock"
        make_dead_socket(os.path.join(sockdir, name + ".sock"))
        st, out = run_client(name,
                             ["sh", "-c", "while :; do printf DEAD_OK; sleep 1; done"],
                             until=b"DEAD_OK")
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
        inner = ["sh", "-c", "echo RUN >> '%s'; sleep 60" % marker]

        # Client A: spawns the master, then we detach it (master stays alive).
        pidA, fdA = pty.fork()
        if pidA == 0:
            os.environ.pop("DCH_SESSION", None)
            os.execv(DCH, [DCH, "-E", "-n", name] + inner)
            os._exit(127)
        for _ in range(100):  # inner started once its marker file exists
            if os.path.exists(marker):
                break
            drain(fdA, 0.1)
        os.kill(pidA, signal.SIGUSR1)  # clean detach -> client A exits, master lives
        for _ in range(100):
            wpid, _ = os.waitpid(pidA, os.WNOHANG)
            if wpid == pidA:
                break
            time.sleep(0.05)
        os.close(fdA)

        # Master should still be listed.
        listed = os.popen("'%s' -ls 2>/dev/null" % DCH).read().split()
        if "inherited-child" in listed:
            bad("non-Codex child inherited Codex ID and overlaid session")
        if name not in listed:
            bad("hot path: master not alive after detach (listed=%r)" % listed)
        else:
            # Client B: must attach to the SAME live master (hot path).
            st, out = run_client(name, inner, drain_s=2.0)
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
                              "sh", "-c", "while :; do printf TRAP_OK; sleep 1; done"])
            os._exit(127)
        out = drain(fd, 10.0, b"TRAP_OK")
        os.kill(pid, signal.SIGHUP)
        os.waitpid(pid, 0)
        os.close(fd)
        shutil.rmtree(bindir, ignore_errors=True)
        shutil.rmtree(trap, ignore_errors=True)
        if "TRAP_OK" in out and "failed to spawn" not in out:
            ok("spawn as bare name from a dir containing ./dch")
        else:
            bad("cwd ./dch trap (out=%r)" % out)

        # 5. --spawn --size: the program starts at that size. It used to
        #    start at 80x24 and get the size from a WINCH sent after the
        #    spawn, so a fast program (an attach client) read 80x24.
        szf = os.path.join(tmp, "size.out")
        os.system("'%s' --spawn szs --size 40x10 -- sh -c "
                  "'stty size </dev/tty >%s.t; mv %s.t %s; exec sleep 60' "
                  ">/dev/null" % (DCH, szf, szf, szf))
        seen = ""
        for _ in range(100):
            if os.path.exists(szf):
                with open(szf) as f:
                    seen = f.read()
                break
            time.sleep(0.05)
        if seen.split() == ["10", "40"]:
            ok("--spawn --size: the program starts at that size")
        else:
            bad("--spawn --size: program saw %r, want 10 40" % seen)

        # 6. A client run from inside Claude Code must not hand the new
        #    session the caller's identity (#111): a resumed Claude saw the
        #    inherited CLAUDE_CODE_CHILD_SESSION and turned transcripts off.
        #    Harness vars (GIT_EDITOR) go only when CLAUDECODE is set; an
        #    explicit --env still wins.
        agent = {"CLAUDECODE": "1", "CLAUDE_CODE_CHILD_SESSION": "1",
                 "CLAUDE_CODE_SESSION_ID": "x", "GIT_EDITOR": "true"}

        def child_env(tag, argv, extra, attach=False):
            out = os.path.join(tmp, tag + ".env")
            dump = ["sh", "-c", "env >%s.t; mv %s.t %s; exec sleep 60"
                    % (out, out, out)]
            # The runner itself may be inside Claude Code: start clean.
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith("CLAUDE") and k not in
                   ("DCH_SESSION", "GIT_EDITOR")}
            env.update(extra)
            if attach:
                pid, fd = pty.fork()
                if pid == 0:
                    os.execve(DCH, [DCH, "-E", "-n", tag] + dump, env)
                    os._exit(127)
            else:
                subprocess.run([DCH, "--spawn", tag] + argv + ["--"] + dump,
                               env=env, stdout=subprocess.DEVNULL)
            got = None
            for _ in range(100):
                if os.path.exists(out):
                    with open(out) as f:
                        got = dict(l.split("=", 1) for l in
                                   f.read().splitlines() if "=" in l)
                    break
                time.sleep(0.05)
            if attach:
                os.kill(pid, signal.SIGHUP)
                os.waitpid(pid, 0)
                os.close(fd)
            return got or {}

        def no_agent(e):
            return not any(k in e for k in agent)

        e = child_env("agsp", [], agent)
        if e and no_agent(e):
            ok("--spawn from Claude Code: caller identity + GIT_EDITOR dropped")
        else:
            bad("--spawn leaked caller agent env: %r" %
                {k: e.get(k) for k in agent})
        e = child_env("agat", [], agent, attach=True)
        if e and no_agent(e):
            ok("dch <new> from Claude Code: caller identity dropped")
        else:
            bad("dch <new> leaked caller agent env: %r" %
                {k: e.get(k) for k in agent})
        e = child_env("agenv", ["--env", "CLAUDE_CODE_SESSION_ID=keep"], agent)
        if e.get("CLAUDE_CODE_SESSION_ID") == "keep" and "CLAUDECODE" not in e:
            ok("--spawn --env CLAUDE_CODE_SESSION_ID survives the scrub")
        else:
            bad("--spawn --env lost to the scrub: %r" %
                e.get("CLAUDE_CODE_SESSION_ID"))
        e = child_env("nocc", [], {"GIT_EDITOR": "true"})
        if e.get("GIT_EDITOR") == "true":
            ok("--spawn without CLAUDECODE keeps GIT_EDITOR")
        else:
            bad("--spawn without CLAUDECODE dropped GIT_EDITOR")
    finally:
        os.system("'%s' -kl >/dev/null 2>&1" % DCH)
        shutil.rmtree(tmp, ignore_errors=True)

    if fails == 0:
        print("PASS: spawn-vs-attach (stray file, dead socket, hot path).")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
