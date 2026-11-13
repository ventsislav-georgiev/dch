#!/usr/bin/env python3
"""`--xray <n> --ref <text> --full --json`'s resolved line (#159, XR6, K13).

Spec #832 v3.2.1 3.7 and 8.3: GitCommitResolver and FileResolver run in the
querying process. This drives them through real sessions and a real git (the
one 8.3's probe picks, skipped when there is none): a live shell, a live
Claude (tests/fake_claude.py) and an ended Claude from History; every state a
real git can give; light streams carry none; the line sits just before
`done`; an ended session never runs git; and the sentinel test: a hostile
repo (fsmonitor, pager, aliases, hooks, textconv, filters, gpg.program with
log.showSignature, a promisor remote over ext::, trace2) runs nothing,
fetches nothing and writes nothing. QA 1.55.0: a commit URL answers only
in a clone of its repo (LOW-1); a git error other than an unknown revision
is no block (LOW-4); a binary file shows no lines (NIT-2). Timeouts, no_git, ambiguous and the
probe order run against a fake git in tests/xray_unit.c, since the probe
takes fixed paths only.

Run:  python3 tests/xray_resolve_test.py        (uses ./dch; run.sh isolates)
"""
import json, os, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
fails = [0]


def check(msg, cond, detail=""):
    print("  %s %s" % ("ok  " if cond else "FAIL", msg))
    if not cond:
        fails[0] += 1
        if detail:
            print("       " + str(detail)[:900])


# /tmp, not TMPDIR: a per-user TMPDIR can sit inside some git work tree
T = os.path.realpath(tempfile.mkdtemp(prefix="xres", dir=os.environ.get("DCH_TEST_TMP", "/tmp")))
HOME = os.path.join(T, "home")
os.makedirs(HOME)
QTMP = os.path.join(T, "qtmp")
os.makedirs(QTMP)
TRACE = os.path.join(T, "trace")
ENV = {k: v for k, v in os.environ.items()
       if not k.startswith(("DCH_SESSION", "DCH_SOCKET_DIR", "DCH_DEBUG",
                            "CLAUDE", "Malloc", "GIT_"))}
ENV.update(XDG_RUNTIME_DIR=T, XDG_STATE_HOME=os.path.join(T, "state"),
           HOME=HOME, DCH_CONFIG=os.path.join(T, "config"),
           DCH_LOGIN_ENV="0", DCH_NO_REMOTE="1", TMPDIR=QTMP,
           DCH_DEBUG=TRACE)
SENT = os.path.join(T, "SENTINEL")


def dch(*a, env=None):
    return subprocess.run([DCH] + list(a), capture_output=True, text=True,
                          timeout=60, env=env or ENV)


def lines(out):
    try:
        return [json.loads(l) for l in out.splitlines() if l.strip()]
    except ValueError:
        return None


def ref(n, text, full=True):
    """--ref's lines and its resolved objects."""
    r = dch("--xray", n, "--ref", text, "--scope", "session",
            *(["--full"] if full else []), "--json")
    ls = lines(r.stdout) or []
    return r, ls, [l["resolved"] for l in ls if "resolved" in l]


def short(sha, n):
    """The first n or more of sha that the grammar takes as a short SHA: a
    letter and a digit. 1 in 100 ten-character prefixes of a commit's SHA is
    all digits, which is no ref (exit 2)."""
    while n < len(sha) and (sha[:n].isdigit() or sha[:n].isalpha()):
        n += 1
    return sha[:n]


def wait_for(fn, secs=10.0):
    end = time.time() + secs
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def files(root):
    out = {}
    for dp, dn, fn in os.walk(root):
        for f in dn + fn:
            p = os.path.join(dp, f)
            st = os.lstat(p)
            out[p] = (st.st_size, st.st_mtime_ns)
    return out


def probe():
    """8.3's probe, as dch runs it."""
    if sys.platform == "darwin":
        c = ["/Library/Developer/CommandLineTools/usr/bin/git"]
        try:
            c.append(os.path.join(os.readlink("/var/db/xcode_select_link"),
                                  "usr/bin/git"))
        except OSError:
            pass
        c += ["/Applications/Xcode.app/Contents/Developer/usr/bin/git",
              "/opt/homebrew/bin/git", "/usr/local/bin/git"]
    else:
        c = ["/usr/bin/git", "/usr/local/bin/git"]
    for p in c:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


