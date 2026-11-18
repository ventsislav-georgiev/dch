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
        # the redraw can outlast the settle at load: wait for its footer
        until(lambda: (v.pump(0.1), "a hide files, commits, URLs" in
                       screen(v, mark))[1])
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
        until(lambda: (v.pump(0.1), "t hide temp files" in screen(v, mark))[1])
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        check("Refs: t shows the temp files, the footer says t hides them",
              all(x in text for x in tmp[:3]) and "t hide temp files" in text
              and "hidden" not in text, text)
        mark = len(v.out)
        v.key(b"t", 1.0)
        until(lambda: (v.pump(0.1), "t temp files 4 hidden" in screen(v, mark))[1])
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
    # the clock off (the card's light child is xray_main): ASan spent the
    # 300 ms budget on the first window, 2 of 8 mentions, on every retry
    v = order_card("xv-o2", b"\x12", env={"DCH_XRAY_NOCLOCK": "1"})
    try:
        got, _ = card_rows(v)
        # A loaded runner (ASan in CI) can spend the 300 ms budget before the
        # oldest windows: a cut drops only older windows (#377), so a prefix,
        # and the card then says so ("(f for more)", xv_draw_card).
        cut = any("(f for more)" in r for r in rows(v.out).values())
        check("card #304: a transcript of several 1 MiB windows: newest "
              "first, no zigzag; a short list only with the card's cut mark",
              (got == want and not cut) or
              (len(got) >= 4 and got == want[:len(got)] and cut),
              (got, cut))
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


def test_tool_mention():
    """#469: the card's row for a Bash result names its call's tool."""
    v = order_card("xv-o4", b"\x12", term="o/r#77")
    try:
        scr = rows(v.out)
        res = [scr[k] for k in sorted(scr) if "RESULT says" in scr[k]]
        check("card #469: a Bash result's mention row reads Bash, not Tool",
              len(res) == 1 and "Bash" in res[0] and "Tool" not in res[0],
              res)
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
    """LOW-V28: a memory file added on a running session shows with no new
    transcript line (a memory file changes apart from it): the follow
    watches the memory dir (#448); DCH_XRAY_MEVERY lowers the 30 s re-read
    of a host that refuses that stream."""
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
    v = Pty([DCH, "--xray", "xv-ml"], rows=30, cols=100,
            env={"DCH_XRAY_MEVERY": "1500"})	# tests only: not 30 s
    try:
        tabbar(v, "Memory ")
        m = re.search(r"Memory (\d+)", rows(v.out).get(2, ""))
        n = int(m.group(1)) if m else -1
        mem_tab(v)
        mem("two.md")
        # XV_MEVERY is 30 s, 1.5 s here; 15 s bounds a hang, not speed
        ok = tabbar(v, "Memory %d" % (n + 1), 15)
        check("memory live: a new memory file shows while the tab is shown, "
              "no transcript line written (LOW-V28)", n > 0 and ok,
              rows(v.out).get(2))
        v.key(b"q")
        v.exit()
    finally:
        v.close()
        dch("-k", "xv-ml")


RPSIDS = ["11111111-2222-4333-8444-5555555555e%d" % k for k in (1, 2, 3)]
REPLY = ("Done. Touched #412 first, then #377, later #418, and last: "
         "close PAY-219.")


def fixture_reply():
    """xv-rp: the last answer says #412, #377, #418, PAY-219 in that order
    (an older one #901); xv-rq: the last turn ended on a tool call."""
    old = turn("first", "2026-11-10T10:00:00Z", say("see #901 and #412"))
    record("xv-rp", RPSIDS[0], old + turn("second", "2026-11-10T11:00:00Z",
                                          say(REPLY)))
    record("xv-rq", RPSIDS[1], old + turn(
        "ended mid-tool", "2026-11-10T11:00:00Z",
        call("r1", "Read", {"file_path": P + "/x.c"})))


def refs_plain(name):
    v = Pty([DCH, "--xray", name], env={"TMPDIR": TPD + "/"})
    v.wait(b"Prompts 1")
    v.pump(0.5)
    mark = len(v.out)
    v.key(b"\t\t", 1.0)
    return v, shown(v, mark)


