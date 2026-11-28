"""`dch --view-agent <name> <id|main>` (#713, Prosper #2050/#2053): switch a
Claude Code's main view to a subagent (or main) through the /tasks dialog.

Drives tests/fake_claude.py as `claude` with its tasks knob: a "Background"
list in Claude Code 2.1.322's row layout (shell rows, local agents, the
"main" leader row while viewing an agent), its transcript stamped with a
version and its subagents' files on disk. The fake logs every byte it reads.
Covers: switching to the 2nd and the 3rd subagent behind a shell row (the
second from viewing-agent, past the main row), a label cut with "…",
ambiguous, not_listed (no such id, ended, failed, a blocking agent: no key
at all; a label missing from the dialog: from main closed with Esc, from
viewing-agent with f on main and the detail saying so), prompt_not_empty
(no byte after the gate), not_claude, main from viewing-agent,
already_main (Esc), one task straight into its detail pane (the target: f;
another: Space), a busy Claude, another Claude Code version (no Esc: the
dialog stays open, detail dialog_open; for main while on main the refusal
dialog_open), a main row that appears between
the reads (f on it, not Esc), DCH_NO_DETECT, and the text-mode shape.
#724 (QA 1.200.0): rows painted as icon + name column + facts (and a name
column cut at 50), a reply's "Background" line above a busy line, only a
line's top-level "version" (C1, C2), descriptions over 200 bytes shown
whole (ASCII, CJK), a shell row equal to the description.
Every row: no Ctrl-C (03) and no x (78) byte after /tasks, Esc only where
the close rule allows it and never while the fake views an agent. On
dch-lite: every Claude row is refused no_screen, nothing typed.

The fake approximates Claude Code's rendering; it is not Claude Code.

Run:  python3 tests/view_agent_test.py        (uses ./dch)
"""
import json, os, re, shutil, stat, subprocess, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
TAG = str(os.getpid())
V = "2.1.322"


def run(*args, timeout=120, env=None):
    return subprocess.run([DCH] + list(args), capture_output=True, text=True,
                          timeout=timeout, env=env)


