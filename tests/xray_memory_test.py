#!/usr/bin/env python3
"""`dch --xray <n> --memory --json` (#322): a session's memories and the
instruction files in force, as JSON lines.

  - a Claude session (an ended history record): the memory/ directory beside
    its transcript, the MEMORY.md index (title, hook, bullet and dash forms,
    a target with '/' or a URL refused, the first of two lines for one file,
    a named file that is gone), frontmatter (name, description, top-level
    and metadata.type, quotes, an unknown type), no frontmatter, a body cut
    at 16 KB, a file over 1 MB with no body, invalid UTF-8, a line of
    escapes kept under 64 KB, a symlink out of the directory and a FIFO
    listed and never read, a directory / dot file / .txt / the index never
    a row
  - session wrote/loaded from Write, Edit, Read and Bash (a redirect or an
    editing verb writes), wrote over loaded, a sidechain's calls ignored,
    chapter the newest turn's anchor (one of --json's, and --chapter opens
    it); the order: session rows newest first, the rest newest mtime first,
    instruction files last in load order
  - instruction files: the user CLAUDE.md and rules, per directory from the
    repo root (.git) to the cwd CLAUDE.md, CLAUDE.local.md and
    .claude/rules/** for every ancestor of the cwd (~/CLAUDE.md above the
    repo too; the config dir's files once); a symlinked instruction file
    read through to a regular target under its own path, one to a FIFO
    listed and never read; AGENTS.md when no CLAUDE.md
  - no transcript: no memory rows, the instruction files still; a shell:
    source none; usage errors
  - a live Codex (tests/fake_codex.c): $CODEX_HOME's AGENTS.md, the repo's
    AGENTS.override.md over AGENTS.md, a shell call loaded, an apply_patch
    with a relative path wrote
  - a peer's --memory through `dch serve` (loopback, a fake tailscale)
    answers as the host's own run does
  - partial when the transcript's read range may drop records
  - nothing written under HOME

Run:  python3 tests/xray_memory_test.py        (uses ./dch; run.sh isolates)
"""
import json, os, re, shutil, signal, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from remote_client_test import OWNER, WHOIS  # noqa: E402
from xray_deny_test import FAKE_TS  # noqa: E402

DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
SID = "11111111-2222-4333-8444-5555555553a2"
SID2 = "11111111-2222-4333-8444-5555555553a3"
TID = "019a2222-3333-7444-8555-6666777783a2"
SECRET = "Zq7MemorySecret"
fails = [0]


def check(msg, cond, detail=""):
    print("  %s %s" % ("ok  " if cond else "FAIL", msg))
    if not cond:
        fails[0] += 1
        if detail:
            print("       " + str(detail)[:1500])


T = os.path.realpath(tempfile.mkdtemp(prefix="dxm",
                                      dir=os.environ.get("TMPDIR", "/tmp")))
HOME = os.path.join(T, "home")
CFG = os.path.join(HOME, ".claude")
PDIR = os.path.join(CFG, "projects", "-repo-sub")
MEM = os.path.join(PDIR, "memory")
REPO = os.path.join(HOME, "repo")
SUB = os.path.join(REPO, "sub")
SOCK = os.path.join(T, "dch-%d" % os.getuid())
ENV = {k: v for k, v in os.environ.items()
       if not k.startswith(("DCH_", "CLAUDE", "CODEX", "Malloc"))}
ENV.update(XDG_RUNTIME_DIR=T, XDG_STATE_HOME=os.path.join(T, "st"),
           HOME=HOME, DCH_CONFIG=os.path.join(T, "config"),
           DCH_LOGIN_ENV="0", DCH_NO_REMOTE="1", DCH_RECORD_MS="1000",
           SHELL="/bin/sh")


def write(p, data, mtime=None):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data.encode() if isinstance(data, str) else data)
    if mtime:
        os.utime(p, (mtime, mtime))


def run(*a, env=None, timeout=30):
    return subprocess.run([DCH] + list(a), capture_output=True,
                          timeout=timeout, env=env or ENV,
                          stdin=subprocess.DEVNULL)