def test_reply():
    """#452: Refs opens with an "in the last reply" group, the answer's
    refs by their last place, the last on top; none without an answer."""
    v, text = refs_plain("xv-rp")
    try:
        at = [text.find(x) for x in ("in the last reply", "PAY-219", "#418",
                                     "#377", "#412", "the rest", "#901")]
        check("Refs #452: the last reply's group, PAY-219, #418, #377, #412 "
              "in that order, then the rest", -1 not in at and
              at == sorted(at) and "on screen now" not in text, text)
        v.key(b"q")
    finally:
        v.close()
    v, text = refs_plain("xv-rq")
    try:
        check("Refs #452: no group when the last turn has no answer",
              "in the last reply" not in text and "#412" in text, text)
        v.key(b"q")
    finally:
        v.close()


def test_reply_live():
    """#452: on a live session a ref on the screen is not repeated in the
    last reply's group."""
    cwd = os.path.realpath(os.path.join(T, "rlive"))
    os.makedirs(cwd, exist_ok=True)
    proj = os.path.join(HOME, ".claude", "projects", cwd.replace("/", "-"))
    os.makedirs(proj, exist_ok=True)
    with open(os.path.join(proj, RPSIDS[2] + ".jsonl"), "w") as f:
        for o in turn("live reply", "2026-11-10T10:00:00Z", say(REPLY)):
            f.write(json.dumps(dict(o, cwd=cwd) if "cwd" in o else o) + "\n")
    dch("--spawn", "xv-rl", "/usr/bin/perl", "-e",
        '$|=1; print "on screen: #412 and PAY-219\\n"; sleep 300')
    pid = until(lambda: json.loads(dch("--cmd", "xv-rl").stdout or "{}")
                .get("pid"), 5)
    start = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(pid)],
                           capture_output=True, text=True,
                           env={"LC_ALL": "C", "TZ": "UTC"}).stdout.strip()
    sd = os.path.join(HOME, ".claude", "sessions")
    os.makedirs(sd, exist_ok=True)
    with open(os.path.join(sd, "%d.json" % pid), "w") as f:
        json.dump({"pid": pid, "sessionId": RPSIDS[2], "cwd": cwd,
                   "procStart": start, "version": "2.1.0",
                   "kind": "interactive", "status": "idle",
                   "statusUpdatedAt": 1}, f)
    v = Pty([DCH, "--xray", "xv-rl"], rows=30, cols=100)
    try:
        v.wait(b"Prompts 1")
        v.pump(0.5)
        mark = len(v.out)
        v.key(b"\t\t", 1.0)
        ok = until(lambda: (v.pump(0.2), "in the last reply" in
                            shown(v, mark))[1], 10)
        text = shown(v, mark)
        lite = dch("--build").stdout.strip() == "lite"   # no mirror
        at = [text.find(x) for x in (
            ("in the last reply", "PAY-219", "#418", "#377", "#412") if lite
            else ("on screen now", "#412", "PAY-219", "in the last reply",
                  "#418", "#377"))]
        check("Refs #452: live: the screen's #412 and PAY-219 in the "
              "on-screen group only, #418 and #377 under the last-reply "
              "line (lite: all four in it)", ok and -1 not in at and at == sorted(at) and
              text.count("#412") == 1 and text.count("PAY-219") == 1, text)
        v.key(b"q")
    finally:
        v.close()
        dch("-k", "xv-rl")


CSIDS = ["11111111-2222-4333-8444-5555555555f%d" % k for k in (7,)]
SAID = "Opened PR #412: idempotency key on checkout, 24h TTL."
# paragraphs before SAID in turn 4's reply: a mention's open scrolls past them
PAD = "Padding alpha.\n\n" + "".join("Padding %d.\n\n" % k for k in range(30))


