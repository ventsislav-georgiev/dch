"""A revive under another name renames Claude's session too (#398).

`dch --revive <rec> --as <new>`, and `--revive --all`'s `<name>-N` copy,
replace the value of claude's session-name option (-n X, --name X,
--name=X, -cn X) with the new dch name when it was the record's dch name.
A name of the user's own, a record without one, and an option VALUE that
reads "-n" stay as they are.

Drives tests/fake_claude.py as `claude` (its start log has the argv) with
HOME, the runtime dir and the state dir all temporary.

Run:  python3 tests/revive_name_test.py        (uses ./dch)
"""
import json, os, shutil, stat, subprocess, sys, tempfile, time, traceback
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))


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


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="dchnm", dir=os.environ.get(
        "DCH_TEST_TMP", "/tmp")))
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
    fails = [0]

    def check(msg, cond):
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails[0] += 1

    def plant(name, args, f=None):
        """A vanished record (a SIGKILLed master): claude with args, its
        own Claude id, in file f (default name). Returns the id."""
        sid, now = str(uuid.uuid4()), int(time.time())
        r = dict(name=name, sock_dir=sockdir, alias="", cwd=proj,
                 argv=[claude] + args, started=now - 900, updated=now - 30,
                 end="", agent=dict(kind="claude", session_id=sid, name="",
                                    cwd=proj, launch=[claude] + args))
        os.makedirs(recdir, mode=0o700, exist_ok=True)
        p = os.path.join(recdir, (f or name) + ".json")
        fd = os.open(p + ".t", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        os.rename(p + ".t", p)
        return sid

    def started(session):
        """The argv the fake claude in session started with."""
        try:
            with open(os.path.join(fc, session + ".log")) as fh:
                for l in fh:
                    o = json.loads(l)
                    if o.get("ev") == "start":
                        return o["argv"]
        except (OSError, ValueError):
            pass
        return None

    try:
        # record args, what claude gets after --resume <id> under --as
        rows = [
            ("ra", ["-n", "ra"], ["-n", "ra2"]),
            ("rb", ["--name", "rb"], ["--name", "rb2"]),
            ("rc", ["--name=rc"], ["--name=rc2"]),
            ("rd", ["-cn", "rd"], ["-cn", "rd2"]),
            ("re", ["--model", "x", "--name", "mine"],
             ["--model", "x", "--name", "mine"]),
            ("rf", ["--model", "x"], ["--model", "x"]),
            # "-n" is --append-system-prompt's value, not the flag: the
            # real --name after it is the one renamed
            ("rg", ["--append-system-prompt", "-n", "--name", "rg"],
             ["--append-system-prompt", "-n", "--name", "rg2"]),
        ]
        for name, args, want in rows:
            sid = plant(name, args)
            r = run("--revive", name, "--as", name + "2", "--no-message")
            got = until(lambda: started(name + "2"), 15)
            check("--revive %s --as %s2 of claude %s: claude gets %s "
                  "[rc=%d got=%r err=%r]" % (name, name, " ".join(args),
                                             " ".join(want), r.returncode,
                                             got, r.stderr),
                  r.returncode == 0 and got == ["--resume", sid] + want)
            run("-k", name + "2", timeout=30)

        # --all: the name is taken, the copy is <name>-2 and so is claude's
        # (the live ta writes ta.json: the dead one's is moved aside)
        subprocess.run([DCH, "--spawn", "ta", "--size", "100x30", "sleep",
                        "300"], capture_output=True, check=True, timeout=30)
        sid = plant("ta", ["-n", "ta"], "ta.1793635888")
        r = run("--revive", "--all", "--no-message", "--json")
        got = until(lambda: started("ta-2"), 15)
        check("--revive --all with ta taken: ta-2's claude gets -n ta-2 "
              "[rc=%d got=%r out=%r]" % (r.returncode, got, r.stdout),
              r.returncode == 0 and got == ["--resume", sid, "-n", "ta-2"])
    except Exception:
        traceback.print_exc()
        fails[0] += 1
    finally:
        for s in os.listdir(sockdir) if os.path.isdir(sockdir) else []:
            if s.endswith(".sock"):
                run("-k", s[:-5], timeout=30)
        shutil.rmtree(tmp, ignore_errors=True)
    print("%s: %d failed" % (os.path.basename(__file__), fails[0]))
    return 1 if fails[0] else 0


if __name__ == "__main__":
    sys.exit(main())