GIT = probe()
GENV = {"HOME": HOME, "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "Ann Example",
        "GIT_AUTHOR_EMAIL": "a@example.com", "GIT_COMMITTER_NAME": "Ann",
        "GIT_COMMITTER_EMAIL": "a@example.com", "LANG": "C"}


def git(repo, *a, inp=None):
    return subprocess.run([GIT, "-C", repo] + list(a), capture_output=True,
                          text=True, env=GENV, input=inp,
                          timeout=30).stdout.strip()


def make_repo(path, subject="Add the widget"):
    os.makedirs(os.path.join(path, "src"), exist_ok=True)
    git(path, "init", "-q", ".")
    with open(os.path.join(path, "src", "w.c"), "w") as f:
        f.write("".join("int w%d;\n" % i for i in range(1, 41)))
    git(path, "add", "-A")
    git(path, "commit", "-qm", subject)
    return git(path, "rev-parse", "HEAD")


def spawn_shell(n, cwd):
    r = dch("--spawn", n, "--size", "80x24", "--cwd", cwd, "sh", "-c",
            "sleep 120")
    check("%s: spawned" % n, r.returncode == 0, r.stderr)


def trace_tail():
    try:
        return open(TRACE).read()
    except OSError:
        return ""


# ---- a live shell: every state a real git and the file rules give ---------