def until(fn, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def ag(i, desc, state="running", bg=True):
    return {"id": i, "type": "general-purpose", "desc": desc, "state": state,
            "bg": bg}


def row(label, i=None, kind="agent"):
    return {"label": label, "id": i, "kind": kind}


SHELL = row("sleep 300", kind="shell")


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    tmp = tempfile.mkdtemp(prefix="dchva")
    home, fc, bindir = (os.path.join(tmp, d) for d in ("home", "fc", "bin"))
    for d in (home, fc, bindir):
        os.mkdir(d)
    os.makedirs(os.path.join(home, ".claude", "sessions"), mode=0o700)
    p = os.path.join(bindir, "claude")
    shutil.copy(os.path.join(HERE, "fake_claude.py"), p)
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
    for k in list(os.environ):
        if k.startswith("CLAUDE") or k in ("DCH_SESSION", "DCH_SOCKET_DIR",
                                           "DCH_NO_DETECT"):
            del os.environ[k]
    os.environ.update(HOME=home, XDG_RUNTIME_DIR=tmp,
                      XDG_STATE_HOME=os.path.join(tmp, "state"),
                      FAKE_CLAUDE_DIR=fc,
                      PATH=bindir + os.pathsep + os.environ["PATH"])
    os.chdir(tmp)
    names = []
    fails = 0

    def check(msg, cond, extra=""):
        nonlocal fails
        print("  %s %s%s" % ("ok  " if cond else "FAIL", msg,
                             "" if cond else " (%s)" % (extra,)))
        if not cond:
            fails += 1

    def events(n):
        try:
            with open(os.path.join(fc, n + ".log")) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def sess(base, tasks=None, cmd=("claude",), size="100x30", **ctl):
        n = "%s%s" % (base, TAG)
        names.append(n)
        if tasks is not None:
            ctl["tasks"] = tasks
        with open(os.path.join(fc, n + ".ctl"), "w") as f:
            json.dump(ctl, f)
        subprocess.run([DCH, "--spawn", n, "--size", size] + list(cmd),
                       capture_output=True, check=True)
        if cmd == ("claude",):
            s = until(lambda: [e for e in events(n) if e["ev"] == "start"], 15)
            if s:
                rec = os.path.join(home, ".claude", "sessions",
                                   "%d.json" % s[0]["pid"])
                until(lambda: os.path.exists(rec), 15)
                until(lambda: lite or "auto mode on" in run("--read",
                                                            n).stdout, 15)
        return n

    def view(n, target, env=None, json_=True):
        """Run the verb; (rc, parsed json or stderr, events it caused)."""
        k = len(events(n))
        r = run("--view-agent", n, target, *(["--json"] if json_ else []),
                env=dict(os.environ, **env) if env else None)
        time.sleep(0.3)     # the fake logs a last key after dch returned
        ev = events(n)[k:]
        if json_:
            try:
                o = json.loads(r.stdout)
            except ValueError:
                o = {"raw": r.stdout, "err": r.stderr}
        else:
            o = r.stderr
        return r.returncode, o, ev

    def keys(ev):
        """(bytes after /tasks opened, lone Escs with the view then)."""
        i = next((k for k, e in enumerate(ev) if e["ev"] == "tasks_open"),
                 None)
        after = [e for e in ev[i:] if e["ev"] == "byte"] if i is not None \
            else []
        esc = [e for k, e in enumerate(after) if e["text"] == "1b" and
               not (k + 1 < len(after) and after[k + 1]["text"] == "5b")]
        return after, esc

    def safe(what, ev, escs=0):
        after, esc = keys(ev)
        check("%s: no Ctrl-C or x after /tasks" % what,
              not [e for e in after if e["text"] in ("03", "78")],
              [e["text"] for e in after])
        check("%s: %d Esc, never while viewing an agent" % (what, escs),
              len(esc) == escs and all(e.get("view") is None and e.get("dlg")
                                       for e in esc), esc)
        check("%s: nothing stopped or killed" % what,
              not [e for e in ev if e["ev"] in ("kill", "stop")],
              [e for e in ev if e["ev"] in ("kill", "stop")])

    def append_line(o):
        """One more transcript line, in Claude Code's compact JSON."""
        d = os.path.join(home, ".claude", "projects", "-fake")
        f = max((os.path.join(d, x) for x in os.listdir(d)
                 if x.endswith(".jsonl")), key=os.path.getmtime)
        with open(f, "a") as fh:
            fh.write(json.dumps(o, separators=(",", ":")) + "\n")

    def views(ev):
        return [e["text"] for e in ev if e["ev"] == "view"]

    def is_(o, result, reason=""):
        return isinstance(o, dict) and o.get("result") == result and \
            o.get("reason") == reason and \
            sorted(o) == ["detail", "name", "reason", "result"]

    global lite
    try:
        # --- switch to the 2nd and the 3rd subagent behind a shell row ---
        three = [ag("a1", "Explore the parser"), ag("a2", "Write the tests"),
                 ag("a3", "Review the docs")]
        n = sess("sw", {"version": V, "agents": three,
                        "rows": [SHELL, row("Explore the parser", "a1"),
                                 row("Write the tests", "a2"),
                                 row("Review the docs", "a3")]})
        lite = run("--read", n).returncode == 3
        rc, o, ev = view(n, "a2")
        if lite:
            check("lite: refused no_screen, exit 1, nothing typed",
                  rc == 1 and is_(o, "refused", "no_screen") and
                  not [e for e in ev if e["ev"] == "byte"], (rc, o))
            c = sess("lnc", cmd=("sh",))
            rc, o, ev = view(c, "main")
            check("lite: a plain shell is not_claude", rc == 1 and
                  is_(o, "refused", "not_claude"), (rc, o))
            return fails
        check("2nd subagent behind a shell: switched, exit 0",
              rc == 0 and is_(o, "switched"), (rc, o))
        check("2nd subagent: the fake views a2", views(ev) == ["a2"],
              views(ev))
        safe("2nd subagent", ev)
        rc, o, ev = view(n, "a3")
        check("3rd subagent, from viewing a2 (main row first): switched",
              rc == 0 and is_(o, "switched") and views(ev) == ["a3"],
              (rc, o, views(ev)))
        safe("3rd subagent", ev)

        # --- a truncated label ---------------------------------------------
        n = sess("tr", {"version": V, "agents": [
            ag("t1", "Investigate the flaky login test on CI")],
            "rows": [SHELL, row("Investigate the flaky…", "t1")]})
        rc, o, ev = view(n, "t1")
        check("a label cut with …: switched", rc == 0 and is_(o, "switched")
              and views(ev) == ["t1"], (rc, o, views(ev)))
        safe("cut label", ev)

        # --- ambiguous: late refusal from main, closed with Esc ------------
        n = sess("am", {"version": V, "agents": [ag("b1", "Run checks"),
                                                 ag("b2", "Run checks")],
                        "rows": [SHELL, row("Run checks", "b1"),
                                 row("Run checks", "b2")]})
        rc, o, ev = view(n, "b1")
        check("two rows read the description: ambiguous, exit 1",
              rc == 1 and is_(o, "refused", "ambiguous"), (rc, o))
        check("ambiguous: the list closed by Esc, the view untouched",
              views(ev) == [] and
              [e["text"] for e in ev if e["ev"] == "tasks_close"] == ["esc"],
              ev[-3:])
        safe("ambiguous", ev, escs=1)

        # --- not_listed before any key: missing, ended, failed, blocking ---
        n = sess("nl", {"version": V, "agents": [
            ag("c1", "Done already", "done"), ag("c2", "Broke", "failed"),
            ag("c3", "Blocking one", bg=False), ag("c4", "Live one")],
            "rows": [SHELL, row("Live one", "c4")]})
        for tid, what in (("nope", "no such id"), ("c1", "ended"),
                          ("c2", "failed"), ("c3", "a blocking agent")):
            rc, o, ev = view(n, tid)
            check("%s: not_listed, exit 1, no key sent" % what,
                  rc == 1 and is_(o, "refused", "not_listed") and
                  not [e for e in ev if e["ev"] == "byte"], (rc, o))
        rc, o, ev = view(n, "c1")
        check("ended: detail names it", "ended" in o.get("detail", ""), o)

        # --- not_listed late: the label missing from the dialog ------------
        n = sess("lm", {"version": V, "agents": [ag("d1", "Hidden agent"),
                                                 ag("d2", "Shown agent")],
                        "rows": [SHELL, row("Shown agent", "d2")]})
        rc, o, ev = view(n, "d1")
        check("missing from the dialog, on main: not_listed",
              rc == 1 and is_(o, "refused", "not_listed"), (rc, o))
        check("missing, on main: closed with Esc, the view untouched",
              views(ev) == [], views(ev))
        safe("missing on main", ev, escs=1)
        n = sess("lv", {"version": V, "view": "d2",
                        "agents": [ag("d1", "Hidden agent"),
                                   ag("d2", "Shown agent")],
                        "rows": [SHELL, row("Shown agent", "d2")]})
        rc, o, ev = view(n, "d1")
        check("missing, viewing an agent: not_listed, detail says the view "
              "moved to main", rc == 1 and is_(o, "refused", "not_listed")
              and "view moved to main" in o.get("detail", ""), (rc, o))
        check("missing, viewing an agent: f on main", views(ev) == ["main"],
              views(ev))
        safe("missing viewing an agent", ev)

        # --- prompt_not_empty: no byte after the gate ----------------------
        n = sess("pn", {"version": V, "agents": [ag("e1", "Some agent")],
                        "rows": [row("Some agent", "e1")]}, draft="hello")
        rc, o, ev = view(n, "e1")
        check("a draft in the composer: prompt_not_empty, no byte sent",
              rc == 1 and is_(o, "refused", "prompt_not_empty") and
              not [e for e in ev if e["ev"] == "byte"], (rc, o))
        # a prompt up: --say's code
        n = sess("pd", {"version": V, "agents": [ag("e2", "Some agent")],
                        "rows": [row("Some agent", "e2")]}, dialog=True)
        rc, o, ev = view(n, "e2")
        check("a prompt up: --say's prompt code, no byte sent",
              rc == 1 and is_(o, "refused", "prompt") and
              not [e for e in ev if e["ev"] == "byte"], (rc, o))

        # --- not_claude ----------------------------------------------------
        c = sess("nc", cmd=("sh",))
        rc, o, ev = view(c, "main")
        check("a plain shell: not_claude, exit 1", rc == 1 and
              is_(o, "refused", "not_claude"), (rc, o))

        # --- main from viewing-agent, already_main -------------------------
        n = sess("mv", {"version": V, "view": "f1",
                        "agents": [ag("f1", "Viewed one")],
                        "rows": [row("Viewed one", "f1")]})
        rc, o, ev = view(n, "main")
        check("main from viewing an agent: switched, the fake on main",
              rc == 0 and is_(o, "switched") and views(ev) == ["main"],
              (rc, o, views(ev)))
        safe("main from viewing", ev)
        n = sess("am2", {"version": V, "agents": [ag("g1", "One"),
                                                  ag("g2", "Two")],
                         "rows": [SHELL, row("One", "g1"), row("Two", "g2")]})
        rc, o, ev = view(n, "main")
        check("main while on main: already_main, exit 0, closed with Esc",
              rc == 0 and is_(o, "already_main") and views(ev) == [],
              (rc, o, views(ev)))
        safe("already_main", ev, escs=1)

        # --- one task: straight into its detail pane -----------------------
        n = sess("dt", {"version": V, "agents": [ag("h1", "Only agent")],
                        "rows": [row("Only agent", "h1")]})
        rc, o, ev = view(n, "h1")
        check("one task, the target, in its detail pane: f, switched",
              rc == 0 and is_(o, "switched") and views(ev) == ["h1"] and
              [e["text"] for e in ev if e["ev"] == "tasks_open"] ==
              ["detail"], (rc, o, views(ev)))
        safe("detail target", ev)
        n = sess("dn", {"version": V, "agents": [ag("h2", "Not shown")],
                        "rows": [SHELL]})
        rc, o, ev = view(n, "h2")
        check("one task, not the target: Space closes, not_listed",
              rc == 1 and is_(o, "refused", "not_listed") and
              [e["text"] for e in ev if e["ev"] == "tasks_close"] ==
              ["space"] and views(ev) == [], (rc, o, ev[-3:]))
        safe("detail non-target", ev)

        # --- a busy Claude -------------------------------------------------
        n = sess("bz", {"version": V, "agents": [ag("i1", "Busy helper")],
                        "rows": [SHELL, row("Busy helper", "i1")]},
                 busy=True)
        rc, o, ev = view(n, "i1")
        def no_keys(ev):
            return not [e for e in ev if e["ev"] in (
                "byte", "line", "tasks_open", "view", "kill", "stop")]

        def busy_refused(o):
            return is_(o, "refused", "busy") and \
                o["detail"] == "Claude is mid-turn; try when idle"
        check("busy Claude: refused busy before /tasks, no key sent",
              rc == 1 and busy_refused(o) and no_keys(ev), (rc, o, ev))

        # --- another version: no Esc, the dialog left open -----------------
        n = sess("vm", {"version": "2.1.300",
                        "agents": [ag("j1", "Hidden"), ag("j2", "Shown")],
                        "rows": [SHELL, row("Shown", "j2")]})
        rc, o, ev = view(n, "j1")
        check("version 2.1.300, late refusal: not_listed, detail "
              "dialog_open (Esc not verified on 2.1.300)",
              rc == 1 and is_(o, "refused", "not_listed") and
              "dialog_open (Esc not verified on 2.1.300)" in
              o.get("detail", ""), (rc, o))
        check("version mismatch: the dialog left open",
              not [e for e in ev if e["ev"] == "tasks_close"], ev[-3:])
        safe("version mismatch", ev, escs=0)
        n = sess("vm2", {"version": "2.1.300", "agents": [ag("j3", "One")],
                         "rows": [SHELL, row("One", "j3")]})
        rc, o, ev = view(n, "main")
        check("version 2.1.300, main while on main: refused dialog_open, "
              "the list left open", rc == 1 and
              is_(o, "refused", "dialog_open") and
              "Esc not verified on 2.1.300" in o.get("detail", "") and
              not [e for e in ev if e["ev"] == "tasks_close"], (rc, o))
        safe("version mismatch on main", ev, escs=0)

        # --- a main row appears between the reads: f, not Esc --------------
        n = sess("ma", {"version": V, "main_after_ms": 3000,
                        "agents": [ag("k1", "Hidden"), ag("k2", "Shown")],
                        "rows": [SHELL, row("Shown", "k2")]})
        rc, o, ev = view(n, "k1", env={"DCH_VIEW_CLOSE_PAUSE_MS": "4000"})
        check("main row appeared before the close: f on it, detail says "
              "the view moved to main", rc == 1 and
              is_(o, "refused", "not_listed") and
              "view moved to main" in o.get("detail", "") and
              views(ev) == ["main"], (rc, o, views(ev)))
        safe("main appeared", ev, escs=0)

        # --- #724 (QA 1.200.0 XI1-XI5) ------------------------------------
        # XI1: rows are icon + name column + facts; the dialog painted so
        # while the close pauses, read from outside
        n = sess("x5", {"version": V, "agents": [
            ag("x51", "sleep 300"), ag("x52", "Write the tests")],
            "rows": [SHELL, row("sleep 300", "x51"),
                     row("Write the tests", "x52")]})
        seen = []
        th = threading.Thread(target=lambda: seen.append(until(
            lambda: [x for x in [run("--read", n).stdout]
                     if "Local agents" in x], 8)))
        th.start()
        rc, o, ev = view(n, "x51", env={"DCH_VIEW_CLOSE_PAUSE_MS": "3000"})
        th.join()
        scr = seen[0][0] if seen and seen[0] else ""
        check("XI1: the fake paints an agent row as icon, name column, "
              "then the facts joined ' \u00b7 '", re.search(
                  "\u25cf sleep 300 {3,}running \u00b7 opus/medium", scr),
              repr(scr[-700:]))
        # XI5: a shell row reading the description is not a match
        check("XI5: a shell row equal to the description is not counted: "
              "switched, not ambiguous", rc == 0 and is_(o, "switched") and
              views(ev) == ["x51"], (rc, o, views(ev)))
        safe("XI5 shell twin", ev)
        long_ = "Investigate the flaky login test that fails on CI runners"
        n = sess("x1", {"version": V, "agents": [ag("x11", long_),
                                                 ag("x12", "Other")],
                        "rows": [row(long_, "x11"), row("Other", "x12")]})
        rc, o, ev = view(n, "x11")
        check("XI1: a 58-column description, its name column cut at 50 "
              "with \u2026: switched", rc == 0 and is_(o, "switched") and
              views(ev) == ["x11"], (rc, o, views(ev)))
        safe("XI1 cut name column", ev)

        # XI8: 3+ blanks inside a description, edge blanks, a name that
        # starts another's (no-facts rows: a pane too narrow for facts)
        cases = [("interior 3 blanks", ["Fix   the bug", "Other"], "y1"),
                 ("trailing blank", ["Write the tests ", "Other"], "y1"),
                 ("leading blank", [" Write the tests", "Other"], "y1"),
                 ("Fix + Fix   the bug", ["Fix", "Fix   the bug"], "y1"),
                 ("Fix + Fix   the bug", ["Fix", "Fix   the bug"], "y2")]
        for k, (tag, descs, hit) in enumerate(cases):
            for facts in (True, False):
                if not facts and tag.startswith("Fix +"):
                    continue
                rr = [row(d, "y%d" % (j + 1)) for j, d in enumerate(descs)]
                if not facts:
                    rr = [dict(r, meta=[]) for r in rr]
                n = sess("y%d%d" % (k, facts), {"version": V, "agents": [
                    ag("y%d" % (j + 1), d) for j, d in enumerate(descs)],
                    "rows": rr})
                rc, o, ev = view(n, hit)
                check("XI8: %s, target %s, %s: switched" % (
                      tag, hit, "facts" if facts else "no facts"),
                      rc == 0 and is_(o, "switched") and views(ev) == [hit],
                      (rc, o, views(ev)))
                safe("XI8 " + tag, ev)

        # XI2: a reply heading "Background" above a busy line
        n = sess("x2", {"version": V, "agents": [ag("x21", "Explore"),
                                                 ag("x22", "Write it")],
                        "rows": [row("Explore", "x21"),
                                 row("Write it", "x22")]},
                 above=["\u23fa Summary", "", "  Background", "",
                        "  The parser is slow.",
                        "\u273b Thinking\u2026 (esc to interrupt)"])
        rc, o, ev = view(n, "x22")
        check("XI2: a reply's 'Background' line does not hide the dialog: "
              "switched", rc == 0 and is_(o, "switched") and
              views(ev) == ["x22"], (rc, o, views(ev)))
        safe("XI2 reply heading", ev)

        # XI7: the dialog's shape (rule, title, a row, hints) in output
        # above the composer is not the dialog dch opened
        decoy = ["─" * 40, "Background", "", "   Local agents (1)",
                 " ❯ ● Decoy   running", "", " Esc to close"]

        def lone_esc(ev):
            b = [e["text"] for e in ev if e["ev"] == "byte"]
            return [k for k, t in enumerate(b) if t == "1b" and
                    not (k + 1 < len(b) and b[k + 1] == "5b")]
        n = sess("r1", {"version": V, "agents": [ag("r11", "Explore"),
                                                 ag("r12", "Write it")],
                        "rows": [row("Explore", "r11"),
                                 row("Write it", "r12")]},
                 above=decoy + ["✻ Thinking… (esc to interrupt)"])
        rc, o, ev = view(n, "r12")
        check("XI7 R1: the dialog's shape in a reply above the open dialog: "
              "switched", rc == 0 and is_(o, "switched") and
              views(ev) == ["r12"], (rc, o, views(ev)))
        safe("XI7 R1", ev)
        for tag, above in (("R2", decoy[1:]), ("R3", decoy)):
            n = sess("r" + tag[1], {"version": V, "agents": [
                ag("r31", "Explore")], "rows": [row("Explore", "r31")],
                "view": "r31", "shut": True}, above=above, busy=True)
            rc, o, ev = view(n, "main")
            check("XI7 %s: the shape%s above a busy composer while viewing "
                  "an agent: refused busy, no key, nothing stopped" %
                  (tag, "" if tag == "R3" else " without its rule"),
                  rc == 1 and busy_refused(o) and no_keys(ev),
                  (rc, o, lone_esc(ev), [e["ev"] for e in ev][-6:]))
        # LOW-XI13: a decoy streamed in after /tasks was typed. Busy turn:
        # refused before typing (D1, D1t); idle turn: it sits above a live
        # composer, so it is not the dialog (timeout, no Esc)
        def late_decoy(n):
            def go():
                k = len(events(n))
                if until(lambda: [e for e in events(n)[k:] if e["ev"] in (
                        "line", "tasks_open")], 8):
                    p = os.path.join(fc, n + ".ctl")
                    with open(p) as f:
                        c = json.load(f)
                    c["above"] = decoy
                    with open(p + ".t", "w") as f:
                        json.dump(c, f)
                    os.replace(p + ".t", p)
            th = threading.Thread(target=go)
            th.start()
            return th
        for tag, tgt, busy in (("D1", "main", True), ("D1t", "r52", True),
                               ("I1", "main", False)):
            n = sess(tag.lower(), {"version": V, "agents": [
                ag("r51", "Explore"), ag("r52", "Decoy")],
                "rows": [row("Explore", "r51")], "view": "r51",
                "shut": True}, busy=busy)
            th = late_decoy(n)
            rc, o, ev = view(n, tgt)
            th.join()
            if busy:
                check("XI13 %s: busy turn, refused busy before typing, no "
                      "key, nothing stopped" % tag,
                      rc == 1 and busy_refused(o) and no_keys(ev),
                      (rc, o, [e["ev"] for e in ev][-6:]))
            else:
                check("XI13 %s: a decoy streamed in above the live "
                      "composer: timeout, no Esc, no f, nothing stopped" %
                      tag, rc == 1 and is_(o, "refused", "timeout") and
                      "no /tasks dialog" in o["detail"] and
                      not lone_esc(ev) and not views(ev) and
                      not [e for e in ev if e["ev"] in ("kill", "stop") or
                           (e["ev"] == "byte" and e["text"] == "66")],
                      (rc, o, lone_esc(ev), [e["ev"] for e in ev][-6:]))
        n = sess("r4", {"version": V, "agents": [ag("r41", "Explore")],
                        "rows": [SHELL, row("Explore", "r41")],
                        "hints": False})
        rc, o, ev = view(n, "r41")
        check("XI7: a list with no 'Esc to close' hints row is not the "
              "dialog: timeout, no key", rc == 1 and
              is_(o, "refused", "timeout") and not views(ev) and
              not lone_esc(ev), (rc, o, views(ev)))

        # XI3: only a transcript line's top-level "version" counts
        n = sess("x3", {"version": V, "agents": [ag("x31", "Write it")],
                        "rows": [row("Write it", "x31"), SHELL]})
        append_line({"type": "assistant", "version": V, "message": {
            "content": [{"type": "tool_use", "id": "toolu_x",
                         "name": "mcp__pkg__info",
                         "input": {"version": "1.0.0"}}]}})
        rc, o, ev = view(n, "main")
        check("XI3 C1: on %s a later tool input's version 1.0.0 is not the "
              "version: already_main (Esc)" % V, rc == 0 and
              is_(o, "already_main"), (rc, o))
        safe("XI3 C1", ev, escs=1)
        n = sess("x4", {"version": "2.1.999", "agents": [
            ag("x41", "Write it")], "rows": [row("Write it", "x41"), SHELL]})
        append_line({"type": "assistant", "version": "2.1.999", "message": {
            "content": [{"type": "tool_use", "id": "toolu_y",
                         "name": "mcp__pkg__pin",
                         "input": {"version": V}}]}})
        rc, o, ev = view(n, "main")
        check("XI3 C2: on 2.1.999 a tool input's version %s does not "
              "enable Esc: dialog_open" % V, rc == 1 and
              is_(o, "refused", "dialog_open") and
              "2.1.999" in o.get("detail", ""), (rc, o))
        safe("XI3 C2", ev, escs=0)

        # XI4: a description over the meta's 200-byte cap, shown whole
        asc = "Investigate " + "the flaky login test " * 11
        n = sess("x6", {"version": V, "agents": [ag("x61", asc),
                                                 ag("x62", "Other")],
                        "rows": [dict(row(asc.rstrip(), "x61"), meta=[]),
                                 dict(row("Other", "x62"), meta=[])]},
                 size="260x30")
        rc, o, ev = view(n, "x61")
        check("XI4: %d bytes of ASCII shown whole at 260 columns: switched"
              % len(asc.rstrip()), rc == 0 and is_(o, "switched") and
              views(ev) == ["x61"], (rc, o, views(ev)))
        safe("XI4 ASCII", ev)
        cjk = "\u4fee\u590d\u767b\u5f55\u6d4b\u8bd5" * 12
        n = sess("x7", {"version": V, "agents": [ag("x71", cjk),
                                                 ag("x72", "Other")],
                        "rows": [dict(row(cjk, "x71"), meta=[]),
                                 dict(row("Other", "x72"), meta=[])]},
                 size="200x30")
        rc, o, ev = view(n, "x71")
        check("XI4: 72 CJK characters (216 bytes) shown whole at 200 "
              "columns: switched", rc == 0 and is_(o, "switched") and
              views(ev) == ["x71"], (rc, o, views(ev)))
        safe("XI4 CJK", ev)

        # --- DCH_NO_DETECT, and the text shape -----------------------------
        n = sess("nd", {"version": V, "agents": [ag("l1", "Agent")],
                        "rows": [row("Agent", "l1")]})
        rc, o, ev = view(n, "l1", env={"DCH_NO_DETECT": "1"})
        check("DCH_NO_DETECT: no_screen, no byte", rc == 1 and
              is_(o, "refused", "no_screen") and
              not [e for e in ev if e["ev"] == "byte"], (rc, o))
        rc, err, ev = view(n, "zz", json_=False)
        check("text mode: stderr 'dch: <name>: <what> (not_listed)', exit 1",
              rc == 1 and err.startswith("dch: %s: " % n) and
              err.rstrip().endswith("(not_listed)"), (rc, err))
        r = run("--view-agent", n)
        check("usage: one word is exit 1", r.returncode == 1 and
              "usage" in r.stderr, r.stderr)
    finally:
        for n in names:
            run("-k", n)
        shutil.rmtree(tmp, ignore_errors=True)
    print("PASS" if not fails else "%d FAIL" % fails)
    return fails


lite = False
if __name__ == "__main__":
    sys.exit(1 if main() else 0)