def memory(name, env=None):
    """(rc, header, rows, done, raw stdout, stderr)"""
    r = run("--xray", name, "--memory", "--json", env=env)
    lines = r.stdout.split(b"\n")
    objs = []
    try:
        objs = [json.loads(x) for x in lines if x]
    except ValueError:
        pass
    rows = [o["memory"] for o in objs if "memory" in o]
    return (r.returncode, objs[0] if objs else None, rows,
            objs[-1] if objs else None, r.stdout, r.stderr.decode())


def record(name, sid, cwd):
    hdir = os.path.join(T, "st", "dch", "sessions", SOCK.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    rec = {"name": name, "cwd": cwd, "end": "exited", "argv": ["claude"],
           "started": 1794299400, "updated": 1794300000,
           "ended": 1794300000, "sock_dir": SOCK}
    if sid:
        rec["agent"] = {"session_id": sid, "cwd": cwd, "config_dir": CFG}
    with open(os.path.join(hdir, name + ".json"), "w") as f:
        json.dump(rec, f)


def turn(who, content, side=False):
    o = {"type": who, "cwd": SUB, "message": {"role": who,
                                              "content": content}}
    if side:
        o["isSidechain"] = True
    return json.dumps(o) + "\n"


def tool(name, **inp):
    tool.n += 1
    return [{"type": "tool_use", "id": "toolu_%d" % tool.n,
             "name": name, "input": inp}]


tool.n = 0


def mempath(n):
    return os.path.join(MEM, n)


# ---- fixtures -------------------------------------------------------------

def fixtures():
    os.makedirs(os.path.join(REPO, ".git"))
    os.makedirs(SUB)
    write(os.path.join(HOME, "CLAUDE.md"), "above the repo\n")
    write(os.path.join(T, "target.md"), "through a symlink\n")
    os.makedirs(os.path.join(SUB, ".claude"))
    os.symlink(os.path.join(T, "target.md"),
               os.path.join(SUB, ".claude", "CLAUDE.md"))
    os.mkfifo(os.path.join(T, "ififo"))
    write(os.path.join(REPO, "CLAUDE.md"), "repo rules\n")
    write(os.path.join(REPO, ".claude", "rules", "a.md"), "rule a\n")
    write(os.path.join(REPO, ".claude", "rules", "deep", "b.md"), "rule b\n")
    write(os.path.join(REPO, ".claude", "rules", "c.txt"), "not md\n")
    os.symlink(os.path.join(T, "ififo"),
               os.path.join(REPO, ".claude", "rules", "link.md"))
    write(os.path.join(SUB, "CLAUDE.local.md"), "local\n")
    write(os.path.join(SUB, "AGENTS.md"), "a CLAUDE.md is in force\n")
    write(os.path.join(CFG, "CLAUDE.md"), "user rules\n")
    write(os.path.join(CFG, "rules", "u.md"), "user rule\n")
    write(os.path.join(T, "secret.md"), SECRET + "\n")
    base = 1794200000
    write(mempath("MEMORY.md"), "# Memory\n"
          "- [Alpha Title](alpha.md) — alpha hook\n"
          "* [Beta](beta.md) - beta hook\n"
          "+ [Gamma](gamma.md): gamma hook\n"
          "- [Dup second](alpha.md) — not this one\n"
          "- [Escape](../secret.md) — refused\n"
          "- [Url](https://x.example/a.md) — refused\n"
          "- [Gone](gone.md) – its file is gone\n", base)
    write(mempath("alpha.md"), "---\nname: alpha name\ndescription: alpha "
          "desc\nmetadata:\n  type: feedback\n---\nalpha body\n", base + 1)
    write(mempath("beta.md"), "---\nname: 'beta name'\ndescription: \"beta "
          "desc\"\ntype: \"project\"\n---\nbeta body\n", base + 2)
    write(mempath("gamma.md"), "gamma has no frontmatter\n", base + 3)
    write(mempath("delta.md"), "---\nname: Delta Name\ndescription: delta "
          "desc\ntype: weird\n---\ndelta\n", base + 4)
    write(mempath("epsilon.md"), "---\ndescription: eps desc\ntype: user\n"
          "---\n", base + 5)
    write(mempath("big.md"), "x" * ((1 << 20) + 1), base + 6)
    write(mempath("long.md"), "é" * 10240, base + 7)  # 20 KB
    write(mempath("bad.md"), b"bad \xff\xfe utf8\n", base + 8)
    write(mempath("esc.md"), "\x01" * 16000, base + 9)
    os.symlink(os.path.join(T, "secret.md"), mempath("link.md"))
    os.mkfifo(mempath("fifo.md"))
    os.utime(mempath("fifo.md"), (base - 5, base - 5))
    os.utime(mempath("link.md"), (base - 10, base - 10),
             follow_symlinks=False)
    os.mkdir(mempath("dir.md"))
    write(mempath(".hidden.md"), "dot file\n")
    write(mempath("notes.txt"), "not md\n")
    write(os.path.join(PDIR, SID + ".jsonl"), "".join([
        turn("user", "first question"),
        turn("assistant", tool("Read", file_path=mempath("gamma.md"))),
        turn("user", "second question"),
        turn("assistant", tool("Write", file_path=mempath("alpha.md"),
                               content="x")),
        turn("assistant", tool("Bash", command="cat %s 2>/dev/null"
                               % mempath("beta.md"))),
        turn("assistant", tool("Bash", command="echo x >> '%s'"
                               % mempath("delta.md"))),
        turn("assistant", tool("Write", file_path=mempath("long.md"),
                               content="y"), side=True),
        turn("user", "third question"),
        turn("assistant", tool("Read", file_path=mempath("alpha.md"))),
        turn("assistant", tool("Read", file_path=os.path.join(REPO,
                                                              "CLAUDE.md"))),
        turn("assistant", tool("Bash", command="sed -i s/a/b/ %s"
                               % mempath("gamma.md"))),
        "{not json\n",
    ]))
    record("xa", SID, SUB)


def snapshot_tree(root):
    out = {}
    for dp, dn, fn in os.walk(root):
        for f in dn + fn:
            p = os.path.join(dp, f)
            st = os.lstat(p)
            out[p] = (st.st_size, st.st_mtime_ns)
    return out


# ---- a Claude session ------------------------------------------------------

def test_claude():
    before = snapshot_tree(HOME)
    t0 = time.time()
    rc, hd, rows, done, raw, err = memory("xa")
    check("a FIFO in the directory does not hang the answer (%.1fs)"
          % (time.time() - t0), time.time() - t0 < 20)
    check("exit 0, the header line", rc == 0 and hd == {
        "schema": 1, "source": "claude", "title": None, "partial": False},
        (rc, hd, err))
    rows = [r for r in rows if r["scope"] != "managed"]  # the host's own
    by = {r["id"]: r for r in rows}
    check("the done line counts every row",
          done and done.get("done") is True and done["dropped"] == 0 and
          done["count"] == raw.count(b'{"memory":'), done)
    check("every line is under 64 KB",
          all(len(x) <= 64 * 1024 for x in raw.split(b"\n")),
          max(len(x) for x in raw.split(b"\n")))
    mems = [r for r in rows if r["kind"] != "instructions"]
    check("one row per memory file, the index's targets included",
          sorted(r["id"] for r in mems) ==
          sorted(["alpha", "beta", "gamma", "gone", "delta", "epsilon",
                  "big", "long", "bad", "esc", "link", "fifo"]),
          [r["id"] for r in mems])
    check("never the index, a dot file, a directory, a .txt or ../",
          not set(by) & {"MEMORY", ".hidden", "dir", "notes", "secret"} and
          not any("secret" in r["path"] for r in mems))
    a, b, g = by.get("alpha", {}), by.get("beta", {}), by.get("gamma", {})
    check("alpha: the index title and hook (first line), metadata.type",
          (a.get("title"), a.get("note"), a.get("kind"), a.get("scope"),
           a.get("path")) == ("Alpha Title", "alpha hook", "feedback",
                              "project", mempath("alpha.md")), a)
    check("beta: '* [..](..) - hook', a quoted top-level type",
          (b.get("title"), b.get("note"), b.get("kind")) ==
          ("Beta", "beta hook", "project"), b)
    check("gamma: '+ [..](..): hook', no frontmatter: kind null",
          (g.get("title"), g.get("note"), g.get("kind"), g.get("body")) ==
          ("Gamma", "gamma hook", None, "gamma has no frontmatter\n"), g)
    d, e = by.get("delta", {}), by.get("epsilon", {})
    check("delta (not indexed): name, description; an unknown type: null",
          (d.get("title"), d.get("note"), d.get("kind")) ==
          ("Delta Name", "delta desc", None), d)
    check("epsilon: no name: the id is the title",
          (e.get("title"), e.get("note"), e.get("kind")) ==
          ("epsilon", "eps desc", "user"), e)
    gone = by.get("gone", {})
    check("gone: the index's row stands, no file: updated and body null",
          (gone.get("title"), gone.get("note"), gone.get("updated"),
           gone.get("body")) == ("Gone", "its file is gone", None, None),
          gone)
    check("updated is the file's mtime, ISO UTC",
          a.get("updated") and a["updated"] == time.strftime(
              "%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.stat(
                  mempath("alpha.md")).st_mtime)), a.get("updated"))
    big, lg = by.get("big", {}), by.get("long", {})
    check("over 1 MB: listed, body null",
          big.get("body") is None and big.get("updated") and
          "cut" not in big, big.get("updated"))
    check("20 KB: body cut at 16 KB on a character boundary, cut:true",
          lg.get("cut") is True and lg.get("body") and
          len(lg["body"].encode()) <= 16384 and
          len(lg["body"].encode()) > 16000 and
          set(lg["body"]) == {"é"}, len(lg.get("body") or ""))
    check("invalid UTF-8: U+FFFD",
          "�" in (by.get("bad", {}).get("body") or ""),
          by.get("bad"))
    es = by.get("esc", {})
    check("a body of escapes: cut until the line fits",
          es.get("cut") is True and es.get("body") and
          set(es["body"]) == {"\x01"}, len(es.get("body") or ""))
    ln, ff = by.get("link", {}), by.get("fifo", {})
    check("a symlink out of the directory: listed, never followed",
          ln.get("body") is None and SECRET.encode() not in raw, ln)
    check("a FIFO: listed, never read", ff.get("body") is None and
          ff.get("updated"), ff)
    # the session
    sess = {r["id"]: (r["session"]) for r in mems if r["session"]}
    check("session: Write and an editing verb wrote, Read and a plain "
          "shell read loaded, wrote over loaded, a sidechain ignored",
          sess == {"alpha": "wrote", "beta": "loaded", "delta": "wrote",
                   "gamma": "wrote"}, sess)
    r = run("--xray", "xa", "--json")
    chs = []
    try:
        chs = json.loads(r.stdout)["chapters"]
    except (ValueError, KeyError):
        pass
    anchors = [c["anchor"] for c in chs]
    check("three chapters in --json", len(anchors) == 3, (chs, r.stderr))
    if len(anchors) == 3:
        check("chapter: the newest winning turn's anchor",
              (a.get("chapter"), b.get("chapter"), d.get("chapter"),
               g.get("chapter")) == (anchors[1], anchors[1], anchors[1],
                                     anchors[2]),
              (anchors, [(x["id"], x["chapter"]) for x in mems]))
        c = run("--xray", "xa", "--chapter", g.get("chapter") or "-",
                "--json")
        check("--chapter opens it", c.returncode == 0 and
              b"third question" in c.stdout, c.stderr)
    check("no session touch: session and chapter null",
          all(r["chapter"] is None for r in mems if not r["session"]))
    order = [r["id"] for r in mems]
    check("order: session rows newest first, then newest mtime first",
          order == ["gamma", "delta", "beta", "alpha", "esc", "bad", "long",
                    "big", "epsilon", "fifo", "link", "gone"], order)
    ins = [r for r in rows if r["kind"] == "instructions"]
    check("instruction files: user, then every ancestor root first (~/"
          "CLAUDE.md above the repo, the config dir's files not twice), "
          "rules recursive, no AGENTS.md while CLAUDE.md is in force",
          [(r["path"], r["scope"]) for r in ins] ==
          [(os.path.join(CFG, "CLAUDE.md"), "user"),
           (os.path.join(CFG, "rules", "u.md"), "user"),
           (os.path.join(HOME, "CLAUDE.md"), "project"),
           (os.path.join(REPO, "CLAUDE.md"), "project"),
           (os.path.join(REPO, ".claude", "rules", "a.md"), "project"),
           (os.path.join(REPO, ".claude", "rules", "deep", "b.md"),
            "project"),
           (os.path.join(REPO, ".claude", "rules", "link.md"), "project"),
           (os.path.join(SUB, ".claude", "CLAUDE.md"), "project"),
           (os.path.join(SUB, "CLAUDE.local.md"), "project")],
          [(r["path"], r["scope"]) for r in ins])
    ib = {r["path"]: r for r in ins}
    rr = ib.get(os.path.join(REPO, "CLAUDE.md"), {})
    check("instruction files last, id and title the path, the body read",
          rows[-len(ins):] == ins and all(
              r["id"] == r["title"] == r["path"] for r in ins) and
          rr.get("body") == "repo rules\n" and
          rr.get("session") == "loaded" and rr.get("chapter") == (
              anchors[2] if len(anchors) == 3 else None), rr)
    sl = ib.get(os.path.join(SUB, ".claude", "CLAUDE.md"), {})
    check("a symlinked CLAUDE.md: read through to its regular target, "
          "path the link's own",
          sl.get("body") == "through a symlink\n" and sl.get("updated"), sl)
    fl = ib.get(os.path.join(REPO, ".claude", "rules", "link.md"), {})
    check("a symlinked rule to a FIFO: listed, never read",
          fl and fl.get("body") is None, fl)
    check("nothing written under HOME", snapshot_tree(HOME) == before)
    # the read range lowered (tests only): records may be dropped
    rc, hd, rows, done, raw, err = memory("xa", env=dict(
        ENV, DCH_XRAY_CAPS="0:0:0:300:300"))
    check("a transcript past the read range: partial true, rows still",
          rc == 0 and hd and hd["partial"] is True and
          b'"alpha"' in raw, (rc, hd, err))