def test_shell():
    repo = os.path.join(T, "repo")
    sha = make_repo(repo)
    plain = os.path.join(T, "plain")
    os.makedirs(plain)
    n, m = "xres-sh", "xres-plain"
    spawn_shell(n, repo)
    spawn_shell(m, plain)
    try:
        r, ls, rv = ref(n, short(sha, 10))
        check("shell commit: --full: one resolved line, ok, the subject",
              r.returncode == 0 and len(rv) == 1 and rv[0]["kind"] ==
              "commit" and rv[0]["state"] == "ok" and rv[0]["title"] ==
              "Add the widget", (r.returncode, r.stderr, r.stdout[-800:]))
        if rv:
            v = rv[0]
            check("shell commit: subtitle author · age · short SHA",
                  v["subtitle"].startswith("Ann Example · ") and
                  v["subtitle"].endswith(" · " + sha[:7]), v)
            check("shell commit: the stat as lines (row, summary), <= 14",
                  v["lines"] and "src/w.c" in v["lines"][0] and
                  "1 file changed" in v["lines"][-1] and
                  len(v["lines"]) <= 14, v)
            check("shell commit: exactly 3.7's fields",
                  sorted(v) == ["kind", "lines", "state", "subtitle",
                                "title"], sorted(v))
        check("shell commit: the resolved line is just before done",
              len(ls) >= 3 and "first" in ls[0] and
              "resolved" in ls[-2] and ls[-1].get("done") is True and
              ls[-1]["why"] is None, [list(l)[1] for l in ls])
        check("all: schema 1 on every line", all(l.get("schema") == 1
                                                for l in ls))
        r, ls, rv = ref(n, short(sha, 10), full=False)
        check("shell commit: a light stream has no resolved line",
              r.returncode == 0 and not rv and ls and
              ls[-1].get("done") is True, r.stdout[-400:])
        r, ls, rv = ref(n, "abcdef1234")
        check("shell commit: a SHA not in the repo: not_commit, "
              "\"Not a commit in repo\"",
              len(rv) == 1 and rv[0]["state"] == "not_commit" and
              rv[0]["title"] == "Not a commit in repo" and
              rv[0]["subtitle"] == "abcdef1" and rv[0]["lines"] == [], rv)
        tree = git(repo, "rev-parse", "HEAD^{tree}")
        r, ls, rv = ref(n, short(tree, 12))
        check("shell commit: a tree SHA: not_commit (\"expected commit "
              "type\")", len(rv) == 1 and rv[0]["state"] == "not_commit" and
              rv[0]["title"] == "Not a commit in repo", rv)
        # a commit URL with a short SHA answers only in a clone of its repo
        # (QA 1.55.0 LOW-1); a full 40-hex SHA is that commit anywhere
        url = "https://github.com/o/r/commit/" + short(sha, 10)
        r, ls, rv = ref(n, url)
        check("shell commit: a short-SHA commit URL, no remote: not_commit",
              len(rv) == 1 and rv[0]["state"] == "not_commit", rv)
        open(TRACE, "w").close()
        r, ls, rv = ref(n, "https://github.com/o/r/commit/" + sha)
        check("shell commit: a full-SHA commit URL, no remote: ok, no config "
              "run (2 spawns)", len(rv) == 1 and rv[0]["state"] == "ok" and
              "kind=commit state=ok spawned=2" in trace_tail(), rv)
        git(repo, "remote", "add", "origin", "git@github.com:O/R.git")
        open(TRACE, "w").close()
        r, ls, rv = ref(n, url)
        check("shell commit: a short-SHA commit URL whose repo is a remote "
              "resolves (3 spawns)", len(rv) == 1 and
              rv[0]["state"] == "ok" and
              "kind=commit state=ok spawned=3" in trace_tail(), rv)
        open(TRACE, "w").close()
        r, ls, rv = ref(n, "https://github.com/someone/else/commit/" +
                        short(sha, 10))
        check("shell commit: a short-SHA commit URL of another repo: "
              "not_commit, show never runs (2 spawns)", len(rv) == 1 and
              rv[0]["state"] == "not_commit" and
              rv[0]["title"] == "Not a commit in repo" and
              "kind=commit state=not_commit spawned=2" in trace_tail(), rv)
        # a remote URL with a token: it matches, and no byte of it leaks
        tok = "ghp_FAKE179TOKEN"
        git(repo, "remote", "set-url", "origin",
            "https://x-access-token:%s@github.com/o/r.git" % tok)
        open(TRACE, "w").close()
        r1, ls, rv = ref(n, url)
        r2, ls2, rv2 = ref(n, "https://github.com/o/x/commit/" + short(sha, 10))
        tr = trace_tail()
        check("shell commit: a remote with userinfo matches; the token is in "
              "no stdout, stderr or trace", len(rv) == 1 and
              rv[0]["state"] == "ok" and len(rv2) == 1 and
              rv2[0]["state"] == "not_commit" and
              not any(x in t for t in (r1.stdout, r1.stderr, r2.stdout,
                                       r2.stderr, tr)
                      for x in (tok, "x-access-token")), (rv, rv2))
        # github.com counts only as a URL's host, and a value's escaped
        # newline cannot forge a second remote (QA 1.57.0 NIT-D1)
        for u in ("https://evil.com//github.com/o/r",
                  "https://evil.com/x@github.com/o/r",
                  "https://evil.com/?u=//github.com/o/r",
                  "https://evil/x\nremote.y.url https://github.com/o/r"):
            git(repo, "config", "remote.origin.url", u)
            r, ls, rv = ref(n, url)
            check("shell commit: a remote %r names another host: not_commit"
                  % u, len(rv) == 1 and rv[0]["state"] == "not_commit", rv)
        git(repo, "config", "remote.origin.url", "git@github.com:O/R.git")
        if git(plain, "rev-parse", "--is-inside-work-tree") == "true":
            print("  skip not_repo: %s is inside a git work tree" % plain)
        else:
            r, ls, rv = ref(m, short(sha, 10))
            check("shell commit: outside a repo: not_repo, \"Not in a git "
                  "repository\"", len(rv) == 1 and
                  rv[0]["state"] == "not_repo" and
                  rv[0]["title"] == "Not in a git repository", rv)

        # files
        r, ls, rv = ref(n, "src/w.c:20")
        check("shell file: ok, the name, \"src/w.c · line 20\", 14 lines "
              "with line 20 seventh",
              len(rv) == 1 and rv[0]["kind"] == "file" and
              rv[0]["state"] == "ok" and rv[0]["title"] == "w.c" and
              rv[0]["subtitle"] == "src/w.c · line 20" and
              len(rv[0]["lines"]) == 14 and rv[0]["lines"][6] == "int w20;",
              rv)
        check("shell file: first is line[0]'s number (14), fields in order",
              rv and rv[0].get("first") == 14 and
              list(rv[0]) == ["kind", "state", "title", "subtitle", "first",
                              "lines"], rv)
        r, ls, rv = ref(n, "src/w.c:39")
        check("shell file: clamped at the end, first 27, line 39 at [12]",
              len(rv) == 1 and rv[0].get("first") == 27 and
              rv[0]["lines"][12] == "int w39;", rv)
        r, ls, rv = ref(n, "src/w.c:20", full=False)
        check("shell file: a light stream has no resolved line", not rv,
              r.stdout[-300:])
        with open(os.path.join(plain, "x.c"), "w") as f:
            f.write("x\n")
        r, ls, rv = ref(n, "../plain/x.c:1")
        check("shell file: outside the project: outside_project, no lines",
              len(rv) == 1 and rv[0]["state"] == "outside_project" and
              rv[0]["lines"] == [] and "first" not in rv[0], rv)
        os.makedirs(os.path.join(repo, ".ssh"))
        with open(os.path.join(repo, ".ssh", "id_ed25519"), "w") as f:
            f.write("PRIVATE KEY CANARY\n")
        r, ls, rv = ref(n, os.path.join(repo, ".ssh", "id_ed25519") + ":1")
        check("shell file: a secret name: secret_name, never read",
              len(rv) == 1 and rv[0]["state"] == "secret_name" and
              "CANARY" not in r.stdout, rv)
        with open(os.path.join(repo, "big.log"), "w") as f:
            f.write("y" * (2 * 1024 * 1024 + 1))
        r, ls, rv = ref(n, "big.log:1")
        check("shell file: over 2 MB: too_large, no lines",
              len(rv) == 1 and rv[0]["state"] == "too_large" and
              rv[0]["lines"] == [], rv)
        os.symlink(os.path.join(plain, "x.c"), os.path.join(repo, "out.c"))
        r, ls, rv = ref(n, "out.c:1")
        check("shell file: a symlink as the name: no resolved line",
              r.returncode == 0 and not rv, r.stdout[-300:])
        r, ls, rv = ref(n, "src/missing.c:3")
        check("shell file: a missing file: no resolved line, done",
              r.returncode == 0 and not rv and ls and
              ls[-1].get("done") is True, r.stdout[-300:])
        # a NUL early: binary, said, no lines of U+FFFD (QA 1.55.0 NIT-2)
        with open(os.path.join(repo, "blob.dat"), "wb") as f:
            f.write(b"PK\x03\x04\x00\x00\xff\xfe\n" * 4)
        r, ls, rv = ref(n, "blob.dat:1")
        check("shell file: a binary file: ok, \"blob.dat · binary\", "
              "no lines", len(rv) == 1 and rv[0]["state"] == "ok" and
              rv[0]["subtitle"] == "blob.dat · binary" and
              rv[0]["lines"] == [] and "\ufffd" not in r.stdout, rv)
        with open(os.path.join(repo, "esc.txt"), "w") as f:
            f.write("a\x1b[31mred\x07 ‮evil\n")
        r, ls, rv = ref(n, "esc.txt:1")
        raw = [l for l in r.stdout.splitlines() if '"resolved"' in l]
        check("shell file: ESC, BEL and bidi in a line come out escaped",
              len(rv) == 1 and raw and "\x1b" not in raw[0] and
              "\\u001b" in raw[0] and "\\u202e" in raw[0] and
              "‮" not in raw[0], raw)

        # nothing written by a resolve (9.5): T holds the state dir, the
        # socket dir, TMPDIR (qtmp) and the repo; the trace is DCH_DEBUG's
        before = files(T)
        open(TRACE, "w").close()
        ref(n, short(sha, 10))
        ref(n, "src/w.c:3")
        tr = trace_tail()
        new = {p: v for p, v in files(T).items()
               if p != TRACE and before.get(p) != v}
        check("nothing written: no file new or changed after a commit and a "
              "file resolve (repo, .git, state, sockets, TMPDIR)", not new,
              sorted(new)[:10])
        check("trace: the resolve says kind and state, never a path or line",
              "xray resolve: kind=commit state=ok spawned=2" in tr and
              "xray resolve: kind=file state=ok spawned=0" in tr and
              repo not in tr and "int w3" not in tr, tr[-600:])
    finally:
        dch("-k", n)
        dch("-k", m)


