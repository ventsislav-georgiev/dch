"""Auto-allow (#074): the master answers Claude Code's plain permission
prompt when the user config says `auto_allow = true`, and `dch --config`.

Drives tests/fake_claude.py as `claude` first on PATH (its `screen` knob
paints a dialog and logs every key typed into it), with HOME, the runtime
dir and DCH_CONFIG temporary. Covers: --config (show, get, set keeping
comments and other lines, --unset, unknown key, bad bool, picker_sort and
detach_key taking only their choices); auto_allow off
answers nothing; on answers "1" once, never the "don't ask again" row, and
--ls-json shows auto_allowed {at,title}; prompts outside Prosper's rule are
left alone (a "don't ask again" Yes only, four options, a mode-change row,
another question, a question form, a quoted dialog above the composer,
a hook's ask on single rows or wrapped mid-word, a hook's "to update hooks"
footer, a permission rule with or without its "/permissions" line, an ask
rule in auto mode, one wrapped mid-word, #790), while a working-directory
reason over "/permissions to update rules" and a classifier ask are answered;
Claude Code's plan approval, three captured caret rows (#092: --status
blocked, never answered; dismissed, not blocked);
the loop guard (the prompt left up, and gone and back, is answered once);
a curly apostrophe in "don’t ask again" (#783); Claude Code's ticking
countdown line, one row or wrapped, answered once while a near-miss
wording still compares literally (#784);
the sidecar cleared when the program exits. Every "left alone" check also
needs a plain prompt answered right after on the same session, so none
passes on a dch without auto-allow. On dch-lite (no terminal mirror):
--config works and nothing is ever answered.

Run:  python3 tests/auto_allow_test.py        (uses ./dch)
"""
import json, os, shutil, stat, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
TAG = str(os.getpid())
RULE = "─" * 100
NO = "No, and tell Claude what to do differently (esc)"
DONT = "Yes, and don't ask again for rm commands in /tmp/proj"


def dialog(opts, question="Do you want to proceed?", title="Bash command",
           body=("   rm -rf build", "   Clean the build tree")):
    rows = [RULE, " " + title, ""] + list(body) + ["", " " + question]
    rows += [(" ❯ " if i == 0 else "   ") + "%d. %s" % (i + 1, o)
             for i, o in enumerate(opts)]
    return rows + ["", " Esc to cancel · Tab to amend"]


BASH2 = dialog(["Yes", NO])
CURLY = dialog(["Yes", DONT.replace("'", "\u2019"), NO])


