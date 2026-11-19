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
import fcntl, json, os, pty, re, select, shutil, signal, struct, subprocess, sys, tempfile, termios, time

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
            check("shell commit: subtitle author · age · short SHA · on "
                  "HEAD (#502)", v["subtitle"].startswith("Ann Example · ")
                  and v["subtitle"].endswith(" · " + sha[:7] + " · on HEAD"),
                  v)
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
        test_light(n, m, repo, sha)
        test_github(m)
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
              "run (3 spawns: #502's merge-base)", len(rv) == 1 and
              rv[0]["state"] == "ok" and "kind=commit state=ok spawned=3" in trace_tail(), rv)
        git(repo, "remote", "add", "origin", "git@github.com:O/R.git")
        open(TRACE, "w").close()
        r, ls, rv = ref(n, url)
        check("shell commit: a short-SHA commit URL whose repo is a remote "
              "resolves (4 spawns)", len(rv) == 1 and
              rv[0]["state"] == "ok" and
              "kind=commit state=ok spawned=4" in trace_tail(), rv)
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
        # git's own URL: a url.<base>.insteadOf in the repo's config
        # rewrites the remote before the match (QA 1.61.0 LOW-X15)
        git(repo, "config", "remote.origin.url", "https://github.com/o/r")
        git(repo, "config", "url.https://evil.com/.insteadOf",
            "https://github.com/")
        r, ls, rv = ref(n, url)
        check("shell commit: insteadOf sends github.com/o/r to evil.com: "
              "not_commit", len(rv) == 1 and
              rv[0]["state"] == "not_commit", rv)
        git(repo, "config", "--remove-section", "url.https://evil.com/")
        git(repo, "config", "remote.origin.url", "gh:o/r")
        git(repo, "config", "url.https://github.com/.insteadOf", "gh:")
        r, ls, rv = ref(n, url)
        check("shell commit: insteadOf makes gh:o/r github.com/o/r: ok",
              len(rv) == 1 and rv[0]["state"] == "ok", rv)
        git(repo, "config", "--remove-section", "url.https://github.com/")
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
        test_light_file(n, repo, plain)
        with open(os.path.join(plain, "x.c"), "w") as f:
            f.write("x\n")
        r, ls, rv = ref(n, "../plain/x.c:1")
        check("shell file: outside the project: outside_project, no lines",
              len(rv) == 1 and rv[0]["state"] == "outside_project" and
              rv[0]["lines"] == [] and "first" not in rv[0], rv)
        os.makedirs(os.path.join(repo, ".ssh"), exist_ok=True)
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
              "xray resolve: kind=commit state=ok spawned=3" in tr and
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


# ---- #502: the light stream's built-in commit resolver, and the card -------

def card_text(n, text, want, secs=20.0):
    """n's view, text's card opened by / and ^R: the screen text (escapes
    out) once want shows, or all of it after secs."""
    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
        os.execve(DCH, [DCH, "--xray", n], ENV)
    os.set_inheritable(fd, False)
    out, plain = b"", ""

    def pump(t):
        nonlocal out
        end = time.time() + t
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], max(0, end - time.time()))
            if not r:
                return
            try:
                b = os.read(fd, 65536)
            except OSError:
                return
            if not b:
                return
            out += b
    try:
        pump(2.0)
        os.write(fd, b"/" + text.encode())
        pump(0.5)
        os.write(fd, b"\x12")
        end = time.time() + secs
        while time.time() < end:
            pump(0.2)
            plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]|\r", "",
                           out.decode("utf-8", "replace"))
            if want in plain:
                break
    finally:
        os.close(fd)
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    return plain