def fixture_said():
    """xv-sd: five prompts, alone in its project; #412 first appears in
    turn 4's reply (no definition), and again in a later user prompt."""
    lines = []
    for k in range(3):
        lines += turn("filler %d" % k, "2026-11-10T1%d:00:00Z" % k,
                      at(say("nothing here"), "2026-11-10T1%d:01:00Z" % k))
    lines += turn("ship the fix", "2026-11-10T13:00:00Z",
                  at(say(PAD + SAID), "2026-11-10T13:01:00Z"))
    lines += turn("is #412 merged?", "2026-11-10T14:00:00Z",
                  at(say("Checked it. Yes, #412 is merged now! Done."),
                     "2026-11-10T14:01:00Z"))
    solo = "/home/demo/solo"
    os.makedirs(os.path.dirname(tl("x", "-home-demo-solo")), exist_ok=True)
    record("xv-sd", CSIDS[0], [dict(o, cwd=solo) if "cwd" in o else o
                               for o in lines], "-home-demo-solo", solo)


def test_said():
    """#455: a card with no definition shows its first mention's line as
    First said (prompt N); rows say You/Reply and the prompt; a light card
    says f scans, never 0 in 0 sessions."""
    v = Pty([DCH, "--xray", "xv-sd"], env={"TMPDIR": TPD + "/",
                                           "NO_COLOR": ""})
    try:
        v.wait(b"Prompts 5")
        v.pump(0.5)
        for _ in range(6):
            if "[Refs" in rows(v.out).get(2, ""):
                break
            v.key(b"\t", 0.5)
        mark = len(v.out)
        v.key(b"\r", 1.5)
        v.wait(b"Mentions: this session ", start=mark)
        v.pump(1.0)
        text = screen(v, 0)
        check("#455: no definition: First said (prompt 4, ...) with the "
              "first mention's line", "First said (prompt 4, ship the fix"
              in text and "Opened PR #412: idempotency" in text and
              "No definition found" not in text, text)
        # #478: a header per turn, newest first, then its mentions (newest
        # first) as who + the sentence around the ref
        lines = text.split("\n")
        at_ = [next((i for i, l in enumerate(lines) if re.search(x, l)), -1)
               for x in (r"p5 is #412 merged\? +\d\d-\d\d \d\d:\d\d",
                         r"Reply Yes, #412 is merged now! *$",
                         r"You is #412 merged\? *$",
                         r"p4 ship the fix +\d\d-\d\d",
                         r"Reply Opened PR #412: idempotency key on "
                         r"checkout, 24h TTL\.$")]
        check("#478: the card groups by turn: p5's header, its two "
              "mentions (Reply, You) with their sentences, then p4's",
              -1 not in at_ and at_ == sorted(at_) and
              at_[1] == at_[0] + 1 and at_[3] == at_[2] + 1 and
              at_[4] == at_[3] + 1, (at_, text))
        check("#478: the ref stands out in the sentence (an SGR on each side)",
              re.search(rb"Yes, \033\[0;1[0-9;]*m#412\033\[0[0-9;]*m is merged",
                        v.out) is not None, v.out[-1500:])
        check("#455: a light card says f scans, never 0 in 0 sessions",
              "this project: f scans" in text and
              "0 in 0 sessions" not in text, text)
        # #478: Enter on a turn's header opens the reader at that turn
        v.key(b"jj", 0.5)
        mark = len(v.out)
        v.key(b"\r", 1.0)
        ok = until(lambda: (v.pump(0.1), "ship the fix" in
                            screen(v, mark))[1], 5)
        text = screen(v, mark)
        check("#478: Enter on p4's header opens the reader at turn 4",
              ok and "Padding alpha" in text and "is #412 merged" not in
              text, text)
        # a header opens at the turn's top; a mention scrolls to the ref
        check("#478: Enter on a header opens at the turn's top, not the ref",
              "Padding alpha" in text and "Opened PR #412" not in text, text)
        v.key(b"q", 0.5)
        mark = len(v.out)
        v.key(b"j\r", 1.0)
        ok = until(lambda: (v.pump(0.1), "ship the fix" in
                            screen(v, mark))[1], 5)
        text = screen(v, mark)
        check("#478: Enter on a mention opens at the ref, its turn's top "
              "scrolled off", ok and "Opened PR #412" in text and
              "Padding alpha" not in text, text)
        v.key(b"q", 0.5)
        v.key(b"q")
        v.key(b"q")
        v.exit()
    finally:
        v.close()


NSIDS = ["11111111-2222-4333-8444-5555555555d%d" % k for k in (6, 7, 4)]


