#!/usr/bin/env python3
"""A new session's hyperlink signal (#122, v1.41.0 QA MED-2).

dch keeps OSC 8 in its mirror (#069), but under TERM=xterm-256color with no
TERM_PROGRAM, Claude Code would strip its statusline's links. v1.41.0 set
FORCE_HYPERLINK=1 for the whole session, and supports-hyperlinks (many Node
CLIs) honours that before it checks isTTY: OSC 8 went into pipes and files.
Now a new session gets LC_TERMINAL=ghostty instead, which Claude Code's
check reads (its last resort, past isTTY) and supports-hyperlinks never
does. Checked here, on the environment of the session command itself and
of a program a shell inside the session starts:
  1. no FORCE_HYPERLINK, LC_TERMINAL=ghostty;
  2. supports-hyperlinks' decision (3.2.0, the copy Claude Code bundles)
     for a pipe is false, for the session's tty false too (so the signal
     is not one it reads at all), and Claude Code's (2.1.318's order) on
     the tty is true: the statusline keeps its links;
  3. a caller's FORCE_HYPERLINK, LC_TERMINAL and an --env LC_TERMINAL=
     win;
  4. when node and an installed supports-hyperlinks are found: the real
     module, run in the session with stdout a pipe, says false (and true
     with FORCE_HYPERLINK=1, so the probe can see a leak);
  5. when a claude binary is found: the check it ships still reads
     LC_TERMINAL against a list with "ghostty".

Run:  python3 tests/hyperlink_env_test.py            (uses ./dch)
"""
import glob, json, os, re, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
ROOT = tempfile.mkdtemp(prefix="dchfh", dir="/tmp")
ENV = dict(os.environ, DCH_SOCKET_DIR=os.path.join(ROOT, "s"),
           XDG_STATE_HOME=os.path.join(ROOT, "state"),
           HOME=os.path.join(ROOT, "home"),
           DCH_CONFIG=os.path.join(ROOT, "config"), DCH_LOGIN_ENV="0",
           DCH_NO_REMOTE="1", TERM="xterm-256color")
for k in ("DCH_SESSION", "MallocStackLogging", "MallocStackLoggingNoCompact",
          "FORCE_HYPERLINK", "LC_TERMINAL", "TERM_PROGRAM",
          "TERM_PROGRAM_VERSION", "WT_SESSION", "VTE_VERSION", "NETLIFY",
          "TERMINAL_EMULATOR", "CLAUDECODE"):
    ENV.pop(k, None)
failures = []
names = []
LINKS = ["ghostty", "Hyper", "kitty", "alacritty", "iTerm.app", "iTerm2",
         "WarpTerminal"]


def check(what, ok, *extra):
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)
        for e in extra:
            print("     " + repr(e))


def dch(*args, env=None):
    return subprocess.run([DCH] + list(args), env=env or ENV,
                          capture_output=True, text=True, timeout=60)