def test_light_file(n, repo, plain):
    """#500: a light file ref is ok/missing from stat alone."""
    import time as _t
    p = os.path.join(repo, "src", "w.c")
    os.utime(p, (1700000000, 1700000000))
    r, ls, rv = ref(n, "src/w.c:20", full=False)
    check("light #500: an existing file: one ok line, name, path, mtime, no "
          "lines, just before done", r.returncode == 0 and len(rv) == 1 and
          rv[0]["kind"] == "file" and rv[0]["state"] == "ok" and
          rv[0]["title"] == "w.c" and rv[0]["lines"] == [] and
          rv[0]["subtitle"] == "src/w.c \u00b7 modified 2023-11-14 22:13 UTC"
          and "resolved" in ls[-2] and ls[-1].get("done") is True,
          r.stdout[-500:])
    check("light #500: no first field (nothing shown)",
          rv and "first" not in rv[0], rv)
    r, ls, rv = ref(n, "src/gone.c:3", full=False)
    check("light #500: a missing file: its own state, \"missing\"",
          r.returncode == 0 and len(rv) == 1 and rv[0]["kind"] == "file" and
          rv[0]["state"] == "missing" and rv[0]["title"] == "Missing" and
          rv[0]["subtitle"] == "src/gone.c" and rv[0]["lines"] == [],
          r.stdout[-400:])
    r, ls, rv = ref(n, "src/gone.c:3")
    check("light #500: --full keeps no line for a missing file", not rv, rv)
    with open(os.path.join(plain, "x.c"), "w") as f:
        f.write("x\n")
    big = os.path.join(repo, "big.light")
    with open(big, "w") as f:
        f.write("y" * (2 * 1024 * 1024 + 1))
    os.symlink(os.path.join(plain, "x.c"), os.path.join(repo, "lnk.c"))
    os.mkfifo(os.path.join(repo, "pipe.c"))
    os.makedirs(os.path.join(repo, ".ssh"), exist_ok=True)
    with open(os.path.join(repo, ".ssh", "k"), "w") as f:
        f.write("K\n")
    for label, text, st in (("outside the project", "../plain/x.c:1", None),
                            ("a secret name", os.path.join(repo, ".ssh", "k") + ":1", None),
                            ("a missing secret name",
                             os.path.join(repo, ".ssh", "nope") + ":1", None),
                            ("a missing file outside", "../plain/nope.c", None),
                            ("a symlink", "lnk.c:1", None),
                            ("a FIFO (never opened)", "pipe.c:1", None)):
        r, ls, rv = ref(n, text, full=False)
        check("light #500: %s: no resolved line" % label,
              r.returncode == 0 and not rv and ls and
              ls[-1].get("done") is True, r.stdout[-300:])
    os.symlink(plain, os.path.join(repo, "outlink"))
    with open(os.path.join(plain, "there.c"), "w") as f:
        f.write("t\n")
    for label, text in (("target missing", "outlink/none.c:1"),
                        ("target present", "outlink/there.c:1")):
        for full in (False, True)[:1 + (label == "target missing")]:
            r, ls, rv = ref(n, text, full=full)
            check("light #510: a symlinked dir out, %s%s: no line"
                  % (label, " (--full)" if full else ""),
                  r.returncode == 0 and not rv and ls and
                  ls[-1].get("done") is True, r.stdout[-300:])
    r, ls, rv = ref(n, "src/gone.c:3", full=False)
    check("light #510: a missing file in the project: still missing",
          len(rv) == 1 and rv[0]["state"] == "missing", rv)
    # #510 walks every level: a missing dir at depth is missing too (NIT-UQ5)
    os.makedirs(os.path.join(repo, "deep", "er"))
    for text in ("nodir/x.c:1", "deep/er/q/r/none.c:1"):
        r, ls, rv = ref(n, text, full=False)
        check("light #510: a missing dir in the project, %s: missing"
              % text[:-2], len(rv) == 1 and rv[0]["state"] == "missing" and
              rv[0]["subtitle"] == text[:-2], rv)
    # #525: a dangling link is judged by where its target points
    os.symlink(os.path.join(repo, "nodir2"), os.path.join(repo, "dangin"))
    os.symlink("nodir3", os.path.join(repo, "danginrel"))
    r, ls, rv = ref(n, "dangin/x.c", full=False)
    check("light #525: a dangling link to a missing in-project dir: missing",
          len(rv) == 1 and rv[0]["state"] == "missing", (rv, r.stdout[-300:]))
    r, ls, rv = ref(n, "danginrel/x.c", full=False)
    check("light #525: a dangling relative link in the project: missing",
          len(rv) == 1 and rv[0]["state"] == "missing", (rv, r.stdout[-300:]))
    for name, tgt in (("dangout", os.path.join(plain, "nodir")),
                      ("dangdot", os.path.join(repo, "..", "plain", "nodir")),
                      ("dangvia", os.path.join(repo, "outlink", "nodir")),
                      ("loop1", "loop1")):
        os.symlink(tgt, os.path.join(repo, name))
        r, ls, rv = ref(n, name + "/x.c", full=False)
        check("light #525: a dangling link %s (out, via a link out, a loop):"
              " no line" % name, r.returncode == 0 and not rv and ls and
              ls[-1].get("done") is True, r.stdout[-300:])
    r, ls, rv = ref(n, "big.light:1", full=False)
    check("light #500: over 2 MB: ok by stat, not too_large",
          len(rv) == 1 and rv[0]["state"] == "ok", rv)
    cfg = ENV["DCH_CONFIG"]
    dfn = os.path.join(T, "define500.sh")
    with open(dfn, "w") as f:
        f.write("#!/bin/sh\necho '{\"title\":\"From my ledger\"}'\n")
    os.chmod(dfn, 0o755)
    with open(cfg, "w") as f:
        f.write("define_command = %s\n" % dfn)
    try:
        r, ls, rv = ref(n, "src/w.c:20", full=False)
        check("light #500: a define_command's answer wins over the stat",
              not rv and any("definition" in l for l in ls), r.stdout[-400:])
    finally:
        os.unlink(cfg)
    t = card_text(n, "src/w.c", "modified 2023")
    check("card #500: the light card shows the file, its path and mtime",
          re.search(r"file w\.c +src/w\.c \u00b7 modified 2023-11-14 22:13 "
                    r"UTC", t), t[-1500:])


