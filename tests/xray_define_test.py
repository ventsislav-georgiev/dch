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
              test_refs_row_full, test_cap_url_bom, test_fetched,
              test_prefilter_fold, test_read, test_read_shell,
              test_lead_in, test_lead_cmd)


# ---- #515: a URL the agent fetched resolves from the transcript ------------

import calendar  # noqa: E402

USID = "11111111-2222-4333-8444-5555555555c5"
DEF_KEYS = ["at", "chapter", "live", "offset", "passage", "project", "rule",
            "session", "title"]
# 2 KB: closed within the 3000-scalar output clip (#527: unclosed is none)
HOSTILE = "\033[31mA\u202eB\u0085C" + "x" * 2048
LONGHOST = ".".join(["h" * 49] * 4) + ".example.com"  # 211


def fixture_fetched():
    """xv-uf, an ended Claude: WebFetch with a <title>, one with a markdown
    heading, a curl, a URL fetched twice, a hostile title, a URL only said."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    lines = []

    def call(ts, k, name, inp, out):
        lines.append({"type": "assistant", "timestamp": ts,
                      "message": {"content": [{"type": "tool_use",
                                               "id": "u%d" % k, "name": name,
                                               "input": inp}]}})
        lines.append({"type": "user", "timestamp": ts,
                      "toolUseResult": {"stdout": out} if name == "Bash"
                      else {}, "message": {"content": [
                          {"type": "tool_result", "tool_use_id": "u%d" % k,
                           "content": out}]}})

    lines.append({"type": "user", "cwd": P, "timestamp":
                  "2026-11-01T10:00:00Z", "message": {"content":
                  "read the docs; never fetch https://never.example.com/n"}})
    call("2026-11-01T10:01:00Z", 1, "WebFetch",
         {"url": "https://docs.example.com/guide", "prompt": "sum"},
         "<html><head><title>The  Guide &amp; &lt;more&gt; &quot;x&quot; "
         "&#39;y&#39;</title></head><body># Not this</body></html>")
    call("2026-11-01T10:02:00Z", 2, "WebFetch",
         {"url": "https://md.example.org/a", "prompt": "sum"},
         "intro line\n# Markdown Heading\nbody")
    call("2026-11-01T10:03:00Z", 3, "Bash",
         {"command": "curl -sL https://curl.example.net/p | head"},
         "<!doctype html><title>Curl Page</title>")
    call("2026-11-02T10:00:00Z", 4, "WebFetch",
         {"url": "https://twice.example.com/x", "prompt": "sum"},
         "<title>Old Title</title>")
    call("2026-11-05T10:00:00Z", 5, "WebFetch",
         {"url": "https://twice.example.com/x", "prompt": "sum"},
         "<title>New Title</title>")
    call("2026-11-06T10:00:00Z", 6, "WebFetch",
         {"url": "https://evil.example.com/", "prompt": "sum"},
         "<title>" + HOSTILE + "</title>")
    # #527: the title rule's QA 1.113.0 cases
    for k, url, out in [
            (7, "https://mdtag.example.com/g", "# HTML Guide\n\nUse the "
             "`<title>` element to name a page; close it with `</title>`.\n"),
            (8, "https://cmt.example.com/c",
             "<!-- <title>Fake</title> --><title>Real</title>"),
            (9, "https://svg.example.com/s", "<html><head></head><body><svg>"
             "<title>Icon close</title></svg><h1>Page</h1></body></html>"),
            (10, "https://fence.example.com/f", "intro\n```sh\n# install deps"
             "\nnpm i\n```\n# Real Heading\n"),
            (12, "https://ent.example.com/e", "<title>Don&#8217;t &#8211; "
             "&#x27;q&#x27; a&nbsp;b &mdash; x&#x202E;y&#27;z&#xFFFFFFFF;"
             "&hellip;</title>"),
            (13, "https://zwj.example.com/z", "<title>" + "a" * 112 +
             "\U0001F469\u200d\U0001F4BB</title>"),
            (14, "https://mark.example.com/m", "<title>" + "a" * 119 +
             "e\u0301</title>"),
            (15, "https://inv.example.com/i",
             "<title>a\u200bb\ufeffc\U000e0041d\u00ade</title>"),
            (16, "https://%s/p" % LONGHOST, "<title>Long Host</title>"),
            (41, "https://tn1.example.com/a", "<html><head><bodyguard>"
             "</bodyguard><title>Real</title></head>"),
            (42, "https://tn2.example.com/a",
             "<head><svgfoo/><title>Real</title>"),
            (43, "https://tn3.example.com/a",
             "<html><header>nav</header><title>Real</title>"),
            (44, "https://ri.example.com/r", "<title>" + "a" * 116 +
             "\U0001F1E7\U0001F1EC</title>"),
            (46, "https://late.example.com/l", "<html><head><meta></head>"
             "<body><h1>Page</h1><title>Late</title></body></html>"),
            (47, "https://sur.example.com/s", "<title>a&#xD800;b&#xDFFF;c"
             "</title>"),
            (45, "https://eng.example.com/e", "<title>x\U0001F3F4\U000E0067"
             "\U000E0062\U000E0065\U000E006E\U000E0067\U000E007F</title>"),
            (48, "https://cut.example.com/c", "<title>" + "a" * 100 +
             "\U0001F3F4\U000E0067\U000E0062\U000E0065\U000E006E\U000E0067"
             "\U000E007F</title>"),
            (49, "https://strayt.example.com/s", "<title>Go \U0001F3F4"
             "\U000E0067\U000E0062\U000E007F\U000E0041\U000E0042 AB</title>"),
            (50, "https://svgself.example.com/s",
             "<head><svg/><title>AfterSvg</title>")]:
        call("2026-11-06T11:00:00Z", k, "WebFetch", {"url": url,
                                                      "prompt": "sum"}, out)
    call("2026-11-06T11:00:00Z", 11, "Bash",
         {"command": "curl -sL https://raw.example.com/install.sh"},
         "#!/bin/sh\n# Copyright 2026 Someone\necho hi\n")
    # #528: not a fetch of the URL: another command's word, -o/-O to a file,
    # two URLs, mixed with a list; a wget to stdout is; ?r=/ is not ?r=
    for k, (cmd, out) in enumerate((
            ("rg -n curl README.md; echo https://nf1.example.com/a",
             "<title>Readme heading</title>"),
            ("curl -s https://nf2.example.com/a -o a.html && cat README.md",
             "<title>Project Readme</title>"),
            ("curl -s https://nf3.example.com/a https://nf3.example.com/b",
             "<title>Two</title>"),
            ("wget -O a.html https://nf4.example.com/a", "<title>W</title>"),
            ("curl -sLo a.html https://nf5.example.com/a", "<title>C</title>"),
            ("curl -s https://nf6.example.com/a; cat README.md",
             "<title>Readme again</title>"),
            ("wget -qO- https://yes.example.com/a", "<title>Wget Out</title>"),
            ("sudo curl -s https://yes2.example.com/a 2>&1 | head",
             "<title>Sudo Curl</title>"),
            # #540, #542: still stdout, still the only fetch
            ("curl -s --output-dir /tmp https://y3.example.com/a",
             "<title>Output Dir</title>"),
            ("curl -sL -o /dev/stdout https://y4.example.com/a",
             "<title>Dev Stdout</title>"),
            ("cd /tmp && curl -s https://y5.example.com/a",
             "<title>After Cd</title>"),
            ("(curl -s https://y6.example.com/a)",
             "<title>Subshell</title>"),
            ("curl -s https://y7.example.com/a && echo ok",
             "<title>Then Echo</title>"),
            ("curl -s https://y8.example.com/a &",
             "<title>Background</title>"),
            ("echo https://y9.example.com/a | xargs curl -s",
             "<title>Xargs</title>"),
            # #543: plain wget saves to a file; a URL after the pipe is
            # another command's
            ("wget https://nf7.example.com/a", "<title>Wget File</title>"),
            ("curl -s localhost:8080/p | grep https://nf8.example.com/a",
             "<title>Grep Url</title>"),
            # #542 review: what the new forms must still refuse
            ("curl -s https://n5.example.com/a && echo $(date)",
             "<title>Echo Runs</title>"),
            ("cd /tmp && curl -s -o f https://n6.example.com/a",
             "<title>Cd To File</title>"),
            ("curl -s https://n7.example.com/a & cat x",
             "<title>Bg Then Cat</title>"),
            # #565: an echo's text is in the output: no tag, no expansion
            ("curl -s https://n8.example.com/a && echo $HOME",
             "<title>Echo Home</title>"),
            ("curl -s https://n9.example.com/a && echo '<title>X</title>'",
             "<html><title>Echo Fake</title>")), 21):
        call("2026-11-07T10:%02d:00Z" % k, k, "Bash", {"command": cmd}, out)
    call("2026-11-07T11:00:00Z", 20, "WebFetch",
         {"url": "https://q.example.com/x?r=/", "prompt": "sum"},
         "<title>Slash In Query</title>")
    with open(os.path.join(pdir, USID + ".jsonl"), "w") as f:
        for o in lines:
            f.write(json.dumps(o) + "\n")
    sockdir = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    rec = {"name": "xv-uf", "cwd": P, "end": "exited", "argv": ["claude"],
           "started": 1794299400, "updated": 1794300000,
           "ended": 1794300000, "sock_dir": sockdir,
           "agent": {"session_id": USID, "cwd": P, "config_dir": cfg}}
    with open(os.path.join(hdir, "xv-uf.json"), "w") as f:
        f.write(json.dumps(rec) + "\n")


def test_fetched():
    fixture_fetched()
    config(None)

    def res(r, full=False):
        p = dch("--xray", "xv-uf", "--ref", r, *(["--full"] if full else []),
                "--json")
        try:
            ls = [json.loads(l) for l in p.stdout.splitlines()]
        except ValueError:
            ls = []
        rv = [l["resolved"] for l in ls if "resolved" in l]
        return p, ls, rv

    def age(iso):
        d = int(time.time()) - calendar.timegm(time.strptime(
            iso, "%Y-%m-%dT%H:%M:%SZ"))
        return "%dd ago" % (d // 86400)

    p, ls, rv = res("https://docs.example.com/guide")
    check("#515 WebFetch <title>: kind url, the title entity-decoded, "
          "\"<host> \u00b7 fetched <age> in <session>\", just before done",
          len(rv) == 1 and rv[0] == {
              "kind": "url", "state": "ok",
              "title": "The Guide & <more> \"x\" 'y'",
              "subtitle": "docs.example.com \u00b7 fetched %s in xv-uf"
              % age("2026-11-01T10:01:00Z"), "lines": []} and
          "resolved" in ls[-2] and ls[-1].get("done") is True,
          (p.stdout[-700:], p.stderr))
    defs = [l["definition"] for l in ls if "definition" in l]
    check("#515: the fetch is the URL's definition, rule fetch",
          [(d.get("rule"), d.get("title")) for d in defs] ==
          [("fetch", "The Guide & <more> \"x\" 'y'")], defs)
    p, ls, rv = res("HTTPS://Docs.Example.COM/guide/#intro")
    check("#515: scheme and host case, a fragment, a trailing slash: the "
          "same URL", len(rv) == 1 and rv[0]["title"].startswith("The Guide")
          and rv[0]["subtitle"].startswith("docs.example.com \u00b7"), rv)
    p, ls, rv = res("https://md.example.org/a", full=True)
    check("#515 a markdown heading (--full too): its first \"# \" line",
          len(rv) == 1 and rv[0]["title"] == "Markdown Heading" and
          rv[0]["subtitle"].startswith("md.example.org \u00b7 fetched "),
          (p.stdout[-500:], rv))
    p, ls, rv = res("https://curl.example.net/p")
    check("#515 curl: the command's one URL, its output's <title>",
          len(rv) == 1 and rv[0]["title"] == "Curl Page" and
          rv[0]["subtitle"] == "curl.example.net \u00b7 fetched %s in xv-uf"
          % age("2026-11-01T10:03:00Z"), rv)
    p, ls, rv = res("https://twice.example.com/x")
    check("#515 fetched twice: the newest wins, its age",
          len(rv) == 1 and rv[0]["title"] == "New Title" and
          rv[0]["subtitle"] == "twice.example.com \u00b7 fetched %s in xv-uf"
          % age("2026-11-05T10:00:00Z"), rv)
    p, ls, rv = res("https://never.example.com/n")
    check("#515 a URL never fetched: no resolved line, done",
          p.returncode == 0 and not rv and ls and ls[-1].get("done") is True
          and any("mention" in l for l in ls), p.stdout[-500:])
    p, ls, rv = res("https://evil.example.com/")
    t = rv[0]["title"] if rv else None
    check("#515 a hostile title (ESC, RLO, NEL, 2 KB): clean, capped at "
          "120 bytes on a character",
          t is not None and t.startswith("[31mA B C") and
          not re.search("[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f"
                        "\u202a-\u202e\u2066-\u2069]", t) and
          len(t.encode()) == 120, repr(t)[:200])
    def title(url):
        rv = res(url)[2]
        return rv[0]["title"] if len(rv) == 1 else None
    t = title("https://mdtag.example.com/g")
    check("#527 a markdown page that mentions `<title>`: its heading", t ==
          "HTML Guide", t)
    t = title("https://cmt.example.com/c")
    check("#527 a <title> in an HTML comment is skipped", t == "Real", t)
    p, ls, rv = res("https://svg.example.com/s")
    check("#527 an inline SVG's <title> in the body is no page title",
          not rv, rv)
    t = title("https://fence.example.com/f")
    check("#527 a \"# \" line in a code fence is no heading",
          t == "Real Heading", t)
    p, ls, rv = res("https://raw.example.com/install.sh")
    check("#527 a curl'd script's \"# \" comment is no title (headings "
          "are WebFetch markdown only)", not rv, rv)
    for n in "123":
        t = title("https://tn%s.example.com/a" % n)
        check("#539 a tag name ends at > / or a blank: <bodyguard>, "
              "<svgfoo/>, </header> are not <body>, <svg>, </head (%s)" % n,
              t == "Real", t)
    p, ls, rv = res("https://late.example.com/l")
    check("#543 a <title> after <body> is no page title (mbody)", not rv, rv)
    t = title("https://sur.example.com/s")
    check("#543 a surrogate entity is left literal, not decoded to U+FFFD "
          "(msurr)", t == "a&#xD800;b&#xDFFF;c", repr(t))
    t = title("https://ent.example.com/e")
    check("#527 numeric and named entities decoded, a decoded bidi or "
          "control blanked, an out-of-range one literal",
          t == "Don\u2019t \u2013 'q' a b \u2014 x y z&#xFFFFFFFF;\u2026",
          repr(t))
    t = title("https://zwj.example.com/z")
    check("#527 the 120-byte cut leaves no dangling ZWJ",
          t == "a" * 112 + "\U0001F469", repr(t))
    t = title("https://mark.example.com/m")
    check("#527 the 120-byte cut never splits a combining mark from its "
          "base", t == "a" * 119, repr(t))
    t = title("https://inv.example.com/i")
    check("#527 U+200B, U+FEFF, U+00AD and tag characters dropped",
          t == "abcde", repr(t))
    t = title("https://ri.example.com/r")
    check("#541 the 120-byte cut never leaves half a flag", t == "a" * 116,
          repr(t))
    t = title("https://eng.example.com/e")
    check("#541 tag characters after U+1F3F4 stay: the England flag",
          t == "x\U0001F3F4\U000E0067\U000E0062\U000E0065\U000E006E"
          "\U000E0067\U000E007F", repr(t))
    t = title("https://cut.example.com/c")
    check("#564 the 120-byte cut drops a flag's tag run with no cancel tag, "
          "and its U+1F3F4", t == "a" * 100, repr(t))
    t = title("https://strayt.example.com/s")
    check("#564 tag characters after a cancel tag are dropped",
          t == "Go \U0001F3F4\U000E0067\U000E0062\U000E007F AB", repr(t))
    t = title("https://svgself.example.com/s")
    check("#564 a self-closing <svg/> in the head is closed: the title "
          "after it", t == "AfterSvg", t)
    rv = res("https://%s/p" % LONGHOST)[2]
    sub = rv[0]["subtitle"] if rv else ""
    check("#527 a long host is capped so \" \u00b7 fetched ... in "
          "<session>\" still fits",
          sub.startswith(LONGHOST[:60] + "\u2026 \u00b7 fetched ") and
          sub.endswith(" in xv-uf"), sub)
    for u, why in (("https://nf1.example.com/a", "rg curl README.md; echo URL"),
                   ("https://nf2.example.com/a", "curl -o a.html && cat"),
                   ("https://nf3.example.com/a", "two URLs"),
                   ("https://nf4.example.com/a", "wget -O file"),
                   ("https://nf5.example.com/a", "curl -sLo file"),
                   ("https://nf6.example.com/a", "curl URL; cat README.md"),
                   ("https://nf7.example.com/a", "plain wget to a file (#543)"),
                   ("https://nf8.example.com/a", "a URL after the pipe (#543)")):
        p, ls, rv = res(u)
        check("#528 %s: not a fetch, no title" % why,
              p.returncode == 0 and not rv and ls and
              ls[-1].get("done") is True, (p.stdout[-400:], rv))
    for u, t in (("https://yes.example.com/a", "Wget Out"),
                 ("https://yes2.example.com/a", "Sudo Curl")):
        p, ls, rv = res(u)
        check("#528 wget -qO- and sudo curl | head: still a fetch",
              len(rv) == 1 and rv[0]["title"] == t, rv)
    for u, t, why in (
            ("https://y3.example.com/a", "Output Dir", "#540 --output-dir"),
            ("https://y4.example.com/a", "Dev Stdout", "#540 -o /dev/stdout"),
            ("https://y5.example.com/a", "After Cd", "#542 cd /tmp && curl"),
            ("https://y6.example.com/a", "Subshell", "#542 (curl)"),
            ("https://y7.example.com/a", "Then Echo", "#542 curl && echo ok"),
            ("https://y8.example.com/a", "Background", "#542 curl &"),
            ("https://y9.example.com/a", "Xargs", "#542 echo URL | xargs curl")):
        p, ls, rv = res(u)
        check("%s: still a fetch, titled" % why,
              len(rv) == 1 and rv[0]["title"] == t, rv)
    for u, why in (("https://n5.example.com/a", "&& echo $(date)"),
                   ("https://n6.example.com/a", "cd && curl -o f"),
                   ("https://n7.example.com/a", "curl & cat x"),
                   ("https://n8.example.com/a", "&& echo $HOME (#565)"),
                   ("https://n9.example.com/a", "&& echo '<title>X</title>' "
                    "(#565)")):
        p, ls, rv = res(u)
        check("#542 %s: not the only stdout fetch, no title" % why,
              p.returncode == 0 and not rv, rv)
    p, ls, rv = res("https://q.example.com/x?r=")
    check("#528: ?r=/ is not ?r=: no title", p.returncode == 0 and not rv, rv)
    p, ls, rv = res("https://q.example.com/x?r=/")
    check("#528: ?r=/ matches itself",
          len(rv) == 1 and rv[0]["title"] == "Slash In Query", rv)
    q = dch("--xray", "xv-cl", "--ref", "#831", "--json")
    qs = [json.loads(l) for l in q.stdout.splitlines()]
    d = [l["definition"] for l in qs if "definition" in l]
    check("#515: other kinds' JSON unchanged: #831's definition keys and "
          "rule, no resolved line",
          not any("resolved" in l for l in qs) and len(d) == 1 and
          d[0]["rule"] == "D1" and sorted(d[0]) == DEF_KEYS, q.stdout[-400:])


def test_prefilter_fold():
    """#515: a fetched URL with an upper-case path or query: the light scan's
    prefilter folds case, so it finds all 7 mentions, as --full does."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    sid = "11111111-2222-4333-8444-5555555555d8"
    u = "https://pf.example.com/Path/Doc?Key=Val"
    ts = lambda i, s: "2026-11-01T%02d:%02d:%02dZ" % (10 + i // 60, i % 60, s)
    lines = [{"type": "user", "cwd": P, "timestamp": ts(0, 0),
              "message": {"content": "go"}},
             {"type": "assistant", "timestamp": ts(0, 1), "message": {
                 "content": [{"type": "tool_use", "id": "f1",
                              "name": "WebFetch", "input": {"url": u}}]}},
             {"type": "user", "timestamp": ts(0, 2), "message": {"content": [
                 {"type": "tool_result", "tool_use_id": "f1",
                  "content": "<title>PF Title</title>"}]}}]
    for i in range(1, 49):
        lines.append({"type": "user", "timestamp": ts(i, 0),
                      "message": {"content": "ask %d" % i}})
        lines.append({"type": "assistant", "timestamp": ts(i, 3),
                      "message": {"content": [{"type": "text", "text":
                                               "filler words " * 1500}]}})
        if i % 8 == 0:
            lines.append({"type": "assistant", "timestamp": ts(i, 5),
                          "message": {"content": [{"type": "text",
                                                   "text": "again " + u}]}})
    with open(os.path.join(pdir, sid + ".jsonl"), "w") as f:
        f.write("".join(json.dumps(o) + "\n" for o in lines))
    sockdir = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    with open(os.path.join(hdir, "xv-pf.json"), "w") as f:
        f.write(json.dumps({"name": "xv-pf", "cwd": P, "end": "exited",
                            "argv": ["claude"], "started": 1794299400,
                            "updated": 1794300000, "ended": 1794300000,
                            "sock_dir": sockdir,
                            "agent": {"session_id": sid, "cwd": P,
                                      "config_dir": cfg}}) + "\n")
    config(None)
    n = {}
    for full in (False, True):
        p = dch("--xray", "xv-pf", "--ref", u, *(["--full"] if full else []),
                "--json")
        n[full] = p.stdout.count('"mention"')
    check("#528 light finds all 7 mentions of an upper-case URL, as --full "
          "(prefilter fold)", n == {False: 7, True: 7}, n)


# ---- #517: a mention says when the agent READ the ref ---------------------

RSID = "11111111-2222-4333-8444-5555555555c7"


def fixture_read():
    """xv-rd, an ended Claude naming src/engine.c: said by the user and the
    reply, read by Read, rg, Grep, a cat after cd, and echoed by a command
    that only writes (cat is its argument, not a command); output paths
    carry a line (a bare relative path in code is no ref)."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    lines = [{"type": "user", "cwd": P, "timestamp": "2026-11-01T10:00:00Z",
              "message": {"content": "where is src/engine.c used?"}},
             {"type": "assistant", "timestamp": "2026-11-01T10:00:01Z",
              "message": {"content": [{"type": "text", "text":
                                       "src/engine.c holds the loop"}]}}]

    def call(k, name, inp, out):
        ts = "2026-11-01T10:0%d:00Z" % k
        lines.append({"type": "assistant", "timestamp": ts,
                      "message": {"content": [{"type": "tool_use",
                                               "id": "r%d" % k, "name": name,
                                               "input": inp}]}})
        lines.append({"type": "user", "timestamp": ts,
                      "toolUseResult": {"stdout": out} if name == "Bash"
                      else {}, "message": {"content": [
                          {"type": "tool_result", "tool_use_id": "r%d" % k,
                           "content": out}]}})

    call(1, "Read", {"file_path": P + "/src/engine.c"}, "1\tint loop;")
    call(2, "Bash", {"command": "rg -n loop src"}, "src/engine.c:1:int loop;")
    call(3, "Grep", {"pattern": "loop"}, "src/engine.c:1:int loop;\n")
    call(4, "Bash", {"command": "cd src && cat ../NOTES"},
         "see src/engine.c:1\n")
    call(5, "Bash", {"command": "echo cat src/engine.c | tee list.txt"},
         "cat src/engine.c:1\n")
    with open(os.path.join(pdir, RSID + ".jsonl"), "w") as f:
        for o in lines:
            f.write(json.dumps(o) + "\n")
    sockdir = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    rec = {"name": "xv-rd", "cwd": P, "end": "exited", "argv": ["claude"],
           "started": 1794299400, "updated": 1794300000,
           "ended": 1794300000, "sock_dir": sockdir,
           "agent": {"session_id": RSID, "cwd": P, "config_dir": cfg}}
    with open(os.path.join(hdir, "xv-rd.json"), "w") as f:
        f.write(json.dumps(rec) + "\n")


def test_read():
    fixture_read()
    config(None)
    p = dch("--xray", "xv-rd", "--ref", "src/engine.c", "--json")
    try:
        ms = [json.loads(l)["mention"] for l in p.stdout.splitlines()
              if '"mention"' in l]
    except ValueError:
        ms = []
    got = [(m.get("who"), m.get("tool"), m.get("read")) for m in ms]
    check("#517 a Read call's file: read \"Read\"",
          ("tool", "Read", "Read") in got, (got, p.stdout[-600:]))
    check("#517 an rg result: read \"rg\"; a Grep result: read \"Grep\"",
          ("tool", "Bash", "rg") in got and ("tool", "Grep", "Grep") in got,
          got)
    check("#517 a reader after cd (cd src && cat): read \"cat\"",
          ms and ms[-2]["line"] == "see src/engine.c:1" and
          ms[-2].get("read") == "cat", ms[-2:])
    check("#517 the user's and a plain reply's mentions: read null",
          [m.get("read", "absent") for m in ms
           if m["who"] in ("user", "assistant")] == [None, None], got)
    check("#517 a command that only writes (cat its argument): read null",
          ms and ms[-1]["line"] == "cat src/engine.c:1" and
          ms[-1]["tool"] == "Bash" and ms[-1].get("read", "absent") is None,
          ms[-1:])
    check("#517 \"read\" is the last key, the keys before unchanged",
          ms and all(list(m)[-1] == "read" and list(m)[-2] == "project"
                     for m in ms), ms[:1])
    v = Pty([DCH, "--xray", "xv-rd"], 30, 100)
    try:
        v.wait(b"Refs ")
        v.pump(0.3)
        v.key(b"\033[Z\033[Z", 1.0)
        v.key(b"a", 1.0)  # Refs shows files: src/engine.c, its one row
        mark = len(v.out)
        v.key(b"\r", 1.0)
        v.wait(b"Mentions: this session", start=mark)
        until(lambda: (v.pump(0.1), "searching" not in screen(v, mark))[1], 10)
        v.pump(0.3)
        s = screen(v, mark)
        check("#517 card: a read mention's row says read, a said one You "
              "or Reply",
              re.search(r"read +see src/engine\.c:1", s) and
              re.search(r"Reply +src/engine\.c holds", s) and
              re.search(r"You +where is src/engine\.c", s), s)
        v.key(b"q")
    finally:
        v.close()


# ---- #534: a lead-in line that names the ref and ends in ':' --------------

LSID = "11111111-2222-4333-8444-5555555555c9"
LEADS = ["Decision for you (OQ-184):  \nShip the lead-in rule?\n- a) yes\n"
         "- b) no\n\nnot the passage",
         "Notes on OQ-186:\n\nOn OQ-189:\nits body",
         "# OQ-187 Heading Title\nthe heading's body\n\n**Lead (OQ-187):**"
         "\nthe lead's body\n\n__Also OQ-190:__ \nits own body",
         "```\nDecision (OQ-188):\nin a fence\n```\nOutside (OQ-191):\n"
         "after it",
         "# OQ-306 Strong First\nstrong body",
         "Lead (OQ-302):\nweak body", "# OQ-302 Real Title\nreal body",
         "Next on OQ-187:\nanother result's body"]
# the operator's shape (Prosper #1641): in the agent's own reply
REPLIES = [("assistant", "Decision for you (OQ-185):\n- a) ship it\n- b) wait"
            "\n- c) drop it\n\nTell me which."),
           ("user", "Re OQ-193:\nplease look"),
           ("assistant", "Status of OQ-187:\nthe reply's body\n\n"
            "**OQ-194 next:**\nits reply body"),
           ("assistant", "Changes in #93:\n- fixed a\n- fixed b"),
           ("assistant", "> Quoted (OQ-317):\n> quoted body"),
           ("assistant", "1. Step (OQ-318):\n   indented body"),
           ("assistant", "2) Next (OQ-319):\nbody"),
           ("assistant", "- Bullet (OQ-320):\nbody"),
           ("assistant", "Decision A (OQ-301):\nbody one"),
           ("assistant", "Decision B (OQ-301):\nbody two"),
           ("assistant", "Later lead (OQ-306):\nweak body"),
           ("assistant", "Two refs OQ-307 and OQ-308:\nbody"),
           ("assistant", "x" * 100 + " (OQ-310):\nbody"),
           ("assistant", "y" * 120 + " (OQ-311):\nbody"),
           ("assistant", "Eight lines (OQ-312):\n" +
            "\n".join("l%d" % i for i in range(8))),
           ("assistant", "Head stop (OQ-313):\nkept\n## Next\nnot kept"),
           ("assistant", "First (OQ-330):\nbody a\nSecond (OQ-331):\nbody b"),
           ("assistant", "Fenced (OQ-332):\nbefore\n```\ncode line\n```\n"
            "after")]


def test_lead_in():
    """xv-ld, an ended Claude whose cat calls each print LEADS's text, then
    REPLIES"""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    lines = [{"type": "user", "cwd": P, "timestamp": "2026-11-01T10:00:00Z",
              "message": {"content": "go"}}]
    for k, out in enumerate(LEADS, 1):
        ts = "2026-11-01T10:0%d:00Z" % k
        lines.append({"type": "assistant", "timestamp": ts,
                      "message": {"content": [{"type": "tool_use",
                                               "id": "l%d" % k,
                                               "name": "Bash", "input": {
                                                   "command": "cat oq.md"}}]}})
        lines.append({"type": "user", "timestamp": ts,
                      "toolUseResult": {"stdout": out}, "message": {
                          "content": [{"type": "tool_result",
                                       "tool_use_id": "l%d" % k,
                                       "content": out}]}})
    for k, (who, text) in enumerate(REPLIES, 10):
        lines.append({"type": who, "timestamp": "2026-11-01T10:%02d:00Z" % k,
                      "message": {"content": [{"type": "text",
                                               "text": text}]}})
    lines.append({"type": "assistant", "timestamp": "2026-11-01T10:40:00Z",
                  "message": {"content": "Str form (OQ-333):\nstring body"}})
    with open(os.path.join(pdir, LSID + ".jsonl"), "w") as f:
        f.write("".join(json.dumps(o) + "\n" for o in lines))
    sockdir = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    with open(os.path.join(hdir, "xv-ld.json"), "w") as f:
        f.write(json.dumps({"name": "xv-ld", "cwd": P, "end": "exited",
                            "argv": ["claude"], "started": 1794299400,
                            "updated": 1794300000, "ended": 1794300000,
                            "sock_dir": sockdir,
                            "agent": {"session_id": LSID, "cwd": P,
                                      "config_dir": cfg}}) + "\n")
    config(None)

    def defs(r):
        p = dch("--xray", "xv-ld", "--ref", r, "--json")
        return [json.loads(l)["definition"] for l in p.stdout.splitlines()
                if '"definition"' in l], p.stdout[-600:]

    d, out = defs("OQ-184")
    check("#534 \"Decision for you (OQ-184):\" defines OQ-184: rule D5, the "
          "line its title, the lines after it to the blank its passage",
          [(x["rule"], x["title"], x["passage"]) for x in d] ==
          [("D5", "Decision for you (OQ-184)",
            "Ship the lead-in rule?\n- a) yes\n- b) no")], (d, out))
    d, out = defs("OQ-185")
    e, _ = defs("OQ-193")
    check("#534 an assistant reply's \"Decision for you (OQ-185):\" and 3 "
          "option lines define OQ-185 (a user's lead-in does not)",
          [(x["rule"], x["title"], x["passage"]) for x in d] ==
          [("D5", "Decision for you (OQ-185)",
            "- a) ship it\n- b) wait\n- c) drop it")] and not e,
          (d, e, out))
    d, out = defs("OQ-186")
    e, _ = defs("OQ-189")
    check("#534 a ':' line with no body after it does not define (the "
          "lead-in after the blank does)", not d and
          [(x["rule"], x["passage"]) for x in e] == [("D5", "its body")],
          (d, e, out))
    d, out = defs("OQ-187")
    e, _ = defs("OQ-190")
    check("#534 a D1 heading for the same ref wins over a lead-in (another "
          "ref's lead-in, '__' and a blank after its ':', still defines)",
          [(x["rule"], x["title"]) for x in d] ==
          [("D1", "Heading Title")] and
          [(x["rule"], x["title"]) for x in e] == [("D5", "Also OQ-190")],
          (d, e, out))
    p = dch("--xray", "xv-ld", "--json")
    try:
        rs = {r["key"].split(":", 1)[-1]: r for r in
              json.loads(p.stdout.splitlines()[0])["refs"]}
    except (ValueError, KeyError, IndexError):
        rs = {}
    t = {k: (rs.get(k) or {}).get("title") for k in ("OQ-187", "OQ-194")}
    check("#534 a D1 heading in one tool result: --ref lists only D1's "
          "definition over lead-ins in another result and in a newer reply, "
          "and the refs title is D1's (a reply's own lead-in titles its ref)",
          [x["rule"] for x in defs("OQ-187")[0]] == ["D1"] and
          t == {"OQ-187": "Heading Title", "OQ-194": "OQ-194 next"},
          (t, p.stdout[:600]))
    d, out = defs("OQ-188")
    e, _ = defs("OQ-191")
    check("#534 a lead-in inside a fence does not define (one after the "
          "fence does)", not d and
          [(x["rule"], x["passage"]) for x in e] == [("D5", "after it")],
          (d, e, out))
    d, out = defs("#93")
    check("#560 \"Changes in #93:\" with a body gives no D5 (an issue has "
          "its own title); OQ-194's lead-in still does",
          not [x for x in d if x["rule"] == "D5"] and
          [x["rule"] for x in defs("OQ-194")[0]] == ["D5"], (d, out))


    check("#562 a leading quote, ordered-list or bullet marker is not the "
          "title", [[x["title"] for x in defs(r)[0]] for r in
                    ("OQ-317", "OQ-318", "OQ-319", "OQ-320")] ==
          [["Quoted (OQ-317)"], ["Step (OQ-318)"], ["Next (OQ-319)"],
           ["Bullet (OQ-320)"]], defs("OQ-317"))

    d, out = defs("OQ-301")
    check("#563 two lead-ins for one ref: the card keeps the first one",
          [(x["rule"], x["title"]) for x in d] ==
          [("D5", "Decision A (OQ-301)")], (d, out))
    d, out = defs("OQ-307")
    e, _ = defs("OQ-308")
    check("#563 a line naming two refs does not define either",
          not [x for x in d + e if x["rule"] == "D5"], (d, e, out))
    d, out = defs("OQ-310")
    e, _ = defs("OQ-311")
    check("#563 a 112-byte lead-in line defines, a 132-byte one does not",
          [x["rule"] for x in d] == ["D5"] and
          not [x for x in e if x["rule"] == "D5"], (d, e, out))
    d, out = defs("OQ-312")
    check("#563 the passage stops at 6 lines",
          [x["passage"] for x in d] == ["l0\nl1\nl2\nl3\nl4\nl5"], (d, out))
    d, out = defs("OQ-313")
    check("#563 the passage stops at a heading",
          [x["passage"] for x in d] == ["kept"], (d, out))
    d, out = defs("OQ-330")
    e, _ = defs("OQ-331")
    check("#563 the passage stops at the next lead-in, which defines its "
          "own ref", [(x["rule"], x["passage"]) for x in d] ==
          [("D5", "body a")] and
          [(x["rule"], x["passage"]) for x in e] == [("D5", "body b")],
          (d, e, out))
    d, out = defs("OQ-332")
    check("#563 fence lines are skipped in the passage",
          [x["passage"] for x in d] == ["before\nafter"], (d, out))
    d, out = defs("OQ-333")
    check("#563 a reply whose content is a string defines",
          [(x["rule"], x["title"]) for x in d] ==
          [("D5", "Str form (OQ-333)")], (d, out))
    p = dch("--xray", "xv-ld", "--json")
    try:
        rs = {r["key"].split(":", 1)[-1]: r for r in
              json.loads(p.stdout.splitlines()[0])["refs"]}
    except (ValueError, KeyError, IndexError):
        rs = {}
    t = {k: ((rs.get(k) or {}).get("title"),
             (rs.get(k) or {}).get("definitions")) for k in
         ("OQ-302", "OQ-306")}
    check("#563 refs: a lead-in and a D1 heading for one ref, in either "
          "order, give one definition, D1's",
          t == {"OQ-302": ("Real Title", 1), "OQ-306": ("Strong First", 1)},
          (t, p.stdout[:600]))


def test_lead_cmd():
    """#559: xv-lc, an ended Claude whose reply has a lead-in for OQ-400;
    the define_command outranks it, and with no answer it stays"""
    record("xv-lc", "11111111-2222-4333-8444-5555555555ca", [
        {"type": "user", "cwd": P, "timestamp": "2026-11-01T10:00:00Z",
         "message": {"content": "go"}},
        {"type": "assistant", "timestamp": "2026-11-01T10:01:00Z",
         "message": {"content": [{"type": "text", "text":
                                  "Decision for you (OQ-400):\n- a) yes"
                                  "\n- b) no"}]}}])

    def ask():
        p = dch("--xray", "xv-lc", "--ref", "OQ-400", "--json")
        return [json.loads(l)["definition"] for l in p.stdout.splitlines()
                if '"definition"' in l], p.stdout[-400:]

    def row():
        v = Pty([DCH, "--xray", "xv-lc"], 30, 100)
        v.wait(b"Refs 1")
        v.pump(0.3)
        v.key(b"\033[Z\033[Z", 1.0)  # Refs; OQ-400 its only row
        mark = len(v.out)
        v.key(b"\r", 1.0)
        v.wait(b"Mentions: this session", start=mark)
        until(lambda: (v.pump(0.1), "searching" not in screen(v, mark))[1],
              10)
        v.pump(0.3)
        s = screen(v, mark)
        v.key(b"q")
        v.close()
        return s

    config(script("lead_def.py", "import json, sys\nprint(json.dumps("
                  "{'title': 'From the command ' + sys.argv[-1]}))\n"))
    d, out = ask()
    check("#559 --ref: a define_command that defines the ref beats its "
          "lead-in", [(x["rule"], x["title"]) for x in d] ==
          [("command", "From the command OQ-400")], (d, out))
    s = row()
    check("#559 Refs row's card: the command's definition, not the lead-in",
          re.search(r"Definition\n From the command OQ-400 (·|-) your "
                    r"define_command\n", s), s)
    config(script("lead_none.py", "pass\n"))
    d, out = ask()
    check("#559 --ref: a define_command that gives nothing leaves the "
          "lead-in", [(x["rule"], x["title"]) for x in d] ==
          [("D5", "Decision for you (OQ-400)")], (d, out))
    s = row()
    check("#559 Refs row's card: no answer from the command, the lead-in "
          "stays", re.search(r"Definition\n D5 Decision for you \(OQ-400\)",
                             s) and "your define_command" not in s, s)

    def slow():
        """The card under a 2 s define_command: the screen once the
        lead-in shows (or after 1 s), how long that took, the end screen"""
        v = Pty([DCH, "--xray", "xv-lc"], 30, 100)
        v.wait(b"Refs 1")
        v.pump(0.3)
        v.key(b"\033[Z\033[Z", 1.0)
        mark = len(v.out)
        t0 = time.time()
        v.key(b"\r", 0)
        until(lambda: (v.pump(0.05), "D5 Decision for you (OQ-400)" in
                       screen(v, mark))[1], 1.0)
        dt, s0 = time.time() - t0, screen(v, mark)
        until(lambda: (v.pump(0.1), "searching" not in screen(v, mark))[1],
              10)
        v.pump(0.3)
        s = screen(v, mark)
        v.key(b"q")
        v.close()
        return dt, s0, s

    config(script("lead_slow.py", "import json, sys, time\ntime.sleep(2)\n"
                  "print(json.dumps({'title': 'Slow answer ' + "
                  "sys.argv[-1]}))\n"))
    dt, s0, s = slow()
    check("#575 Refs row's card: a slow define_command, the lone lead-in "
          "shows at once", dt < 1.0 and
          re.search(r"Definition\n D5 Decision for you \(OQ-400\)", s0),
          (dt, s0))
    check("#575 Refs row's card: the slow command's answer then replaces "
          "the lead-in", re.search(r"Definition\n Slow answer OQ-400 (·|-) "
                                   r"your define_command\n", s) and
          "D5 Decision for you" not in s, s)
    p = dch("--xray", "xv-lc", "--ref", "OQ-400", "--json")
    ls = [json.loads(l) for l in p.stdout.splitlines()]
    check("#575 --ref: a lone lead-in goes out first as a lead line, the "
          "slow command's answer stays the only definition",
          [k for l in ls for k in l if k in ("lead", "definition")] ==
          ["lead", "definition"] and
          [l["lead"]["rule"] for l in ls if "lead" in l] == ["D5"] and
          [l["definition"]["rule"] for l in ls if "definition" in l] ==
          ["command"], p.stdout[-600:])
    config(script("lead_slow_none.py", "import time\ntime.sleep(2)\n"))
    dt, s0, s = slow()
    check("#575 Refs row's card: a slow command that answers nothing, the "
          "lead-in shows at once and stays", dt < 1.0 and
          re.search(r"Definition\n D5 Decision for you \(OQ-400\)", s) and
          "your define_command" not in s and "2 definitions" not in s,
          (dt, s))
    t0 = time.time()
    p = dch("--xray", "xv-ld", "--ref", "OQ-302", "--json")
    dt = time.time() - t0
    check("#575 --ref: a ref with a D1 is unchanged under a slow command: "
          "no lead line, no wait", dt < 1.0 and '"lead"' not in p.stdout and
          [json.loads(l)["definition"]["rule"] for l in p.stdout.splitlines()
           if '"definition"' in l] == ["D1"], (dt, p.stdout[-600:]))
    config(None)


# #531: only a reader's own output is read: not a write, a filter, a list
READS = [("cat > out.md <<'EOF'\nsrc/engine.c:1\nEOF", None),
         ("make 2>&1 | tail -30", None), ("npm test | grep -i fail", None),
         ("cat a.md > b.md && echo done", None),
         ("sed -n -i 's/a/b/p' src/engine.c", None),
         ("sed -i 's/a/b/' src/engine.c", None),
         ("sed 's/a/b/' src/engine.c", None),
         ("cat NOTES", "cat"), ("cat NOTES | head", "cat"),
         ("sed -n 1,5p NOTES", "sed"), ("sed --quiet 1,5p NOTES", "sed"),
         ("sed --silent 1,5p NOTES", "sed"),
         ("bash -lc 'cat NOTES'", "cat"), ("rg -n engine", "rg"),
         ("grep -n engine", None),
         ("cat NOTES | python3 run.py", None),
         ("cat NOTES | psql -f -", None), ("cat NOTES | node x.js", None),
         ("cat NOTES | grep x | sort", "cat"),
         ("cat NOTES; cat NOTES", "cat"), ("cat NOTES\ncat NOTES", "cat"),
         ("set -e; cat NOTES", "cat"), ("true && cat NOTES", "cat"),
         ("source x && cat NOTES", "cat"), (". x; export A=1; cat NOTES", "cat"),
         ("cat NOTES; make", None), ("cat NOTES; cat a > b", None),
         # NIT-UV3: stdin-only readers, a redirect's target, a second command
         ("head -n 5", None), ("sed -n --in-place=.bak 1p NOTES", None),
         ("grep TODO 2>&1", None), ("grep TODO 2>/dev/null", None),
         ("cat <<EOF\nx\nEOF", None), ("cat < NOTES", None),
         ("make; cat NOTES", None),
         ("cat NOTES 1>out", None), ("cat NOTES &>log", None),
         ("cat NOTES >| out", None), ("sed -n 1p NOTES > out", None),
         ("bash -c 'cat NOTES' > out", None), ("bash -c 'cat NOTES > g'", None),
         ("cat NOTES 2>/dev/null", "cat")]


def test_read_shell():
    """xv-rs, an ended Claude whose Bash calls each print src/engine.c:K
    (K the call's), their commands READS's"""
    sid = "11111111-2222-4333-8444-5555555555c8"
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    lines = [{"type": "user", "cwd": P, "timestamp": "2026-11-01T10:00:00Z",
              "message": {"content": "go"}}]
    for k, (cmd, _) in enumerate(READS):
        ts = "2026-11-01T10:%02d:00Z" % (k + 1)
        out = "k%d src/engine.c:1\n" % k
        lines.append({"type": "assistant", "timestamp": ts,
                      "message": {"content": [{"type": "tool_use",
                                               "id": "s%d" % k, "name": "Bash",
                                               "input": {"command": cmd}}]}})
        lines.append({"type": "user", "timestamp": ts,
                      "toolUseResult": {"stdout": out}, "message": {
                          "content": [{"type": "tool_result",
                                       "tool_use_id": "s%d" % k,
                                       "content": out}]}})
    with open(os.path.join(pdir, sid + ".jsonl"), "w") as f:
        for o in lines:
            f.write(json.dumps(o) + "\n")
    sockdir = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    rec = {"name": "xv-rs", "cwd": P, "end": "exited", "argv": ["claude"],
           "started": 1794299400, "updated": 1794300000,
           "ended": 1794300000, "sock_dir": sockdir,
           "agent": {"session_id": sid, "cwd": P, "config_dir": cfg}}
    with open(os.path.join(hdir, "xv-rs.json"), "w") as f:
        f.write(json.dumps(rec) + "\n")
    p = dch("--xray", "xv-rs", "--ref", "src/engine.c", "--json")
    rd = {}
    for l in p.stdout.splitlines():
        if '"mention"' in l:
            m = json.loads(l)["mention"]
            rd[m["line"]] = m.get("read", "absent")
    for k, (cmd, want) in enumerate(READS):
        check("#531 %r: read %s" % (cmd, json.dumps(want)),
              rd.get("k%d src/engine.c:1" % k, "absent") == want,
              (rd, p.stderr[-300:]))



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
            "chain\U0001F469" + "\u200d\U0001F469" * 39,
            "alias " + "\U0001F469" + "\u200d\U0001F469" * 19,
            # 12 cells in a space of 11 (S < W < 2S): the VY1 blank
            "alias " + "\U0001F469" + "\u200d\U0001F469" * 5]):
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
            if k in (8, 9, 10, 11):
                # #434 VY1: the drawn cells reach the edge (a wide
                # character that does not fit may leave one blank)
                sp = 41 - 2 - 2 - cells("open \u00b7 your define_command")
                dw = cells(t.strip()) if r else 0
                check("#434 VY1: row %d: drawn %d cells of %d" % (k, dw, sp),
                      sp - 1 <= dw <= sp, r)
            v.key(b"q")
        finally:
            v.close()


if __name__ == "__main__":
    main()