def wait_file(p, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(p) and os.path.getsize(p):
            time.sleep(0.1)
            with open(p) as f:
                return f.read()
        time.sleep(0.1)
    return ""


def env_of(text):
    return dict(l.split("=", 1) for l in text.split("\n") if "=" in l)


def ver(s):
    p = [int(x) if x.isdigit() else 0 for x in (s or "").split(".")]
    return (p + [0, 0, 0])[:3]


def supports_hyperlinks(e, tty):
    """supports-hyperlinks 3.2.0 (no argv flags; supportsColor taken as
    true on a tty, false off it, as with no FORCE_COLOR)."""
    f = e.get("FORCE_HYPERLINK")
    if f:
        return not (f.lstrip("-").isdigit() and int(f) == 0)
    if e.get("NETLIFY"):
        return True
    if not tty:
        return False
    if "WT_SESSION" in e:
        return True
    if e.get("CI") or e.get("TEAMCITY_VERSION"):
        return False
    tp = e.get("TERM_PROGRAM")
    if tp:
        v = ver(e.get("TERM_PROGRAM_VERSION"))
        if tp == "iTerm.app":
            return v[1] >= 1 if v[0] == 3 else v[0] > 3
        if tp == "WezTerm":
            return v[0] >= 20200620
        if tp == "vscode":
            return v[0] > 1 or (v[0] == 1 and v[1] >= 72)
        if tp == "ghostty":
            return True
    vte = e.get("VTE_VERSION")
    if vte:
        if vte == "0.50.0":
            return False
        v = ver(vte)
        return v[0] > 0 or v[1] >= 50
    return e.get("TERM") == "alacritty"


def claude_links(e, tty):
    """Claude Code's statusline check (2.1.318 to 2.1.322), in its order (no settings
    `hyperlinks` key)."""
    f = e.get("FORCE_HYPERLINK")
    if f:
        return not (f.lower() in ("false", "no", "off") or
                    (f.lstrip("-").isdigit() and int(f) == 0))
    if supports_hyperlinks(e, tty):
        return True
    tp = e.get("TERM_PROGRAM")
    if tp in LINKS or e.get("TERMINAL_EMULATOR") == "JetBrains-JediTerm":
        return True
    if e.get("WT_SESSION") and tp != "tmux" and not e.get("TMUX"):
        return True
    if tp == "tmux" and ver(e.get("TERM_PROGRAM_VERSION"))[:2] >= [3, 4]:
        return True
    return e.get("LC_TERMINAL") in LINKS or "kitty" in e.get("TERM", "")


def session_envs(tag, *spawn_args, env=None):
    """The env of a session command and of a child of its shell."""
    a, b = (os.path.join(ROOT, "%s-%s.env" % (tag, w)) for w in "ab")
    na, nb = tag + "a", tag + "b"
    names.extend([na, nb])
    dch("--spawn", na, *spawn_args, "/bin/sh", "-c",
        "env > %s; exec sleep 60" % a, env=env)
    dch("--spawn", nb, *spawn_args, "/bin/sh", env=env)
    dch("--run", nb, "env > %s" % b, env=env)
    return env_of(wait_file(a)), env_of(wait_file(b))


def find_module():
    m = os.environ.get("DCH_TEST_SUPPORTS_HYPERLINKS")
    if m:
        return m
    node = shutil.which("node", path=ENV.get("PATH"))
    roots = ["/opt/homebrew/lib/node_modules", "/usr/local/lib/node_modules"]
    if node:
        roots.append(os.path.join(os.path.dirname(os.path.dirname(
            os.path.realpath(node))), "lib", "node_modules"))
    for r in roots:
        for pat in ("supports-hyperlinks", "*/node_modules/supports-hyperlinks",
                    "@*/*/node_modules/supports-hyperlinks"):
            hit = sorted(glob.glob(os.path.join(r, pat, "package.json")))
            if hit:
                return os.path.dirname(hit[0])
    return None


def node_probe(mod):
    node = shutil.which("node", path=ENV.get("PATH"))
    if not node:
        print("skip the real supports-hyperlinks (no node)")
        return
    if not mod:
        print("skip the real supports-hyperlinks (not installed)")
        return
    js = "process.stdout.write(String(require(%s).stdout))" % json.dumps(mod)
    for tag, extra, want in (("np", [], "false"),
                             ("nf", ["--env", "FORCE_HYPERLINK=1"], "true")):
        out = os.path.join(ROOT, tag + ".out")
        names.append(tag)
        dch("--spawn", tag, *extra, "/bin/sh")
        dch("--run", tag, "%s -e %s | cat > %s" % (
            node, "'" + js + "'", out))
        got = wait_file(out, 20)
        check("supports-hyperlinks in a session, stdout a pipe%s: %s"
              % (" (FORCE_HYPERLINK=1)" if extra else "", want),
              got == want, got, mod)


def shipped_check():
    c = shutil.which("claude", path=ENV.get("PATH"))
    if not c:
        print("skip Claude Code's shipped check (no claude)")
        return
    try:
        with open(os.path.realpath(c), "rb") as f:
            data = f.read()
    except OSError as e:
        print("skip Claude Code's shipped check (%s)" % e)
        return
    if b"supportsHyperlink" not in data:
        print("skip Claude Code's shipped check (not a bundle read here)")
        return
    lc = re.search(rb"\.LC_TERMINAL;if\(\w+&&(\w+)\.includes\(\w+\)\)"
                   rb"return!0", data)
    # The list's minified name is reused by other modules (2.1.322 has
    # three `var L=[`): take the definition nearest before the check.
    lst = None
    if lc:
        for lst in re.finditer(rb"(?:var|let|const) " + re.escape(lc.group(1))
                               + rb"=\[([^\]]*)\]", data[:lc.start()]):
            pass
    check("Claude Code's link check still reads LC_TERMINAL in a list with "
          "\"ghostty\" (else revisit #122)",
          lst is not None and b'"ghostty"' in lst.group(1),
          os.path.realpath(c))


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL dch not executable at " + DCH)
        return 1
    for d in ("s", "state", "home"):
        os.makedirs(os.path.join(ROOT, d))
    try:
        for where, e in zip(("the session command", "a shell's child"),
                            session_envs("h")):
            check("%s: no FORCE_HYPERLINK" % where,
                  "FORCE_HYPERLINK" not in e,
                  e.get("FORCE_HYPERLINK"))
            check("%s: LC_TERMINAL=ghostty" % where,
                  e.get("LC_TERMINAL") == "ghostty", e.get("LC_TERMINAL"))
            check("%s: supports-hyperlinks says no OSC 8 into a pipe"
                  % where, e and not supports_hyperlinks(e, False))
            check("%s: ... nor on the tty (it never reads the signal)"
                  % where, e and not supports_hyperlinks(e, True))
            check("%s: Claude Code's statusline keeps its links" % where,
                  e and claude_links(e, True))
        a, b = session_envs("c0", env=dict(ENV, FORCE_HYPERLINK="0"))
        check("a caller's FORCE_HYPERLINK=0 reaches the session",
              a.get("FORCE_HYPERLINK") == "0" == b.get("FORCE_HYPERLINK"))
        a, b = session_envs("c1", env=dict(ENV, LC_TERMINAL="iTerm2"))
        check("a caller's LC_TERMINAL wins",
              a.get("LC_TERMINAL") == "iTerm2" == b.get("LC_TERMINAL"))
        a, b = session_envs("c2", "--env", "LC_TERMINAL=")
        check("--env LC_TERMINAL= wins (the opt-out)",
              a.get("LC_TERMINAL") == "" == b.get("LC_TERMINAL"), a, b)
        node_probe(find_module())
        shipped_check()
    finally:
        for n in names:
            dch("-k", n)
        shutil.rmtree(ROOT, ignore_errors=True)
    print("%d failure(s)" % len(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