def test_light(n, m, repo, sha):
    side = git(repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m",
               "Side work")
    r, ls, rv = ref(n, short(sha, 10), full=False)
    check("light #502: a commit on HEAD: one resolved line, ok, the subject, "
          "just before done", r.returncode == 0 and len(rv) == 1 and
          rv[0]["kind"] == "commit" and rv[0]["state"] == "ok" and
          rv[0]["title"] == "Add the widget" and len(ls) >= 2 and
          "resolved" in ls[-2] and ls[-1].get("done") is True,
          r.stdout[-600:])
    check("light #502: its subtitle ends \" · on HEAD\"", rv and
          rv[0]["subtitle"].endswith(" · %s · on HEAD" % sha[:7]), rv)
    r, ls, rv = ref(n, short(side, 10), full=False)
    check("light #502: a commit off HEAD: \" · not on HEAD\"",
          len(rv) == 1 and rv[0]["title"] == "Side work" and
          rv[0]["subtitle"].endswith(" · %s · not on HEAD" % side[:7]), rv)
    for label, who, text in (("a SHA not in the repo", n, "abcdef1234"),
                             ("not a repo", m, short(sha, 10))):
        r, ls, rv = ref(who, text, full=False)
        check("light #502: %s: no resolved line, no warning, done" % label,
              r.returncode == 0 and not rv and not r.stderr and ls and
              ls[-1].get("done") is True and
              ls[-1].get("define_warn") is None, (r.stderr, r.stdout[-400:]))
    # a define_command that answers wins: no built-in line
    cfg = ENV["DCH_CONFIG"]
    dfn = os.path.join(T, "define.sh")
    with open(dfn, "w") as f:
        f.write("#!/bin/sh\necho '{\"title\":\"From my ledger\"}'\n")
    os.chmod(dfn, 0o755)
    with open(cfg, "w") as f:
        f.write("define_command = %s\n" % dfn)
    try:
        r, ls, rv = ref(n, short(sha, 10), full=False)
        defs = [l["definition"] for l in ls if "definition" in l]
        check("light #502: a define_command's answer wins over the built-in",
              not rv and len(defs) == 1 and defs[0]["source"] == "command"
              and defs[0]["title"] == "From my ledger", r.stdout[-600:])
        with open(dfn, "w") as f:
            f.write("#!/bin/sh\nexit 3\n")
        r, ls, rv = ref(n, short(sha, 10), full=False)
        check("light #502: a define_command with no answer: the built-in "
              "still resolves", len(rv) == 1 and rv[0]["state"] == "ok", rv)
    finally:
        os.unlink(cfg)
    t = card_text(n, short(sha, 10), "commit Add the widget")
    check("card #502: the light card shows the commit, its subject, on HEAD",
          re.search(r"commit Add the widget +Ann Example · [^\n]*· %s · on "
                    r"HEAD" % sha[:7], t), t[-1500:])


# ---- #507: issues and pulls through a fake gh (never the real one) --------

GH_FAKE = r"""#!/bin/sh
echo "$*" >> '%(log)s'
echo $$ >> '%(pids)s'
/usr/bin/env >> '%(envlog)s'
case "$*" in
*repos/acme/widgets/issues/1234)
  echo '{"number":1234,"title":"Fix the \u001b[2Jwidget","state":"open","kind":"issue"}' ;;
*repos/acme/widgets/issues/77)
  echo '{"number":77,"title":"Ship the widget","state":"MERGED","kind":"pull"}' ;;
*repos/other/thing/issues/12)
  echo '{"number":12,"title":"Old thing","state":"closed","kind":"issue"}' ;;
*repos/acme/widgets/issues/44)
  echo '{"number":45,"title":"Wrong one","state":"open","kind":"issue"}' ;;
*repos/acme/widgets/issues/88)
  echo '{"number":88,"title":"Odd state","state":"draft","kind":"pull"}' ;;
*repos/acme/widgets/issues/500)
  echo 'gh: Not Found (HTTP 404)' >&2; exit 1 ;;
*repos/acme/widgets/issues/600)
  exec /bin/sleep 20 ;;
*repos/acme/widgets/issues/700)
  exec /bin/sleep 5 ;;
*repos/acme/widgets/issues/701)
  /bin/sleep 2
  echo '{"number":701,"title":"Slow but sure","state":"open","kind":"issue"}' ;;
*repos/acme/widgets/issues/702)
  /bin/sleep 0.3
  echo '{"number":702,"title":"Quick enough","state":"open","kind":"issue"}' ;;
*repos/acme/widgets/issues/703)
  /bin/sleep 1
  echo '{"number":703,"title":"One at a time","state":"open","kind":"issue"}' ;;
*repos/acme/widgets/issues/704)
  /bin/ps -o pgid= -p $PPID > '%(pids)s.pgid'
  echo '{"number":704,"title":"On its own","state":"open","kind":"issue"}' ;;
*) echo 'gh: unexpected' >&2; exit 1 ;;
esac
"""


