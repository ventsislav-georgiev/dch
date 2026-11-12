#!/usr/bin/env python3
"""The file window's guard (#178, QA 1.55.0 MED-1, LOW-2): QA's repro, with
FAKE files only, under a temp HOME.

`--xray <n> --ref <file ref> --scope session --full --json` reads a file the
transcript never mentioned. On 1.55.0's base a pane in ~ made the whole home
the project, a pane in / the whole disk, and ~/.claude/.credentials.json,
~/.config/gh/hosts.yml, .git/config, an upper-case .ENV.production and a hard
link to a file outside all came back with state "ok" and their lines. Now:
outside_project for a root that is $HOME, above it or /, and for a hard link;
secret_name for the deny list (case-insensitive, any part of the path). The
same answers come through a real `dch serve` on 127.0.0.1 (its `xray` op),
from a client that reaches it only through remote_hosts. No FAKE text may
appear in any refused answer. The sessions are ended Claude sessions (a
History record and a transcript each), so the test runs on a lite build too.

Run:  python3 tests/xray_deny_test.py        (uses ./dch)
"""
import json, os, re, shutil, signal, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from remote_client_test import FAKE_TS, OWNER, WHOIS  # noqa: E402

DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
fails = []


def check(msg, cond, extra=""):
    print("  %s %s" % ("ok  " if cond else "FAIL", msg))
    if not cond:
        fails.append(msg)
        if extra:
            print("       " + str(extra)[:900])