# ---- Claude: live and ended ----------------------------------------------

SID = "11111111-2222-4333-8444-5555555555c9"


def history(name, sid, cfg, cwd):
    sockdir = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    rec = {"name": name, "cwd": cwd, "end": "exited",
           "argv": ["claude"], "started": 1794299400, "updated": 1794300000,
           "ended": 1794300000, "sock_dir": sockdir,
           "agent": {"session_id": sid, "cwd": cwd, "config_dir": cfg}}
    with open(os.path.join(hdir, name + ".json"), "w") as f:
        f.write(json.dumps(rec) + "\n")


def transcript(sid, cwd):
    pdir = os.path.join(HOME, ".claude", "projects", "-x")
    os.makedirs(pdir, exist_ok=True)
    with open(os.path.join(pdir, sid + ".jsonl"), "w") as f:
        f.write(json.dumps({"type": "user", "cwd": cwd,
                            "timestamp": "2026-11-10T10:00:00Z",
                            "message": {"content": "see the commit"}}) + "\n")


def test_ended():
    repo = os.path.join(T, "repo")
    sha = git(repo, "rev-parse", "HEAD")
    transcript(SID, repo)
    history("xres-end", SID, os.path.join(HOME, ".claude"), repo)
    open(TRACE, "w").close()
    r, ls, rv = ref("xres-end", short(sha, 10))
    tr = trace_tail()
    check("ended commit: state ended, \"Session ended\", git never spawned",
          r.returncode == 0 and len(rv) == 1 and
          rv[0]["state"] == "ended" and rv[0]["title"] == "Session ended" and
          "kind=commit state=ended spawned=0" in tr,
          (r.returncode, r.stderr, rv, tr[-300:]))
    check("ended commit: the line comes before done",
          ls and "resolved" in ls[-2] and ls[-1].get("done") is True,
          [list(l)[1] for l in ls])
    r, ls, rv = ref("xres-end", "src/w.c:2")
    check("ended file: an ended session still resolves files",
          len(rv) == 1 and rv[0]["state"] == "ok" and
          rv[0]["lines"][1] == "int w2;", rv)
    r, ls, rv = ref("xres-end", short(sha, 10), full=False)
    check("ended commit: light: no resolved line", r.returncode == 0 and
          not rv, r.stdout[-300:])


