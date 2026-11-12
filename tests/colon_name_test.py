#!/usr/bin/env python3
"""A local session named a:b stays local (#122, v1.41.0 QA MED-1).

`dch -n proj:a` makes a local session whose name has a ':'. A control verb
takes host:name remote only when the part before the ':' is a known host
(--hosts, remote_hosts, a cached remote row) and no local session or History
record has that exact name. Each mode below runs the whole life of proj:a:
-n, -k, --revive (plain and the picker's <file>.json --json), --pin,
--unpin, Enter in the picker's History tab, --forget; and --spawn of a new
a:b name:
  off      DCH_NO_REMOTE=1
  cfgoff   remote_hosts = off
  unknown  remote on, remote_hosts names another host only
  known    remote_hosts names proj (127.0.0.1:1, refused at once): proj:a
           has a local record, so it stays local; a name with none still
           goes remote (exit 7, unreachable) - routing is not lost.
A missing proj:zz is a local "no session" for the control verbs; only with
remote on (unknown) is it followed by one hint line naming --host. -a keeps
discovery while remote is on, and is local with remote off.
No host is ever dialled but loopback port 1, and no tailscale runs (the
configs name hosts, so there is no discovery; PATH has no tailscale).

Run:  python3 tests/colon_name_test.py            (uses ./dch)
"""
import json, os, pty, re, shutil, signal, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
ROOT = tempfile.mkdtemp(prefix="dchcolon", dir="/tmp")
failures = []
ROW_RE = re.compile("^([>▌ ]) . (\\S+)")
RULE_RE = re.compile("^[─-]{20,}$")

MODES = {
    "off": ({"DCH_NO_REMOTE": "1"}, ""),
    "cfgoff": ({}, "remote_hosts = off\n"),
    "unknown": ({}, "remote_hosts = other=127.0.0.1:1\n"),
    "known": ({}, "remote_hosts = proj=127.0.0.1:1\n"),
}


def check(what, ok, *extra):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)
        for e in extra:
            print("     " + repr(e))


def mkenv(mode):
    extra, cfg = MODES[mode]
    d = os.path.join(ROOT, mode)
    for sub in ("s", "state", "home"):
        os.makedirs(os.path.join(d, sub))
    with open(os.path.join(d, "config"), "w") as f:
        f.write(cfg)
    env = dict(os.environ, DCH_SOCKET_DIR=os.path.join(d, "s"),
               XDG_STATE_HOME=os.path.join(d, "state"),
               HOME=os.path.join(d, "home"),
               DCH_CONFIG=os.path.join(d, "config"), DCH_LOGIN_ENV="0",
               PATH="/usr/bin:/bin", TERM="xterm-256color")
    for k in ("DCH_SESSION", "DCH_NO_REMOTE", "MallocStackLogging",
              "MallocStackLoggingNoCompact", "NO_COLOR", "COLORTERM"):
        env.pop(k, None)
    env.update(extra)
    return env


def run(env, *args):
    return subprocess.run([DCH] + list(args), env=env, capture_output=True,
                          text=True, timeout=60)


def live(env, name):
    return name in run(env, "-ls").stdout.split("\n")