def ticking(t, tail="", wrap=False):
    """BASH2 under Claude Code's countdown line (#784) at t seconds left."""
    w = (" \u26a0 Claude Code will automatically block this request in "
         "%d:%02d, to avoid holding up an unattended session%s"
         % (t // 60, t % 60, tail))
    if wrap:
        w = w.replace(", to avoid ", ", to\n   avoid ")
    rows = dialog(["Yes", NO])
    q = rows.index(" Do you want to proceed?")
    return rows[:q - 1] + w.split("\n") + rows[q:]
BASH3 = dialog(["Yes", DONT, NO])


def screen(name):
    """A captured Claude Code screen under tests/screens/, as rows."""
    with open(os.path.join(HERE, "screens", name), encoding="utf-8") as f:
        return f.read().rstrip("\n").split("\n")

HOOK = ("   rm -rf build",
        "   Hook PreToolUse:Bash requires confirmation for this command:",
        "   rm needs a human", "   [~/.claude/hooks/guard.sh]")
PRULE = ("   rm -rf build",
         "   Permission rule Bash(rm:*) requires confirmation for this "
         "command.", "   /permissions to update rules")
OTHER = dialog(["Yes", NO], body=("   swift build", "   Build it"))


def run(*args, env=None):
    return subprocess.run([DCH] + list(args), capture_output=True, text=True,
                          timeout=60, env=env)


def until(fn, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main():
    if not os.access(DCH, os.X_OK):
        print("FAIL: dch not executable at", DCH)
        return 1
    tmp = tempfile.mkdtemp(prefix="dchv")
    home = os.path.join(tmp, "home")
    fc = os.path.join(tmp, "fc")
    bindir = os.path.join(tmp, "bin")
    for d in (home, fc, bindir):
        os.mkdir(d)
    os.makedirs(os.path.join(home, ".claude", "sessions"), mode=0o700)
    p = os.path.join(bindir, "claude")
    shutil.copy(os.path.join(HERE, "fake_claude.py"), p)
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
    for k in list(os.environ):
        if k.startswith("CLAUDE") or k in ("DCH_SESSION", "DCH_SOCKET_DIR",
                                           "DCH_NO_DETECT", "DCH_CONFIG",
                                           "XDG_CONFIG_HOME"):
            del os.environ[k]
    cfg = os.path.join(tmp, "cfg", "dch", "config")
    os.environ.update(HOME=home, XDG_RUNTIME_DIR=tmp,
                      XDG_STATE_HOME=os.path.join(tmp, "state"),
                      DCH_CONFIG=cfg, FAKE_CLAUDE_DIR=fc,
                      PATH=bindir + os.pathsep + os.environ["PATH"])
    os.chdir(tmp)
    names, pids, fails, lite = [], set(), 0, False

    def check(msg, cond):
        nonlocal fails
        print("  %s %s" % ("ok  " if cond else "FAIL", msg))
        if not cond:
            fails += 1

    gen = [0]

    def setctl(n, **ctl):
        gen[0] += 1
        ctl["gen"] = gen[0]       # every write repaints: output to check
        with open(os.path.join(fc, n + ".ctl.tmp"), "w") as f:
            json.dump(ctl, f)
        os.rename(os.path.join(fc, n + ".ctl.tmp"),
                  os.path.join(fc, n + ".ctl"))

    def events(n):
        try:
            with open(os.path.join(fc, n + ".log")) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def keys(n):
        return [e["text"] for e in events(n) if e["ev"] == "key"]

    def sess(base, size="100x30", **ctl):
        n = "%s%s" % (base, TAG)
        names.append(n)
        setctl(n, **ctl)
        subprocess.run([DCH, "--spawn", n, "--size", size, "claude"],
                       capture_output=True, check=True)
        s = until(lambda: [e for e in events(n) if e["ev"] == "start"], 15)
        if s:
            pids.add(s[0]["pid"])
        until(lambda: lite or "auto mode on" in run("--read", n).stdout)
        return n

    def show(n, rows, **ctl):
        """Paint rows as the dialog; True once the screen shows them."""
        setctl(n, screen=rows, **ctl)
        return lite or until(lambda: rows[-3].strip() in
                             run("--read", n).stdout, 5)

    def row(n):
        for r in json.loads(run("--ls-json").stdout or "[]"):
            if r["name"] == n:
                return r
        return {}

    try:
        # --- dch --config ------------------------------------------------
        r = run("--config")
        check("--config with no file: the path, then the defaults",
              r.returncode == 0 and r.stdout.splitlines() ==
              [cfg, "auto_allow = false", "sidekick_pattern = -qa",
               "picker_sort = activity", "detach_key = detach",
               "remote_hosts = ", "focus_resize = true",
               "xray_ring = 2M", "tasks_command = ",
               "memory_command = "])
        r = run("--config", "auto_allow")
        check("--config <key> unset: exit 1, nothing printed, no error",
              r.returncode == 1 and r.stdout == r.stderr == "")
        r = run("--config", "nope", "1")
        check("--config unknown key: exit 1, no file written",
              r.returncode == 1 and "unknown config key: nope" in r.stderr
              and
              run("--config", "nope").returncode == 1 and
              not os.path.exists(cfg))
        r = run("--config", "auto_allow", "ture")
        check("--config auto_allow <not a bool>: exit 1",
              r.returncode == 1 and "true or false" in r.stderr and
              not os.path.exists(cfg))
        for k, bad in (("picker_sort", "size"), ("detach_key", "Switch"),
                       ("picker_sort", "")):
            r = run("--config", k, bad)
            check("--config %s %r: exit 1, names the choices" % (k, bad),
                  r.returncode == 1 and " takes " in r.stderr and
                  not os.path.exists(cfg))
        os.makedirs(os.path.dirname(cfg))
        for k, good in (("picker_sort", "name"), ("detach_key", "switch")):
            r = run("--config", k, good)
            check("--config %s %s: set" % (k, good), r.returncode == 0 and
                  run("--config", k).stdout == good + "\n")
        os.unlink(cfg)
        with open(cfg, "w") as f:
            f.write("# mine\nsidekick_pattern = old\nother = 1\n"
                    "sidekick_pattern = older\nlast = x")
        r = run("--config", "sidekick_pattern", "^side-")
        with open(cfg) as f:
            body = f.read()
        check("--config set: the key's first line replaced, its later ones "
              "dropped, comments and other lines kept",
              r.returncode == 0 and body ==
              "# mine\nsidekick_pattern = ^side-\nother = 1\nlast = x\n")
        check("--config <key>: the value",
              run("--config", "sidekick_pattern").stdout == "^side-\n")
        r = run("--config", "sidekick_pattern", "--unset")
        with open(cfg) as f:
            body = f.read()
        check("--config <key> --unset: removed, the rest kept",
              r.returncode == 0 and body == "# mine\nother = 1\nlast = x\n"
              and run("--config", "sidekick_pattern").returncode == 1)
        shutil.rmtree(os.path.dirname(cfg))
        newdir = os.path.join(tmp, "new", "deeper")
        r = run("--config", "auto_allow", "true",
                env=dict(os.environ, DCH_CONFIG=os.path.join(newdir, "c")))
        check("--config set creates the parent dir",
              r.returncode == 0 and
              open(os.path.join(newdir, "c")).read() == "auto_allow = true\n")

        probe = sess("probe")
        lite = run("--read", probe).returncode == 3

        # --- off: nothing answered; switched on, the same prompt is ------
        off = sess("off")
        ok = show(off, BASH2)
        time.sleep(1.5)
        before = keys(off)
        run("--config", "auto_allow", "true")
        setctl(off, screen=BASH2)        # a repaint: output, then quiet
        if lite:
            time.sleep(1.5)
            check("lite: nothing answered, auto_allowed null",
                  ok and keys(off) == [] and
                  row(off).get("auto_allowed", 0) is None)
            run("--config", "auto_allow", "false")
            return finish(fails, tmp, names, pids)
        check("off (no config): nothing answered; on: answered with 1",
              ok and before == [] and until(lambda: keys(off) == ["1"], 5))

        # --- on: the 3-option prompt gets 1, never the "don't ask" row ---
        t0 = int(time.time())
        on = sess("on")
        show(on, BASH3)
        got = until(lambda: keys(on), 5)
        time.sleep(2.5)              # past the verify re-read
        aa = row(on).get("auto_allowed") or {}
        check("on: the 3-option prompt answered once with 1 (Yes), never "
              "2 (don't ask again)", got == ["1"] and keys(on) == ["1"])
        check("--ls-json auto_allowed {at, title}",
              aa.get("title") == "Bash command" and
              t0 - 1 <= aa.get("at", 0) <= time.time() + 1)
        check("--config auto_allow: true",
              run("--config", "auto_allow").stdout == "true\n")

        # --- a curly apostrophe in "don't ask again" (#783) -------------
        cu = sess("curly")
        show(cu, CURLY)
        check("a curly \u2019 in \"don\u2019t ask again\": answered with 1",
              until(lambda: keys(cu) == ["1"], 5))

        # --- not ask rules (#790): a working-directory reason with
        # "/permissions to update rules", a classifier ask: answered
        for nm, body in (("a working-directory reason over \"/permissions "
                          "to update rules\"", (
                              "   rm -rf build", "   Path is outside the "
                              "allowed working directories",
                              "   /permissions to update rules")),
                         ("a classifier ask", (
                              "   rm -rf build", "   Classifier bash_safety "
                              "requires confirmation for this command.",
                              "   Auto mode needs your confirmation for "
                              "this action"))):
            n = sess("nothook%d" % len(names))
            show(n, dialog(["Yes", NO], body=body))
            check("%s: answered with 1" % nm,
                  until(lambda: keys(n) == ["1"], 5))

        # --- the ticking countdown (#784): one prompt, not one a second --
        # each left up (sticky) and redrawn with a new M:SS every ~1 s; a
        # near-miss wording (trailing text) still compares literally
        # (the line is 103 cells: one row at 120; at 100 Claude Code wraps it)
        tk = {"plain": ({}, sess("tick", "120x30")), "wrapped": ({"wrap": True},
              sess("tickw")), "near-miss": ({"tail": " soon"},
              sess("tickn", "120x30"))}
        for t in range(95, 89, -1):
            for kw, n in tk.values():
                setctl(n, screen=ticking(t, **kw), sticky=True)
            time.sleep(1)
        got = {k: keys(n) for k, (_, n) in tk.items()}
        check("countdown ticking each second: answered once",
              got["plain"] == ["1"])
        check("countdown wrapped over two rows: answered once",
              got["wrapped"] == ["1"])
        check("countdown near-miss (trailing text): compared literally, "
              "each new M:SS a new prompt", len(got["near-miss"]) >= 2)

        # --- left alone, each followed by a plain prompt that is answered -
        alone = {
            "a don't-ask-again Yes only": dialog([DONT, NO]),
            "four options": dialog(["Yes", DONT, "Yes, and don't ask again "
                                    "for git commands", NO]),
            "a mode-change middle row": dialog(
                ["Yes", "Yes, allow all edits during this session "
                 "(shift+tab)", NO]),
            "don't ask again with the mode key": dialog(
                ["Yes", DONT + " (shift+tab)", NO]),
            "another question": dialog(
                ["Yes", "No"], question="Do you trust the files in this "
                "folder?"),
            "Yes with a key hint": dialog(["Yes (tab)", NO]),
            "a question form": [RULE, " ☐ Proceed", "",
                                "Do you want to proceed?", "",
                                "❯ 1. Yes", "  2. No"],
            "Yes/No not the last thing on screen": BASH2 + ["$ "],
            # a hook's ask (Prosper #790): the "Hook … requires
            # confirmation for this" line alone, wrapped mid-word, and
            # only the "… to update hooks" footer
            "a hook ask": dialog(["Yes", NO], body=HOOK),
            "a hook ask wrapped mid-word": dialog(["Yes", NO], body=(
                HOOK[0], "   Hook PreToolUse:Bash requires confirma",
                "   tion for this command:") + HOOK[2:]),
            "a hook's update-hooks footer": dialog(["Yes", NO], body=(
                "   rm -rf build", "   settings.json to update hooks")),
            # an ask rule: a permission rule, with and without its
            # "/permissions to update rules" line (managed policy draws
            # none), auto mode's, and one wrapped mid-word
            "a permission rule": dialog(["Yes", NO], body=PRULE),
            "a permission rule, no /permissions line": dialog(
                ["Yes", NO], body=PRULE[:2]),
            "an ask rule in auto mode": dialog(["Yes", NO], body=(
                "   rm -rf build", "   Ask rule Bash(rm:*) overrides auto "
                "mode for this command.",
                "   /permissions to let auto mode decide")),
            "an ask rule wrapped mid-word": dialog(["Yes", NO], body=(
                "   rm -rf build", "   Ask rule Bash(rm:*) overrides auto mo",
                "   de for this command.")),
            # a managed-policy rule over 300 characters, wrapped, with no
            # "/permissions" line to back it (Prosper QA #613 INFO-1)
            "a permission rule over 300 chars": dialog(["Yes", NO], body=(
                "   rm -rf build", "   Permission rule Bash(rm:" ) + tuple(
                "   " + "x" * 70 for _ in range(5)) + (
                "   *) requires confirmation for this command.",)),
            "an ask rule over 300 chars": dialog(["Yes", NO], body=(
                "   rm -rf build", "   Ask rule Bash(rm:") + tuple(
                "   " + "y" * 70 for _ in range(5)) + (
                "   *) overrides auto mode for this command.",)),
        }
        ss = {}
        for k, (name, rows) in enumerate(alone.items()):
            ss[name] = sess("alone%d" % k)
            show(ss[name], rows)
        # Claude Code 2.1.318's plan approval (#092), captured: blocked for
        # --status on every caret row, and never answered — a "1" there
        # approves the plan and switches the session to auto mode
        for c in (1, 2, 3):
            name = "the plan approval, caret on %d" % c
            n = ss[name] = sess("plan%d" % c, "100x44")
            show(n, screen("plan-caret-%d.txt" % c))
            check(name + ": --status blocked",
                  until(lambda: run("--status", n).stdout == "blocked\n", 5))
        esc = sess("planesc", "100x44")
        show(esc, screen("plan-after-esc.txt"))
        check("the plan approval dismissed: --status not blocked",
              run("--status", esc).stdout.strip() in ("idle", "working"))
        # a quoted dialog in the transcript, above an empty composer
        quoted = sess("quoted")
        setctl(quoted, above=["⏺ Claude asked:"] + BASH2)
        until(lambda: "rm -rf build" in run("--read", quoted).stdout, 5)
        time.sleep(1.5)
        left = {n: keys(s) for n, s in ss.items()}
        left["quoted"] = keys(quoted)
        ss["quoted"] = quoted
        for n in ss.values():
            show(n, OTHER)
        for name, s in ss.items():
            check("left alone: %s (a plain prompt after it answered)" % name,
                  left[name] == [] and until(lambda: keys(s) == ["1"], 5))

        # --- the loop guard ----------------------------------------------
        lg = sess("loop")
        show(lg, BASH2, sticky=True)     # "1" leaves it up
        until(lambda: keys(lg), 5)
        time.sleep(2.5)                  # the verify re-read: same prompt
        setctl(lg, screen=BASH2, sticky=True)    # a repaint of it
        time.sleep(1)
        stayed = keys(lg)
        setctl(lg)                       # gone: the composer
        until(lambda: "auto mode on" in run("--read", lg).stdout, 5)
        time.sleep(0.6)
        show(lg, BASH2)                  # the same prompt back, within 30 s
        time.sleep(1.5)
        back = keys(lg)
        show(lg, OTHER)
        check("loop guard: a prompt left up is answered once (verify "
              "re-read and a repaint too)", stayed == ["1"])
        check("loop guard: the same prompt back within 30 s is left alone; "
              "a different one is answered",
              back == ["1"] and until(lambda: keys(lg) == ["1", "1"], 5))

        # --- the sidecar goes with the program ---------------------------
        had = row(on).get("auto_allowed") is not None
        run("--respawn", on, "--", "sh")
        setctl(on)                       # the composer, then /exit
        until(lambda: "auto mode on" in run("--read", on).stdout, 5)
        run("--run", on, "/exit")
        check("auto_allowed cleared when the program exits",
              had and until(lambda: row(on).get("auto_allowed", 0) is None,
                            8))
        return finish(fails, tmp, names, pids)
    except Exception:
        import traceback
        traceback.print_exc()
        finish(fails, tmp, names, pids)
        return 1


def finish(fails, tmp, names, pids):
    for n in names:
        subprocess.run([DCH, "-k", n], capture_output=True)
    time.sleep(0.3)
    for p in pids:          # safety net: only the fakes this test started
        if alive(p):
            try:
                os.kill(p, 9)
            except OSError:
                pass
    shutil.rmtree(tmp, ignore_errors=True)
    if fails:
        print("FAIL: %d auto-allow check(s) failed" % fails)
        return 1
    print("PASS: auto-allow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