def test_live_claude():
    n = "xres-cl"
    repo = os.path.join(T, "repo")
    sha = git(repo, "rev-parse", "HEAD")
    bindir = os.path.join(T, "bin")
    fdir = os.path.join(T, "fake")
    for d in (bindir, fdir):
        os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    env = dict(ENV, PATH=bindir + ":" + ENV.get("PATH", "/usr/bin:/bin"),
               FAKE_CLAUDE_DIR=fdir)
    r = subprocess.run([DCH, "--spawn", n, "--size", "80x24", "--cwd", repo,
                        "claude"], capture_output=True, text=True, timeout=30,
                       env=env)
    check("live claude: spawned", r.returncode == 0, r.stderr)
    try:
        sdir = os.path.join(HOME, ".claude", "sessions")
        rec = wait_for(lambda: [f for f in os.listdir(sdir)
                                if f.endswith(".json")]
                       if os.path.isdir(sdir) else None, 10)
        sid = json.load(open(os.path.join(sdir, rec[0])))["sessionId"]
        transcript(sid, repo)
        rv = wait_for(lambda: ref(n, short(sha, 10))[2], 10)
        check("live claude commit: ok in the session's cwd",
              rv and len(rv) == 1 and rv[0]["state"] == "ok" and
              rv[0]["title"] == "Add the widget", rv)
    finally:
        dch("-k", n)


# ---- the sentinel test: a hostile repo runs nothing, fetches nothing -------

