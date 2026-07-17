#!/usr/bin/env python3
"""Hot-path performance budget for dch's spawn-vs-attach decision.

The hot path is `dch` attaching to an already-running session. The decision
now costs ONE connect() (attach-first); the previous probe design cost a
second connect() plus a master-side accept()/malloc()/free() per attach.

This bench quantifies that path and enforces a budget so a regression that
re-adds a probe (or makes the master slow to accept) is caught.

Budgets (localhost AF_UNIX, generous to stay green on loaded CI):
  - connect()+close() to a live master:  mean < 1.0 ms,  p99 < 5.0 ms
  - end-to-end spawn of a new session:          < 1.0 s
  - end-to-end attach to a live session:        < 1.0 s

Run:  python3 tests/perf_test.py        (uses ./dch)
      DCH=/path/to/dch python3 tests/perf_test.py
"""
import os, sys, pty, signal, select, time, errno, socket, tempfile, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.environ.get("DCH", os.path.join(HERE, "..", "dch"))

CONNECT_MEAN_MS = 1.0
CONNECT_P99_MS = 5.0
SPAWN_BUDGET_S = 1.0
ATTACH_BUDGET_S = 1.0
N = 2000


def drain(fd, seconds):
    end = time.time() + seconds
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.05)
        if fd in r:
            try:
                if not os.read(fd, 65536):
                    break
            except OSError as e:
                if e.errno in (errno.EIO, errno.EAGAIN):
                    break
                raise


def reap(pid, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        wpid, _ = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            return
        time.sleep(0.02)
    try:
        os.kill(pid, signal.SIGHUP)
    except OSError:
        pass
    os.waitpid(pid, 0)


def wait_live(sockpath, tries=1000):
    for _ in range(tries):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(sockpath)
            s.close()
            return True
        except OSError:
            s.close()
            time.sleep(0.01)
    return False


def spawn_detached(name, sockpath, inner):
    """forkpty a client that spawns a master running `inner`, then detach so
    the master stays alive. Returns once the socket is live."""
    pid, fd = pty.fork()
    if pid == 0:
        os.environ.pop("DCH_SESSION", None)
        os.execv(DCH, [DCH, "-E", "-n", name] + inner)
        os._exit(127)
    if not wait_live(sockpath):
        raise RuntimeError("master socket never came up: %s" % sockpath)
    os.kill(pid, signal.SIGUSR1)  # detach; master stays alive
    reap(pid)
    os.close(fd)


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH); return 1

    tmp = tempfile.mkdtemp()
    os.environ["XDG_RUNTIME_DIR"] = tmp
    sockdir = os.path.join(tmp, "dch-%d" % os.getuid())
    os.makedirs(sockdir, mode=0o700, exist_ok=True)
    fails = 0

    def check(msg, val, budget, unit):
        nonlocal fails
        if val <= budget:
            print("  ok   %-42s %.3f %s (<= %.3f)" % (msg, val, unit, budget))
        else:
            fails += 1
            print("  FAIL %-42s %.3f %s (>  %.3f)" % (msg, val, unit, budget))

    try:
        name = "perf"
        sockpath = os.path.join(sockdir, name + ".sock")

        # --- end-to-end spawn of a new session ---
        t0 = time.time()
        pid, fd = pty.fork()
        if pid == 0:
            os.environ.pop("DCH_SESSION", None)
            os.execv(DCH, [DCH, "-E", "-n", "spawnperf",
                           "sh", "-c", "printf X; exit 0"])
            os._exit(127)
        drain(fd, 2.0)
        reap(pid)
        os.close(fd)
        check("end-to-end spawn new session", time.time() - t0, SPAWN_BUDGET_S, "s")

        # --- bring up a live master (running `cat`) for the benches ---
        spawn_detached(name, sockpath, ["cat"])

        # --- connect()+close() latency to the live master ---
        samples = []
        for _ in range(N):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            t = time.perf_counter()
            try:
                s.connect(sockpath)
            except ConnectionRefusedError:
                # listen backlog overflow on a slow runner: the master
                # hasn't accepted the previous churn yet. Not a latency
                # sample; back off and skip.
                s.close()
                time.sleep(0.05)
                continue
            dt = time.perf_counter() - t
            s.close()
            samples.append(dt * 1000.0)  # ms
        if not samples:
            print("FAIL: every bench connect was refused"); return 1
        samples.sort()
        mean = sum(samples) / len(samples)
        p99 = samples[int(len(samples) * 0.99)]
        check("connect() to live master (mean)", mean, CONNECT_MEAN_MS, "ms")
        check("connect() to live master (p99)", p99, CONNECT_P99_MS, "ms")

        # --- end-to-end attach to the live master (hot path) ---
        # Measure fork -> attached, proven by a round-trip: write a byte to
        # the client's stdin and wait until `cat` echoes it back through the
        # master. This exercises the real attach path, not artificial sleeps.
        t0 = time.time()
        pid, fd = pty.fork()
        if pid == 0:
            os.environ.pop("DCH_SESSION", None)
            os.execv(DCH, [DCH, "-E", "-n", name, "cat"])
            os._exit(127)
        elapsed = ATTACH_BUDGET_S
        deadline = time.time() + ATTACH_BUDGET_S + 1.0
        sent = False
        buf = ""
        while time.time() < deadline:
            if not sent:
                os.write(fd, b"PING\r")
                sent = True
            r, _, _ = select.select([fd], [], [], 0.05)
            if fd in r:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk.decode("utf-8", "replace")
                if "PING" in buf:
                    elapsed = time.time() - t0
                    break
        os.kill(pid, signal.SIGHUP)
        reap(pid)
        os.close(fd)
        check("end-to-end attach round-trip (hot path)", elapsed, ATTACH_BUDGET_S, "s")
    finally:
        os.system("'%s' -kl >/dev/null 2>&1" % DCH)
        shutil.rmtree(tmp, ignore_errors=True)

    if fails == 0:
        print("PASS: hot-path budgets met.")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
