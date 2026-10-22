"""Master respawn primitive: `dch --respawn` / `dch --cmd` (MSG_RESPAWN/MSG_CMD).

A master normally exits when its child does. Armed with `--respawn`, it keeps
the session -- socket, attached clients, terminal mirror -- across the child's
exit and either starts a new command on a fresh pty right away (argv form) or
holds childless until a second `--respawn` brings one (no argv). `--cmd`
reports the current child pid, state, argv, how the last child ended, and the
raw output it wrote since the arm.

Run:  python3 tests/respawn_test.py        (uses ./dch)
"""
import json, os, pty, select, shutil, signal, socket, struct, subprocess
import sys
import tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
# Absolute: the test chdirs into a project dir to check the respawn cwd.
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
TAG = str(os.getpid())
MIRROR = True


def run(*args, **kw):
    return subprocess.run([DCH] + list(args), capture_output=True,
                          text=True, **kw)


def spawn(name, *cmd, env=None, size="80x24"):
    subprocess.run([DCH, "--spawn", name, "--size", size] + list(cmd),
                   env=env, capture_output=True, check=True)


def cmd(name):
    r = run("--cmd", name)
    return json.loads(r.stdout) if r.returncode == 0 else None


def masters(sock):
    """pids of live masters serving `sock` (argv carries --master-of <path>)."""
    out = subprocess.run(["ps", "-eo", "pid,command"],
                         capture_output=True, text=True).stdout
    return [int(l.split()[0]) for l in out.splitlines()
            if "--master-of" in l and sock in l]


def ppid(pid):
    r = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                       capture_output=True, text=True)
    return int(r.stdout.strip()) if r.stdout.strip() else -1