def test_github(m):
    repo = os.path.join(T, "ghrepo")
    make_repo(repo, "Widgets")
    git(repo, "remote", "add", "origin", "git@github.com:acme/widgets.git")
    fb, nob = os.path.join(T, "fakegh"), os.path.join(T, "nogh")
    os.makedirs(fb)
    os.makedirs(nob)
    log, envlog = os.path.join(T, "gh.log"), os.path.join(T, "gh.env")
    pids = os.path.join(T, "gh.pids")
    with open(os.path.join(fb, "gh"), "w") as f:
        f.write(GH_FAKE % {"log": log, "envlog": envlog, "pids": pids})
    os.chmod(os.path.join(fb, "gh"), 0o755)
    genv = {k: v for k, v in ENV.items() if k != "DCH_NO_GITHUB"}
    cache = os.path.join(T, "ghcache")
    genv.update(PATH=fb, XRES_CANARY="leak", XDG_CACHE_HOME=cache)
    ghd = os.path.join(cache, "dch", "gh")
    n = "xres-gh"
    r = dch("--spawn", n, "--size", "80x24", "--cwd", repo, "sh", "-c",
            "echo 'see acme/widgets#700 and acme/widgets#701'; sleep 120")
    check("%s: spawned" % n, r.returncode == 0, r.stderr)

    def q(text, full=True, env=genv, who=n, keep=False):
        open(log, "w").close()
        if not keep:	# every row starts cold, #508 rows keep the cache
            shutil.rmtree(cache, ignore_errors=True)
        r = dch("--xray", who, "--ref", text,
                "--scope", "session", *(["--full"] if full else []),
                "--json", env=env)
        ls = lines(r.stdout) or []
        return r, ls, [l["resolved"] for l in ls if "resolved" in l], \
            open(log).read()

    try:
        r, ls, rv, lg = q("#1234")
        check("#507: a bare #N via the origin remote: an open issue",
              r.returncode == 0 and len(rv) == 1 and rv[0]["kind"] ==
              "issue" and rv[0]["state"] == "ok" and rv[0]["subtitle"] ==
              "open · acme/widgets#1234" and "resolved" in ls[-2] and
              ls[-1].get("done") is True, (r.stderr, r.stdout[-600:]))
        check("#507: the title's controls are stripped",
              rv and rv[0]["title"] == "Fix the  [2Jwidget", rv)
        check("#507: gh api, read-only, github.com, --jq, the issues path",
              lg.startswith("api --hostname github.com --method GET --jq ")
              and lg.rstrip().endswith(" repos/acme/widgets/issues/1234"),
              lg)
        ev = open(envlog).read().split("\n") if os.path.exists(envlog) \
            else []
        check("#507: gh's env: the GH_* switches, nothing else leaks",
              all(e in ev for e in ("GH_PROMPT_DISABLED=1",
                                    "GH_NO_UPDATE_NOTIFIER=1", "NO_COLOR=1",
                                    "GH_PAGER=cat", "HOME=" + HOME,
                                    "PATH=" + fb)) and
              not any(e.startswith(("XRES_CANARY", "DCH_", "TMPDIR"))
                      for e in ev), ev)
        # NIT-UP5: a proxy and a CA bundle reach gh; GH_HOST and co do not
        pv = {"HTTPS_PROXY": "p1", "HTTP_PROXY": "p2", "NO_PROXY": "p3",
              "https_proxy": "p4", "http_proxy": "p5", "no_proxy": "p6",
              "SSL_CERT_FILE": "/c/f", "SSL_CERT_DIR": "/c/d"}
        os.unlink(envlog)
        q("#1234", env=dict(genv, **pv, GH_HOST="evil.example",
                            GH_ENTERPRISE_TOKEN="x", LD_PRELOAD="/x.so"))
        ev = open(envlog).read().split("\n")
        check("#521: proxy and CA env reach gh unchanged; GH_HOST, "
              "GH_ENTERPRISE_TOKEN, LD_PRELOAD do not",
              all(k + "=" + v in ev for k, v in pv.items()) and
              not any(e.startswith(("GH_HOST", "GH_ENTERPRISE_TOKEN",
                                    "LD_PRELOAD")) for e in ev), ev)
        r, ls, rv, lg = q("https://github.com/acme/widgets/pull/77")
        check("#507: a pull URL: a merged pull",
              len(rv) == 1 and rv[0]["kind"] == "pull" and rv[0]["title"] ==
              "Ship the widget" and rv[0]["subtitle"] ==
              "merged · acme/widgets#77", (r.stderr, rv))
        r, ls, rv, lg = q("other/thing#12")
        check("#507: owner/repo#N: that repo, not the origin's; closed",
              len(rv) == 1 and rv[0]["subtitle"] == "closed · other/thing#12"
              and rv[0]["title"] == "Old thing", (r.stderr, rv, lg))
        r, ls, rv, lg = q("widgets#1234")
        check("#507: repo#N naming the origin's repo resolves",
              len(rv) == 1 and rv[0]["subtitle"] ==
              "open · acme/widgets#1234", rv)
        r, ls, rv, lg = q("#1234", full=True)
        check("#507 --full: the issue line too",
              len(rv) == 1 and rv[0]["state"] == "ok" and rv[0]["lines"] == []
              and rv[0]["subtitle"] == "open · acme/widgets#1234", rv)
        # a definition the transcript holds does not stop the built-in
        # (QA 1.110.0 NIT-UO7 mdefs: xr_define's `if (d0) goto builtin`)
        bindir = os.path.join(T, "bin")
        fdir = os.path.join(T, "fake")
        for d in (bindir, fdir):
            os.makedirs(d, exist_ok=True)
        shutil.copy(os.path.join(HERE, "fake_claude.py"),
                    os.path.join(bindir, "claude"))
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        cl = "xres-ghcl"
        senv = dict(ENV, FAKE_CLAUDE_DIR=fdir)
        senv["PATH"] = bindir + ":" + ENV.get("PATH", "/usr/bin:/bin")
        r = subprocess.run([DCH, "--spawn", cl, "--size", "80x24", "--cwd",
                            repo, "claude"], capture_output=True, text=True,
                           timeout=30, env=senv)
        check("#507 define: a live claude in the origin repo: spawned",
              r.returncode == 0, r.stderr)
        try:
            sdir = os.path.join(HOME, ".claude", "sessions")
            sid = wait_for(lambda: [j["sessionId"] for j in (
                json.load(open(os.path.join(sdir, f)))
                for f in os.listdir(sdir) if f.endswith(".json"))
                if j["cwd"] == repo] if os.path.isdir(sdir) else None, 10)[0]
            transcript(sid, repo)
            out = "# #1234 Fix the widget"
            with open(os.path.join(HOME, ".claude", "projects", "-x",
                                   sid + ".jsonl"), "a") as f:
                for o in ({"type": "assistant", "message": {"content": [
                              {"type": "tool_use", "id": "b1", "name": "Bash",
                               "input": {"command": "gh issue view 1234"}}]}},
                          {"type": "user", "toolUseResult": {"stdout": out},
                           "message": {"content": [{
                               "type": "tool_result", "tool_use_id": "b1",
                               "content": out}]}}):
                    f.write(json.dumps(o) + "\n")
            q("#1234", who=cl)	# light reads the cache (#522)
            r, ls, rv, lg = q("#1234", who=cl, full=False, keep=True)
            check("#507 define: a definition the transcript holds: the "
                  "built-in still resolves", any("definition" in l
                  for l in ls) and len(rv) == 1 and
                  rv[0]["state"] == "ok", r.stdout[-600:])
        finally:
            dch("-k", cl)
        for label, text, env, who, gh_ran in (
                ("gh missing", "#1234", dict(genv, PATH=nob), n, False),
                ("gh exits 1", "#500", genv, n, True),
                ("another number answered", "#44", genv, n, True),
                ("an invalid owner", "-rf/r#1", genv, n, False),
                ("an invalid owner in a URL",
                 "https://github.com/-acme/widgets/issues/9", genv, n, False),
                ("a leading zero, bare", "#0123", genv, n, False),
                ("a leading zero, o/r", "o/r#012", genv, n, False),
                ("a leading zero, URL",
                 "https://github.com/o/r/issues/0012", genv, n, False),
                ("an unknown state (draft)", "#88", genv, n, True),
                ("repo#N not the origin's", "gadgets#3", genv, n, False),
                ("not a repo (no origin)", "#1234", genv, m, False)):
            r, ls, rv, lg = q(text, env=env, who=who)
            check("#507: %s: no resolved line, no noise, done" % label,
                  r.returncode == 0 and not rv and not r.stderr and ls and
                  ls[-1].get("done") is True and
                  ls[-1].get("define_warn") is None and
                  bool(lg) == gh_ran, (r.stderr, r.stdout[-300:], lg))
        t0 = time.time()
        r, ls, rv, lg = q("#600")
        dt = time.time() - t0
        check("#507: gh hanging past the deadline: no line, done in < 7 s",
              r.returncode == 0 and not rv and ls and
              ls[-1].get("done") is True and dt < 7, (dt, r.stdout[-300:]))
        cfg = ENV["DCH_CONFIG"]
        with open(cfg, "w") as f:
            f.write("xray_github = false\n")
        try:
            r, ls, rv, lg = q("#1234")
            check("#507: xray_github = false: no line, gh never runs",
                  r.returncode == 0 and not rv and not lg and
                  ls[-1].get("done") is True, (rv, lg))
        finally:
            os.unlink(cfg)
        r, ls, rv, lg = q("#1234", env=dict(genv, DCH_NO_GITHUB="1"))
        check("#507: DCH_NO_GITHUB=1 (the suite's guard): gh never runs",
              not rv and not lg, (rv, lg))
        # ---- #518: --no-github, per call, the config never written
        ref = ["--ref", "#1234", "--scope", "session", "--json"]
        for label, argv, env in (
                ("--no-github", ref + ["--no-github"], genv),
                ("DCH_NO_GITHUB=1", ref, dict(genv, DCH_NO_GITHUB="1"))):
            open(log, "w").close()
            r = dch("--xray", n, *argv, env=env)
            rv = [l["resolved"] for l in lines(r.stdout) or []
                  if "resolved" in l]
            check("#518: %s: no resolved line, gh never ran" % label,
                  r.returncode == 0 and not rv and
                  not open(log).read(), (r.stderr, rv, open(log).read()))
        r, ls, rv, lg = q("#1234")
        check("#518: without --no-github gh runs and the line comes",
              len(rv) == 1 and lg, (rv, lg))
        check("#518: --no-github never writes the config",
              not os.path.exists(ENV["DCH_CONFIG"]), "")
        r = dch("--xray", n, "--search", "x", "--no-github", "--json",
                env=genv)
        check("#518: --no-github without --ref: bad usage, exit 2",
              r.returncode == 2 and "--no-github comes with --ref"
              in r.stderr, (r.returncode, r.stderr))
        rq_no_github(n, genv, fb, log, cache)
        # ---- #508: the disk cache of gh's answers
        ent = os.path.join(ghd, "acme,widgets,1234")
        r, ls, rv, lg = q("#1234")
        check("#508: the first ask runs gh and writes the entry, dir 0700, "
              "file 0600",
              lg and os.path.isfile(ent) and
              os.stat(ent).st_mode & 0o777 == 0o600 and
              os.stat(ghd).st_mode & 0o777 == 0o700 and
              sorted(os.listdir(ghd)) == ["acme,widgets,1234"],
              (lg, os.listdir(ghd) if os.path.isdir(ghd) else None))
        r, ls, rv2, lg = q("ACME/Widgets#1234", keep=True)
        check("#508: a second --ref within the TTL (case variant): same "
              "line, gh not run",
              not lg and len(rv2) == 1 and rv2[0] == rv[0], (lg, rv2))
        old = time.time() - 700
        os.utime(ent, (old, old))
        r, ls, rv2, lg = q("#1234", keep=True)
        check("#508: an entry past 10 min: gh runs again, entry renewed",
              lg and len(rv2) == 1 and time.time() - os.stat(ent).st_mtime
              < 60, (lg, rv2))
        neg = os.path.join(ghd, "acme,widgets,500")
        r, ls, rv, lg = q("#500")
        r2, ls, rv, lg2 = q("#500", keep=True)
        check("#508: a negative answer is cached: gh asked once, no line",
              lg and not lg2 and not rv and os.path.isfile(neg) and
              open(neg).read() == "none", (lg, lg2))
        os.utime(neg, (time.time() - 100, time.time() - 100))
        r, ls, rv, lg = q("#500", keep=True)
        check("#508: a none entry of 100 s still holds", not lg, lg)
        os.utime(neg, (time.time() - 130, time.time() - 130))
        r, ls, rv, lg = q("#500", keep=True)
        check("#508: a none entry past 2 min asks again", bool(lg), lg)
        r, ls, rv, lg = q("#600")
        check("#508: a timeout is not cached",
              not os.path.exists(os.path.join(ghd, "acme,widgets,600")), lg)
        for label, mk in (
                ("corrupt", lambda p: open(p, "w").write("{not json")),
                ("symlinked", lambda p: os.symlink(
                    os.path.join(T, "ghcache-target"), p))):
            q("#1234")
            tgt = os.path.join(T, "ghcache-target")
            with open(tgt, "w") as f:
                f.write('{"number":1234,"title":"Planted","state":"open",'
                        '"kind":"issue"}')
            os.unlink(ent)
            mk(ent)
            r, ls, rv, lg = q("#1234", keep=True)
            check("#508: a %s entry is ignored (gh runs, the real title)"
                  % label, lg and len(rv) == 1 and
                  rv[0]["title"] != "Planted", (lg, rv))
            if label == "corrupt":
                check("#508: a corrupt entry is overwritten",
                      open(ent).read().startswith("{\"number\":1234"),
                      open(ent).read())
            os.unlink(tgt)
        if os.geteuid() == 0:
            q("#1234")
            os.chown(ent, 12345, 12345)
            r, ls, rv, lg = q("#1234", keep=True)
            check("#508: a foreign-owned entry is ignored", bool(lg), lg)
        q("#1234")
        os.utime(ent, (time.time() + 365 * 86400,) * 2)
        r, ls, rv, lg = q("#1234", keep=True)
        check("#508: an entry stamped a year ahead is not fresh: gh runs",
              bool(lg), lg)
        q("#1234")
        r, ls, rv, lg = q("#1234", keep=True,
                          env=dict(genv, DCH_TEST_XRAY_UID="12345"))
        check("#508: an entry of another uid is ignored (non-root)",
              bool(lg), lg)
        q("#1234")
        os.chmod(ghd, 0o777)
        r, ls, rv, lg = q("#1234", keep=True)
        check("#508: a 0777 gh/ is tightened to 0700 and used",
              not lg and os.stat(ghd).st_mode & 0o777 == 0o700, lg)
        for label in ("gh", "dch"):
            shutil.rmtree(cache, ignore_errors=True)
            tgt = os.path.join(T, "ghcache-dir")
            shutil.rmtree(tgt, ignore_errors=True)
            os.makedirs(os.path.join(tgt, "gh"))
            os.makedirs(os.path.join(cache, "dch"))
            if label == "gh":
                os.rmdir(os.path.join(tgt, "gh"))
                os.symlink(tgt, ghd)
            else:
                os.rmdir(os.path.join(cache, "dch"))
                os.symlink(tgt, os.path.join(cache, "dch"))
            r, ls, rv, lg1 = q("#1234", keep=True)
            r, ls, rv, lg2 = q("#1234", keep=True)
            check("#508: a symlinked %s/: cache skipped, target empty, gh "
                  "runs each time" % label, lg1 and lg2 and
                  not [f for _, _, fs in os.walk(tgt) for f in fs], (lg1, lg2))
        for label, env, cfgtxt in (
                ("xray_github = false", genv, "xray_github = false\n"),
                ("DCH_NO_GITHUB=1", dict(genv, DCH_NO_GITHUB="1"), None)):
            if cfgtxt:
                with open(ENV["DCH_CONFIG"], "w") as f:
                    f.write(cfgtxt)
            try:
                q("#1234", env=env)
                check("#508: %s: no cache dir, nothing written" % label,
                      not os.path.exists(cache), label)
            finally:
                if cfgtxt:
                    os.unlink(ENV["DCH_CONFIG"])
        # ---- #522: light never waits for gh; a detached fill warms the cache
        def idle():
            """no fill's lock left, no fake gh and no --ref dch running"""
            ps = subprocess.run(["ps", "-A", "-o", "pid=,command="],
                                capture_output=True, text=True).stdout
            alive = []
            for p in (open(pids).read().split() if os.path.exists(pids)
                      else []):
                try:
                    os.kill(int(p), 0)
                    alive.append(p)
                except OSError:
                    pass
            return not alive and not any(
                f.endswith(".lock") for f in (os.listdir(ghd)
                                              if os.path.isdir(ghd) else [])) \
                and not [l for l in ps.splitlines()
                         if DCH in l and "--ref" in l]
        t0 = time.time()
        r, ls, rv, lg = q("acme/widgets#700", full=False)
        dt = time.time() - t0
        check("#522 light: gh sleeping 5 s: --ref returns in < 2.8 s with "
              "the mention counted and no resolved line",
              r.returncode == 0 and dt < 2.8 and not rv and
              any(l.get("tally", {}).get("count") == 1 for l in ls) and
              ls[-1].get("done") is True, (dt, [list(l) for l in ls]))
        check("#522: the hanging fill ends, nothing cached, no gh or dch "
              "left", wait_for(idle, 10) and
              not os.path.exists(os.path.join(ghd, "acme,widgets,700")),
              os.listdir(ghd) if os.path.isdir(ghd) else None)
        t0 = time.time()
        r, ls, rv, lg = q("acme/widgets#702", full=False)
        dt = time.time() - t0
        check("#522 light: gh answering in 0.3 s: the first --ref shows the "
              "title, in < 1 s", len(rv) == 1 and
              rv[0]["title"] == "Quick enough" and dt < 1, (dt, rv))
        t0 = time.time()
        r, ls, rv, lg = q("acme/widgets#701", full=False)
        dt = time.time() - t0
        check("#522 light: gh answering in 2 s: --ref returns in < 1.95 s "
              "with no resolved line", not rv and dt < 1.95, (dt, rv))
        ent = os.path.join(ghd, "acme,widgets,701")
        filled = wait_for(lambda: os.path.isfile(ent) and idle(), 10)
        r2, ls2, rv2, lg2 = q("acme/widgets#701", full=False, keep=True)
        check("#522 light: gh answering in 2 s: after the fill the next "
              "--ref shows the title from the cache, gh not run again",
              filled and len(rv2) == 1 and
              rv2[0]["title"] == "Slow but sure" and not lg2,
              (filled, rv, rv2, lg2))
        r, ls, rv, lg = q("acme/widgets#701", full=True)
        check("#522 --full: still waits for gh and shows the title",
              len(rv) == 1 and rv[0]["title"] == "Slow but sure" and
              "resolved" in ls[-2], (rv, lg))
        # ---- #530: an unusable cache starts no fill: light asks gh itself
        for label in ("a symlinked gh/", "a file in the gh/ path",
                      "the cache home a file"):
            shutil.rmtree(cache, ignore_errors=True)
            tgt = os.path.join(T, "ghcache-dir")
            shutil.rmtree(tgt, ignore_errors=True)
            os.makedirs(tgt)
            if label == "the cache home a file":
                open(cache, "w").close()
            else:
                os.makedirs(os.path.join(cache, "dch"))
                if label == "a symlinked gh/":
                    os.symlink(tgt, ghd)
                else:
                    open(ghd, "w").close()
            t0 = time.time()
            r, ls, rv, lg = q("acme/widgets#702", full=False, keep=True)
            dt = time.time() - t0
            check("#530 light: %s: gh asked directly, the title in < 1 s, "
                  "nothing written" % label, len(rv) == 1 and
                  rv[0]["title"] == "Quick enough" and dt < 1 and lg and
                  not os.listdir(tgt), (dt, rv, lg))
            if os.path.isfile(cache):
                os.unlink(cache)
        for label, off in (("an hour ahead", 3600), ("61 s old", -61)):
            shutil.rmtree(cache, ignore_errors=True)
            os.makedirs(ghd)
            lock = os.path.join(ghd, "acme,widgets,702.lock")
            open(lock, "w").close()
            os.utime(lock, (time.time() + off,) * 2)
            r, ls, rv, lg = q("acme/widgets#702", full=False, keep=True)
            check("#530 light: a fill lock %s is stale: broken, filled, the "
                  "title" % label, len(rv) == 1 and
                  rv[0]["title"] == "Quick enough" and lg and
                  wait_for(idle, 10), (rv, lg, os.listdir(ghd)))
        for label in ("", ", a 61 s old lock taken over by one"):
            shutil.rmtree(cache, ignore_errors=True)
            open(log, "w").close()
            if label:
                os.makedirs(ghd)
                lock = os.path.join(ghd, "acme,widgets,703.lock")
                open(lock, "w").close()
                os.utime(lock, (time.time() - 61,) * 2)
            ps5 = [subprocess.Popen([DCH, "--xray", n, "--ref",
                                     "acme/widgets#703", "--scope", "session",
                                     "--json"], env=genv,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
                   for _ in range(5)]
            for p in ps5:
                p.wait(30)
            wait_for(idle, 10)
            runs = open(log).read().count("issues/703")
            check("#530 light: 5 refs at once on a 1 s gh run gh once (the "
                  "O_EXCL lock%s)" % label, runs == 1 and
                  not [f for f in os.listdir(ghd) if ".lock" in f],
                  (runs, os.listdir(ghd)))
        pg = pids + ".pgid"
        r, ls, rv, lg = q("acme/widgets#704", full=False)
        wait_for(lambda: os.path.exists(pg) and idle(), 10)
        fpg = open(pg).read().strip() if os.path.exists(pg) else ""
        check("#530 light: the fill leaves the caller's process group "
              "(setsid)", fpg.isdigit() and int(fpg) != os.getpgrp(),
              (fpg, os.getpgrp()))
        check("#522: no stray gh or dch children left", wait_for(idle, 10),
              open(pids).read() if os.path.exists(pids) else None)
    finally:
        dch("-k", n)


def rq_no_github(n, genv, fb, log, cache):
    """#518: --no-github in a --requests request, to a loopback host (the
    harness of tests/remote_client_test.py) whose PATH holds the fake gh"""
    sys.path.insert(0, HERE)
    from remote_client_test import OWNER, WHOIS
    from serve_lib import fake_ts
    ts = os.path.join(T, "fakets")
    os.makedirs(ts)
    fake_ts(ts)
    for nm, o in (("status.json", OWNER), ("whois.json", WHOIS)):
        with open(os.path.join(ts, nm), "w") as f:
            f.write(json.dumps(o))
    genv = dict(genv, PATH=fb + ":" + ts + ":/usr/bin:/bin")
    slog = os.path.join(T, "serve.log")
    with open(slog, "w") as lf:
        sp = subprocess.Popen([DCH, "serve", "--bind", "127.0.0.1",
                               "--port", "0"],
                              env=dict(genv, DCH_SERVE_TEST_LOOPBACK="1"),
                              stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=lf)
    try:
        port = None
        end = time.time() + 10
        while time.time() < end and port is None and sp.poll() is None:
            m = re.search(r"listening on 127\.0\.0\.1:(\d+)",
                          open(slog).read())
            port = int(m.group(1)) if m else None
            time.sleep(0.05)
        check("#518: a loopback host for the requests rows", port,
              open(slog).read())
        if not port:
            return
        cfg = os.path.join(T, "rconfig")
        with open(cfg, "w") as f:
            f.write("remote_hosts = srv=127.0.0.1:%d\n" % port)
        cenv = {k: v for k, v in ENV.items() if k != "DCH_NO_REMOTE"}
        cenv.update(DCH_CONFIG=cfg, XDG_CACHE_HOME=os.path.join(T, "ccache"),
                    PATH=ts + ":/usr/bin:/bin")
        rp = subprocess.Popen([DCH, "--xray", "srv:" + n, "--requests",
                               "--json"], env=cenv, text=True,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)

        def req(i, argv):
            shutil.rmtree(cache, ignore_errors=True)
            open(log, "w").close()
            rp.stdin.write(json.dumps({"req": i, "argv": argv}) + "\n")
            rp.stdin.flush()
            got = []
            while True:
                l = rp.stdout.readline()
                if not l:
                    break
                o = json.loads(l)
                got.append(o)
                if o.get("req") == i and "exit" in o:
                    break
            return got, open(log).read()

        rd = rp.stdout.readline()	# ready
        check("#518: the requests client is ready", "ready" in rd,
              (rd, rp.poll() is not None and rp.stderr.read()))
        rf = ["--ref", "#1234", "--scope", "session", "--json"]
        for label, argv, gh in (("--no-github", rf + ["--no-github"], False),
                                ("no flag", rf, True)):
            got, lg = req(1, argv)
            rv = [g for g in got if "resolved" in json.dumps(g.get("line"))]
            check("#518: --requests to a host, %s: %s" % (label,
                  "gh runs, the line comes" if gh else
                  "no resolved line, gh never ran"),
                  got and got[-1].get("exit") == 0 and
                  bool(lg) == gh and bool(rv) == gh, (got[-3:], lg))
        got, lg = req(2, ["--json", "--no-github"])
        check("#518: --requests: --no-github without --ref is refused here",
              got and got[-1].get("exit") == 2 and any(
                  "--no-github comes with --ref" in g.get("stderr", "")
                  for g in got), got)
        rp.stdin.close()
        rp.wait(timeout=10)
        open(log, "w").close()
        shutil.rmtree(cache, ignore_errors=True)
        r = dch("--xray", "srv:" + n, *rf, "--no-github", env=cenv)
        check("#518: --xray host:name --no-github: forwarded, gh never ran",
              r.returncode == 0 and "resolved" not in r.stdout and
              not open(log).read(), (r.stderr, r.stdout[-300:]))
    finally:
        sp.terminate()
        sp.wait()


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
        r4, ls4, rv4 = ref(n, short(signed, 12), full=False)
        r5, ls5, rv5 = ref(n, short(parent, 12), full=False)
        check("hostile #502: light resolves the signed commit (not on HEAD) "
              "and HEAD's parent (on HEAD) under the same guards",
              len(rv4) == 1 and rv4[0]["subtitle"].endswith(" not on HEAD")
              and len(rv5) == 1 and rv5[0]["subtitle"].endswith(" · on HEAD"),
              (rv4, rv5, r4.stderr, r5.stderr))
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
