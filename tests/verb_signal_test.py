"""--say, --run and --revive stop at a safe point on a signal (#126).

A first SIGINT, SIGTERM or SIGHUP types nothing more, takes text already
typed back out of the composer (never an Enter) and reports why; a second
one exits at once. Drives tests/fake_claude.py as `claude` first on PATH,
with HOME, the runtime dir and the state dir temporary. The fake's
eat_enters knob keeps the text in the composer, so the verb is still in its
gate when the signal comes. Covers: --run SIGTERMed (withdrawn, exit 1,
composer empty, nothing sent); --say SIGTERMed in the parent only (it
forwards to the child that types: one withdrawn line, no child left);
--say under a Ctrl-C to its whole process group (the child does not count
the parent's forward as a second signal), also with both pending at once
(the child SIGSTOPped across the Ctrl-C); --say of text that wraps and
spans lines (the fake's Ctrl-U kills one visual row, as Claude Code's
does); a dialog over the text (draft_left: the keys went to the dialog);
a paste that never painted in the land wait (draft_left: it may still
paint); a second signal (exit 143 at
once); --revive SIGTERMed while it waits for an idle Claude (interrupted,
nothing typed) and once its message is typed (withdrawn). On dch-lite there
is no gate to stop: skipped.

Run:  python3 tests/verb_signal_test.py        (uses ./dch)
"""
import json, os, select, shutil, signal, stat, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
TAG = str(os.getpid())
# setup waits stretch under a sanitizer build on a slow runner; the bounds
# the checks assert on the verb itself do not
SLOW = 3 if os.environ.get("ASAN_OPTIONS") else 1


def run(*args, timeout=120, env=None):
    return subprocess.run([DCH] + list(args), capture_output=True, text=True,
                          timeout=timeout, env=env)


def until(fn, timeout=10.0 * SLOW):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def kids_of(pid):
    out = subprocess.run(["/bin/ps", "-A", "-o", "pid=,ppid="],
                         capture_output=True, text=True).stdout
    return [int(a) for a, b in (l.split() for l in out.splitlines())
            if int(b) == pid]


