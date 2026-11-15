#!/usr/bin/env python3
"""The `dch --xray <n>` view, part 1 of 4 (#365): the
tabs and card of an ended Claude, the reader, the hostile corpus, Refs,
look-up, card order, temp-dir refs; Memory read again on a live
session (its 30 s timer: here, the shortest part).

The harness, the fixtures and the list of what the four parts cover
are in xray_view_lib.py.

Run:  python3 tests/xray_view_test.py      (uses ./dch)
"""
from xray_view_lib import *  # noqa: F401,F403 (harness, fixtures, T)


def test_refusals():
    r = dch("--xray", "xv-cl")
    check("no tty: refused with exit 2 and a clear message",
          r.returncode == 2 and r.stdout == "" and r.stderr ==
          "dch: the --xray view needs a terminal (--json prints the "
          "object)\n", (r.returncode, r.stdout, r.stderr))
    for a in (["--scope", "all"], ["--full"], ["--budget-ms", "5"]):
        v = Pty([DCH, "--xray", "xv-cl"] + a)
        try:
            rc = v.exit()
            check("%s with no --json and no --ref on a terminal: exit 2, "
                  "usage, no view" % a[0], rc == 2 and
                  b"--scope, --full and --budget-ms come with --ref" in v.out
                  and b"\033[?1049h" not in v.out, (rc, v.out))
        finally:
            v.close()
    v = Pty([DCH, "--xray", "nosuch"])
    try:
        rc = v.exit()
        i = v.out.find(b"dch: no such session")
        check("no such session: exit 1, the message after the terminal is "
              "restored", rc == 1 and i > 0 and
              v.out.rfind(RESTORE, 0, i) >= 0, (rc, v.out))
    finally:
        v.close()


def test_view():
    before = files(T)
    v = Pty([DCH, "--xray", "xv-cl"])
    try:
        ok = v.wait(b"Prompts 2") and v.wait(b"Refs 3")
        check("open: the tabs carry the index's counts", ok, v.out[-600:])
        check("#295: a Claude session's first tab is Prompts, the word "
              "Chapters gone", ok and b"Chapters" not in v.out, v.out[-600:])
        check("open: the chapter list shows both titles",
              v.wait(b"second prompt about PROJ-5") and
              v.wait(b"look at #831"), v.out[-600:])
        check("mutant 11: the chapter title's OSC, BEL, U+202E and U+061C "
              "show as ?", v.wait(b"o/r#7 ?]0;pwned? evil?title al?m"),
              v.out[-600:])
        mark = len(v.out)
        v.key(b"\033[Z\033[Z", 1.0)  # back to Search, then Refs (#291)
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        at = [text.find(x) for x in ("PROJ-5", "#831", "o/r#7")]
        check("Refs: the latest mentioned first (#262)",
              -1 not in at and at == sorted(at), (at, text))
        v.key(b"\033[B", 0.3)
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("card: #831 opens light with its definition and counts",
              v.wait(b"light", start=mark) and
              v.wait(b"Definition", start=mark) and
              v.wait(b"Mentions: this session ", start=mark),
              v.out[mark:])
        check("mutant 11: the definition title's SGR shows as ?",
              v.wait(b"Canary ?[31mred?[0m passage", start=mark),
              v.out[mark:])
        check("mutant 11: a mention line's U+2028 and U+061C show as ?",
              v.wait(b"See #831 again?and? PROJ-5", start=mark),
              v.out[mark:])
        mark = len(v.out)
        v.key(b"f", 1.0)
        check("card: f runs the full scan", v.wait(b"full scan", start=mark),
              v.out[mark:])
        v.key(b"y")
        check("card: y copies the ref (OSC 52)",
              v.wait(b"\033]52;c;" + base64.b64encode(b"#831")),
              v.out[-300:])
        # #227: p is the scope toggle (o until 1.60.x); o opens, as in the list
        mark = len(v.out)
        v.key(b"p", 1.0)
        ok = until(lambda: (v.pump(0.1), re.search(
            r"full scan (\u00b7|-) other projects", screen(v, mark)) and
            "p this project" in screen(v, mark))[1], 5)
        check("card: p scans other projects, its key then says this project",
              ok, screen(v, mark))
        mark = len(v.out)
        v.key(b"p", 1.0)
        ok = until(lambda: (v.pump(0.1), "p other projects" in
                            screen(v, mark))[1], 5)
        text = screen(v, mark)
        check("card: p again scans this project only",
              ok and "full scan" in text and not re.search(
                  r"full scan (\u00b7|-) other projects", text), text[:800])
        mark = len(v.out)
        v.key(b"o", 1.0, full=True)
        text = screen(v, mark)
        check("card: o on the ended xv-cl says so and stays on the card",
              "xv-cl has ended: no session to open" in text and
              v.reap() is None, text)
        mark = len(v.out)
        v.key(b"\033", 0.5)
        v.key(b"\t\t")  # Refs, Search, then Prompts: Tab wraps (#291)
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("reader: Enter opens the selected chapter (the newest)",
              v.wait(b"You", start=mark) and v.wait(b"noted", start=mark),
              v.out[mark:])
        mark = len(v.out)
        v.key(b"q")
        check("reader: q goes back to the list",
              v.wait(b"Prompts 2", start=mark), v.out[mark:])
        v.key(b"q")
        rc = v.exit()
        check("quit: q exits 0 and leaves the alternate screen",
              rc == 0 and RESTORE in v.out[-60:], (rc, v.out[-80:]))
        bad = [b for b in (b"\033]0;pwned", b"\007 evil", b"\xe2\x80\xae",
                           b"\033[31mred", b"\xd8\x9c", b"\xe2\x80\xa8")
               if b in v.out]
        check("mutant 11: no corpus ESC, BEL or bidi byte in the output",
              not bad, bad)
    finally:
        v.close()
    after = files(T)
    check("nothing written: no file under the test root changed",
          before == after, set(before.items()) ^ set(after.items()))