def test_agents_fallback():
    """No CLAUDE.md anywhere: AGENTS.md, .claude/AGENTS.md"""
    proj = os.path.join(T, "agproj")
    os.makedirs(os.path.join(proj, ".git"))
    write(os.path.join(proj, "AGENTS.md"), "ag\n")
    write(os.path.join(proj, ".claude", "AGENTS.md"), "ag2\n")
    env = dict(ENV, HOME=os.path.join(T, "home2"))
    os.makedirs(env["HOME"])
    hdir = os.path.join(T, "st", "dch", "sessions", SOCK.replace("/", "-"))
    with open(os.path.join(hdir, "xg.json"), "w") as f:
        json.dump({"name": "xg", "cwd": proj, "end": "exited",
                   "argv": ["claude"], "started": 1794299400,
                   "updated": 1794300000, "ended": 1794300000,
                   "sock_dir": SOCK,
                   "agent": {"session_id": SID2, "cwd": proj,
                             "config_dir": os.path.join(env["HOME"],
                                                        ".claude")}}, f)
    rc, hd, rows, done, raw, err = memory("xg", env=env)
    rows = [r for r in rows if r["scope"] != "managed"]
    check("no transcript: no memory rows, partial false, the instruction "
          "files: AGENTS.md when no CLAUDE.md is in force",
          rc == 0 and hd and hd["source"] == "claude" and
          not hd["partial"] and [r["path"] for r in rows] ==
          [os.path.join(proj, "AGENTS.md"),
           os.path.join(proj, ".claude", "AGENTS.md")], (rc, hd, rows, err))