def fixture_narrow():
    """xv-nw: one reply with 99 words before #412, alone in its project."""
    solo = "/home/demo/narrow"
    words = " ".join("w%03d" % k for k in range(1, 100))
    os.makedirs(os.path.dirname(tl("x", "-home-demo-narrow")), exist_ok=True)
    record("xv-nw", NSIDS[0], [dict(o, cwd=solo) if "cwd" in o else o
                               for o in turn("go", "2026-11-10T13:00:00Z",
                                  at(say(words + " so #412 holds."),
                                     "2026-11-10T13:01:00Z"))],
           "-home-demo-narrow", solo)
    # CJK with one space before the ref: snapping to it would drop the
    # whole context, so the scalar cut stays (#483's half-room rule)
    solo = "/home/demo/narrowcjk"
    os.makedirs(os.path.dirname(tl("x", "-home-demo-narrowcjk")),
                exist_ok=True)
    record("xv-nwc", NSIDS[1], [dict(o, cwd=solo) if "cwd" in o else o
                                for o in turn("go", "2026-11-10T13:00:00Z",
                                   at(say("\u6f22\u5b57" * 60 + " so #412 holds."),
                                      "2026-11-10T13:01:00Z"))],
           "-home-demo-narrowcjk", solo)
    # decomposed e + U+0301 runs: the row's cut lands on a mark (#488)
    solo = "/home/demo/narrowmark"
    os.makedirs(os.path.dirname(tl("x", "-home-demo-narrowmark")),
                exist_ok=True)
    record("xv-nwm", NSIDS[2], [dict(o, cwd=solo) if "cwd" in o else o
                                for o in turn("go", "2026-11-10T13:00:00Z",
                                   at(say("e\u0301" * 60 + " so #412 holds."),
                                      "2026-11-10T13:01:00Z"))],
           "-home-demo-narrowmark", solo)


def narrow_row(name):
    """The card's Reply row for #412 of session `name` at 50 columns."""
    v = Pty([DCH, "--xray", name], rows=30, cols=50,
            env={"TMPDIR": TPD + "/", "NO_COLOR": ""})
    try:
        v.wait(b"Prompts 1")
        v.pump(0.5)
        for _ in range(6):
            if "[Refs" in rows(v.out).get(2, ""):
                break
            v.key(b"\t", 0.5)
        mark = len(v.out)
        v.key(b"\r", 1.5)
        v.wait(b"Mentions: this session ", start=mark)
        v.pump(1.0)
        text = screen(v, 0)
        v.key(b"q", 0.5)
        v.key(b"q")
        v.exit()
    finally:
        v.close()
    return next((l for l in text.split("\n") if "Reply" in l and
                 "#412" in l), ""), text


def test_narrow_cjk():
    """NIT-UH2 with #483's rule: a CJK row keeps its context."""
    row, text = narrow_row("xv-nwc")
    check("NIT-UH2: a CJK sentence's narrow row keeps CJK before the ref "
          "(no snap to the one space)", re.search(ELL + "\u6f22|" + ELL +
          "\u5b57", row) is not None and "so #412" in row, text)


def test_narrow_mark():
    """#488: a narrow row's cut never leaves a lone combining mark."""
    row, text = narrow_row("xv-nwm")
    m = re.search(ELL + "(.)", row)
    check("#488: the narrow row's first scalar after the ellipsis is not a "
          "combining mark and #412 shows", m is not None and
          not ("\u0300" <= m.group(1) <= "\u036f") and "so #412" in row,
          text)


def test_narrow():
    """NIT-UH2: a mention row cut to 50 columns starts on a whole word."""
    row, text = narrow_row("xv-nw")
    check("NIT-UH2: the narrow row starts on a whole word after the "
          "ellipsis and keeps #412", re.search(ELL + r"w\d\d\d ", row)
          is not None and "so #412" in row, text)


TSIDS = ["11111111-2222-4333-8444-5555555555e%d" % k for k in (8, 9)]
TDEF = "`ERR_LEDGER_SKEW` is raised when the ledger clock drifts past 5 s."
TREPLY = ("Fixed #412. `ERR_LEDGER_SKEW` no longer fires; covered by "
          "`test_retry_timeout`, close PAY-219. "
          "Run `--json` or `-v` with `RETRY_MAX=3`, edit `README.md` and "
          "`main.c`, call `parseN()` or `foo(x)`; see `LedgerClock`.")