def test_refs():
    o = json.loads(dch("--xray", "xv-rf", "--json").stdout)
    keys = [x["key"] for x in o["refs"]]
    check("refs fixture: the engine lists every ref (13)",
          len(keys) == 13 and "file:" + LONG in keys and
          "file:" + WIDE in keys, keys)
    v = Pty([DCH, "--xray", "xv-rf"], env=RENV)
    try:
        v.wait(b"Prompts 1")
        v.pump(0.5)
        check("#294: two tabs, Prompts and Refs, no On screen",
              b"Refs 13" in v.out and b"On screen" not in v.out,
              v.out[-600:])
        mark = len(v.out)
        v.key(b"\033[Z\033[Z", 1.0)
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        check("Refs: the tab count stays the engine's total (13)",
              "Refs 13" in text, text)
        check("Refs #294: an ended session has no screen, no group line",
              "on screen now" not in text and "the rest" not in text, text)
        check("Refs #262: opens on pulls, issues and tickets only; the "
              "footer names a and counts the other kinds",
              "o/r#7" in text and "alpha_handler" not in text and
              "example.com" not in text and
              "a files, commits, URLs 12 hidden" in text, text)
        mark = len(v.out)
        v.key(b"a", 1.0)
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        check("Refs: one faint line says what a ref is and that Enter "
              "opens its card", re.search("Every ref this session touched, "
                                          "latest first; (\u21b5|enter) "
                                          "opens a card", text), text)
        check("Refs #262: a shows the other kinds, the footer says a hides "
              "them", "alpha_handler" in text and
              "a hide files, commits, URLs" in text, text)
        check("Refs: a file under the project shows relative to it",
              "file    src/very/deep/module/alpha_handler.c" in text and
              "file    src/main.c" in text and P + "/src" not in text, text)
        check("Refs: a file under HOME starts with ~",
              "file    ~/notes/todo.md" in text, text)
        check("Refs: a path too long keeps its tail, the ellipsis in front",
              re.search(ELL + "[^\n]*/for/the/view/test/zeta_handler\\.c",
                        text) and "src/very/deep/module/with" not in text,
              text)
        order = ["alpha_handler.c", "README.md", "src/main.c", "o/r#7",
                 "example.com", "~/notes/todo.md", "zeta_handler.c",
                 "beta_handler.c"]
        at = [text.find(x) for x in order]
        check("Refs: one line, so one latest mention: by count, then the most "
              "recent first (later in the snapshot)", -1 not in at and at == sorted(at), (at, text))
        tmp = ["/tmp/scratch-one.txt", "/private/tmp/demo-502/tasks/out.log",
               "/var/folders/xx/abc/T/t.1", "/home/demo/tmpx/note.txt",
               "~/tmpx/note.txt"]
        check("Refs: files under /tmp, /private/tmp, /var/folders and "
              "$TMPDIR are hidden", not any(x in text for x in tmp), text)
        check("Refs: the footer shows t and how many are hidden",
              "t temp files 4 hidden" in text, text)
        mark = len(v.out)
        v.key(b"t", 1.0)
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        check("Refs: t shows the temp files, the footer says t hides them",
              all(x in text for x in tmp[:3]) and "t hide temp files" in text
              and "hidden" not in text, text)
        mark = len(v.out)
        v.key(b"t", 1.0)
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        check("Refs: t again hides them", "t temp files 4 hidden" in text and
              "/tmp/scratch-one.txt" not in text, text)
        mark = len(v.out)
        v.key(b"/")
        v.key(b"scratch-one", 1.0)
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        check("Refs: the filter takes t as text; only a hidden row matches: "
              "said", "/scratch-one" in text and
              "Only temp files: t shows them" in text, text)
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("Refs: the filter done, the footer counts the hidden match",
              v.wait(b"t temp files 1 hidden", start=mark), v.out[mark:])
        mark = len(v.out)
        v.key(b"t", 1.0)
        v.key(b"\r", 1.0)
        check("Refs: Enter on the shown temp row opens its card",
              v.wait(b"/tmp/scratch-one.txt  light", start=mark),
              v.out[mark:])
        mark = len(v.out)
        v.key(b"f")
        check("card: f shows the resolver's line (QA 1.56.0 NIT-V2)",
              v.wait(b"file Outside this session's project  "
                     b"/tmp/scratch-one.txt", start=mark), v.out[mark:])
        v.key(b"\033", 0.5)
        v.key(b"\033", 0.5)
        v.key(b"t", 0.5)
        mark = len(v.out)
        v.key(b"/")
        v.key(P.encode() + b"/src/main", 1.0)
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        check("Refs: the filter matches the full key, the row shows it "
              "relative", "file    src/main.c" in text and
              "alpha_handler" not in text, text)
        mark = len(v.out)
        v.key(b"\r", 1.0)  # done, and Enter on the row, as the picker's
        check("Refs: Enter opens the card of the full key",
              v.wait(P.encode() + b"/src/main.c  light", start=mark),
              v.out[mark:])
        v.key(b"\033", 0.5)
        v.key(b"\033", 0.5)
        mark = len(v.out)
        v.resize(30, 60)
        v.wait(b"#diff-0123456789abcdef", start=mark)
        v.pump(0.3)
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        check("Refs, 60 columns: a long URL keeps its host and its end",
              re.search("url     example\\.com" + ELL +
                        "[^\n]*#diff-0123456789abcdef", text) and
              "https://" not in text, text)
        wide = [r.rstrip() for r in scr.values()
                if r.rstrip().endswith(WIDE[-7:])]
        check("Refs, 60 columns: a wide-character path keeps its tail within "
              "the width", wide and re.search(ELL, wide[0]) and
              cells(wide[0]) <= 60 and "docs/" not in wide[0],
              (wide, [cells(r) for r in wide]))
        v.key(b"q")
        check("Refs: q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()


def test_latest():
    v = Pty([DCH, "--xray", "xv-lt"], env=RENV)
    try:
        v.wait(b"Prompts 1")
        v.pump(0.5)
        mark = len(v.out)
        v.key(b"\033[Z\033[Z", 1.0)
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        at = [text.find(x) for x in ("#77", "PROJ-9", "o/r#1")]
        check("Refs #262: the latest mentioned on top, not the most counted",
              -1 not in at and at == sorted(at), (at, text))
        check("Refs #262: commits and files hidden, the footer counts them",
              "deadbee" not in text and "old.c" not in text and
              "a files, commits, URLs 3 hidden" in text, text)
        mark = len(v.out)
        v.key(b"a", 1.0)
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        at = [text.find(x) for x in ("new.c", "PROJ-9", "old.c")]
        check("Refs #262: a shows them, in the same latest-first order",
              "deadbee" in text and -1 not in at and at == sorted(at),
              (at, text))
        mark = len(v.out)
        v.key(b"a", 1.0)
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        check("Refs #262: a again hides them",
              "deadbee" not in text and
              "a files, commits, URLs 3 hidden" in text, text)
        v.key(b"q")
        check("Refs #262: q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()


def test_lookup():
    """#263: a ref typed in the filter, matched by no row or not by the one
    wanted, opens its card with ^R; plain words say ^R takes refs only."""
    v = Pty([DCH, "--xray", "xv-lt"], env=RENV)
    try:
        v.wait(b"Prompts 1")
        v.pump(0.5)
        mark = len(v.out)
        v.key(b"/PROJ-9", 1.0)
        text = shown(v, mark)
        check("lookup #263: a ref in the Prompts filter, no title has it: "
              "the empty line and the footer name ^R",
              "No match: ^R looks it up" in text and "^R look up" in text,
              text)
        mark = len(v.out)
        v.key(b"\x12", 1.0)
        check("lookup #263: ^R opens the typed ref's card with its mention",
              v.wait(b"PROJ-9  light", start=mark) and
              v.wait(b"now PROJ-9", start=mark), v.out[mark:])
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("lookup #263: Enter on its mention reads the chapter",
              v.wait(b"the latest fixture", start=mark) and
              v.wait(b"now PROJ-9", start=mark), v.out[mark:])
        v.key(b"\033", 0.5)
        mark = len(v.out)
        v.key(b"\033", 1.0)
        text = shown(v, mark)
        check("lookup #263: Esc back to the list, the filter kept",
              "/PROJ-9" in text and "Prompts 1" in text, text)
        v.key(b"/\x15", 0.3)
        mark = len(v.out)
        v.key(b"just words", 1.0)
        text = shown(v, mark)
        check("lookup #263: plain words matching nothing: one faint line "
              "says ^R searches for them (#291); ^R search, not look up, in "
              "the footer", "No match; ^R searches the session for it" in
              text and "^R search" in text and "^R look up" not in text,
              text)
        mark = len(v.out)
        v.key(b"\x12", 1.0)
        check("lookup #263: ^R on words searches them on Search (#291), no "
              "card", v.wait(b"Search: just words", start=mark) and
              v.wait(b'No match for "just words"', start=mark) and
              b"light" not in v.out[mark:], v.out[mark:])
        v.key(b"\033[Z", 0.5)   # Search back to Refs
        v.key(b"\033", 1.0)     # the filter cleared
        mark = len(v.out)
        v.key(b"/ deadbeef1 ", 1.0)
        text = shown(v, mark)
        check("lookup #263: a full commit hash, its row held short: no row, "
              "^R named", "No match: ^R looks it up" in text and
              "^R look up" in text, text)
        mark = len(v.out)
        v.key(b"\x12", 1.0)
        check("lookup #263: ^R opens the hidden commit's card, outer blanks "
              "cut", v.wait(b"deadbeef1  light", start=mark) and
              v.wait(b"commit deadbeef1", start=mark), v.out[mark:])
        v.key(b"\033", 0.5)
        v.key(b"/\x15", 0.3)
        mark = len(v.out)
        v.key(P.encode() + b"/new.c", 1.0)
        text = shown(v, mark)
        check("lookup #263: a file, its kind hidden: a and ^R both named",
              "Only files, commits and URLs: a shows them" in text and
              "^R look up" in text, text)
        mark = len(v.out)
        v.key(b"\x12", 1.0)
        check("lookup #263: ^R opens a hidden file's card",
              v.wait(P.encode() + b"/new.c  light", start=mark) and
              v.wait(b"commit deadbeef1 in", start=mark), v.out[mark:])
        v.key(b"q", 0.5)
        v.key(b"q")
        v.exit()
    finally:
        v.close()
    v = Pty([DCH, "--xray", "xv-cl"])
    try:
        v.wait(b"Prompts 2")
        v.pump(0.5)
        mark = len(v.out)
        v.key(b"/o/r#7", 1.0)
        text = shown(v, mark)
        check("lookup #263: a ref a chapter title holds: the row stays, the "
              "footer names ^R", "^R look up" in text and
              "No match" not in text, text)
        mark = len(v.out)
        v.key(b"\x12", 1.0)
        check("lookup #263: ^R opens the ref's card, not the row's chapter",
              v.wait(b"o/r#7  light", start=mark), v.out[mark:])
        v.key(b"\033", 0.5)
        v.key(b"q")
        v.exit()
    finally:
        v.close()


def test_order():
    v = order_card("xv-o1", b"\x12")
    try:
        got, _ = card_rows(v)
        check("card #304: a transcript under 1 MiB lists PROJ-9's mentions "
              "newest first", got == ["FOUR", "THREE", "TWO", "ONE"], got)
        mark = len(v.out)
        v.key(b"j\r", 1.0)
        check("card #304: Enter on the second mention reads its prompt",
              v.wait(b"prompt THREE", start=mark) and
              b"prompt FOUR" not in v.out[mark:], v.out[mark:][-600:])
        v.key(b"q")
    finally:
        v.close()
    want = ["FOUR2", "FOUR", "THREE2", "THREE", "TWO2", "TWO", "ONE2", "ONE"]
    v = order_card("xv-o2", b"\x12")
    try:
        got, _ = card_rows(v)
        check("card #304: a transcript of several 1 MiB windows: newest "
              "first, no zigzag", got == want, got)
        v.key(b"jj", 0.5)
        mark = len(v.out)
        v.key(b"f", 0.5)
        v.wait(b"full scan", 20, start=mark)
        settle(v)
        got, sel = card_rows(v)
        # f reads the project: this session's lines first, then the others'
        check("card #304: after f (the engine sends oldest first) still "
              "newest first, the selection on its mention",
              got[:8] == want and sel == ["THREE2"], (got, sel))
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("card #304: Enter on it reads its prompt",
              v.wait(b"prompt THREE", start=mark) and
              b"prompt FOUR" not in v.out[mark:], v.out[mark:][-600:])
        v.key(b"q")
    finally:
        v.close()
    v = order_card("xv-o3", b"\x12")
    try:
        got, _ = card_rows(v)
        check("card #304: 250 mentions in one window: newest first",
              len(got) > 20 and got == sorted(got, reverse=True), got)
        v.key(b"jjjjj", 0.5)
        _, before = card_rows(v)
        mark = len(v.out)
        v.key(b"f", 0.5)
        v.wait(b"full scan", 20, start=mark)
        settle(v)
        got, sel = card_rows(v)
        check("card #304: the selected mention stays selected while f's "
              "lines arrive, newest first", len(before) == 1 and
              sel == before and got == sorted(got, reverse=True),
              (before, sel, got))
        v.key(b"q")
    finally:
        v.close()


def test_tmp():
    v, scr, text = refs_tab("xv-tp")
    try:
        check("Refs #220: a project under $TMPDIR shows its own file, only "
              "the scratch file outside it is hidden",
              "file    src/main.c" in text and "other.txt" not in text and
              "t temp files 1 hidden" in text, text)
        url = [r.rstrip() for r in scr.values() if "url     " in r]
        hit = [r for r in url if ".example/github.com/o/r/pull/1" in r]
        check("Refs #220: a host wider than half the row keeps its start, "
              "then the ellipsis and the URL's end",
              hit and re.search("url     github\\.com\\.a+" + ELL +
                                "a*\\.example/github\\.com/o/r/pull/1$",
                                hit[0]), url)
        big = [r for r in url if "zzzz" in r]
        check("Refs #220: a host wider than the row is cut at its end",
              big and re.search("url     z+" + ELL + "$", big[0]) and
              "/end" not in big[0] and cells(big[0]) <= 100,
              (big, [cells(r) for r in big]))
        idn = [r for r in url if "fin" in r]
        check("Refs #220: a wide-character host keeps its start within the "
              "width", idn and "url     x\u4f8b" in idn[0] and
              re.search(ELL, idn[0]) and cells(idn[0]) <= 100,
              (url, [cells(r) for r in url]))
        v.key(b"q")
    finally:
        v.close()
    v, scr, text = refs_tab("xv-tl")
    try:
        check("Refs #220: a project's file is not scratch by realpath either "
              "(the session's cwd is a symlink to it)",
              re.search(r"file    \S*src/real\.c", text) and
              "t temp files 1 hidden" in text, text)
        v.key(b"q")
    finally:
        v.close()
    v, scr, text = refs_tab("xv-tx")
    try:
        # the engine folds ".." (xray.c, 2.5) before the view sees the text;
        # the view's own ".." rule is behind it
        check("Refs #220: a temp path written with .. is scratch, also one "
              "through the project dir",
              "file    src/main.c" in text and "leak.txt" not in text and
              "esc/x" not in text and "t temp files 2 hidden" in text, text)
        v.key(b"q")
    finally:
        v.close()
    # /usr/bin stands for a project outside every temp dir (QA 1.60.0
    # NIT-T5: a checkout in one would be scratch itself)
    v, scr, text = refs_tab("xv-tr")
    try:
        check("Refs #220: a project outside the temp dirs resolves no row: "
              "a temp-dir symlink into it stays scratch",
              "rlink/env" not in text and
              not re.search(r"file +env\b", text) and
              "t temp files 1 hidden" in text, text)
        v.key(b"q")
    finally:
        v.close()


def test_memory_live():
    """LOW-V28: Memory shown on a running session is read again every 30 s
    with no new transcript line (a memory file changes apart from it)."""
    cwd = os.path.realpath(os.path.join(T, "mlive"))
    os.makedirs(cwd, exist_ok=True)
    proj = os.path.join(HOME, ".claude", "projects", cwd.replace("/", "-"))
    os.makedirs(os.path.join(proj, "memory"), exist_ok=True)
    with open(os.path.join(proj, MLSID + ".jsonl"), "w") as f:
        for o in turn("live memory", "2026-11-10T10:00:00Z", say("ok")):
            f.write(json.dumps(dict(o, cwd=cwd) if "cwd" in o else o) + "\n")
    mem = lambda n: open(os.path.join(proj, "memory", n), "w").write(n)
    mem("one.md")
    dch("--spawn", "xv-ml", "/bin/sleep", "300")
    pid = until(lambda: json.loads(dch("--cmd", "xv-ml").stdout or "{}")
                .get("pid"), 5)
    start = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(pid)],
                           capture_output=True, text=True,
                           env={"LC_ALL": "C", "TZ": "UTC"}).stdout.strip()
    sd = os.path.join(HOME, ".claude", "sessions")
    os.makedirs(sd, exist_ok=True)
    with open(os.path.join(sd, "%d.json" % pid), "w") as f:
        json.dump({"pid": pid, "sessionId": MLSID, "cwd": cwd,
                   "procStart": start, "version": "2.1.0",
                   "kind": "interactive", "status": "idle",
                   "statusUpdatedAt": 1}, f)
    v = Pty([DCH, "--xray", "xv-ml"], rows=30, cols=100)
    try:
        tabbar(v, "Memory ")
        m = re.search(r"Memory (\d+)", rows(v.out).get(2, ""))
        n = int(m.group(1)) if m else -1
        mem_tab(v)
        mem("two.md")
        # XV_MEVERY is 30 s; 120 s bounds a hang on a slow runner, not speed
        ok = tabbar(v, "Memory %d" % (n + 1), 120)
        check("memory live: a new memory file shows while the tab is shown, "
              "no transcript line written (LOW-V28)", n > 0 and ok,
              rows(v.out).get(2))
        v.key(b"q")
        v.exit()
    finally:
        v.close()
        dch("-k", "xv-ml")


def main():
    run_steps(fixture, test_refusals, test_view, fixture_refs, test_refs,
              fixture_latest, test_latest, test_lookup, fixture_order,
              test_order, fixture_tmp, test_tmp, test_memory_live)


if __name__ == "__main__":
    main()