def alive(pid):
    try:
        os.kill(pid, 0)     # a probe only
    except ProcessLookupError:
        return False
    # a zombie is gone for our purpose
    st = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)],
                        capture_output=True, text=True).stdout.strip()
    return bool(st) and not st.startswith("Z")


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchvs", dir="/tmp"))
    home, fc, bindir = (os.path.join(tmp, d) for d in ("home", "fc", "bin"))
    for d in (home, fc, bindir):
        os.makedirs(d)
    os.makedirs(os.path.join(home, ".claude", "sessions"), mode=0o700)
    p = os.path.join(bindir, "claude")
    shutil.copy(os.path.join(HERE, "fake_claude.py"), p)
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
    for k in list(os.environ):
        if k.startswith("CLAUDE") or k in ("DCH_SESSION", "DCH_SOCKET_DIR",
                                           "DCH_NO_DETECT", "DCH_REVIVE_MSG",
                                           "DCH_NO_HISTORY"):
            del os.environ[k]
    os.environ.update(HOME=home, XDG_RUNTIME_DIR=tmp,
                      XDG_STATE_HOME=os.path.join(tmp, "state"),
                      FAKE_CLAUDE_DIR=fc, DCH_RECORD_MS="300",
                      DCH_LOGIN_ENV="0", DCH_AGENT_STEP_MS="30000",
                      # the gate keeps re-Entering for 25 s: the signal
                      # comes while it does
                      DCH_AGENT_REENTER_MS="5000",
                      PATH=bindir + os.pathsep + os.environ["PATH"])
    os.chdir(tmp)
    names, pids, fails = [], set(), [0]

    def check(msg, cond, why=None):
        """why: a callable giving the evidence, printed on failure only."""
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails[0] += 1
            for l in why() if why else ():
                print("       " + l[:240])

    def ev(rc, out, err, secs, **setup):
        return ["rc=%r secs=%.1f %s" % (rc, secs, " ".join(
                    "%s=%r" % kv for kv in setup.items())),
                "out=%r err=%r" % (out[-120:], err[-120:])]

    def setctl(n, **ctl):
        with open(os.path.join(fc, n + ".ctl.tmp"), "w") as f:
            json.dump(ctl, f)
        os.rename(os.path.join(fc, n + ".ctl.tmp"),
                  os.path.join(fc, n + ".ctl"))

    def sess(base, *cmd, **ctl):
        n = "%s%s" % (base, TAG)
        names.append(n)
        setctl(n, **ctl)
        subprocess.run([DCH, "--spawn", n, "--size", "100x30"] + list(cmd),
                       capture_output=True, check=True)
        return n

    def events(n):
        try:
            with open(os.path.join(fc, n + ".log")) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def starts(n):
        return [e for e in events(n) if e["ev"] == "start"]

    def typed(n):
        return [e["text"] for e in events(n) if e["ev"] == "line"]

    def screen(n):
        return run("--read", n).stdout

    def up(n, k=0):
        s = until(lambda: len(starts(n)) > k and starts(n)[k], 15 * SLOW)
        if s:
            pids.add(s["pid"])
            until(lambda: os.path.exists(os.path.join(
                home, ".claude", "sessions", "%d.json" % s["pid"])),
                15 * SLOW)
            until(lambda: lite or "auto mode on" in screen(n), 15 * SLOW)
        return s

    def composer(n, text):
        """The composer line holds text."""
        return any(l.startswith("❯") and text in l.replace("\xa0", " ")
                   for l in screen(n).splitlines())

    def start(*args, group=False, env=None):
        return subprocess.Popen([DCH] + list(args), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                start_new_session=group, env=env)

    def finish_p(pr, timeout=15, pre=""):
        """pre: stdout read_until() already took."""
        t0 = time.time()
        try:
            out, err = pr.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            pr.kill()
            out, err = pr.communicate()
        return pr.returncode, pre + out, err, time.time() - t0

    def read_until(pr, needle, timeout):
        """pr's stdout up to the chunk holding needle, without blocking
        past timeout (raw reads: communicate() takes the rest after)."""
        fd, buf = pr.stdout.fileno(), b""
        deadline = time.time() + timeout
        while needle.encode() not in buf and time.time() < deadline:
            if select.select([fd], [], [], 0.1)[0]:
                chunk = os.read(fd, 4096)
                if not chunk:
                    break
                buf += chunk
        return buf.decode()

    def pasted(n):
        """The fake has the paste's start (it logs it before its lag)."""
        return any(e["ev"] == "paste" for e in events(n))

    lite = False
    try:
        probe = sess("vprobe", "sh")
        lite = run("--read", probe).returncode == 3
        run("-k", probe)
        names.remove(probe)
        if lite:
            print("  skip dch-lite: no gate to stop")
            return finish(fails[0], tmp, names, pids)

        # --- --run, SIGTERM once the text is in the composer -------------
        a = sess("vrun", "claude", eat_enters=99)
        sa = up(a)
        pr = start("--run", a, "hello", "from", "run")
        held = until(lambda: composer(a, "hello from run"))
        pr.send_signal(signal.SIGTERM)
        rc, out, err, secs = finish_p(pr)
        check("--run SIGTERMed mid-gate: exit 1, `withdrawn` (%.1f s)" % secs,
              held and rc == 1 and err.startswith("dch: %s: withdrawn — " % a)
              and secs < 10,
              lambda: ev(rc, out, err, secs, up=bool(sa), held=held))
        check("--run: the composer is empty, nothing was sent",
              until(lambda: not composer(a, "hello from run"), 3) and
              typed(a) == [],
              lambda: ["typed=%r screen=%r" % (typed(a), screen(a)[-160:])])

        # --- --say, SIGTERM to the parent only: it forwards ---------------
        b = sess("vsay", "claude", eat_enters=99)
        sb = up(b)
        pr = start("--say", b, "--json", "--", "hello", "from", "say")
        held = until(lambda: composer(b, "hello from say"))
        kids = kids_of(pr.pid)
        pr.send_signal(signal.SIGTERM)
        rc, out, err, secs = finish_p(pr)
        lines = [json.loads(l) for l in out.splitlines() if l.strip()]
        check("--say SIGTERMed (parent only): exit 1, one `withdrawn` line "
              "(%.1f s)" % secs,
              held and rc == 1 and len(lines) == 1 and
              lines[0].get("reason") == "withdrawn" and
              lines[0].get("result") == "failed" and secs < 10,
              lambda: ev(rc, out, err, secs, up=bool(sb), held=held))
        check("--say: its child is gone with it, the composer is empty, "
              "nothing was sent",
              len(kids) == 1 and not any(alive(k) for k in kids) and
              until(lambda: not composer(b, "hello from say"), 3) and
              typed(b) == [],
              lambda: ["kids=%r alive=%r typed=%r screen=%r" % (
                  kids, [alive(k) for k in kids], typed(b),
                  screen(b)[-120:])])

        # --- --say under a Ctrl-C to its process group -------------------
        pr = start("--say", b, "--json", "--", "hello", "again", group=True)
        held = until(lambda: composer(b, "hello again"))
        try:
            os.killpg(pr.pid, signal.SIGINT)
        except OSError:
            pass        # it already ended (a draft refused it)
        rc, out, err, secs = finish_p(pr)
        lines = [json.loads(l) for l in out.splitlines() if l.strip()]
        check("--say Ctrl-C (its group): one signal, not two: `withdrawn`, "
              "composer empty (%.1f s)" % secs,
              held and rc == 1 and len(lines) == 1 and
              lines[0].get("reason") == "withdrawn" and
              until(lambda: not composer(b, "hello again"), 3) and
              typed(b) == [],
              lambda: ev(rc, out, err, secs, held=held, typed=typed(b)))

        # --- --say: text that wraps and spans lines (#126 round 2) --------
        # Claude Code's Ctrl-U kills one visual row (or the newline before
        # it), so taking this back needs a Ctrl-U per row and per newline
        long_ = ("a message long enough to wrap past the hundred column "
                 "pane width of this session, so that its first line takes "
                 "two visual rows in the composer")
        pr = start("--say", b, "--json", "--", long_, "\nline two\nline",
                   "three")
        held = until(lambda: composer(b, "a message long enough"))
        pr.send_signal(signal.SIGTERM)
        rc, out, err, secs = finish_p(pr)
        lines = [json.loads(l) for l in out.splitlines() if l.strip()]
        check("--say of wrapped, multi-line text SIGTERMed: `withdrawn`, "
              "composer empty, nothing sent (%.1f s)" % secs,
              held and rc == 1 and len(lines) == 1 and
              lines[0].get("reason") == "withdrawn" and secs < 10 and
              until(lambda: not composer(b, "a message long enough"), 3) and
              typed(b) == [],
              lambda: ev(rc, out, err, secs, held=held, typed=typed(b)))

        # --- --say: the child holds the tty's SIGINT and the forward at once
        # (a Ctrl-C to the group while the child is not scheduled): it must
        # not read them as two signals. SIGSTOP makes the race certain.
        pr = start("--say", b, "--json", "--", "hello", "both", group=True)
        held = until(lambda: composer(b, "hello both"))
        kids = kids_of(pr.pid)
        if held and len(kids) == 1:
            os.kill(kids[0], signal.SIGSTOP)
            try:
                os.killpg(pr.pid, signal.SIGINT)
            except OSError:
                pass
            # the parent's forward is pending too (a parent slower than
            # this sends it after SIGCONT: the plain group case above)
            time.sleep(1.0)
            os.kill(kids[0], signal.SIGCONT)
        rc, out, err, secs = finish_p(pr)
        lines = [json.loads(l) for l in out.splitlines() if l.strip()]
        check("--say child with the group's SIGINT and the forward pending "
              "together: `withdrawn`, not killed (%.1f s)" % secs,
              held and len(kids) == 1 and rc == 1 and len(lines) == 1 and
              lines[0].get("reason") == "withdrawn" and
              until(lambda: not composer(b, "hello both"), 3) and
              typed(b) == [],
              lambda: ev(rc, out, err, secs, held=held, kids=kids,
                         typed=typed(b)))

        # --- a dialog over the composer when the signal comes ------------
        # the Ctrl-Us go to the dialog and the text sits under it
        # (the fake raises it at the first Ctrl-U: a dialog put up from
        # here while --run polls its Enter reads as "sent", as a real tool
        # prompt after a taken message does, so the order must be fixed)
        d = sess("vdlg", "claude", eat_enters=99, dialog_on_kill=True)
        sd = up(d)
        pr = start("--run", d, "hello", "under", "a", "dialog")
        held = until(lambda: composer(d, "hello under a dialog"))
        pr.send_signal(signal.SIGTERM)
        rc, out, err, secs = finish_p(pr)
        check("--run SIGTERMed with a dialog over its text: `draft_left`, "
              "not `withdrawn` (%.1f s)" % secs,
              held and rc == 1 and
              err.startswith("dch: %s: draft_left — " % d) and
              "Do you want to proceed?" in screen(d) and
              typed(d) == [],
              lambda: ev(rc, out, err, secs, up=bool(sd), held=held,
                         typed=typed(d), screen=screen(d)[-80:]))

        # --- a paste that has not painted when the signal comes, nor by
        # the end of the land wait: it may still paint, so `draft_left`.
        # The signal waits for the fake to have the paste: one before
        # anything is typed is `interrupted`, another case.
        e = sess("vlag", "claude", lag_ms=4000)
        se = up(e)
        pr = start("--run", e, "late", "paint",
                   env=dict(os.environ, DCH_AGENT_LAND_MS="1500"))
        held = until(lambda: pasted(e))
        pr.send_signal(signal.SIGTERM)
        rc, out, err, secs = finish_p(pr)
        check("--run SIGTERMed before its paste painted (nor by the end of "
              "the land wait): `draft_left`, not `not_typed` (%.1f s)" % secs,
              held and rc == 1 and
              err.startswith("dch: %s: draft_left — " % e) and
              typed(e) == [],
              lambda: ev(rc, out, err, secs, up=bool(se), pasted=held,
                         typed=typed(e)))

        # --- a second signal exits at once --------------------------------
        # both land while the fake sleeps its 6 s lag on the paste's start:
        # the verb is armed (it typed) and still waits for the paste
        c = sess("vtwice", "claude", lag_ms=6000)
        sc = up(c)
        pr = start("--run", c, "slow", "paste")
        held = until(lambda: pasted(c))
        pr.send_signal(signal.SIGTERM)
        time.sleep(0.3)
        pr.send_signal(signal.SIGTERM)
        rc, out, err, secs = finish_p(pr)
        check("a second SIGTERM: exit 143 at once (%.1f s)" % secs,
              held and rc == 143 and secs < 5,
              lambda: ev(rc, out, err, secs, up=bool(sc), pasted=held))

        # --- --revive: SIGTERM while it waits for an idle Claude ---------
        def ended(base, **ctl):
            n = sess(base, "claude")
            s = up(n)
            rp = os.path.join(tmp, "state", "dch", "sessions",
                              os.path.join(tmp, "dch-%d" % os.getuid())
                              .replace("/", "-"), n + ".json")

            def rec():
                try:
                    with open(rp) as f:
                        return json.load(f)
                except (OSError, ValueError):
                    return {}
            until(lambda: (rec().get("agent") or {}).get("session_id")
                  == s["sid"], 15 * SLOW)
            setctl(n, **ctl)
            run("-k", n)
            until(lambda: rec().get("ended", 0) > 0, 15 * SLOW)
            return n

        def status_of(s):
            """The fake's own record: what --revive reads for idle."""
            try:
                with open(os.path.join(home, ".claude", "sessions",
                                       "%d.json" % s["pid"])) as f:
                    return json.load(f).get("status")
            except (OSError, ValueError, TypeError):
                return None

        # the signal lands only once revive is provably in its wait: it
        # printed `resuming` (revive_message is next), the second fake is
        # up and busy (so the wait cannot end before the signal)
        r1 = ended("vrvbusy", busy=True)
        pr = start("--revive", r1, "--json")
        pre = read_until(pr, '"phase":"resuming"', 30 * SLOW)
        s1 = up(r1, 1)
        busy = s1 and until(lambda: status_of(s1) == "busy", 15 * SLOW)
        pr.send_signal(signal.SIGTERM)
        rc, out, err, secs = finish_p(pr, pre=pre)
        fin = [json.loads(l) for l in out.splitlines() if l.strip()]
        fin = [l for l in fin if "phase" not in l]
        check("--revive SIGTERMed waiting for idle: exit 1, final line "
              "`interrupted`, nothing typed (%.1f s)" % secs,
              '"phase":"resuming"' in pre and s1 and busy and rc == 1 and
              len(fin) == 1 and fin[0].get("reason") == "interrupted" and
              secs < 10 and
              not [e for e in events(r1) if e["ev"] == "line" and
                   e["pid"] == s1["pid"]],
              lambda: ev(rc, out, err, secs, s1=s1 and s1["pid"], busy=busy,
                         resuming='"phase":"resuming"' in pre,
                         typed=typed(r1)))

        r2 = ended("vrveat", eat_enters=99)
        pr = start("--revive", r2, "--json")
        s2 = up(r2, 1)
        held = until(lambda: composer(r2, "resume all work"), 20 * SLOW)
        pr.send_signal(signal.SIGTERM)
        rc, out, err, secs = finish_p(pr)
        fin = [json.loads(l) for l in out.splitlines() if l.strip()]
        fin = [l for l in fin if "phase" not in l]
        check("--revive SIGTERMed once its message is typed: `withdrawn`, "
              "composer empty, nothing sent (%.1f s)" % secs,
              s2 and held and rc == 1 and len(fin) == 1 and
              fin[0].get("reason") == "withdrawn" and secs < 10 and
              until(lambda: not composer(r2, "resume all work"), 3) and
              not [e for e in events(r2) if e["ev"] == "line" and
                   e["pid"] == s2["pid"]],
              lambda: ev(rc, out, err, secs, s2=s2 and s2["pid"], held=held,
                         typed=typed(r2)))
        return finish(fails[0], tmp, names, pids)
    except Exception:
        import traceback
        traceback.print_exc()
        finish(fails[0], tmp, names, pids)
        return 1


def finish(fails, tmp, names, pids):
    for n in names:
        subprocess.run([DCH, "-k", n], capture_output=True)
    time.sleep(0.3)
    for p in pids:          # safety net: only the fakes this test started
        try:
            os.kill(p, 0)
            os.kill(p, 9)
        except OSError:
            pass
    shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print("FAIL: %d signal check(s) failed" % fails)
        return 1
    print("PASS: --say/--run/--revive stop safely on a signal")
    return 0


if __name__ == "__main__":
    sys.exit(main())