def term_lines():
    """Turn 1 defines ERR_LEDGER_SKEW, then says #377 and OLD_FLAG_NAME
    (the rest's group); a tool's output alone says
    TOOL_ONLY_TERM; turn 2's answer: #412, the two terms, PAY-219."""
    return (turn("why does the ledger skew", "2026-11-10T10:00:00Z",
                 call("t1", "Bash", {"command": "make check"}),
                 result("t1", "fail: TOOL_ONLY_TERM in tool_only_case"),
                 at(say(TDEF + " Older: #377 and `OLD_FLAG_NAME`."),
                    "2026-11-10T10:01:00Z")) +
            turn("fix it", "2026-11-10T11:00:00Z",
                 at(say(TREPLY), "2026-11-10T11:01:00Z")))


def fixture_terms():
    """xv-tm: term_lines, ended, alone in its project."""
    solo = "/home/demo/terms"
    os.makedirs(os.path.dirname(tl("x", "-home-demo-terms")), exist_ok=True)
    record("xv-tm", TSIDS[0], [dict(o, cwd=solo) if "cwd" in o else o
                               for o in term_lines()], "-home-demo-terms",
           solo)


def test_terms():
    """#459: terms (backticks, CONST_CASE / snake_case) show in Refs by
    default, in the last reply's group after its machine refs; a term only
    a tool's output says is never listed; the tab count has them."""
    o = json.loads(dch("--xray", "xv-tm", "--json").stdout)
    keys = [x["key"] for x in o["refs"]]
    v, text = refs_plain("xv-tm")
    try:
        at_ = [text.find(x) for x in ("in the last reply", "PAY-219", "#412",
                                      "test_retry_timeout", "ERR_LEDGER_SKEW")]
        check("Refs #459: the last reply's group lists its terms after its "
              "machine refs, the last on top", -1 not in at_ and
              at_ == sorted(at_) and text.count("ERR_LEDGER_SKEW") == 1, text)
        at_ = [text.find(x) for x in ("the rest", "#377", "OLD_FLAG_NAME")]
        check("Refs #459: the rest's group: its terms after its machine "
              "refs", -1 not in at_ and at_ == sorted(at_), text)
        check("Refs #459: prose terms listed by default; one only a tool's "
              "output says is not", "Pull requests, issues, tickets and "
              "terms" in text and "ERR_LEDGER_SKEW" in text and
              "TOOL_ONLY_TERM" not in text and "tool_only_case" not in text,
              text)
        check("Refs #459: the tab count includes the terms, which a does "
              "not hide: no hidden count", ("Refs %d" % len(keys)) in text
              and "term:ERR_LEDGER_SKEW" in keys and "hidden" not in text,
              (keys, text))
        check("Refs #473: a flag, K=V, call or bare filename in backticks is "
              "no term; a CamelCase name is",
              "term:LedgerClock" in keys and not [
                  k for k in keys if k in (
                      "term:--json", "term:-v", "term:RETRY_MAX=3",
                      "term:README.md", "term:main.c", "term:parseN()",
                      "term:foo(x)")], keys)
        v.key(b"a", 1.0)
        v.pump(0.5)
        text = screen(v, 0)
        check("Refs #459: a term only in tool output is never listed (a "
              "shows every kind)", "TOOL_ONLY_TERM" not in text and
              "tool_only_case" not in text and
              not any("TOOL_ONLY" in k for k in keys), (keys, text))
        v.key(b"q")
    finally:
        v.close()


