#!/usr/bin/env python3
"""define_command (#405): a ref the transcripts do not define, defined by
the user's own command.

On xray_view_lib's ended Claude xv-cl (#831 defined in its transcript,
PROJ-5 only mentioned), `dch --xray xv-cl --ref <ref> --json` and the card:
  - unset: no definition, no define_warn, nothing run;
  - JSON lines: a definition per object, "source":"command", rule
    "command", its title, body as the passage (20 lines, 2 KB), url,
    status; the ref the last argv word, DCH_REF, DCH_REF_SHAPE,
    DCH_SESSION; at most 8;
  - a plain-text first line is the title; ANSI escapes and control bytes
    out of titles and bodies, bad UTF-8 as U+FFFD;
  - failures (slow -> stopped at 5 s and the group gone, exit 3, a missing
    program, nothing printed, 2 MB of output): no definition, the done
    line's define_warn;
  - #831, defined by the transcript: the command never runs, the lines
    as with no define_command;
  - the card: the title marked "your define_command", its body and url;
    a failure: "No definition found" and the reason on that line.

Run:  python3 tests/xray_define_test.py      (uses ./dch)
"""
from xray_view_lib import *  # noqa: F401,F403 (harness, fixtures, T)

CONFIG = ENV["DCH_CONFIG"]
PY = sys.executable
MARK = os.path.join(T, "ran")


def script(name, body, warm=True):
    p = os.path.join(T, name)
    with open(p, "w") as f:
        f.write("#!%s\n" % PY + body)
    os.chmod(p, 0o755)
    # run once first: the first exec of a new file waits on endpoint
    # security's scan, which under load took about 3 s (#405)
    if warm:
        subprocess.run([p], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60)
    return p


def config(cmd):
    with open(CONFIG, "w") as f:
        if cmd is not None:
            f.write("define_command = %s\n" % cmd)


def ref(r, *more):
    t0 = time.time()
    p = dch("--xray", "xv-cl", "--ref", r, *more, "--json")
    try:
        ls = [json.loads(l) for l in p.stdout.splitlines()]
    except ValueError:
        ls = []
    defs = [l["definition"] for l in ls if "definition" in l]
    done = ls[-1] if ls and ls[-1].get("done") else {}
    return p, defs, done, time.time() - t0


OK = script("ok.py", r'''
import json, os, sys
open(%r, "w").write(" ".join(sys.argv[1:]))
print(json.dumps({"title": "Ticket <%%s|%%s|%%s|%%s>" %% (sys.argv[-1],
                  os.environ.get("DCH_REF"), os.environ.get("DCH_REF_SHAPE"),
                  os.environ.get("DCH_SESSION")),
                  "body": "\n\nfirst line\n" + "\n".join(
                      "line %%d" %% k for k in range(2, 40)),
                  "url": "https://example.com/t/PROJ-5", "status": "open"}))
print("not json, skipped")
print(json.dumps({"title": "\u001b[31mred\u001b[0m T\u0007x",
                  "body": "a\u001b]0;pwn\u0007b \u001b[1mc" + "é" * 3000}))
for k in range(10):
    print(json.dumps({"title": "extra %%d" %% k}))
''' % MARK)


