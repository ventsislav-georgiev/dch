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
  - the ref as written (#411): a 40-hex SHA, a commit URL, a pull URL,
    ./x.c reach the argv and DCH_REF unchanged, X-ray's key text in
    DCH_REF_KEY; a ref starting with - is never run (define_warn);
  - a plain-text first line is the title; ANSI escapes and control bytes
    out of titles and bodies, bad UTF-8 as U+FFFD;
  - failures (slow -> stopped at 5 s and the group gone, exit 3, a missing
    program, nothing printed, 2 MB of output): no definition, the done
    line's define_warn; also when it printed a definition first (#412);
  - #412: stderr's line in define_warn cleaned; a plain-text first line the
    only definition; a first line of escapes only is blank; a '{' line that
    does not parse is skipped; C1, NEL and bidi controls out;
  - #831, defined by the transcript: the command never runs, the lines
    as with no define_command;
  - #415: Enter on a commit row of the Refs list sends the row's full
    SHA (argv, DCH_REF), the short key in DCH_REF_KEY;
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
import json, os, re, sys
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
print('{"title":"half"}', flush=True)
time.sleep(30)
''' % pidf, warm=False))
    # one 5 s run for both slow rows: this one and #412's halfslow
    p, defs, done, secs = ref("PROJ-5")
    pids = [int(x) for x in open(pidf).read().split()] \
        if os.path.exists(pidf) else []
    check("slow: stopped at 5 s, its group gone, no definition",
          p.returncode == 0 and not defs and secs < 11 and
          done.get("define_warn") ==
          "define_command: stopped early after 5 s" and len(pids) == 2 and
          until(lambda: not any(alive(x) for x in pids), 3),
          (secs, done, pids))
    want = "define_command: stopped early after 5 s"
    check("%s: a definition then a failure: none, define_warn %r"
          % ("halfslow.py", want), p.returncode == 0 and not defs and
          done.get("define_warn") == want, (p.stdout[-400:], p.stderr))

    for name, body, want in (
            ("fail.py", "import sys\nsys.stderr.write('boom\\n')\n"
             "print('{\"title\":\"x\"}')\nsys.exit(3)\n",
             "define_command: exit 3: boom"),
            ("quiet.py", "pass\n",
             "define_command: no definition printed"),
            ("huge.py", "import sys\nsys.stdout.write('y' * (3 << 20))\n",
             "define_command: stopped early at 1 MB of output")):
        config(script(name, body))
        p, defs, done, _ = ref("PROJ-5")
        w = done.get("define_warn", "")
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


REC = os.path.join(T, "rec.json")
SHA = "0123456789abcdef0123456789abcdef01234567"


def test_written():
    config(script("rec.py", r'''
import json, os, sys
json.dump({"argv": sys.argv[1:], "ref": os.environ.get("DCH_REF"),
           "key": os.environ.get("DCH_REF_KEY"),
           "shape": os.environ.get("DCH_REF_SHAPE")}, open(%r, "w"))
print(json.dumps({"title": "rec"}))
''' % REC))
    for r, shape, key in (
            (SHA, "commit", "0123456"),
            ("https://github.com/o/r/commit/" + SHA, "commit", "0123456"),
            ("https://github.com/o/r/pull/41", "pull", "o/r#41"),
            ("  PROJ-5 ", "ticket", "PROJ-5"),
            ("./x.c", "file", None)):
        if os.path.exists(REC):
            os.unlink(REC)
        p, defs, done, _ = ref(r)
        g = json.load(open(REC)) if os.path.exists(REC) else {}
        w = r.strip()
        check("as written: %r the last argv word and DCH_REF, its key "
              "DCH_REF_KEY, shape %s" % (r, shape),
              p.returncode == 0 and [x.get("title") for x in defs] ==
              ["rec"] and g.get("argv") == [w] and g.get("ref") == w and
              g.get("shape") == shape and (
                  g.get("key") == key if key else
                  g.get("key", "").startswith("/") and
                  g.get("key", "").endswith("/x.c")), (g, done))
    for r in ("-rf/r#1", "--x/y#1"):
        if os.path.exists(REC):
            os.unlink(REC)
        p, defs, done, _ = ref(r)
        check("%r, starting with -: never run, no definition, define_warn"
              % r, p.returncode == 0 and not defs and
              not os.path.exists(REC) and done.get("define_warn") ==
              "define_command: not run for a ref starting with -",
              (p.returncode, p.stdout[-400:], p.stderr))
    config(None)


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


def test_failed_after_rows():
    """#412: definitions printed before a failure are dropped, define_warn
    set (cli and card); stderr's line cleaned; the parse fixes."""
    # warmed with no DCH_REF: it exits at once (endpoint security, #405)
    pre = "import os, sys, time\nif 'DCH_REF' not in os.environ: sys.exit(0)\n"
    for name, body, want in (
            ("half.py", "print('{\"title\":\"half\"}')\n"
             "sys.stderr.write('lookup failed\\n')\nsys.exit(3)\n",
             "define_command: exit 3: lookup failed"),
            ("halfbig.py", "print('{\"title\":\"half\"}')\n"
             "sys.stdout.write('{\"title\":\"last\"}' + ' ' * (1 << 20))\n",
             "define_command: stopped early at 1 MB of output")):
        config(script(name, pre + body))
        p, defs, done, _ = ref("PROJ-5")
        check("%s: a definition then a failure: none, define_warn %r"
              % (name, want), p.returncode == 0 and not defs and
              done.get("define_warn") == want, (p.stdout[-400:], p.stderr))

    config(script("esc.py", r'''
import sys
sys.stderr.buffer.write(b"\x1b]0;PWN\x07err \x1b[31mred\xe2\x80\xaedir"
                        b"\xc2\x9bX\xc2\x85Y\n")
sys.exit(3)
'''))
    p, defs, done, _ = ref("PROJ-5")
    check("stderr's line in define_warn: escapes, C1, NEL, bidi out",
          done.get("define_warn") == "define_command: exit 3: err reddirXY",
          done)

    for name, out, want in (
            ("plainjson.py", b'plain title\n{"title":"json second"}\n',
             ["plain title"]),
            ("escfirst.py", b'\x1b[0m\x1b[?25l\nReal title\nmore\n',
             ["Real title"]),
            ("badjson.py", b'{"title":"t","body":"ok \xff\xfe bad"}\n'
             b'plain after\n{"title":"second"}\n', ["second"]),
            ("c1.py", b'{"title":"T\\u009b31mC\\u0085x\\u202ey","body":'
             b'"B\\u009b2J\\u2066b","status":"s\\u202e"}\n', ["T31mCxy"])):
        config(script(name, "import sys\nsys.stdout.buffer.write(%r)\n" % out))
        p, defs, done, _ = ref("PROJ-5")
        check("%s: the definitions %r" % (name, want),
              p.returncode == 0 and [x.get("title") for x in defs] == want and
              "define_warn" not in done, (p.stdout[-500:], done))
        if name == "c1.py" and defs:
            check("...C1 and bidi out of its body and status",
                  defs[0].get("passage") == "B2Jb" and
                  defs[0].get("status") == "s", defs[0])

    v, s = card(script("halfcard.py", pre + "print('{\"title\":\"half\"}')\n"
                       "sys.stderr.write('ledger down\\n')\nsys.exit(3)\n"))
    try:
        check("card: a definition then exit 3: No definition found and the "
              "reason, not the half definition",
              re.search(r"No definition found (·|-) define_command: exit 3: "
                        r"ledger down", s) and "half" not in s, s)
        v.key(b"q")
    finally:
        v.close()
    config(None)


def test_refs_row_full():
    """#415: a Refs-list row opens the card with its fullest form: a
    commit's full SHA, not the 7-hex key text."""
    record("xv-sha", "11111111-2222-4333-8444-5555555555d5", [
        {"type": "user", "cwd": P, "timestamp": "2026-11-10T10:00:00Z",
         "message": {"content": "the sha fixture"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "fixed in %s today" % SHA}]}}])
    config(script("rec2.py", r'''
import json, os, sys
json.dump({"argv": sys.argv[1:], "ref": os.environ.get("DCH_REF"),
           "key": os.environ.get("DCH_REF_KEY")}, open(%r, "w"))
print(json.dumps({"title": "rec"}))
''' % REC))
    if os.path.exists(REC):
        os.unlink(REC)
    v, _, scr = refs_tab("xv-sha")  # Refs, a: the commit its only row
    try:
        mark = len(v.out)
        v.key(b"\r", 1.0)
        until(lambda: (v.pump(0.1), os.path.exists(REC) and
                       "searching" not in screen(v, mark))[1], 15)
        g = json.load(open(REC)) if os.path.exists(REC) else {}
        check("#415: Refs row of a commit: the full SHA in argv and DCH_REF, "
              "the short key in DCH_REF_KEY", g.get("argv") == [SHA] and
              g.get("ref") == SHA and g.get("key") == "0123456",
              (g, scr, screen(v, mark)))
        os.unlink(REC) if os.path.exists(REC) else None
        mark = len(v.out)
        v.key(b"f", 1.0)  # the full re-scan runs --ref again
        until(lambda: (v.pump(0.1), os.path.exists(REC) and
                       "searching" not in screen(v, mark))[1], 15)
        g = json.load(open(REC)) if os.path.exists(REC) else {}
        check("#415: after f on that card: still the full SHA in argv and "
              "DCH_REF", g.get("argv") == [SHA] and g.get("ref") == SHA and
              g.get("key") == "0123456", (g, screen(v, mark)))
        v.key(b"q")
    finally:
        v.close()
    config(None)


def test_cap_url_bom():
    """#417: past 8 definitions only a 9th definition stops the command
    (the 8 stand); other lines are ignored and the exit status decides. A
    url loses C1 and bidi controls; a BOM or colour codes round a JSON line
    leave it JSON."""
    pre = ("import sys\nif 'DCH_REF' not in __import__('os').environ: "
           "sys.exit(0)\nw = sys.stdout.buffer.write\n"
           "for k in range(8): w(b'{\"title\":\"d%d\"}\\n' % k)\n")
    for name, body, n, want in (
            ("cap_esc.py", "w(b'\\x1b[0m\\n \\x1b[?25l\\n')\n"
             "sys.stderr.write('late\\n')\nsys.exit(3)\n", 0,
             "define_command: exit 3: late"),
            ("cap_bad.py", "w(b'{\"title\":\\n')\nsys.exit(3)\n", 0,
             "define_command: exit 3"),
            ("cap_esc0.py", "w(b'\\x1b[0m\\n')\n", 8, None),
            ("cap_ninth.py", "w(b'{\"title\":\"d8\"}\\n')\nsys.exit(3)\n",
             8, None)):
        config(script(name, pre + body))
        p, defs, done, _ = ref("PROJ-5")
        check("%s: 8 definitions then more: %d stand, define_warn %r"
              % (name, n, want), p.returncode == 0 and len(defs) == n and
              done.get("define_warn") == want, (p.stdout[-400:], p.stderr))

    config(script("url_ctl.py", r'''
import sys
sys.stdout.buffer.write(b'{"title":"t","url":"https://x/\\u202eab\\u0085c'
                        b'\xc2\x9b"}\n')
'''))
    p, defs, done, _ = ref("PROJ-5")
    check("url: C1, NEL and bidi controls out in --json",
          [x.get("url") for x in defs] == ["https://x/abc"], p.stdout[-400:])

    config(script("bom.py", r'''
import sys
sys.stdout.buffer.write(b'\xef\xbb\xbf{"title":"bom","body":"b"}\n'
                        b'{"title":"two"}\n')
'''))
    p, defs, done, _ = ref("PROJ-5")
    check("a BOM before the first JSON line: still JSON, both definitions",
          [x.get("title") for x in defs] == ["bom", "two"] and
          defs[0].get("passage") == "b", p.stdout[-400:])
    config(script("green.py", r'''
import sys
sys.stdout.buffer.write(b'\x1b[32m{"title":"green"}\x1b[0m\n'
                        b'\x1b[1m{"title":"two"}\n')
'''))
    p, defs, done, _ = ref("PROJ-5")
    check("colour codes round JSON lines: still JSON, both definitions",
          [x.get("title") for x in defs] == ["green", "two"],
          p.stdout[-400:])
    config(None)


def main():
    run_steps(fixture, test_cli, test_written, test_card,
              test_failed_after_rows, test_card_width, test_card_zwj,
              test_refs_row_full, test_cap_url_bom)


def test_card_width():
    """#413 (LOW-AV5): a title as wide as the row is cut, the status and
    the "your define_command" mark stay on the card at every width."""
    config(script("wide.py", "import json\nprint(json.dumps({'title': '"
                  + "設" * 60 + "', 'status': 'open'}))\n"))
    for cols in (24, 41, 80, 100):
        v = Pty([DCH, "--xray", "xv-cl"], 30, cols)
        try:
            # below about 80 cols the tab bar drops its counts, so no
            # "Refs 3": the tab bar and the list's selection drawn
            until(lambda: (v.pump(0.1), "[Prompts" in rows(v.out).get(2, "")
                           and "\u258c" in rows(v.out).get(4, ""))[1])
            v.pump(0.3)
            v.key(b"\033[Z\033[Z", 1.0)
            mark = len(v.out)
            v.key(b"\r", 1.0)
            until(lambda: (v.pump(0.1),
                           "your define_command" in screen(v, mark))[1], 10)
            r = [x for x in rows(v.out[mark:]).values()
                 if "your define_command" in x]
            check("#413: %d cols: the mark on the card, the row in its "
                  "width" % cols, r and r[-1].rstrip().endswith(
                      "your define_command") and cells(r[-1].rstrip())
                  <= cols, r)
            if cols >= 41:
                check("#413: %d cols: the wide title cut, the status kept"
                      % cols, r and "設" in r[-1] and "open " in r[-1], r)
            v.key(b"q")
        finally:
            v.close()


def test_card_zwj():
    """#418 (NIT-VA3): the title's cut never leaves a ZWJ at its end.
    #424: nor splits a flag or a skin tone, nor blanks a long ZWJ chain."""
    unit = "\U0001F469\u200d\U0001F4BB"
    for k, title in enumerate(["a" * k + unit * 30 for k in range(4)] + [
            "a" * 30 + "\U0001F1E7\U0001F1EC" * 5,
            "a" * 29 + "\U0001F44D\U0001F3FD" * 5,
            "a" * 31 + "\U0001F1E7\U0001F1EC" * 5,
            "a" * 30 + "\U0001F44D\U0001F3FD" * 5,
            "\U0001F469" + "\u200d\U0001F469" * 60,
            "chain\U0001F469" + "\u200d\U0001F469" * 39]):
        config(script("zwj.py", "import json\nprint(json.dumps({'title': '"
                      + title + "', 'status': 'open'}))\n"))
        v = Pty([DCH, "--xray", "xv-cl"], 30, 41)
        try:
            # 41 cols: no counts in the tab bar, so no "Refs 3"
            until(lambda: (v.pump(0.1), "[Prompts" in rows(v.out).get(2, "")
                           and "\u258c" in rows(v.out).get(4, ""))[1])
            v.pump(0.3)
            v.key(b"\033[Z\033[Z", 1.0)
            mark = len(v.out)
            v.key(b"\r", 1.0)
            until(lambda: (v.pump(0.1),
                           "your define_command" in screen(v, mark))[1], 10)
            r = [x for x in rows(v.out[mark:]).values()
                 if "your define_command" in x]
            check("#418 VA3: %d: no ZWJ before the status" % k,
                  r and "  open" in r[-1] and "\u200d" not in
                  r[-1].split("  open")[0][-1:], r)
            t = r[-1].split("  open")[0] if r else ""
            if k in (4, 6):
                check("#424 VW3: no lone regional indicator",
                      len(re.findall("[\U0001F1E6-\U0001F1FF]", t)) % 2 == 0,
                      r)
            if k in (5, 7):
                check("#424 VW3: no skin tone without its base",
                      not re.search("(^|[^\U0001F44D])[\U0001F3FB-\U0001F3FF]",
                                    t), r)
            if k == 8:
                check("#424 VW4: a long ZWJ chain is not blank",
                      "\U0001F469" in t, r)
            if k == 9:
                check("#429 VX2: text, then a long chain: more than one "
                      "character of it", t.strip().startswith("chain") and
                      t.count("\U0001F469") > 1, r)
            v.key(b"q")
        finally:
            v.close()


if __name__ == "__main__":
    main()