def test_hostile():
    hr = os.path.join(T, "hostile")
    make_repo(hr, "Hostile but plain")
    evil = os.path.join(T, "evil.sh")
    with open(evil, "w") as f:
        f.write("#!/bin/sh\necho \"$0 $*\" >> '%s'\n" % SENT)
    os.chmod(evil, 0o755)
    # a signed commit (any gpgsig header makes show verify it)
    tree = git(hr, "rev-parse", "HEAD^{tree}")
    parent = git(hr, "rev-parse", "HEAD")
    obj = ("tree %s\nparent %s\nauthor A <a@b> 1700000000 +0000\n"
           "committer A <a@b> 1700000000 +0000\n"
           "gpgsig -----BEGIN PGP SIGNATURE-----\n \n abc\n"
           " -----END PGP SIGNATURE-----\n\nSigned subject\n" % (tree, parent))
    signed = git(hr, "hash-object", "-t", "commit", "-w", "--stdin",
                 inp=obj)
    # a commit whose blob is gone, in a partial clone with an ext:: promisor
    with open(os.path.join(hr, "lazy.txt"), "w") as f:
        f.write("lazy blob 7d1e\n")
    git(hr, "add", "lazy.txt")
    git(hr, "commit", "-qm", "Lazy")
    lazy = git(hr, "rev-parse", "HEAD")
    blob = git(hr, "rev-parse", "HEAD:lazy.txt")
    os.unlink(os.path.join(hr, ".git", "objects", blob[:2], blob[2:]))
    with open(os.path.join(hr, ".gitattributes"), "w") as f:
        f.write("* diff=evil filter=evil\n")
    cfg = [("core.fsmonitor", evil), ("core.pager", evil),
           ("pager.show", evil), ("alias.show", "!" + evil),
           ("alias.rev-parse", "!" + evil), ("diff.external", evil),
           ("diff.evil.textconv", evil), ("diff.evil.command", evil),
           ("filter.evil.clean", evil), ("filter.evil.smudge", evil),
           ("filter.evil.process", evil), ("log.showSignature", "true"),
           ("gpg.program", evil), ("gpg.ssh.program", evil),
           ("gpg.x509.program", evil), ("core.sshCommand", evil),
           ("core.askPass", evil), ("credential.helper", "!" + evil),
           ("core.repositoryformatversion", "1"),
           ("extensions.partialClone", "origin"),
           ("remote.origin.url", "ext::" + evil + " %G"),
           ("remote.origin.promisor", "true"),
           ("remote.origin.partialclonefilter", "blob:none"),
           ("protocol.ext.allow", "always"), ("protocol.allow", "always"),
           ("trace2.eventTarget", os.path.join(T, "trace2.out")),
           ("trace2.normalTarget", os.path.join(T, "trace2.out")),
           ("core.hooksPath", os.path.join(hr, ".git", "hooks"))]
    for k, v in cfg:
        git(hr, "config", k, v)
    hooks = os.path.join(hr, ".git", "hooks")
    os.makedirs(hooks, exist_ok=True)
    for h in ("pre-commit", "post-checkout", "post-index-change",
              "fsmonitor-watchman", "reference-transaction", "post-merge",
              "pre-auto-gc", "post-rewrite"):
        shutil.copy(evil, os.path.join(hooks, h))
    # control: the same repo does run evil.sh for a plain git show, so the
    # hostile config is live, not inert
    for a in ([signed + "^{commit}"], [lazy + "^{commit}", "--stat"]):
        subprocess.run([GIT, "-C", hr, "--no-pager", "show"] + a,
                       capture_output=True, env=dict(GENV, GIT_TERMINAL_PROMPT="0"),
                       timeout=30)
    fired = os.path.exists(SENT) and open(SENT).read()
    check("hostile control: plain git show runs the planted program "
          "(showSignature, lazy fetch)", fired and fired.count("\n") >= 2,
          fired)
    if os.path.exists(SENT):
        os.unlink(SENT)
    for p in (os.path.join(T, "trace2.out"),):
        if os.path.exists(p):
            os.unlink(p)

    n = "xres-evil"
    spawn_shell(n, hr)
    try:
        before = files(T)
        open(TRACE, "w").close()
        r1, ls1, rv1 = ref(n, short(signed, 12))
        r2, ls2, rv2 = ref(n, short(lazy, 12))
        r3, ls3, rv3 = ref(n, "src/w.c:1")
        check("hostile: the signed commit still resolves (ok), its "
              "signature unchecked", len(rv1) == 1 and rv1[0]["state"] == "ok"
              and rv1[0]["title"] == "Signed subject", (rv1, r1.stderr))
        # git fails on the missing blob: no line, never not_commit (LOW-4)
        check("hostile: the lazy commit, nothing fetched: no block, never "
              "\"Not a commit\"", r2.returncode == 0 and not rv2 and
              ls2 and ls2[-1].get("done") is True,
              (r2.returncode, rv2, r2.stdout[-300:], r2.stderr[-600:]))
        check("hostile: a file ref reads the file, no filter runs",
              len(rv3) == 1 and rv3[0]["state"] == "ok" and
              rv3[0]["lines"][0] == "int w1;", rv3)
        check("hostile: the sentinel stays absent: no fsmonitor, pager, "
              "alias, hook, textconv, filter, gpg, ssh, askpass, credential "
              "or ext:: fetch ran", not os.path.exists(SENT),
              os.path.exists(SENT) and open(SENT).read())
        check("hostile: trace2 targets in repo config wrote nothing",
              not os.path.exists(os.path.join(T, "trace2.out")))
        new = {p: v for p, v in files(T).items()
               if p != TRACE and before.get(p) != v}
        check("hostile: nothing written anywhere under the test root "
              "(index, objects, FETCH_HEAD, TMPDIR)", not new,
              sorted(new)[:10])
        for bad in ("--output=" + SENT, "-c", "--end-of-options",
                    "-cfoo=" + SENT):
            r = dch("--xray", n, "--ref", bad, "--scope", "session", "--full",
                    "--json")
            check("hostile: a ref that looks like an option (%s) is no ref "
                  "(exit 2), nothing run" % bad[:16], r.returncode == 2 and
                  r.stderr.startswith("dch: ") and not r.stdout and
                  not os.path.exists(SENT), (r.returncode, r.stderr))
    finally:
        dch("-k", n)