def test_terms_live():
    """#459, the audit's gap 2: the chord over a live session lists the
    screen's term ERR_LEDGER_SKEW in the on-screen group after the
    machine ref; one the index does not hold (tool output) is not there.
    Enter on it opens its card: the first mention as its definition, its
    mentions newest first."""
    cwd = os.path.realpath(os.path.join(T, "tlive"))
    os.makedirs(cwd, exist_ok=True)
    proj = os.path.join(HOME, ".claude", "projects", cwd.replace("/", "-"))
    os.makedirs(proj, exist_ok=True)
    with open(os.path.join(proj, TSIDS[1] + ".jsonl"), "w") as f:
        for o in term_lines():
            f.write(json.dumps(dict(o, cwd=cwd) if "cwd" in o else o) + "\n")
    dch("--spawn", "xv-tv", "/usr/bin/perl", "-e",
        '$|=1; print "on screen: #412 ERR_LEDGER_SKEW TOOL_ONLY_TERM\\n";'
        ' sleep 300')
    pid = until(lambda: json.loads(dch("--cmd", "xv-tv").stdout or "{}")
                .get("pid"), 5)
    start = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(pid)],
                           capture_output=True, text=True,
                           env={"LC_ALL": "C", "TZ": "UTC"}).stdout.strip()
    sd = os.path.join(HOME, ".claude", "sessions")
    os.makedirs(sd, exist_ok=True)
    with open(os.path.join(sd, "%d.json" % pid), "w") as f:
        json.dump({"pid": pid, "sessionId": TSIDS[1], "cwd": cwd,
                   "procStart": start, "version": "2.1.0",
                   "kind": "interactive", "status": "idle",
                   "statusUpdatedAt": 1}, f)
    lite = dch("--build").stdout.strip() == "lite"   # no mirror
    a = Pty([DCH, "-a", "xv-tv"], rows=30, cols=100)
    try:
        attached(a)
        a.pump(0.3)
        mark = len(a.out)
        a.key(b"\x1c", 0.05, full=True)
        a.key(b"x", 0.05, full=True)
        ok = a.wait(b"[Refs", start=mark) and until(
            lambda: (a.pump(0.2), "in the last reply" in shown(a, mark) and
                     (lite or "on screen now" in shown(a, mark)))[1], 10)
        text = shown(a, mark)
        want = (("in the last reply", "PAY-219", "#412", "LedgerClock",
                 "test_retry_timeout", "ERR_LEDGER_SKEW") if lite else
                ("on screen now", "#412", "ERR_LEDGER_SKEW",
                 "in the last reply", "PAY-219", "test_retry_timeout"))
        at_ = [text.find(x) for x in want]
        check("Refs #459: the chord on a live session: ERR_LEDGER_SKEW in "
              "the on-screen group after #412, not again below; "
              "TOOL_ONLY_TERM not listed (lite: no screen, the last "
              "reply's group)", ok and -1 not in at_ and at_ == sorted(at_)
              and text.count("ERR_LEDGER_SKEW") == 1 and
              "TOOL_ONLY_TERM" not in text, text)
        mark = len(a.out)
        # its row: the on-screen group's second (lite: the reply's fifth,
        # LedgerClock's row before it, #473)
        a.key(b"jjjj" if lite else b"j", 0.5, full=True)
        a.key(b"\r", 0.5, full=True)
        ok = a.wait(b"Mentions: this session ", start=mark)
        settle(a)
        text = screen(a, mark)
        # #478: grouped by turn, p2's header over p1's
        rep = re.search(r"p2 fix it .*\n.*Reply +`ERR_LEDGER_SKEW` no "
                        r"longer fires", text)
        first = re.search(r"p1 why does the ledger skew .*\n.*Reply +"
                          r"`ERR_LEDGER_SKEW` is raised", text)
        check("card #459: Enter on ERR_LEDGER_SKEW opens its card, the first "
              "mention as its definition, mentions newest first (p2 over "
              "p1)", ok and "ERR_LEDGER_SKEW" in text and
              re.search(r"term +`ERR_LEDGER_SKEW` is raised when the ledger "
                        "clock drifts", text) and
              text.count("is raised when the ledger") == 2 and rep and
              first and rep.start() < first.start(), text)
        a.key(b"q", 0.3, full=True)
        a.key(b"q", 0.3, full=True)
    finally:
        a.close()
        dch("-k", "xv-tv")


RCSIDS = ["11111111-2222-4333-8444-479000000%03d" % k for k in (1, 2, 3)]