def test_cli():
    config(None)
    p, defs, done, _ = ref("PROJ-5")
    check("unset: no definition, no define_warn",
          p.returncode == 0 and done and not defs and
          "define_warn" not in done, (p.returncode, p.stdout[-400:]))
    base = p.stdout

    config(OK + " --flag")
    p, defs, done, _ = ref("PROJ-5")
    d = defs[0] if defs else {}
    check("json: a definition from the command, source command, rule "
          "command, <n>'s session, no at",
          p.returncode == 0 and d.get("source") == "command" and
          d.get("rule") == "command" and d.get("session") == "xv-cl" and
          d.get("at") is None and "define_warn" not in done,
          (p.returncode, p.stdout[-800:], p.stderr))
    check("...the ref the last argv word, DCH_REF, DCH_REF_SHAPE, "
          "DCH_SESSION", d.get("title") ==
          "Ticket <PROJ-5|PROJ-5|ticket|xv-cl>" and
          open(MARK).read() == "--flag PROJ-5", (d, open(MARK).read()))
    pas = d.get("passage") or ""
    check("...its body the passage: leading blanks off, 20 lines",
          pas.startswith("first line\nline 2\n") and
          pas.count("\n") == 19 and pas.endswith("line 20"), pas)
    check("...url and status kept", d.get("url") ==
          "https://example.com/t/PROJ-5" and d.get("status") == "open", d)
    e = defs[1] if len(defs) > 1 else {}
    check("...escapes and control bytes out of a title",
          e.get("title") == "red Tx", e)
    pas = e.get("passage") or ""
    check("...and out of a body, cut at 2 KB on a character",
          pas.startswith("ab cé") and len(pas.encode()) <= 2048 and
          len(pas.encode()) > 2040 and "\033" not in pas, pas[:40])
    check("...at most 8 definitions (the non-JSON line after the first "
          "skipped)", len(defs) == 8 and defs[-1].get("title") == "extra 5",
          [x.get("title") for x in defs])

    config(script("plain.py", r'''
import sys
sys.stdout.buffer.write(b"\n\x1b[1mPlain \xff title\x1b[0m\nmore text\n")
'''))
    p, defs, done, _ = ref("PROJ-5")
    check("plain text: its first line the title, escapes out, bad UTF-8 "
          "as U+FFFD, one definition", [x.get("title") for x in defs] ==
          ["Plain � title"] and defs[0].get("passage") is None,
          p.stdout[-500:])

    os.unlink(MARK)
    config(OK)
    p, defs, done, _ = ref("#831")
    config(None)
    q, qdefs, _, _ = ref("#831")
    check("#831, defined by the transcript: the command never runs, the "
          "answer as with none", p.returncode == 0 and defs and
          not os.path.exists(MARK) and
          all("source" not in x for x in defs) and
          re.sub(r'"elapsed_ms":\d+', "", p.stdout) ==
          re.sub(r'"elapsed_ms":\d+', "", q.stdout), (p.stdout[-600:],
                                                       q.stdout[-600:]))

    pidf = os.path.join(T, "slow.pid")
    config(script("slow.py", r'''
import os, subprocess, time
k = subprocess.Popen(["sleep", "30"])
open(%r, "w").write("%%d %%d" %% (os.getpid(), k.pid))
time.sleep(30)
''' % pidf, warm=False))
    p, defs, done, secs = ref("PROJ-5")
    pids = [int(x) for x in open(pidf).read().split()] \
        if os.path.exists(pidf) else []
    check("slow: stopped at 5 s, its group gone, no definition",
          p.returncode == 0 and not defs and secs < 11 and
          done.get("define_warn") ==
          "define_command: stopped early after 5 s" and len(pids) == 2 and
          until(lambda: not any(alive(x) for x in pids), 3),
          (secs, done, pids))

    for name, body, want in (
            ("fail.py", "import sys\nsys.stderr.write('boom\\n')\n"
             "print('{\"title\":\"x\"}')\nsys.exit(3)\n", None),
            ("quiet.py", "pass\n",
             "define_command: no definition printed"),
            ("huge.py", "import sys\nsys.stdout.write('y' * (3 << 20))\n",
             "define_command: stopped early at 1 MB of output")):
        config(script(name, body))
        p, defs, done, _ = ref("PROJ-5")
        w = done.get("define_warn", "")
        if want is None:   # a row then exit 3: its rows stand (as tasks)
            check("exit 3 after a row: the row stands", len(defs) == 1, done)
            config(script("fail2.py", "import sys\nsys.stderr.write("
                          "'boom\\n')\nsys.exit(3)\n"))
            p, defs, done, _ = ref("PROJ-5")
            w, want = done.get("define_warn", ""), \
                "define_command: exit 3: boom"
            name = "fail2.py"
        check("%s: no definition, done's define_warn %r" % (name, want),
              p.returncode == 0 and not defs and w == want, (done, p.stderr))
    config(os.path.join(T, "no-such-program"))
    p, defs, done, _ = ref("PROJ-5")
    check("a missing program: no definition, define_warn cannot run",
          p.returncode == 0 and not defs and re.fullmatch(
              r"define_command: cannot run \S*/no-such-program: No such "
              r"file or directory", done.get("define_warn", "")), done)
    config(None)
    check("unset again: the answer as before",
          re.sub(r'"elapsed_ms":\d+', "", ref("PROJ-5")[0].stdout) ==
          re.sub(r'"elapsed_ms":\d+', "", base))


def card(cmd):
    config(cmd)
    v = Pty([DCH, "--xray", "xv-cl"], 30, 100)
    v.wait(b"Refs 3")
    v.pump(0.3)
    v.key(b"\033[Z\033[Z", 1.0)  # Refs; PROJ-5 is its first row (#262)
    mark = len(v.out)
    v.key(b"\r", 1.0)
    v.wait(b"Mentions: this session", start=mark)
    until(lambda: (v.pump(0.1), "searching" not in screen(v, mark))[1], 10)
    v.pump(0.3)
    return v, screen(v, mark)


def test_card():
    v, s = card(OK)
    try:
        check("card: the command's definition, marked as yours, its status",
              re.search(r"Ticket <PROJ-5\|PROJ-5\|ticket\|xv-cl> open "
                        r"(·|-) your define_command", s) and
              "No definition found" not in s, s)
        check("...its url and its body's own lines below it",
              "https://example.com/t/PROJ-5" in s and
              re.search(r"\n *first line *\n *line 2 *\n *line 3", s), s)
        check("...and the escaped title shows no escape", "red Tx" in s
              and "\033]0;pwn" not in v.out.decode("utf-8", "replace"), s)
        v.key(b"q")
    finally:
        v.close()
    v, s = card(script("fail3.py", "import sys\nsys.stderr.write("
                       "'ledger down\\n')\nsys.exit(3)\n"))
    try:
        check("card: a failed command: No definition found and the reason "
              "on that line", re.search(r"No definition found (·|-) "
                                        r"define_command: exit 3: ledger "
                                        r"down", s), s)
        v.key(b"q")
    finally:
        v.close()


def main():
    run_steps(fixture, test_cli, test_card)


if __name__ == "__main__":
    main()