def main():
    # /tmp, not TMPDIR: a per-user TMPDIR can sit inside some git work tree
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="xdeny", dir="/tmp"))
    fake = os.path.join(tmp, "fake")
    os.makedirs(fake)
    with open(os.path.join(fake, "tailscale"), "w") as f:
        f.write(FAKE_TS)
    os.chmod(os.path.join(fake, "tailscale"), 0o755)
    for name, obj in (("status.json", OWNER), ("whois.json", WHOIS)):
        with open(os.path.join(fake, name), "w") as f:
            json.dump(obj, f)
    with open(os.path.join(fake, "ip.txt"), "w") as f:
        f.write("100.100.1.1\n")
    base = {k: v for k, v in os.environ.items()
            if not k.startswith(("DCH_", "MallocStackLogging", "CLAUDE"))}

    def side(name, **kw):
        d = os.path.join(tmp, name)
        os.makedirs(os.path.join(d, "home"))
        e = dict(base, XDG_RUNTIME_DIR=d, XDG_STATE_HOME=os.path.join(d, "st"),
                 HOME=os.path.join(d, "home"), DCH_LOGIN_ENV="0",
                 DCH_CONFIG=os.path.join(d, "config"), SHELL="/bin/sh",
                 PATH=fake + ":/usr/bin:/bin", TERM="xterm")
        e.update(kw)
        return e

    senv = side("srv", DCH_NO_REMOTE="1")
    cenv = side("cli")
    home = senv["HOME"]

    def put(path, text="FAKE\n"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    # the server user's home: fake credential stores and a plain file
    put(os.path.join(home, ".claude", ".credentials.json"),
        '{"token":"FAKE"}\n')
    put(os.path.join(home, ".codex", "auth.json"), '{"FAKE":1}\n')
    put(os.path.join(home, ".config", "gh", "hosts.yml"),
        "github.com: oauth_token: FAKE\n")
    put(os.path.join(home, "notes.txt"), "FAKE notes\n")
    # a project (a .git dir is its root), its secrets, a hard link out
    proj = os.path.join(tmp, "proj")
    out = os.path.join(tmp, "out")
    put(os.path.join(proj, ".git", "config"),
        "url = https://u:FAKE@example.invalid/x\n")
    put(os.path.join(proj, "up", ".ENV.production"), "TOKEN=FAKE\n")
    put(os.path.join(proj, "a.txt"), "plain line\n")
    put(os.path.join(out, "o.txt"), "FAKE outside\n")
    os.link(os.path.join(out, "o.txt"), os.path.join(proj, "hard.txt"))

    procs = []
    ss = {"xd-home": home, "xd-root": "/", "xd-proj": proj,
          "xd-claude": os.path.join(home, ".claude")}
    # ended Claude sessions (History records and transcripts): files resolve
    # against the record's cwd, on a lite build too (no shell ring there)
    sockdir = os.path.join(tmp, "srv", "dch-%d" % os.getuid())
    hdir = os.path.join(tmp, "srv", "st", "dch", "sessions",
                        sockdir.replace("/", "-"))
    pdir = os.path.join(home, ".claude", "projects", "-x")
    os.makedirs(hdir)
    os.makedirs(pdir)
    for k, (n, cwd) in enumerate(sorted(ss.items())):
        sid = "11111111-2222-4333-8444-55555555%04d" % k
        with open(os.path.join(pdir, sid + ".jsonl"), "w") as f:
            f.write(json.dumps({"type": "user", "cwd": cwd,
                                "timestamp": "2026-11-10T10:00:00Z",
                                "message": {"content": "hello"}}) + "\n")
        with open(os.path.join(hdir, n + ".json"), "w") as f:
            f.write(json.dumps({
                "name": n, "cwd": cwd, "end": "exited", "argv": ["claude"],
                "started": 1794299400, "updated": 1794300000,
                "ended": 1794300000, "sock_dir": sockdir,
                "agent": {"session_id": sid, "cwd": cwd,
                          "config_dir": os.path.join(home, ".claude")}})
                    + "\n")

    def dch(env, *args):
        return subprocess.run([DCH] + list(args), env=env, timeout=30,
                              capture_output=True, text=True,
                              stdin=subprocess.DEVNULL)

    def resolved(env, n, ref):
        r = dch(env, "--xray", n, "--ref", ref, "--scope", "session",
                "--full", "--json")
        rv = []
        for l in r.stdout.splitlines():
            try:
                o = json.loads(l)
            except ValueError:
                continue
            if "resolved" in o:
                rv.append(o["resolved"])
        return r, rv

    cases = [
        ("a pane in ~: ~/.claude/.credentials.json", "xd-home",
         "~/.claude/.credentials.json:1", "outside_project"),
        ("a pane in ~: ~/.config/gh/hosts.yml", "xd-home",
         "~/.config/gh/hosts.yml:1", "outside_project"),
        ("a pane in ~: a plain ~/notes.txt", "xd-home", "~/notes.txt:1",
         "outside_project"),
        ("a pane in /: the home's notes.txt", "xd-root",
         os.path.join(home, "notes.txt") + ":1", "outside_project"),
        ("a pane in ~/.claude: .credentials.json", "xd-claude",
         "~/.claude/.credentials.json:1", "secret_name"),
        ("a pane in ~/.codex's sibling ~/.claude: ~/.codex/auth.json",
         "xd-claude", "~/.codex/auth.json:1", "outside_project"),
        ("a project: .git/config with a token remote", "xd-proj",
         os.path.join(proj, ".git", "config") + ":1", "secret_name"),
        ("a project: up/.ENV.production (upper case)", "xd-proj",
         os.path.join(proj, "up", ".ENV.production") + ":1", "secret_name"),
        ("a project: a hard link to a file outside", "xd-proj",
         "hard.txt:1", "outside_project"),
    ]
    try:
        r, rv = resolved(senv, "xd-proj", "a.txt:1")
        check("control: a plain project file is read (ok, its line)",
              len(rv) == 1 and rv[0]["state"] == "ok" and
              rv[0]["lines"] == ["plain line"], (r.returncode, r.stderr, rv))
        for label, n, ref, want in cases:
            r, rv = resolved(senv, n, ref)
            check("local, %s: %s, no lines, no FAKE" % (label, want),
                  len(rv) == 1 and rv[0]["state"] == want and
                  rv[0]["lines"] == [] and "FAKE" not in r.stdout,
                  (r.returncode, r.stderr, rv))

        # the same through dch serve's xray op on loopback
        log = os.path.join(tmp, "serve.log")
        with open(log, "w") as lf:
            sp = subprocess.Popen([DCH, "serve", "--bind", "127.0.0.1",
                                   "--port", "0"],
                                  env=dict(senv, DCH_SERVE_TEST_LOOPBACK="1"),
                                  stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=lf)
        procs.append(sp)
        port, end = None, time.time() + 10
        while time.time() < end and port is None and sp.poll() is None:
            m = re.search(r"listening on 127\.0\.0\.1:(\d+)", open(log).read())
            port = int(m.group(1)) if m else None
            time.sleep(0.05)
        check("dch serve listens on a loopback test port", port is not None,
              open(log).read())
        if port is None:
            return 1
        with open(cenv["DCH_CONFIG"], "w") as f:
            f.write("remote_hosts = srv=127.0.0.1:%d\n" % port)
        r, rv = resolved(cenv, "srv:xd-proj", "a.txt:1")
        check("serve: the control is read (ok)",
              len(rv) == 1 and rv[0]["state"] == "ok", (r.stderr, rv))
        for label, n, ref, want in cases:
            r, rv = resolved(cenv, "srv:" + n, ref)
            check("serve, %s: %s, no lines, no FAKE" % (label, want),
                  len(rv) == 1 and rv[0]["state"] == want and
                  rv[0]["lines"] == [] and "FAKE" not in r.stdout,
                  (r.returncode, r.stderr, rv))
    finally:
        for p in procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
        shutil.rmtree(tmp, ignore_errors=True)
    print("all ok" if not fails else "%d FAILED" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