def fixture_recall():
    """#479: xv-ra and xv-rb share /home/demo/recall; xv-rb says #412 in its
    turns 2 and 3; xv-rc, in another project, says it once."""
    for k, (name, proj, asks) in enumerate((
            ("xv-ra", "recall", ["mine: see #412 today"]),
            ("xv-rb", "recall", ["start", "Plan the cache. Then fix #412 first.",
                                 "Is #412 done?", "bye"]),
            ("xv-rc", "farproj", ["far away #412 here"]))):
        cwd = "/home/demo/" + proj
        lines = []
        for t, a in enumerate(asks):
            lines += turn(a, "2026-11-1%dT1%d:00:00Z" % (k, t),
                          at(say("ok %d" % t), "2026-11-1%dT1%d:01:00Z" % (k, t)))
        os.makedirs(os.path.dirname(tl("x", "-home-demo-" + proj)),
                    exist_ok=True)
        record(name, RCSIDS[k], [dict(o, cwd=cwd) if "cwd" in o else o
                                 for o in lines], "-home-demo-" + proj, cwd)


def test_recall():
    """#479: the card's other same-project session: a session header (its
    name, its age), its turns newest first with their sentences; Enter on
    the session header or a turn header opens that session's reader there."""
    v = Pty([DCH, "--xray", "xv-ra"], env={"TMPDIR": TPD + "/",
                                           "NO_COLOR": ""})
    try:
        v.wait(b"Prompts 1")
        v.pump(0.5)
        for _ in range(6):
            if "[Refs" in rows(v.out).get(2, ""):
                break
            v.key(b"\t", 0.5)
        mark = len(v.out)
        v.key(b"\r", 1.5)
        v.wait(b"Mentions: this session ", start=mark)
        mark = len(v.out)
        v.key(b"f", 0.5)
        until(lambda: (v.pump(0.1), "in 1 sessions" in screen(v, mark))[1], 10)
        v.pump(0.5)
        text = screen(v, 0)
        lines = text.split("\n")
        at_ = [next((i for i, l in enumerate(lines) if re.search(x, l)), -1)
               for x in (r"p1 mine: see #412 today",
                         r"You mine: see #412 today",
                         r"also discussed in xv-rb, \d+[mhd] ago",
                         r"p3 Is #412 done\? +\d\d-\d\d \d\d:\d\d",
                         r"You Is #412 done\? *$",
                         r"p2 Plan the cache\. Then fix #412 first\.",
                         r"You Then fix #412 first\. *$")]
        check("#479: the card: this session's turn, then 'also discussed in "
              "xv-rb, <age>', its p3 and p2 headers each over its sentence",
              -1 not in at_ and at_ == list(range(at_[0], at_[0] + 7)) and
              "xv-rc" not in text and "far away" not in text, (at_, text))
        v.key(b"j", 0.5)
        mark = len(v.out)
        v.key(b"\r", 1.0)
        ok = until(lambda: (v.pump(0.1), "ok 2" in screen(v, mark))[1], 5)
        text = screen(v, mark)
        check("#479: Enter on xv-rb's header opens its reader at its newest "
              "turn (p3)", ok and "Is #412 done?" in text and "Plan the cache"
              not in text, text)
        v.key(b"q", 0.5)
        v.key(b"jjj", 0.5)
        mark = len(v.out)
        v.key(b"\r", 1.0)
        ok = until(lambda: (v.pump(0.1), "ok 1" in screen(v, mark))[1], 5)
        text = screen(v, mark)
        check("#479: Enter on xv-rb's p2 header opens its reader at p2",
              ok and "Plan the cache" in text and "Is #412 done?" not in
              text, text)
        v.key(b"q", 0.5)
        v.key(b"q")
        v.key(b"q")
        v.exit()
    finally:
        v.close()


def main():
    run_steps(fixture, test_refusals, test_view, fixture_refs, test_refs,
              fixture_latest, test_latest, test_lookup, fixture_order,
              test_order, test_tool_mention, fixture_tmp, test_tmp, test_memory_live,
              fixture_reply, test_reply, test_reply_live,
              fixture_said, test_said, fixture_narrow, test_narrow, test_narrow_cjk,
              test_narrow_mark,
              fixture_terms, test_terms,
              test_terms_live, fixture_recall, test_recall)


if __name__ == "__main__":
    main()
