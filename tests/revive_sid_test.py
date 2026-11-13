"""`dch --revive` never starts a second Claude on one conversation (#064).

A cold start under a crashed session's name moves its record aside
(<name>.<ended>.json); if the new session resumes the same Claude id by
hand, two records carry one id. `--revive --all` takes the newest record of
an id and skips the rest (`dup_sid`), and skips any record whose id a live
Claude already runs (`live_sid`): one in a live session, found by probing
that session, or one outside any (a Claude that outlived its master, or
runs in a plain terminal; looked for in our Claude sessions dir and in each
record's config_dir, #065). `--revive <name> [--as <new>]` refuses such a
record (`live_sid`), naming the holder and the id.

Drives tests/fake_claude.py as `claude` with HOME, the runtime dir and the
state dir all temporary.

Run:  python3 tests/revive_sid_test.py        (uses ./dch)
"""
import json, os, pty, shutil, signal, stat, subprocess, sys, tempfile, time
import traceback, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
X = "7606da6b-bfcd-4069-a797-e3dda4898901"
Y = "0badc0de-0000-4000-8000-000000000064"
Z = "5eed5eed-0000-4000-8000-000000000064"
W = "c0f1d1c0-0000-4000-8000-000000000065"


def run(*args, timeout=120):
    return subprocess.run([DCH] + list(args), capture_output=True, text=True,
                          timeout=timeout)