def test_git_error():
    """A git that fails for another reason is no block, never "Not a commit"
    (QA 1.55.0 LOW-4): an invalid submodule.<name>.update in .gitmodules
    makes git show die on a commit that changes the gitlink."""
    sr = os.path.join(T, "badmods")
    first = make_repo(sr, "Before the submodule")
    git(sr, "update-index", "--add", "--cacheinfo", "160000,%s,subm" % first)
    with open(os.path.join(sr, ".gitmodules"), "w") as f:
        f.write('[submodule "subm"]\n\tpath = subm\n\turl = ./x\n'
                "\tupdate = !touch '%s'\n" % SENT)
    git(sr, "add", ".gitmodules")
    git(sr, "commit", "-qm", "Add the submodule")
    sha = git(sr, "rev-parse", "HEAD")
    n = "xres-mods"
    spawn_shell(n, sr)
    try:
        r, ls, rv = ref(n, short(sha, 10))
        check("git error: show dies on the bad .gitmodules: no block, never "
              "\"Not a commit\"", r.returncode == 0 and not rv and ls and
              ls[-1].get("done") is True, (rv, r.stdout[-300:]))
        bad = (r.returncode, ls[-1] if ls else None)
        r, ls, rv = ref(n, short(first, 10))
        check("git error: a commit that does not touch the gitlink: ok",
              len(rv) == 1 and rv[0]["state"] == "ok" and
              rv[0]["title"] == "Before the submodule", rv)
        check("git error: the --ref exit code and end line are those of an "
              "ok resolve (exit 0, done, partial false, why null)",
              ls and bad == (r.returncode, ls[-1]) and bad[0] == 0 and
              bad[1]["partial"] is False and bad[1]["why"] is None,
              (bad, r.returncode, ls[-1:]))
        r, ls, rv = ref(n, "abcdef1234")
        check("git error: a SHA not in the repo is still not_commit",
              len(rv) == 1 and rv[0]["state"] == "not_commit", rv)
        check("git error: the update command never ran",
              not os.path.exists(SENT))
    finally:
        dch("-k", n)


def test_no_git():
    """No git on the probe paths: the commit block says so, nothing runs."""
    sid = "11111111-2222-4333-8444-5555555555d1"
    cwd = os.path.join(T, "nogit")
    os.makedirs(cwd)
    transcript(sid, cwd)
    history("xres-nogit", sid, os.path.join(HOME, ".claude"), cwd)
    # an ended session never probes; this host has no git, so a live one
    # would say no_git: covered by the units' probe seam
    r, ls, rv = ref("xres-nogit", "abcdef1234")
    check("no git: an ended session still says ended, nothing spawned",
          len(rv) == 1 and rv[0]["state"] == "ended", rv)


