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

#205 (QA 1.57.0 MED-D1, LOW-D1..D3): only a git worktree in
<repo>/.claude/worktrees/<name> (its .git a git file) lifts that .claude; a
config dir that is its own git root (.claude, .codex, .kube, .docker,
.password-store), a worktrees/w symlinked to a .kube repo, a fake .git (empty,
dangling, FIFO, not "gitdir: ", a symlink, a clone's dir) refuse; the
firmlink alias /System/Volumes/Data$HOME is the home; the new names refuse;
a worktree, a nested one and ~/.config/nvim still read their lines.

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
    cases = []

    # #205 (QA 1.57.0 MED-D1): a config dir that is its own git root, out
    # of HOME (CLAUDE_CONFIG_DIR, CODEX_HOME, ...): every file refused
    for d, f in ((".claude", ".credentials.json"), (".claude", "settings.json"),
                 (".codex", "auth.json"), (".codex", "config.toml"),
                 (".kube", "config"), (".docker", "config.json"),
                 (".password-store", "notes.txt")):
        c = os.path.join(tmp, "cfg", d)
        os.makedirs(os.path.join(c, ".git"), exist_ok=True)
        put(os.path.join(c, f), '{"token":"FAKE"}\n')
        ss["xd-cfg" + d] = c
        cases.append(("a git root that is %s itself: %s" % (d, f),
                      "xd-cfg" + d, os.path.join(c, f) + ":1", "secret_name"))
    # a typed .claude/worktrees/w that is a symlink to a .kube repo (its .git
    # a dir, or a git file): the real path decides
    for k, gitfile in enumerate((False, True)):
        sk = os.path.join(tmp, "sk%d" % k, ".kube")
        os.makedirs(sk)
        if gitfile:
            put(os.path.join(sk, ".git"), "gitdir: /nonexistent/wt\n")
        else:
            os.makedirs(os.path.join(sk, ".git"))
        put(os.path.join(sk, "config"), "token: FAKE\n")
        sy = os.path.join(tmp, "sy%d" % k)
        os.makedirs(os.path.join(sy, ".git"))
        os.makedirs(os.path.join(sy, ".claude", "worktrees"))
        os.symlink(sk, os.path.join(sy, ".claude", "worktrees", "w"))
        w = os.path.join(sy, ".claude", "worktrees", "w")
        ss["xd-sym%d" % k] = w
        cases.append(("worktrees/w a symlink to a .kube repo (%s): config"
                      % ("git file" if gitfile else ".git dir"),
                      "xd-sym%d" % k, os.path.join(w, "config") + ":1",
                      "secret_name"))

    # LOW-D1: a fake .git in <proj>/.claude, <proj>/.codex and in the
    # worktree shape; a clone (a .git dir) there is no worktree either
    def fake_git(path, kind):
        if kind == "empty":
            put(path, "")
        elif kind == "dangling":
            os.symlink(os.path.join(tmp, "nonexistent"), path)
        elif kind == "fifo":
            os.mkfifo(path)
        elif kind == "notgitdir":
            put(path, "ref: refs/heads/main\n")
        elif kind == "symlink":   # to a valid git file
            put(path + "-target", "gitdir: /nonexistent/wt\n")
            os.symlink(path + "-target", path)
        else:
            os.makedirs(path)
    for kind in ("empty", "dangling", "fifo", "notgitdir"):
        for d, f in ((".claude", "settings.local.json"),
                     (".codex", "auth.json")):
            c = os.path.join(tmp, "fg-" + kind, d)
            os.makedirs(c)
            fake_git(os.path.join(c, ".git"), kind)
            put(os.path.join(c, f))
            ss["xd-fg-%s%s" % (kind, d)] = c
            cases.append(("a %s .git in <proj>/%s: %s" % (kind, d, f),
                          "xd-fg-%s%s" % (kind, d),
                          os.path.join(c, f) + ":1", "secret_name"))
    for kind in ("empty", "dangling", "fifo", "notgitdir", "symlink", "dir"):
        w = os.path.join(tmp, "fw-" + kind, ".claude", "worktrees", "w")
        os.makedirs(w)
        os.makedirs(os.path.join(tmp, "fw-" + kind, ".git"))
        fake_git(os.path.join(w, ".git"), kind)
        put(os.path.join(w, "a.txt"))
        ss["xd-fw-" + kind] = w
        cases.append(("a %s .git in .claude/worktrees/w: a.txt" % kind,
                      "xd-fw-" + kind, os.path.join(w, "a.txt") + ":1",
                      "secret_name"))

    # the exception's own shape still reads: a worktree (its .git a git
    # file), a nested one, and a git repo below ~/.config
    wr = os.path.join(tmp, "wr")
    ww = os.path.join(wr, ".claude", "worktrees", "w")
    wy = os.path.join(ww, ".claude", "worktrees", "y")
    os.makedirs(os.path.join(wr, ".git", "worktrees"))
    put(os.path.join(ww, ".git"), "gitdir: %s/.git/worktrees/w\n" % wr)
    put(os.path.join(wy, ".git"), "gitdir: %s/.git/worktrees/y\n" % wr)
    put(os.path.join(ww, "a.txt"), "worktree line\n")
    put(os.path.join(wy, "a.txt"), "nested line\n")
    for f in (".env", ".claude/settings.json", ".ssh/config",
              ".credentials.json", ".kube/config"):
        put(os.path.join(ww, f))
    put(os.path.join(wy, ".claude", "s.json"))
    put(os.path.join(home, ".config", "nvim", "init.lua"), "nvim line\n")
    os.makedirs(os.path.join(home, ".config", "nvim", ".git"))
    ss.update({"xd-wt": ww, "xd-wty": wy,
               "xd-nvim": os.path.join(home, ".config", "nvim")})
    cases += [
        ("a worktree in .claude/worktrees: its own a.txt", "xd-wt",
         "a.txt:1", "ok:worktree line"),
        ("a nested worktree: its own a.txt", "xd-wty", "a.txt:1",
         "ok:nested line"),
        ("a git repo below ~/.config (nvim): init.lua", "xd-nvim",
         "init.lua:1", "ok:nvim line"),
        ("a nested worktree: its own .claude/s.json", "xd-wty",
         os.path.join(wy, ".claude", "s.json") + ":1", "secret_name"),
        ("a nested worktree: the outer one's a.txt", "xd-wty",
         os.path.join(ww, "a.txt") + ":1", "outside_project"),
    ] + [("a worktree: its own " + f, "xd-wt",
          os.path.join(ww, f) + ":1", "secret_name")
         for f in (".env", ".claude/settings.json", ".ssh/config",
                   ".credentials.json", ".kube/config")]

    # LOW-D2: the firmlink alias /System/Volumes/Data$HOME as a session's
    # cwd (an ended Claude's record holds it as written)
    hw = os.path.join(home, ".claude", "worktrees", "w")
    put(os.path.join(hw, ".git"), "gitdir: /nonexistent/wt\n")
    put(os.path.join(hw, "README.md"), "FAKE readme\n")
    put(os.path.join(home, ".config", "foo", "token.txt"), "FAKE token\n")
    ss["xd-hw"] = hw
    cases.append(("a worktree in ~/.claude: README.md", "xd-hw",
                  "README.md:1", "secret_name"))
    fl = "/System/Volumes/Data"
    if os.path.isdir(fl) and os.path.samestat(os.stat(fl + home),
                                               os.stat(home)):
        ss.update({"xd-al-hw": fl + hw, "xd-al-home": fl + home,
                   "xd-al-cfg": fl + os.path.join(home, ".config", "foo")})
        cases += [
            ("firmlink cwd, a worktree in ~/.claude: README.md", "xd-al-hw",
             "README.md:1", "secret_name"),
            ("firmlink cwd, the same typed whole", "xd-al-hw",
             fl + os.path.join(hw, "README.md") + ":1", "secret_name"),
            ("firmlink cwd ~: notes.txt", "xd-al-home", "notes.txt:1",
             "outside_project"),
            ("firmlink cwd ~/.config/foo (no git): token.txt", "xd-al-cfg",
             "token.txt:1", "outside_project"),
        ]
    else:
        print("  skip firmlink alias: no %s alias of the home here" % fl)

    # LOW-D3: the names added
    for f in (".zsh_sessions/x.session", ".credentials.json", ".terraformrc",
              "terraform.rc", ".config/github-copilot/apps.json",
              ".databrickscfg", ".composer/auth.json", "Xauthority",
              ".ICEauthority"):
        put(os.path.join(proj, f), "token=FAKE\n")
        cases.append(("a project: " + f, "xd-proj",
                      os.path.join(proj, f) + ":1", "secret_name"))
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

    cases[:0] = [
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

    def good(r, rv, want):
        if want.startswith("ok:"):  # read: its one line
            return (len(rv) == 1 and rv[0]["state"] == "ok" and
                    rv[0]["lines"] == [want[3:]])
        return (len(rv) == 1 and rv[0]["state"] == want and
                rv[0]["lines"] == [] and "FAKE" not in r.stdout)
    try:
        r, rv = resolved(senv, "xd-proj", "a.txt:1")
        check("control: a plain project file is read (ok, its line)",
              len(rv) == 1 and rv[0]["state"] == "ok" and
              rv[0]["lines"] == ["plain line"], (r.returncode, r.stderr, rv))
        for label, n, ref, want in cases:
            r, rv = resolved(senv, n, ref)
            check("local, %s: %s" % (label, want), good(r, rv, want),
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
            check("serve, %s: %s" % (label, want), good(r, rv, want),
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