def wait_live(env, name, present, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if live(env, name) == present:
            return True
        time.sleep(0.2)
    return False


def wait_ended(env, name, timeout=10.0):
    """-k unlinks the socket and returns; the master writes the record's end
    later, at exit. Wait for it, so a --forget after is not undone."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            h = json.loads(run(env, "--history", "--json").stdout or "[]")
        except ValueError:
            h = []
        # a not-live record whose end is not written yet reads "vanished"
        if h and not any(r.get("name") == name and
                         r.get("end") in (None, "", "vanished") for r in h):
            return True
        time.sleep(0.2)
    return False


def new_attached(env, name):
    """`dch -n name /bin/sh` on a pty, detached once the session is up."""
    pid, fd = pty.fork()
    if pid == 0:
        os.execve(DCH, [DCH, "-n", name, "/bin/sh"], env)
    up = wait_live(env, name, True)
    time.sleep(0.3)
    os.write(fd, b"\x1c")
    end = time.time() + 10
    st = None
    while time.time() < end:
        try:
            os.read(fd, 65536)
        except OSError:
            pass
        p, st = os.waitpid(pid, os.WNOHANG)
        if p:
            break
        time.sleep(0.05)
    else:
        os.kill(pid, signal.SIGKILL)
        os.close(fd)
        os.waitpid(pid, 0)
        fd = None
    if fd is not None:
        os.close(fd)
    return up and st is not None and os.WIFEXITED(st) and \
        os.WEXITSTATUS(st) == 0


def screen(env, pk):
    return run(env, "--read", pk).stdout


def wait_screen(env, pk, pred, timeout=10.0):
    end = time.time() + timeout
    s = ""
    while time.time() < end:
        s = screen(env, pk)
        if pred(s):
            return s
        time.sleep(0.2)
    return s


def body(s):
    out = []
    for l in s.split("\n")[3:]:
        if RULE_RE.match(l):
            break
        out.append(l)
    return out


def rows(s):
    return [m.group(2) for m in map(ROW_RE.match, body(s)) if m]


def current(s):
    for m in map(ROW_RE.match, body(s)):
        if m and m.group(1) != " ":
            return m.group(2)
    return None


def picker_revive(env, name):
    """Enter on name's row in the picker's History tab: Resumed as name."""
    pk = "pkc"
    run(env, "--spawn", pk, "--size", "100x30", "env", "NO_COLOR=1", DCH,
        "-l")
    wait_screen(env, pk, lambda s: "History " in s)
    for _ in range(6):
        if "[History" in screen(env, pk):
            break
        run(env, "--keys", pk, "right")
        time.sleep(0.3)
    end = time.time() + 10
    while time.time() < end:
        s = screen(env, pk)
        r, cur = rows(s), current(s)
        if cur == name:
            break
        if cur in r and name in r:
            d = r.index(name) - r.index(cur)
            run(env, "--keys", pk, *(["down"] * d if d > 0 else ["up"] * -d))
        time.sleep(0.3)
    run(env, "--keys", pk, "enter")
    s = wait_screen(env, pk, lambda s: "Resumed as %s" % name in s, 20)
    run(env, "-k", pk)
    return "Resumed as %s" % name in s, s


def one_mode(mode):
    env = mkenv(mode)
    t = "[%s] " % mode
    a = "proj:a"
    check(t + "dch -n proj:a makes a local session", new_attached(env, a))
    r = run(env, "-k", a)
    check(t + "-k proj:a", r.returncode == 0 and wait_live(env, a, False), r)
    r = run(env, "--revive", a)
    check(t + "--revive proj:a revives it here",
          r.returncode == 0 and wait_live(env, a, True), r)
    run(env, "-k", a)
    wait_live(env, a, False)
    r = run(env, "--revive", a + ".json", "--json")
    check(t + "--revive proj:a.json --json (the picker's form)",
          r.returncode == 0 and '"result":"ok"' in r.stdout
          and wait_live(env, a, True), r)
    run(env, "-k", a)
    wait_live(env, a, False)
    r = run(env, "--pin", a, "--json")
    check(t + "--pin proj:a", r.returncode == 0 and '"pinned"' in r.stdout, r)
    r = run(env, "--unpin", a, "--json")
    check(t + "--unpin proj:a",
          r.returncode == 0 and '"unpinned"' in r.stdout, r)
    if run(env, "--build").stdout.split()[:1] != ["full"]:
        print("skip " + t + "the picker (build has no terminal mirror)")
    else:
        ok, s = picker_revive(env, a)
        check(t + "the picker's History Enter revives proj:a",
              ok and wait_live(env, a, True), s)
        run(env, "-k", a)
        wait_live(env, a, False)
    wait_ended(env, a)
    r = run(env, "--forget", a, "--json")
    h = run(env, "--history", "--json").stdout
    check(t + "--forget proj:a", r.returncode == 0 and
          '"forgotten"' in r.stdout and '"proj:a"' not in h, r, h)
    if mode != "known":
        r = run(env, "--spawn", "proj:b", "/bin/sh")
        check(t + "--spawn proj:b is local",
              r.returncode == 0 and wait_live(env, "proj:b", True), r)
        r = run(env, "--status", "proj:b")
        check(t + "--status proj:b answers here", r.returncode == 0, r)
        run(env, "-k", "proj:b")
        hint = "(proj is not a known host; --host proj to reach it)\n"
        r = run(env, "--status", "proj:zz")
        check(t + "a missing proj:zz is a local no-such-session (exit 1)",
              r.returncode == 1 and "no session: proj:zz" in r.stderr, r)
        # the hint only while remote is on: unknown, not off / cfgoff
        check(t + "the not-a-known-host hint " +
              ("follows it" if mode == "unknown" else "is absent"),
              r.stderr.endswith(hint) if mode == "unknown"
              else "not a known host" not in r.stderr, r)
        r = run(env, "--spawn", "proj:c", "/bin/sh")
        check(t + "a local --spawn that works has no hint",
              r.returncode == 0 and "not a known host" not in r.stderr, r)
        run(env, "-k", "proj:c")
        r = run(env, "-a", "proj:zz")
        if mode == "unknown":	# -a keeps discovery: the remote error
            check(t + "-a proj:zz: not a known host (known: other), exit 1",
                  r.returncode == 1 and "proj is not a known host (known: "
                  "other)" in r.stderr and "no session" not in r.stderr, r)
        else:
            check(t + "-a proj:zz with remote off: no session (exit 1)",
                  r.returncode == 1 and r.stderr == "no session: proj:zz\n",
                  r)
    else:
        r = run(env, "--spawn", "proj:b", "/bin/sh")
        check(t + "--spawn proj:b with no local record goes to proj (7)",
              r.returncode == 7 and "proj: unreachable" in r.stderr
              and not live(env, "proj:b"), r)
        r = run(env, "--status", "PROJ:zz")
        check(t + "the host part is case-insensitive, as -a's (7)",
              r.returncode == 7 and "unreachable" in r.stderr, r)
        r = run(env, "-a", "proj:zz")
        check(t + "-a proj:zz goes to proj (refused, exit 1)",
              r.returncode == 1 and "proj: Connection refused" in r.stderr
              and "no session" not in r.stderr, r)


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL dch not executable at " + DCH)
        return 1
    try:
        for mode in MODES:
            one_mode(mode)
    finally:
        for mode in MODES:
            d = os.path.join(ROOT, mode)
            if os.path.isdir(d):
                env = mkenv_existing(d)
                for n in ("proj:a", "proj:b", "proj:c", "pkc"):
                    run(env, "-k", n)
        shutil.rmtree(ROOT, ignore_errors=True)
    print("%d failure(s)" % len(failures))
    return 1 if failures else 0


def mkenv_existing(d):
    return dict(os.environ, DCH_SOCKET_DIR=os.path.join(d, "s"),
                XDG_STATE_HOME=os.path.join(d, "state"),
                HOME=os.path.join(d, "home"), DCH_NO_REMOTE="1",
                DCH_CONFIG=os.path.join(d, "config"), DCH_LOGIN_ENV="0")


if __name__ == "__main__":
    sys.exit(main())