def test_worktree():
    """#205 (QA 1.57.0 MED-D1): a real `git worktree add` worktree in
    <repo>/.claude/worktrees/<name> (and one nested in it) reads its own
    files and refuses its own deny-list entries; a .claude that is itself
    a git repo, or a worktree below a denied dir, refuses everything."""
    def put(path, text="FAKE\n"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    R = os.path.join(T, "wtrepo")
    make_repo(R)
    W = os.path.join(R, ".claude", "worktrees", "w")
    Y = os.path.join(W, ".claude", "worktrees", "y")
    git(R, "worktree", "add", "-q", W)
    git(R, "worktree", "add", "-q", Y)
    for f in (".env", ".claude/settings.local.json", ".ssh/config",
              ".credentials.json", ".kube/config"):
        put(os.path.join(W, f))
    put(os.path.join(R, ".claude", "settings.local.json"))
    put(os.path.join(Y, ".claude", "s.json"))
    C = os.path.join(T, "cfgrepo", ".claude")  # CLAUDE_CONFIG_DIR as a repo
    os.makedirs(C)
    git(C, "init", "-q", ".")
    put(os.path.join(C, ".credentials.json"), '{"accessToken":"FAKE"}\n')
    put(os.path.join(C, "settings.json"))
    K = os.path.join(T, "kx", ".kube", "repo")  # a worktree under .kube
    make_repo(K)
    KW = os.path.join(K, ".claude", "worktrees", "w")
    git(K, "worktree", "add", "-q", KW)
    ok = all(os.path.isfile(os.path.join(d, ".git")) for d in (W, Y, KW))
    check("worktree: git worktree add made three worktrees (.git files)", ok)
    for k, (n, cwd) in enumerate((("xres-wt", W), ("xres-wty", Y),
                                  ("xres-cfg", C), ("xres-kw", KW))):
        sid = "11111111-2222-4333-8444-5555555555e%d" % k
        transcript(sid, cwd)
        history(n, sid, os.path.join(HOME, ".claude"), cwd)
    for label, n, text, want in (
            ("its own src/w.c", "xres-wt", "src/w.c:2", "ok"),
            ("its own .env", "xres-wt", W + "/.env:1", "secret_name"),
            ("its own .claude/settings.local.json", "xres-wt",
             W + "/.claude/settings.local.json:1", "secret_name"),
            ("its own .ssh/config", "xres-wt", W + "/.ssh/config:1",
             "secret_name"),
            ("its own .credentials.json", "xres-wt",
             W + "/.credentials.json:1", "secret_name"),
            ("its own .kube/config", "xres-wt", W + "/.kube/config:1",
             "secret_name"),
            ("the main repo's .claude/settings.local.json", "xres-wt",
             R + "/.claude/settings.local.json:1", "outside_project"),
            ("nested: its own src/w.c", "xres-wty", "src/w.c:2", "ok"),
            ("nested: its own .claude/s.json", "xres-wty",
             Y + "/.claude/s.json:1", "secret_name"),
            ("nested: the outer worktree's src/w.c", "xres-wty",
             W + "/src/w.c:2", "outside_project"),
            ("a .claude that is a git repo: .credentials.json", "xres-cfg",
             C + "/.credentials.json:1", "secret_name"),
            ("a .claude that is a git repo: settings.json", "xres-cfg",
             C + "/settings.json:1", "secret_name"),
            ("a worktree below .kube: src/w.c", "xres-kw",
             KW + "/src/w.c:2", "secret_name")):
        r, ls, rv = ref(n, text)
        if want == "ok":
            good = (len(rv) == 1 and rv[0]["state"] == "ok" and
                    rv[0]["lines"][1] == "int w2;")
        else:
            good = (len(rv) == 1 and rv[0]["state"] == want and
                    rv[0]["lines"] == [] and "FAKE" not in r.stdout)
        check("worktree: %s: %s" % (label, want), good,
              (r.returncode, r.stderr, rv))


def main():
    if subprocess.run([DCH, "--build"], capture_output=True,
                      text=True).stdout.strip() != "full":
        print("  skip resolvers: a lite build has no shell ring")
        return
    try:
        if not GIT:
            print("  skip resolvers over a real git: none on 8.3's probe")
            test_no_git()
        else:
            test_shell()
            test_ended()
            test_live_claude()
            test_hostile()
            test_git_error()
            test_worktree()
    finally:
        shutil.rmtree(T, ignore_errors=True)
    if fails[0]:
        print("%d failed" % fails[0])
        sys.exit(1)


if __name__ == "__main__":
    main()