def test_shell_and_usage():
    record("xs", None, SUB)
    rc, hd, rows, done, raw, err = memory("xs")
    check("a shell session: source none, no rows",
          rc == 0 and hd and hd["source"] == "none" and rows == [] and
          done == {"done": True, "count": 0, "dropped": 0}, (rc, raw, err))
    for a, why in ((["--memory"], "no --json"),
                   (["--memory", "--memory", "--json"], "twice"),
                   (["--memory", "--follow", "--json"], "--follow"),
                   (["--memory", "--search", "abc", "--json"], "--search"),
                   (["--memory", "--chapter", "f1:0", "--json"],
                    "--chapter"),
                   (["--memory", "--scope", "all", "--json"], "--scope")):
        r = run("--xray", "xa", *a)
        check("bad usage (exit 2): --memory with %s" % why,
              r.returncode == 2 and r.stdout == b"", (r.returncode,
                                                      r.stderr))
    r = run("--xray", "nosuch", "--memory", "--json")
    check("no such session: exit 1", r.returncode == 1 and
          b"no such session" in r.stderr, r.stderr)


# ---- a live Codex ----------------------------------------------------------

def cxline(sec, typ, payload):
    return json.dumps({"timestamp": "2026-11-10T10:00:%02d.000Z" % sec,
                       "type": typ, "payload": payload}) + "\n"


