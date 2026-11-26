"""`dch --say` into a Codex session (#658).

A native `codex` (tests/fake_codex.c) runs tests/fake_codex_tui.py, which
paints Codex 0.167's screens (parity spec A1-A5) and logs every key it reads.
HOME and the runtime dir are temporary; no real Codex runs. Covers: idle
composer (pasted, then Tab, which submits there: sent), busy and streaming
(the busy row hidden) (pasted, then Tab: queued, never Enter, which would
steer the turn), a "!" text (pasted, so no shell mode, then Tab), a "/"
text (pasted, then Enter: the slash popup takes Tab),
an approval dialog (skipped/prompt, no key), no composer and the side
conversation's composer (skipped/no_screen, no key). On dch-lite every Codex
session is skipped/no_screen with nothing typed, as for Claude.

Run:  python3 tests/say_codex_test.py        (uses ./dch)
"""
import json, os, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
DCH = os.path.abspath(os.environ.get("DCH", os.path.join(HERE, "..", "dch")))
TAG = str(os.getpid())
fails = 0


def check(msg, cond, extra=""):
    global fails
    print("  %s %s%s" % ("ok  " if cond else "FAIL", msg,
                         "" if cond else " (%s)" % (extra,)))
    if not cond:
        fails += 1


def run(*args, timeout=120):
    return subprocess.run([DCH] + list(args), capture_output=True, text=True,
                          timeout=timeout)


