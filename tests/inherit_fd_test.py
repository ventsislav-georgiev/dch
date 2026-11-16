"""A master spawned from a caller with extra open fds does not keep them (#400).

The caller (think: Claude inside another session running `dch --spawn`)
hands an inheritable pipe write end and a pty slave to `dch --spawn`. After
the spawn returns and the caller closes its copies, the pipe must read EOF
(neither the master nor the session's program holds the write end) and,
where lsof exists, no process may hold the pty slave.

Run:  python3 tests/inherit_fd_test.py        (uses ./dch)
"""
import os, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))


def main():
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchfd", dir=os.environ.get("DCH_TEST_TMP", "/tmp")))
    for k in list(os.environ):
        if k.startswith("DCH_"):
            del os.environ[k]
    os.environ.update(XDG_RUNTIME_DIR=tmp, DCH_LOGIN_ENV="0",
                      XDG_STATE_HOME=os.path.join(tmp, "state"),
                      HOME=os.path.join(tmp, "home"))
    os.makedirs(os.environ["HOME"])
    fails = []

    def check(msg, cond):
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails.append(msg)

    m, s = os.openpty()
    slave = os.ttyname(s)
    r, w = os.pipe()
    os.dup2(s, 60)
    os.dup2(w, 61)
    os.close(s)
    os.close(w)
    try:
        p = subprocess.run([DCH, "--spawn", "fd", "--size", "80x24",
                            "sleep", "300"], pass_fds=(60, 61),
                           capture_output=True, text=True, timeout=30)
        os.close(60)
        os.close(61)
        check("spawn exits 0", p.returncode == 0)
        os.set_blocking(r, False)
        eof = False
        end = time.time() + 3
        while time.time() < end and not eof:
            try:
                eof = os.read(r, 1) == b""
            except BlockingIOError:
                time.sleep(0.05)
        check("inherited pipe reads EOF: master and program dropped it", eof)
        lsof = shutil.which("lsof") or ("/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else None)
        if lsof:
            held = subprocess.run([lsof, "-t", slave], capture_output=True,
                                  text=True).stdout.split()
            check("nobody holds the caller's pty slave %s (held by %s)"
                  % (slave, held or "none"), not held)
        else:
            print("  SKIP pty slave check: no lsof")
    finally:
        subprocess.run([DCH, "-k", "fd"], capture_output=True, timeout=30)
        os.close(m)
        os.close(r)
        shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print("FAILED: %d" % len(fails))
        sys.exit(1)
    print("all ok")


if __name__ == "__main__":
    main()