def cxuser(sec, text):
    return cxline(sec, "response_item", {
        "type": "message", "role": "user",
        "content": [{"type": "input_text", "text": text}]})


def test_codex():
    fake = os.path.join(T, "bin", "codex")
    os.makedirs(os.path.dirname(fake))
    b = subprocess.run(["/usr/bin/cc", "-o", fake,
                        os.path.join(HERE, "fake_codex.c")],
                       capture_output=True, text=True, timeout=60)
    check("the fake codex builds", b.returncode == 0, b.stderr)
    codex = os.path.join(HOME, ".codex")
    repo = os.path.join(T, "cxrepo")
    sub = os.path.join(repo, "sub")
    os.makedirs(os.path.join(repo, ".git"))
    os.makedirs(sub)
    write(os.path.join(codex, "AGENTS.md"), "codex user\n")
    write(os.path.join(repo, "AGENTS.md"), "shadowed\n")
    write(os.path.join(repo, "AGENTS.override.md"), "override\n")
    write(os.path.join(sub, "AGENTS.md"), "sub agents\n")
    write(os.path.join(sub, "CLAUDE.md"), "not Codex's\n")
    log = os.path.join(T, "cx.log")
    n = "xm-cx"
    r = run("--spawn", n, "--size", "80x24", "--cwd", sub, fake,
            env=dict(ENV, FAKE_CODEX_LOG=log))
    check("codex: spawned", r.returncode == 0, r.stderr)
    try:
        end, got = time.time() + 10, None
        while time.time() < end and not got:
            got = os.path.exists(log) and open(log).read().endswith("\n") \
                and open(log).read().split()
            time.sleep(0.05)
        check("codex: the fake runs", bool(got))
        if not got:
            return
        day = os.path.join(codex, "sessions", "2026", "11", "10")
        write(os.path.join(day, "rollout-2026-11-10T10-00-00-%s.jsonl"
                           % TID),
              cxline(0, "session_meta", {"id": TID, "cwd": sub,
                                         "cli_version": "0.167.0"}) +
              cxuser(1, "read the notes") +
              cxline(2, "response_item", {
                  "type": "function_call", "name": "shell", "call_id": "c1",
                  "arguments": json.dumps({"command": [
                      "bash", "-lc", "cat " + os.path.join(codex,
                                                          "AGENTS.md")]})}) +
              cxuser(3, "now edit") +
              cxline(4, "response_item", {
                  "type": "custom_tool_call", "name": "apply_patch",
                  "call_id": "c2",
                  "input": "*** Begin Patch\n*** Update File: AGENTS.md\n"
                           "@@\n-a\n+b\n*** End Patch\n"}))
        snaps = os.path.join(codex, "shell_snapshots")
        os.makedirs(snaps, exist_ok=True)
        marker = got[1] if got[1] != "-" else None
        write(os.path.join(snaps, TID + ".1.sh"),
              "# Snapshot file\nexport DCH_SESSION=%s\n%s" % (
                  n, "export DCH_NATIVE_BRIDGE_ID=%s\n" % marker
                  if marker else ""))
        rc, hd, rows, done, raw, err = memory(n)
        check("codex: source codex", rc == 0 and hd and
              hd["source"] == "codex" and not hd["partial"], (rc, raw, err))
        got_rows = [(r["path"], r["scope"], r["session"]) for r in rows]
        check("codex: $CODEX_HOME/AGENTS.md, the root's override over its "
              "AGENTS.md, the cwd's AGENTS.md; a shell read loaded, a "
              "relative apply_patch wrote; no CLAUDE.md",
              got_rows == [(os.path.join(codex, "AGENTS.md"), "user",
                            "loaded"),
                           (os.path.join(repo, "AGENTS.override.md"),
                            "project", None),
                           (os.path.join(sub, "AGENTS.md"), "project",
                            "wrote")], got_rows)
        check("codex: every row kind instructions", rows and all(
            r["kind"] == "instructions" for r in rows))
    finally:
        run("-k", n)