def until(fn, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    return fn()


def main():
    tmp = tempfile.mkdtemp(prefix="dchsc")
    home, bindir = os.path.join(tmp, "home"), os.path.join(tmp, "bin")
    os.makedirs(home)
    os.makedirs(bindir)
    b = subprocess.run(["cc", "-o", os.path.join(bindir, "codex"),
                        os.path.join(HERE, "fake_codex.c")],
                       capture_output=True, text=True, timeout=60)
    if b.returncode:
        print("FAIL: the fake codex does not build:", b.stderr)
        return 1
    for k in list(os.environ):
        if k.startswith(("CLAUDE", "CODEX")) or k in (
                "DCH_SESSION", "DCH_SOCKET_DIR", "DCH_NO_DETECT"):
            del os.environ[k]
    os.environ.update(HOME=home, XDG_RUNTIME_DIR=tmp,
                      XDG_STATE_HOME=os.path.join(tmp, "state"),
                      CODEX_HOME=os.path.join(home, ".codex"),
                      FAKE_CODEX_TUI=os.path.join(HERE, "fake_codex_tui.py"),
                      PATH=bindir + os.pathsep + os.environ["PATH"])
    os.chdir(tmp)
    names = []
    lite = False

    def sess(base, mode):
        n = "%s%s" % (base, TAG)
        d = os.path.join(tmp, n)
        os.mkdir(d)
        with open(os.path.join(d, "mode"), "w") as f:
            f.write(mode)
        names.append(n)
        subprocess.run([DCH, "--spawn", n, "--size", "100x30", "codex"],
                       check=True, capture_output=True,
                       env=dict(os.environ, FAKE_CODEX_DIR=d))
        want = {"idle": "Ask Codex", "busy": "to interrupt)",
                "dialog": "Press enter to confirm", "none": "Loading", "stream": "Streaming",
                "side": "follow-up"}[mode]
        if not lite:
            until(lambda: want in run("--read", n).stdout)
        else:
            time.sleep(1)  # lite: no screen to wait on
        return n, d

    def keys(d):
        try:
            with open(os.path.join(d, "keys.log")) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def say(n, *text):
        r = run("--say", n, "--json", "--", *text)
        try:
            return r.returncode, json.loads(r.stdout.splitlines()[0])
        except (ValueError, IndexError):
            return r.returncode, {"raw": r.stdout + r.stderr}

    try:
        p = "probe%s" % TAG
        subprocess.run([DCH, "--spawn", p, "sh"], check=True,
                       capture_output=True)
        lite = run("--read", p).returncode == 3
        run("-k", p)
        print("build:", "lite" if lite else "full")

        if lite:
            n, d = sess("lite", "idle")
            rc, j = say(n, "hello")
            check("lite: a Codex session is skipped/no_screen, exit 0",
                  rc == 0 and j.get("result") == "skipped" and
                  j.get("reason") == "no_screen", j)
            check("lite: nothing typed", keys(d) == [], keys(d))
            return
        # idle: pasted, then Tab as a later push (spec A4): Tab submits an
        # idle composer as Enter does (A5), sent
        n, d = sess("idle", "idle")
        rc, j = say(n, "hello codex")
        k = keys(d)
        check("idle: sent, exit 0",
              rc == 0 and j.get("result") == "ok" and j.get("reason") == "",
              j)
        check("idle: the text arrived as one bracketed paste",
              "\x1b[200~hello codex\x1b[201~" in k, k)
        check("idle: then Tab on its own, no Enter",
              k[-1:] == ["\t"] and "\r" not in "".join(k), k)
        check("idle: the composer is empty again, the text in history",
              until(lambda: "› hello codex" in run("--read", n).stdout),
              run("--read", n).stdout)
        # a "!" text is pasted too: no shell mode in Codex
        rc, j = say(n, "!", "ls")
        k2 = keys(d)[len(k):]
        check("idle '!': pasted whole, then Tab, sent",
              rc == 0 and j.get("result") == "ok" and
              "\x1b[200~! ls\x1b[201~" in k2 and k2[-1:] == ["\t"],
              (j, k2))
        # a "/" text keeps Enter: Codex's slash popup takes Tab
        rc, j = say(n, "/status")
        k3 = keys(d)[len(k) + len(k2):]
        check("idle '/': pasted, then Enter, no Tab, sent",
              rc == 0 and j.get("result") == "ok" and
              "\x1b[200~/status\x1b[201~" in k3 and k3[-1:] == ["\r"] and
              "\t" not in "".join(k3), (j, k3))

        # streaming: the busy row hidden, the plain composer, fresh output
        # (spec A2): still Tab, so the message queues instead of steering
        n, d = sess("stream", "stream")
        rc, j = say(n, "when you are done")
        k = keys(d)
        check("streaming: sent, pasted, then Tab, never Enter",
              rc == 0 and j.get("result") == "ok" and
              "\x1b[200~when you are done\x1b[201~" in k and
              k[-1:] == ["\t"] and "\r" not in "".join(k), (j, k))

        # busy: Tab queues for the turn's end; Enter would steer it
        n, d = sess("busy", "busy")
        rc, j = say(n, "after this turn")
        k = keys(d)
        check("busy: sent, exit 0", rc == 0 and j.get("result") == "ok", j)
        check("busy: pasted, then Tab, never Enter",
              "\x1b[200~after this turn\x1b[201~" in k and
              k[-1:] == ["\t"] and "\r" not in "".join(k), k)
        check("busy: shown queued",
              "Queued follow-up inputs" in run("--read", n).stdout)

        # a dialog: skipped as prompt, not one key reaches it
        n, d = sess("dlg", "dialog")
        rc, j = say(n, "hi")
        check("dialog: skipped/prompt, exit 0",
              rc == 0 and j.get("result") == "skipped" and
              j.get("reason") == "prompt", j)
        time.sleep(0.5)
        check("dialog: no key sent", keys(d) == [], keys(d))

        # no composer on screen, and the side conversation's composer
        for base, mode in (("none", "none"), ("side", "side")):
            n, d = sess(base, mode)
            rc, j = say(n, "hi")
            check("%s: skipped/no_screen, exit 0" % mode,
                  rc == 0 and j.get("result") == "skipped" and
                  j.get("reason") == "no_screen", j)
            time.sleep(0.5)
            check("%s: no key sent" % mode, keys(d) == [], keys(d))
    finally:
        for n in names:
            run("-k", n)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
    if fails:
        print("%d failed" % fails)
    sys.exit(1 if fails else 0)