def until(fn, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def ndjson(out):
    try:
        return [json.loads(l) for l in out.splitlines()]
    except ValueError:
        return None


def finals(out):
    return [o for o in ndjson(out) or [] if "result" in o]


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchsid", dir=os.environ.get("DCH_TEST_TMP", "/tmp")))
    home, fc, bindir = (os.path.join(tmp, d) for d in ("home", "fc", "bin"))
    proj, state = os.path.join(tmp, "proj"), os.path.join(tmp, "state")
    for d in (home, fc, bindir, proj):
        os.makedirs(d)
    claude = os.path.join(bindir, "claude")
    shutil.copy(os.path.join(HERE, "fake_claude.py"), claude)
    os.chmod(claude, os.stat(claude).st_mode | stat.S_IXUSR)
    with open(os.path.join(fc, "version"), "w") as f:
        f.write("1.0.0")
    for k in list(os.environ):
        if k.startswith("CLAUDE") or k.startswith("DCH_") or \
                k.startswith("Malloc"):
            del os.environ[k]
    os.environ.update(HOME=home, XDG_RUNTIME_DIR=tmp, XDG_STATE_HOME=state,
                      FAKE_CLAUDE_DIR=fc, DCH_RECORD_MS="300",
                      PATH=bindir + os.pathsep + os.environ["PATH"],
                      DCH_AGENT_STEP_MS="20000", DCH_LOGIN_ENV="0")
    sockdir = os.path.join(tmp, "dch-%d" % os.getuid())
    recdir = os.path.join(state, "dch", "sessions", sockdir.replace("/", "-"))
    os.chdir(proj)
    fails, kids = [0], []

    def check(msg, cond):
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails[0] += 1

    def rec(f):
        try:
            with open(os.path.join(recdir, f + ".json")) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def plant(f, name, sid, seen, end="", **cfg):
        """A vanished record (a SIGKILLed master) --all takes, Claude as
        its command: what p13's two crashes leave behind. end: how it
        ended instead."""
        now = int(time.time())
        r = dict(name=name, sock_dir=sockdir, alias="", cwd=proj,
                 argv=[claude], started=now - 900, updated=now - seen,
                 end=end, agent=dict(kind="claude", session_id=sid, name="",
                                    cwd=proj, launch=[claude], **cfg))
        os.makedirs(recdir, mode=0o700, exist_ok=True)
        p = os.path.join(recdir, f + ".json")
        fd = os.open(p + ".t", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        os.rename(p + ".t", p)

    def claudes(sid):
        out = subprocess.run(["/bin/ps", "-axo", "pid=,command="],
                             capture_output=True, text=True,
                             errors="replace").stdout
        return [l for l in out.splitlines()
                if claude in l and "--resume " + sid in l and
                "--master-of" not in l]

    try:
        # --- two records of one id: the newest wins -----------------------
        plant("w", "w", X, 30)
        plant("w.1793635888", "w", X, 120)
        r = run("--revive", "--all", "--no-message", "--json")
        f = finals(r.stdout)
        oks = [o for o in f if o["result"] == "ok"]
        dup = [o for o in f if o["result"] == "skipped"]
        check("--revive --all of two records with one id: one revived (w, "
              "the newest), one skipped dup_sid with the id, exit 0 "
              "[rc=%d %r]" % (r.returncode, r.stdout),
              r.returncode == 0 and len(f) == 2 and len(oks) == 1 and
              oks[0]["as"] == "w" and oks[0]["session_id"] == X and
              oks[0]["running"] is True and
              len(dup) == 1 and dup[0]["reason"] == "dup_sid" and
              dup[0]["session_id"] == X and
              "w.1793635888" in dup[0]["detail"])
        until(lambda: len(claudes(X)) >= 1, 15)
        time.sleep(1.0)
        check("...exactly one claude --resume %s running" % X[:8],
              len(claudes(X)) == 1)
        check("...the skipped record is kept (history, not deleted)",
              rec("w.1793635888") is not None)

        # --- the id is live: --all skips it, text and --json --------------
        until(lambda: ((rec("w") or {}).get("agent") or {})
              .get("session_id") == X, 15)
        # and for that Claude's own record: --revive finds a live Claude
        # only by it, and the fake writes it seconds late under load (QA
        # 1.60.0 LOW-T9; dch's side of that window is #278's report)
        until(lambda: any(os.path.exists(os.path.join(
            home, ".claude", "sessions", l.split()[0] + ".json"))
            for l in claudes(X)), 15)
        r = run("--revive", "--all", "--no-message")
        check("--revive --all with the id live in w: 'w: skipped — claude "
              "<id> already runs in live session w (record ...)', exit 0 "
              "[rc=%d %r]" % (r.returncode, r.stdout),
              r.returncode == 0 and r.stdout ==
              "w: skipped — claude %s already runs in live session w "
              "(record w.1793635888)\n" % X)
        r = run("--revive", "--all", "--no-message", "--json")
        f = finals(r.stdout)
        check("...--json: one skipped live_sid object with the id "
              "[%r]" % r.stdout,
              r.returncode == 0 and len(f) == 1 and
              f[0]["result"] == "skipped" and f[0]["reason"] == "live_sid" and
              f[0]["session_id"] == X and f[0]["running"] is True and
              "live session w" in f[0]["detail"])
        check("...still one claude on the id", len(claudes(X)) == 1)

        # --- by name: refused, with or without --as ------------------------
        r = run("--revive", "w.1793635888", "--as", "w2", "--no-message")
        check("--revive <aside> --as w2 with the id live: exit 1, stderr "
              "names live session w and the id, nothing on stdout "
              "[rc=%d %r %r]" % (r.returncode, r.stdout, r.stderr),
              r.returncode == 1 and r.stdout == "" and
              "live session w" in r.stderr and X in r.stderr)
        plant("z", "z", X, 30)
        r = run("--revive", "z", "--no-message", "--json")
        f = finals(r.stdout)
        check("--revive z (another name, same id) --json: failed live_sid "
              "naming live session w, with the id, exit 1 [%r]" % r.stdout,
              r.returncode == 1 and len(f) == 1 and
              f[0]["result"] == "failed" and f[0]["reason"] == "live_sid" and
              f[0]["session_id"] == X and "live session w" in f[0]["detail"]
              and f[0]["running"] is False)
        check("...no session z started and still one claude on the id",
              not os.path.exists(os.path.join(sockdir, "z.sock")) and
              len(claudes(X)) == 1)

        # --- a Claude outside any session (outlived its master) -----------
        pid, fd = pty.fork()
        if pid == 0:
            os.environ["DCH_SESSION"] = "gone"
            os.execv(claude, [claude, "--resume", Y])
        # pty.fork()'s master is inheritable: the next kid would hold it,
        # and closing ours would not hang this pty up (QA 1.59.0 MED-T2)
        os.set_inheritable(fd, False)
        kids.append((pid, fd))
        until(lambda: os.path.exists(os.path.join(
            home, ".claude", "sessions", "%d.json" % pid)), 15)
        plant("orph", "orph", Y, 30)
        r = run("--revive", "orph", "--no-message")
        check("--revive orph whose id a claude outside any session runs: "
              "exit 1, stderr names that claude's pid and the id "
              "[rc=%d %r]" % (r.returncode, r.stderr),
              r.returncode == 1 and "claude pid %d" % pid in r.stderr and
              Y in r.stderr and
              not os.path.exists(os.path.join(sockdir, "orph.sock")))

        # --- #065: one outside any session, under the record's config dir --
        alt = os.path.join(tmp, "alt-cfg")
        pid, fd = pty.fork()
        if pid == 0:
            os.environ.update(DCH_SESSION="gone2", CLAUDE_CONFIG_DIR=alt)
            os.execv(claude, [claude, "--resume", W])
        os.set_inheritable(fd, False)
        kids.append((pid, fd))
        until(lambda: os.path.exists(os.path.join(
            alt, "sessions", "%d.json" % pid)), 15)
        plant("orphc", "orphc", W, 30, config_dir=alt)
        r = run("--revive", "orphc", "--no-message")
        check("--revive orphc whose id a claude outside any session runs "
              "under the record's config_dir: exit 1, names that claude "
              "[rc=%d %r]" % (r.returncode, r.stderr),
              r.returncode == 1 and "claude pid %d" % pid in r.stderr and
              W in r.stderr and
              not os.path.exists(os.path.join(sockdir, "orphc.sock")))

        # --- the newest record ended on purpose: an older crash stays down
        for end in ("stopped", "killed", "exit 0"):
            E = str(uuid.uuid4())
            nm = "e" + end.replace(" ", "")
            plant(nm, nm, E, 30, end=end)
            plant(nm + ".1793635000", nm, E, 120)
            r = run("--revive", "--all", "--no-message", "--json")
            sk = [o for o in finals(r.stdout) if o.get("session_id") == E]
            check("--revive --all, newest record of an id ended %r, an "
                  "older crash of it: skipped dup_sid, nothing revived "
                  "(MED-A) [rc=%d %r]" % (end, r.returncode, r.stdout),
                  r.returncode == 0 and len(sk) == 1 and
                  sk[0]["result"] == "skipped" and
                  sk[0]["reason"] == "dup_sid" and
                  not os.path.exists(os.path.join(sockdir, nm + ".sock"))
                  and not claudes(E))

        # --- control: an id nobody runs still revives ---------------------
        plant("free", "free", Z, 30)
        r = run("--revive", "free", "--no-message")
        check("control: --revive of an id no claude runs is ok "
              "[rc=%d %r %r]" % (r.returncode, r.stdout, r.stderr),
              r.returncode == 0 and "claude resumed %s" % Z in r.stdout)
    except Exception:
        traceback.print_exc()
        fails[0] += 1
    finally:
        for pid, fd in kids:
            try:
                os.kill(pid, signal.SIGKILL)
                os.close(fd)
                end = time.time() + 10
                while not os.waitpid(pid, os.WNOHANG)[0]:
                    if time.time() > end:
                        print("  leaving fake claude %d unreaped" % pid)
                        break
                    time.sleep(0.05)
            except OSError:
                pass
        for s in os.listdir(sockdir) if os.path.isdir(sockdir) else []:
            if s.endswith(".sock"):
                run("-k", s[:-5], timeout=30)
        shutil.rmtree(tmp, ignore_errors=True)
    print("%s: %d failed" % (os.path.basename(__file__), fails[0]))
    return 1 if fails[0] else 0


if __name__ == "__main__":
    sys.exit(main())
