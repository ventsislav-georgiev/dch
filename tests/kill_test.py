"""-k and --stop signal exactly the session's master (#060, #027).

The master is found by pid (the socket's listener, else an exact argv scan),
never by a `pkill -f` regex over the command line. Covers: -k/--stop `app`
leaves `app-sock-server` running; names with ERE metacharacters and a `'`
stop and are gone; a dch copy under another name spawns and -k's its master;
a master whose socket no longer answers (a dead node in its place) is still
found by its argv, and only it.

Run:  python3 tests/kill_test.py        (uses ./dch)
"""
import os, shutil, socket, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))


def main():
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchk", dir="/tmp"))
    for k in list(os.environ):
        if k.startswith("DCH_"):
            del os.environ[k]
    os.environ.update(XDG_RUNTIME_DIR=tmp, DCH_LOGIN_ENV="0",
                      XDG_STATE_HOME=os.path.join(tmp, "state"),
                      HOME=os.path.join(tmp, "home"))
    os.makedirs(os.environ["HOME"])
    sockdir = os.path.join(tmp, "dch-%d" % os.getuid())
    # #027: a copy whose name has no "dch" before " --master-of"
    other = os.path.join(tmp, "sessiond")
    shutil.copy(DCH, other)
    fails = []

    def check(msg, cond):
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails.append(msg)

    def masters(n, tail=".sock -- "):
        """pids whose argv is `<exe> --master-of <n's socket> -- ...`."""
        sp = os.path.join(sockdir, n + tail)
        out = subprocess.run(["ps", "-axo", "pid=,args="], text=True,
                             capture_output=True).stdout
        return [int(l.split(None, 1)[0]) for l in out.splitlines()
                if " --master-of " + sp in l]

    def spawn(n, exe=DCH):
        r = subprocess.run([exe, "--spawn", n, "--size", "80x24", "sh"],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0 and masters(n)

    def gone(n, t=5.0):
        end = time.time() + t
        while masters(n) and time.time() < end:
            time.sleep(0.05)
        return not masters(n) and \
            not os.path.exists(os.path.join(sockdir, n + ".sock"))

    def end(how, n, exe=DCH):
        return subprocess.run([exe, how, n], capture_output=True, text=True,
                              timeout=30)

    try:
        for how in ("-k", "--stop"):
            # sibling: `app` as an ERE also matches `app-sock-server`'s path
            sib = spawn("app") and spawn("app-sock-server")
            r = end(how, "app")
            check("%s app: exit 0, app gone" % how,
                  sib and r.returncode == 0 and gone("app"))
            time.sleep(0.3)
            check("%s app leaves app-sock-server running" % how,
                  sib and masters("app-sock-server") and
                  os.path.exists(os.path.join(sockdir,
                                              "app-sock-server.sock")))
            end("-k", "app-sock-server")
            for n in ("fix(ui)", "it's", "a.b*[c]$"):
                ok = spawn(n)
                r = end(how, n)
                check("%s %s: exit 0, master gone" % (how, n),
                      ok and r.returncode == 0 and gone(n))

        # #027: spawn + -k through a renamed binary
        ok = spawn("ren", other)
        r = end("-k", "ren", other)
        check("renamed binary: -k exits 0, master gone",
              ok and r.returncode == 0 and gone("ren"))

        # no listener answers: a dead socket node where the master's was
        ok = spawn("fb+1") and spawn("fbb1")
        sp = os.path.join(sockdir, "fb+1.sock")
        os.rename(sp, os.path.join(tmp, "moved.sock"))
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(sp)
        s.close()
        r = end("-k", "fb+1")
        check("dead socket: -k finds the master by argv, master gone",
              ok and r.returncode == 0 and gone("fb+1"))
        check("dead socket: -k fb+1 leaves fbb1 running",
              ok and masters("fbb1"))
    finally:
        for p in masters("", ""):
            os.kill(p, 15)
        shutil.rmtree(tmp, ignore_errors=True)
    print("%d failed" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