# ---- a peer's --memory through dch serve -----------------------------------

def test_remote():
    fake = os.path.join(T, "fake")
    os.makedirs(fake)
    with open(os.path.join(fake, "tailscale"), "w") as f:
        f.write(FAKE_TS)
    os.chmod(os.path.join(fake, "tailscale"), 0o755)
    for name, obj in (("status.json", OWNER), ("whois.json", WHOIS)):
        with open(os.path.join(fake, name), "w") as f:
            json.dump(obj, f)
    with open(os.path.join(fake, "ip.txt"), "w") as f:
        f.write("100.100.1.1\n")
    senv = dict(ENV, DCH_SERVE_TEST_LOOPBACK="1",
                PATH=fake + ":/usr/bin:/bin")
    cdir = os.path.join(T, "cli")
    os.makedirs(os.path.join(cdir, "home"))
    cenv = dict(senv, XDG_RUNTIME_DIR=cdir, XDG_STATE_HOME=cdir + "/st",
                HOME=cdir + "/home", DCH_CONFIG=cdir + "/config",
                DCH_REMOTE_BOX_MS="5000")
    del cenv["DCH_NO_REMOTE"], cenv["DCH_SERVE_TEST_LOOPBACK"]
    log = os.path.join(T, "serve.log")
    with open(log, "w") as lf:
        sp = subprocess.Popen([DCH, "serve", "--bind", "127.0.0.1",
                               "--port", "0"], env=senv,
                              stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=lf)
    try:
        port, end = None, time.time() + 30
        while time.time() < end and port is None and sp.poll() is None:
            m = re.search(r"listening on 127\.0\.0\.1:(\d+)",
                          open(log).read())
            port = int(m.group(1)) if m else None
            time.sleep(0.05)
        check("dch serve listens on a loopback test port", port is not None,
              open(log).read()[-600:])
        if port is None:
            return
        with open(cenv["DCH_CONFIG"], "w") as f:
            f.write("remote_hosts = srv=127.0.0.1:%d\n" % port)
        loc = run("--xray", "xa", "--memory", "--json", env=senv)
        rem = run("--xray", "srv:xa", "--memory", "--json", env=cenv,
                  timeout=60)
        check("a peer's --memory answers as the host's own dch (%d bytes)"
              % len(loc.stdout),
              loc.returncode == 0 and b'"memory"' in loc.stdout and
              (rem.returncode, rem.stdout) == (0, loc.stdout),
              (loc.returncode, loc.stderr[:200], rem.returncode,
               rem.stdout[:300], rem.stderr[:300]))
        r = run("--xray", "srv:xa", "--memory", "--follow", "--json",
                env=cenv)
        check("a peer's --memory --follow: bad usage, never sent",
              r.returncode == 2, (r.returncode, r.stderr))
    finally:
        if sp.poll() is None:
            sp.send_signal(signal.SIGTERM)
            try:
                sp.wait(10)
            except subprocess.TimeoutExpired:
                sp.kill()
                sp.wait(10)


def main():
    try:
        fixtures()
        test_claude()
        test_agents_fallback()
        test_shell_and_usage()
        test_codex()
        test_remote()
    finally:
        shutil.rmtree(T, ignore_errors=True)
    print("all ok" if not fails[0] else "%d FAILED" % fails[0])
    return 1 if fails[0] else 0


if __name__ == "__main__":
    sys.exit(main())