def until(fn, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def screen(name):
    return run("--read", name).stdout


def seen(name, text, timeout=5.0):
    """`text` shows up on the mirror. A lite build has none to look at: give
    the child a moment, then let a --read (which drains the pty before it
    answers "no mirror") pull its output in, and trust the timing."""
    if not MIRROR:
        time.sleep(0.5)
        run("--read", name)
        return True
    return until(lambda: text in screen(name), timeout)


def gone(sock, timeout=5.0):
    return until(lambda: not os.path.exists(sock) and not masters(sock),
                 timeout)


def main():
    global MIRROR
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1

    tmp = tempfile.mkdtemp()
    os.environ["XDG_RUNTIME_DIR"] = tmp
    os.environ.pop("DCH_SESSION", None)
    os.environ.pop("DCH_SOCKET_DIR", None)
    sockdir = os.path.join(tmp, "dch-%d" % os.getuid())
    proj = os.path.join(tmp, "proj")
    os.mkdir(proj)
    os.chdir(proj)
    names = []
    fails = 0
    att_pid = None

    def check(msg, cond):
        nonlocal fails
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails += 1

    def sess(base):
        n = "%s%s" % (base, TAG)
        names.append(n)
        return n, os.path.join(sockdir, n + ".sock")

    try:
        # --- armed respawn with argv replaces the child in place ----------
        a, asock = sess("rsa")
        spawn(a, "sh", "-c", "echo first_child; sleep 1; echo bye_a; exit 3",
              size="100x30")
        MIRROR = run("--read", a).returncode != 3
        m0 = masters(asock)
        ino0 = os.lstat(asock).st_ino
        c0 = cmd(a)
        check("--cmd reports the launch argv and a running child",
              c0 and c0["argv"][0] == "sh" and c0["pid"] and
              c0["state"] == "running")
        cwd_out = os.path.join(tmp, "cwd.out")
        new = ["sh", "-c", "pwd -P > '%s'; echo second_child; stty size; "
               "sleep 30" % cwd_out]
        check("--respawn with argv arms (exit 0)",
              run("--respawn", a, "--", *new).returncode == 0)
        check("--cmd says armed", (cmd(a) or {}).get("state") == "armed")
        c1 = until(lambda: (lambda c: c if c and c["pid"] != c0["pid"]
                            else None)(cmd(a)))
        check("--cmd returns the respawned argv", c1 and c1["argv"] == new)
        check("--cmd returns the new child pid, a child of the same master",
              c1 and c1["pid"] and len(m0) == 1 and ppid(c1["pid"]) == m0[0])
        check("same master pid across the respawn", masters(asock) == m0)
        check("socket inode unchanged", os.lstat(asock).st_ino == ino0)
        check("old child's exit code reported (3)",
              c1 and c1["exit_code"] == 3 and c1["exit_signal"] is None)
        check("mirror keeps the old output and shows the new child's",
              seen(a, "second_child") and
              (not MIRROR or "bye_a" in screen(a)))
        check("new child got the launch cwd",
              until(lambda: os.path.exists(cwd_out) and
                    open(cwd_out).read().strip() == os.path.realpath(proj)))
        check("new child got the live window size", seen(a, "30 100"))

        # --- respawn survives a later live --restart -----------------------
        r = run("--restart", a)
        check("--restart after a respawn exits 0", r.returncode == 0)
        c2 = cmd(a)
        check("after --restart --cmd still reports respawned argv + pid",
              c2 and c2["argv"] == new and c2["pid"] == c1["pid"])
        check("after --restart same master pid", masters(asock) == m0)

        # --- restart refused while armed ----------------------------------
        r1, r1sock = sess("rsb")
        spawn(r1, "sh")
        run("--respawn", r1)
        r = run("--restart", r1)
        check("--restart refused while a respawn is armed (exit 1)",
              r.returncode == 1)
        run("--respawn", r1, "--cancel")
        check("--restart works again after --cancel",
              run("--restart", r1).returncode == 0)

        # --- hold: exit output stays readable, second respawn spawns ------
        h, hsock = sess("rsh")
        spawn(h, "sh", "-c",
              "echo PRE_TAIL_MARK; sleep 1; echo 'claude --resume abc-123'; "
              "echo POST_TAIL_MARK; exit 0")
        seen(h, "PRE_TAIL_MARK")
        check("--respawn without argv arms a hold",
              run("--respawn", h).returncode == 0)
        ch = until(lambda: (lambda c: c if c and c["state"] == "held"
                            else None)(cmd(h)))
        check("held: --cmd state held, pid null, exit code 0",
              ch and ch["pid"] is None and ch["exit_code"] == 0)
        check("held: exit tail has the post-arm output",
              ch and "POST_TAIL_MARK" in ch["tail"] and
              "claude --resume abc-123" in ch["tail"])
        check("held: exit tail has nothing from before the arm",
              ch and "PRE_TAIL_MARK" not in ch["tail"])
        check("held: socket still there, master alive",
              os.path.exists(hsock) and len(masters(hsock)) == 1)
        check("held: exit output readable via --read",
              not MIRROR or "claude --resume abc-123" in screen(h))
        check("held: --send is dropped, not fatal",
              run("--send", h, "dropped").returncode == 0 and
              len(masters(hsock)) == 1)
        check("held: a second hold is a no-op",
              run("--respawn", h).returncode == 0 and
              (cmd(h) or {}).get("state") == "held")
        # A WINCH while held (any connection may send one): remembered and
        # applied to the next pty.
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(hsock)
        s.sendall(struct.pack("=BH", 3, 0) + struct.pack("HHHH", 33, 111, 0, 0))
        s.close()
        time.sleep(0.2)
        check("held: second --respawn starts the child now",
              run("--respawn", h, "--", "sh", "-c",
                  "stty size; echo held_respawned; sleep 30").returncode == 0)
        chh = until(lambda: (lambda c: c if c and c["pid"] else None)(cmd(h)))
        check("held: --cmd reports the new child", chh and
              chh["state"] == "running" and chh["argv"][0] == "sh")
        check("held: tail and exit status survive the respawn",
              chh and "POST_TAIL_MARK" in chh["tail"] and
              chh["exit_code"] == 0)
        check("held: resize received while held reaches the new pty",
              seen(h, "33 111"))
        check("held: mirror shows the respawned child", seen(h, "held_respawned"))

        # --- hold timeout ends the session --------------------------------
        t, tsock = sess("rst")
        spawn(t, "sh", "-c", "sleep 0.5; exit 0",
              env=dict(os.environ, DCH_HOLD_MS="600"))
        run("--respawn", t)
        check("hold timeout ends the session", gone(tsock, 6.0))

        # --- unarmed exit still ends the session --------------------------
        u, usock = sess("rsu")
        spawn(u, "sh", "-c", "sleep 0.3; exit 0")
        run("--read", u)        # open the --spawn gate like any control verb
        check("unarmed exit ends the session", gone(usock, 5.0))

        # --- exec failure on respawn ends the session (no loop) -----------
        e, esock = sess("rse")
        spawn(e, "sh", "-c", "sleep 0.5; exit 0")
        run("--respawn", e, "--", "/nonexistent/dch-respawn-zz")
        check("exec failure on respawn ends the session", gone(esock, 5.0))

        # --- cancel: armed, cancelled, child exit ends the session --------
        k, ksock = sess("rsk")
        spawn(k, "sh", "-c", "sleep 1; exit 0")
        run("--respawn", k, "--", "sh")
        check("--respawn --cancel exits 0",
              run("--respawn", k, "--cancel").returncode == 0)
        check("cancelled arm reads running",
              (cmd(k) or {}).get("state") == "running")
        check("cancelled arm: child exit ends the session", gone(ksock, 5.0))

        # --- cancel while held ends the session ---------------------------
        kh, khsock = sess("rsq")
        spawn(kh, "sh", "-c", "sleep 0.3; exit 0")
        run("--respawn", kh)
        until(lambda: (cmd(kh) or {}).get("state") == "held")
        check("--cancel on a held session answers 0",
              run("--respawn", kh, "--cancel").returncode == 0)
        check("--cancel on a held session ends it", gone(khsock, 5.0))

        # --- arm TTL: an expired arm is no arm ----------------------------
        x, xsock = sess("rsx")
        spawn(x, "sh", "-c", "sleep 1.2; exit 0",
              env=dict(os.environ, DCH_RESPAWN_TTL_MS="300"))
        run("--respawn", x, "--", "sh")
        time.sleep(0.6)
        check("expired arm reads running",
              (cmd(x) or {}).get("state") == "running")
        check("expired arm: child exit ends the session", gone(xsock, 5.0))

        # --- grandchild holding the slave does not stall the respawn ------
        g, gsock = sess("rsg")
        holder = "37.%s" % TAG
        # The holder ignores SIGHUP, so the session leader's exit does not
        # take it down and it keeps the slave open. On Linux pty EOF then
        # never comes and only the master's waitpid() of its child can see
        # the exit; macOS revokes the tty when the session leader exits, so
        # there EOF arrives anyway and this checks the end-to-end outcome.
        spawn(g, "sh", "-c", "(trap '' HUP; exec sleep %s) & echo gc_parent; "
              "sleep 0.5; exit 0" % holder)
        run("--respawn", g, "--", "sh", "-c", "echo gc_respawned; sleep 30")
        t0 = time.time()
        cg = until(lambda: (lambda c: c if c and c["argv"][-1].endswith(
            "sleep 30") else None)(cmd(g)), 5.0)
        check("respawn happens promptly with a grandchild holding the slave",
              cg and time.time() - t0 < 4.0 and seen(g, "gc_respawned"))
        subprocess.run(["pkill", "-f", "sleep %s" % holder],
                       capture_output=True)

        # --- exit by signal is reported -----------------------------------
        sg, sgsock = sess("rss")
        spawn(sg, "sleep", "30")
        cs = cmd(sg)
        run("--respawn", sg)
        if cs and cs["pid"]:
            os.kill(cs["pid"], signal.SIGKILL)
        cs = until(lambda: (lambda c: c if c and c["state"] == "held"
                            else None)(cmd(sg)))
        check("exit by signal reported (9, code null)",
              cs and cs["exit_signal"] == 9 and cs["exit_code"] is None)

        # --- client-side bounds -------------------------------------------
        r = run("--respawn", sg, "--", "x" * 5000)
        check("over-PKT_MAX argv rejected with a clear error",
              r.returncode == 1 and "limit" in r.stderr)
        r = run("--respawn", sg, "--cancel", "--", "sh")
        check("--cancel with a command is an error", r.returncode == 1)
        r = run("--respawn", "no_such_session_zz")
        check("--respawn on a missing session exits 1", r.returncode == 1)

        # --- the dead child's terminal modes do not outlive it -------------
        md, mdsock = sess("rsm")
        spawn(md, "sh", "-c",
              "echo primary_mark; printf '\\033[?2004h\\033[?1000h"
              "\\033[>1u\\033[?1049h'; echo alt_mark; sleep 1; exit 0")
        seen(md, "alt_mark")
        run("--respawn", md, "--", "sh", "-c", "echo fresh_mark; sleep 30")
        seen(md, "fresh_mark")
        scr = screen(md)
        check("mode reset: mirror back on the primary screen",
              not MIRROR or ("primary_mark" in scr and "fresh_mark" in scr
                             and "alt_mark" not in scr))
        mpid, mfd = pty.fork()
        if mpid == 0:
            os.execv(DCH, [DCH, "-E", "-n", md])
            os._exit(127)
        rep = b""
        deadline = time.time() + 3
        while time.time() < deadline and b"fresh_mark" not in rep:
            r_, _, _ = select.select([mfd], [], [], 0.1)
            if mfd in r_:
                try:
                    chunk = os.read(mfd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                rep += chunk
        os.kill(mpid, signal.SIGHUP)
        os.waitpid(mpid, 0)
        check("mode reset: attach after respawn re-arms none of the dead "
              "child's modes", (not MIRROR or b"fresh_mark" in rep) and
              not any(m in rep for m in (b"\033[?2004h", b"\033[?1000h",
                                         b"\033[>1u", b"\033[?1049h")))

        # --- an attached client stays connected across a respawn ----------
        at, atsock = sess("rsc")
        att_pid, fd = pty.fork()
        if att_pid == 0:
            os.execv(DCH, [DCH, "-E", "-n", at, "sh", "-c",
                           "echo att_first; sleep 1; exit 0"])
            os._exit(127)
        buf = b""
        until(lambda: os.path.exists(atsock), 5.0)
        run("--respawn", at, "--", "sh", "-c",
            "echo att_second_mark; sleep 30")
        deadline = time.time() + 6
        while time.time() < deadline and b"att_second_mark" not in buf:
            r_, _, _ = select.select([fd], [], [], 0.1)
            if fd in r_:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
        check("attached client sees the respawned child's output",
              b"att_second_mark" in buf)
        check("attached client still connected after the respawn",
              os.waitpid(att_pid, os.WNOHANG) == (0, 0))

        # --- a respawned child starts with default signal dispositions ----
        # The master ignores these five after the cold child is forked, and
        # execve keeps SIG_IGN; a respawned child ignoring SIGHUP outlives
        # `dch -k`. perl reports an ignored-on-entry signal as IGNORE (on
        # Linux and macOS alike); python cannot (it ignores PIPE/XFSZ itself).
        sigs = "PIPE XFSZ HUP TTIN TTOU"
        dump = ("print join(q( ), map { qq($_=) . ($SIG{$_} // q(DFL)) } "
                "qw(%s)), qq(\\n)" % sigs)
        sig_cold = os.path.join(tmp, "sig.cold")
        sig_resp = os.path.join(tmp, "sig.resp")
        sd, sdsock = sess("rsd")
        spawn(sd, "sh", "-c", "perl -e '%s' > '%s'; sleep 1; exit 0"
              % (dump, sig_cold))
        run("--respawn", sd, "--", "sh", "-c",
            "perl -e '%s' > '%s'; exec sleep 3%s" % (dump, sig_resp, TAG))
        rd = until(lambda: os.path.exists(sig_resp) and
                   os.path.getsize(sig_resp) and open(sig_resp).read())
        cold = open(sig_cold).read() if os.path.exists(sig_cold) else ""
        check("respawned child's ignored signals equal a cold child's "
              "(cold=%r respawned=%r)" % (cold.strip(), (rd or "").strip()),
              cold and rd == cold and "IGNORE" not in cold)

        # --- `dch -k` on a respawned session leaves no child alive ---------
        ck = until(lambda: (lambda c: c if c and c["pid"] and
                            c["argv"][-1].endswith("sleep 3%s" % TAG)
                            else None)(cmd(sd)))
        check("--cmd reports the respawned child before -k", bool(ck))
        if ck:
            run("-k", sd)

            def dead(pid):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return True
                return False
            check("dch -k on a respawned session kills its child",
                  until(lambda: dead(ck["pid"]), 5.0))
            if not dead(ck["pid"]):
                os.kill(ck["pid"], signal.SIGKILL)  # our orphaned sleep

        # --- self-heal still runs while armed -----------------------------
        # Armed, the loop polls every 200 ms; that must not starve the heal
        # tick (here 1 s) for the whole arm TTL.
        hl, hlsock = sess("rsl")
        spawn(hl, "sleep", "30", env=dict(os.environ, DCH_HEAL_MS="1000"))
        run("--respawn", hl, "--", "sh")
        hlm = masters(hlsock)
        os.unlink(hlsock)
        healed = until(lambda: os.path.exists(hlsock), 3.0)
        check("armed: a deleted socket is re-bound within a heal period",
              healed)
        if not healed:
            for mp in hlm:      # unreachable for -k: stop it directly
                os.kill(mp, signal.SIGTERM)
        check("armed: the re-bound socket serves the same session",
              (cmd(hl) or {}).get("state") == "armed")
    finally:
        for n in names:
            subprocess.run([DCH, "-k", n], capture_output=True)
        if att_pid:
            try:
                os.kill(att_pid, signal.SIGHUP)
                os.waitpid(att_pid, 0)
            except OSError:
                pass
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        print("FAIL: %d respawn check(s) failed" % fails)
        return 1
    print("PASS: respawn primitive")
    return 0


if __name__ == "__main__":
    sys.exit(main())
