#!/usr/bin/env python3
"""The `dch --xray <n>` view, part 3 of 4 (#365): Tasks,
Memory (big, the command), the found place, twin hits, the tab bar's
widths.

The harness, the fixtures and the list of what the four parts cover
are in xray_view_lib.py.

Run:  python3 tests/xray_view_tasks_test.py      (uses ./dch)
"""
from xray_view_lib import *  # noqa: F401,F403 (harness, fixtures, T)


def test_tasks():
    """#320: no tab without a task list; the tab last, its open count; the
    groups and their order, marks, ages, blocked by; Enter reads the turn
    that last changed the task, q back to it; the filter; hostile text."""
    v = Pty([DCH, "--xray", "xv-nt"], rows=30, cols=100)
    try:
        v.wait(b"Prompts 1")
        v.pump(1.5)
        tabs = []
        for _ in range(4):
            v.key(b"\t", 0.4)
            tabs.append(rows(v.out).get(2, ""))
        check("Tasks: a session with no task list has no Tasks tab, Tab "
              "never lands on one", all("Tasks" not in t for t in tabs) and
              re.search(r"\[Prompts", tabs[-1]), tabs)
        v.key(b"q")
        v.exit()
    finally:
        v.close()
    v = Pty([DCH, "--xray", "xv-tk"], rows=30, cols=100)
    try:
        ok = tabbar(v, "Tasks 3")
        t = rows(v.out).get(2, "")
        check("Tasks: the last tab, its count the open tasks (1 in progress, "
              "2 pending)", ok and 0 <= t.find("Prompts 3") <
              t.find("Search") < t.find("Tasks 3") and
              re.search(r"Tasks 3\s*$", t), t)
        v.key(b"\033[Z", 0.8)
        r = tasks_rows(v)
        check("Tasks: Shift-Tab from Prompts opens it; In progress, Pending, "
              "Done newest first, Dropped, each group counted",
              [x for x in r if not x.startswith(("▌", "▶", "○",
                                                 "✓", "✗"))
               and not x[1:].lstrip().startswith(("▶", "○",
                                                  "✓", "✗"))] ==
              ["In progress 1", "Pending 2", "Done 2", "Dropped 1"] and
              [x.split()[2] for x in r if re.match(r"^\W*[▶○✓"
                                                    r"✗] \d ", x)]
              == ["Fixing", "Write", "evil", "Recent", "Old", "Dropped"], r)
        line = lambda w: next((x for x in r if w in x), "")
        check("Tasks: the active row shows its active text, an age at the "
              "right (2h), selected first",
              re.search(r"^▌▶ 1 Fixing the login test 2h$",
                        line("Fixing")), line("Fixing"))
        check("Tasks: blocked by on the row (its age from the update that "
              "said it); ages 5m and 3d on Done",
              re.search(r"Write docs blocked by 1 2h$", line("Write")) and
              line("Recent").endswith(" 5m") and
              line("Old done").endswith(" 3d"), r)
        check("Tasks: a hostile title shows with ?, cut to one line",
              re.search(r"evil \?\]0;pwned\? bi\?di w+…", line("evil")),
              line("evil"))
        bad = [b for b in (b"\033]0;pwned", b"\007", b"\xe2\x80\xae")
               if b in v.out]
        check("Tasks: no ESC, BEL or bidi byte of a title reached the "
              "terminal", not bad, bad)
        v.key(b"jjj", 0.5)
        check("Tasks: j by task, past the group lines", "Recent done" in
              head(v), head(v))
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("Tasks: Enter reads the turn that last changed the task (3 of "
              "3)", v.wait(b"3 of 3", start=mark) and
              v.wait(b"TaskUpdate", start=mark), screen(v, mark))
        mark = len(v.out)
        v.key(b"q", 1.0)
        check("Tasks: q back to the same task", tabbar(v, "Tasks 3") and
              "Recent done" in head(v), head(v))
        mark = len(v.out)
        v.key(b"/DOCS", 0.8)
        r = tasks_rows(v)
        check("Tasks: / filters, any case, only the groups with a match",
              r == ["Pending 1", "▌○ 2 Write docs blocked by 1 " +
                    r[-1].split()[-1]] if len(r) == 2 else False, r)
        v.key(b"\x15ci fails", 0.8)
        r = tasks_rows(v)
        check("Tasks: the filter matches a note", len(r) == 2 and
              "Fixing the login test" in r[1], r)
        v.key(b"\x15zzzq", 0.8)
        check("Tasks: no match says so", "No match" in screen(v, mark),
              screen(v, mark))
        v.key(b"\033", 0.3)
        v.key(b"\033", 0.5)
        check("Tasks: Esc clears the filter, every row back",
              len(tasks_rows(v)) == 10, tasks_rows(v))
        v.key(b"q")
        check("Tasks: q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()


def test_tasks_big():
    """#320: 20000 tasks list fast and scroll without lag; Pending in the
    answer's order (G the last created)."""
    x = 4 if os.environ.get("DCH_TEST_NO_BUDGET") else 1  # sanitizer build
    t0 = time.time()
    v = Pty([DCH, "--xray", "xv-tb"], rows=30, cols=100)
    try:
        ok = tabbar(v, "Tasks 20000", 15)
        dt = time.time() - t0
        check("Tasks big: 20000 tasks counted in the tab in %.2f s (under %d)"
              % (dt, 5 * x), ok and dt < 5 * x, rows(v.out).get(2))
        v.key(b"\033[Z", 0.5)
        lag = []
        for k, want in ((b"G", "bulk task 20000"), (b"g", "bulk task 00001"),
                        (b"\033[6~", None), (b"j", None)):
            mark = len(v.out)
            s = time.time()
            v.key(k, 0)
            v.wait(b"\xe2\x96\x8c", 5, start=mark)
            lag.append(time.time() - s)
            v.pump(0.2)
            if want:
                check("Tasks big: %s selects %s" % (k, want), want in head(v),
                      head(v))
        check("Tasks big: every key painted within %g s (%s)" %
              (0.5 * x, ", ".join("%.3f" % t for t in lag)),
              max(lag) < 0.5 * x, lag)
        mark = len(v.out)
        s = time.time()
        v.key(b"/19999", 0)
        ok = v.wait(b"bulk task 19999", 5, start=mark)
        check("Tasks big: a filter over 20000 rows within %d s (%.3f s)" %
              (x, time.time() - s), ok and time.time() - s < x,
              screen(v, mark))
        v.key(b"\033", 0.3)
        v.key(b"q")
        check("Tasks big: q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()


def test_memory():
    """#323: no tab without a memory; the tab last, every row counted; This
    session (wrote, loaded), Memories, Instructions; the card and its text,
    cut; Enter on the card reads the turn, q back to it, q to the row; the
    filter over the text."""
    v = Pty([DCH, "--xray", "xv-nt"], rows=30, cols=100)
    try:
        v.wait(b"Prompts 1")
        v.pump(1.5)
        check("Memory: a session with no memory has no Memory tab",
              "Memory" not in rows(v.out).get(2, ""), rows(v.out).get(2))
        v.key(b"q")
        v.exit()
    finally:
        v.close()
    # instruction files above T (a stray /tmp/CLAUDE.md, the machine-wide
    # file) are the host's and the product walks to / whatever HOME is: count
    # them with the CLI's same walk, only the number, never a path or body
    host = sum(1 for x in dch("--xray", "xv-me", "--memory", "--json")
               .stdout.splitlines() if '"kind":"instructions"' in x
               and (MP + "/") not in x)
    v = Pty([DCH, "--xray", "xv-me"], rows=30, cols=100)
    try:
        ok = tabbar(v, "Memory %d" % (5 + host))
        t = rows(v.out).get(2, "")
        check("Memory: the last tab, counting every row", ok and
              re.search(r"Search\s+Memory %d\s*$" % (5 + host), t), t)
        r = [re.sub(r" (now|\d+[mhd])$", "", x).lstrip("▌ ") for x in
             mem_tab(v)]
        check("Memory: This session (wrote, then loaded), Memories (newest "
              "first), Instructions (the path short); kind and note faint",
              r[:7] == ["This session 2", "Alpha rule feedback alpha hook "
                        "wrote", "Beta notes project beta hook loaded",
                        "Memories 2", "Gamma ref reference gamma hook", "long",
                        "Instructions %d" % (1 + host)] and
              "CLAUDE.md" in r[7:], r)
        check("Memory: no word chapter on the screen",
              b"chapter" not in v.out.lower(), "")
        v.key(b"jj", 0.5)
        mark = len(v.out)
        v.key(b"\r", 0.8)
        scr = screen(v, mark)
        check("Memory: Enter opens the card: its fields and its text",
              "Memory gamma" in scr and "Kind reference" in scr and
              "Note gamma hook" in scr and "Path " + MDIR + "/gamma.md" in
              scr and "gamma line 01" in scr and "zebrafish" not in scr, scr)
        mark = len(v.out)
        v.key(b"G", 0.5)
        check("Memory: the card's text scrolls to its end",
              "the zebrafish line" in screen(v, mark), screen(v, mark))
        v.key(b"\r", 0.5)
        check("Memory: Enter on a row this session did not touch says so",
              "did not write or load it" in rows(v.out).get(29, "") or
              "did not write or load it" in screen(v, mark), screen(v, mark))
        v.key(b"q", 0.5)
        check("Memory: q from the card back to the same row",
              "Gamma ref" in head(v), head(v))
        v.key(b"j\r", 0.8)
        scr = screen(v, mark)
        check("Memory: a text over 16 KB says it was cut and names the path",
              re.search(r"Text cut at 16 KB: the whole text is in \S*/long\.md",
                        scr), scr)
        v.key(b"q", 0.5)
        v.key(b"gg", 0.3)
        mark = len(v.out)
        v.key(b"\r", 0.8)
        check("Memory: a session row's card says this session wrote it",
              "this session wrote it; enter reads that prompt" in
              screen(v, mark), screen(v, mark))
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("Memory: Enter on the card reads the turn that wrote it (2 of "
              "2)", v.wait(b"2 of 2", start=mark) and
              v.wait(b"remember the rule", start=mark), screen(v, mark))
        mark = len(v.out)
        v.key(b"q", 0.8)
        check("Memory: q from the reader back to the card",
              "Memory alpha" in screen(v, mark), screen(v, mark))
        v.key(b"q", 0.5)
        check("Memory: q from the card back to the row",
              "Alpha rule" in head(v), head(v))
        v.key(b"/" + NEEDLE.upper().encode(), 0.8)
        r = tasks_rows(v)
        check("Memory: / filters over a memory's text, any case",
              len(r) == 2 and r[0] == "Memories 1" and "Gamma ref" in r[1],
              r)
        v.key(b"\033", 0.3)
        v.key(b"\033", 0.5)
        v.key(b"q")
        check("Memory: q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()


def test_memory_big():
    """#323: 20000 memories: the tab and the whole list fast."""
    t0 = time.time()
    v = Pty([DCH, "--xray", "xv-mb"], rows=30, cols=100)
    try:
        ok = tabbar(v, "Memory ", 15)
        dt = time.time() - t0
        ok2 = tabbar(v, "Memory 20000", 15)
        dt2 = time.time() - t0
        # a bound for a hang, not a speed target: 1.75 to 4.17 s measured,
        # by the machine's load (#325)
        check("Memory big: the tab in %.2f s, all 20000 rows in %.2f s "
              "(under 15)" % (dt, dt2), ok and ok2 and dt2 < 15,
              rows(v.out).get(2))
        mem_tab(v)
        mark = len(v.out)
        s = time.time()
        v.key(b"G", 0)
        ok = v.wait(b"\xe2\x96\x8c m", 5, start=mark)
        check("Memory big: G painted within 0.5 s (%.3f s)" %
              (time.time() - s), ok and time.time() - s < 0.5 and
              re.search(r"m\d{5}", head(v)), head(v))
        v.key(b"q")
        check("Memory big: q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()


def test_place():
    """MED-V21: Enter on each hit opens the reader at that hit; LOW-V18: a
    good search clears a refusal's status."""
    v = Pty([DCH, "--xray", "xv-sp"], rows=30, cols=100)
    try:
        v.wait(b"Prompts 1")
        v.pump(0.5)
        for _ in range(3):  # Responses, Refs, Search
            v.key(b"\t", 0.5)
        v.key(b"needle\r", 0.5)
        ok = tabbar(v, "Search 3")
        v.pump(0.3)
        h = [(w, t) for w, t, _ in hits(rows(v.out))]
        check("place: 3 hits in one turn, the output, the reply, the prompt",
              ok and [w for w, _ in h] == ["Bash", "Reply", "You"], h)
        seen = []
        for k, (who, want, not_) in enumerate((
                ("Bash", "PLACE-C", ("PLACE-A", "PLACE-B")),
                ("Reply", "PLACE-B", ("PLACE-A", "PLACE-C")),
                ("You", "PLACE-A", ("PLACE-B", "PLACE-C")))):
            if k:
                v.key(b"j", 0.5)
            mark = len(v.out)
            v.key(b"\r", 1.0)
            v.wait(want.encode(), start=mark)
            v.pump(0.5)
            text = screen(v, mark)
            seen.append(text)
            # the body, under the head (its title is PLACE-A's prompt)
            scr = rows(v.out[mark:])
            body = "\n".join(scr[r] for r in sorted(scr) if r > 2)
            check("place: Enter on the %s hit opens the reader with its line "
                  "(%s) on screen, not the others (MED-V21)" % (who, want),
                  "1 of 1" in scr.get(2, "") and want + " " in body and
                  not any(n in body for n in not_), text)
            mark = len(v.out)
            v.key(b"q", 1.0)
            check("place: q back to the same hit, the %s one" % who,
                  tabbar(v, "Search 3") and who in head(v), head(v))
        mark = len(v.out)
        v.key(b"/\x15x\r", 1.0)
        check("place: a 1-byte text is refused in the status line",
              "bad search" in screen(v, mark), screen(v, mark))
        v.key(b"\x15needle\r", 1.0)
        tabbar(v, "Search 3")
        v.pump(0.3)
        now = "\n".join(rows(v.out).values())
        seen.append(now)
        check("place: a good search after it clears the refusal's status "
              "(LOW-V18)", "bad search" not in now and
              len(hits(rows(v.out))) == 3, now)
        check("place: the word chapter on no screen",
              not any("chapter" in t.lower() for t in seen), seen)
        v.key(b"q")
        v.exit()
    finally:
        v.close()


def test_twins():
    """LOW-V29: two Bash outputs in one turn with the same text around the
    word: each hit opens at its own output, not both at the first."""
    ts = "2026-11-10T12:0%d:00Z"
    out = "".join("filler one %03d\n" % i for i in range(100)) + \
        "the needle is here\npad\npad\npad"
    record("xv-sq", QSID,
           turn("twins", ts % 0,
                at(call("b1", "Bash", {"command": "cat a"}), ts % 1),
                at(result("b1", out), ts % 1),
                at(say("MARK-2 above"), ts % 1),
                at(call("b2", "Bash", {"command": "cat b"}), ts % 2),
                at(result("b2", out), ts % 2),
                at(say("MARK-3 below"), ts % 2)),
           proj="-mem-proj", cwd=MP)
    v = Pty([DCH, "--xray", "xv-sq"], rows=30, cols=100)
    try:
        v.wait(b"Prompts 1")
        for _ in range(3):  # Responses, Refs, Search
            v.key(b"\t", 0.3)
        v.key(b"needle\r", 0.3)
        ok = tabbar(v, "Search 2")
        for k, (want, not_) in enumerate((("MARK-3", "MARK-2"),
                                          ("MARK-2", "MARK-3"))):
            if k:
                v.key(b"j", 0.3)
            mark = len(v.out)
            v.key(b"\r", 0)
            v.wait(b"needle is here", start=mark)
            v.wait(want.encode(), 3, start=mark)
            v.pump(0.3)
            scr = rows(v.out[mark:])
            body = "\n".join(scr[r] for r in sorted(scr) if r > 2)
            check("twins: hit %d opens at its own output (%s under the "
                  "needle, not %s) (LOW-V29)" % (k, want, not_), ok and
                  "the needle is here" in body and want in body and
                  not_ not in body, body)
            v.key(b"q", 0)
            tabbar(v, "Search 2")
        v.key(b"q")
        v.exit()
    finally:
        v.close()
    # LOW-V33: three twins, the first one's text past the reader's 3000
    # character cut of an output: each still opens at its own place
    record("xv-sc", "11111111-2222-4333-8444-5555555555fa",
           turn("cut twins", ts % 0,
                at(call("c1", "Bash", {"command": "cat a"}), ts % 1),
                at(result("c1", "zfill\n" * 600 + out), ts % 1),
                at(say("MARK-2 above"), ts % 1),
                at(call("c2", "Bash", {"command": "cat b"}), ts % 2),
                at(result("c2", out), ts % 2),
                at(say("MARK-3 below"), ts % 2),
                at(call("c3", "Bash", {"command": "cat c"}), ts % 3),
                at(result("c3", out), ts % 3),
                at(say("MARK-4 below"), ts % 3)),
           proj="-mem-proj", cwd=MP)
    v = Pty([DCH, "--xray", "xv-sc"], rows=30, cols=100)
    try:
        v.wait(b"Prompts 1")
        for _ in range(3):
            v.key(b"\t", 0.3)
        v.key(b"needle\r", 0.3)
        ok = tabbar(v, "Search 3")
        # newest first: c3, c2, then c1 at its output's start
        for k, (want, nots) in enumerate((
                ("MARK-4", ("MARK-2", "MARK-3", "zfill")),
                ("MARK-3", ("MARK-2", "MARK-4", "zfill")),
                ("zfill", ("needle", "MARK-")))):
            if k:
                v.key(b"j", 0.3)
            mark = len(v.out)
            v.key(b"\r", 0)
            v.wait(want.encode(), start=mark)
            v.pump(0.3)
            scr = rows(v.out[mark:])
            body = "\n".join(scr[r] for r in sorted(scr) if r > 2)
            check("cut twins: hit %d opens at its own output (%s, none of "
                  "%s) (LOW-V33)" % (k, want, nots), ok and want in body
                  and not any(n in body for n in nots), body)
            v.key(b"q", 0)
            tabbar(v, "Search 3")
        v.key(b"q")
        v.exit()
    finally:
        v.close()
    # LOW-V34, LOW-V35 (#356): a twin the reader cuts or never shows, in
    # an entry of another role or tool, or beside an unrelated cut output:
    # each hit opens at its own entry (the entries say their offset). The
    # place is "path" (the Write's card) above any needle row, else the
    # PLACE-x under the first needle row, else the first START-x (a cut
    # entry's start).
    pad = "\n".join(["pad " * 12] * 4)

    def place(x):
        return (pad + "\nthe needle is here\n" + pad + "\ngap\ngap\n"
                "PLACE-%s\n" % x)

    def cutout(x, fill="z\n", n=3200):
        return "START-%s\n" % x + fill * n + place(x)

    def bash(i, o):
        return [at(call("t%d" % i, "Bash", {"command": "cat %d" % i}),
                   ts % i), at(result("t%d" % i, o), ts % i)]

    def multi(*tus):  # one assistant line with several tool_use blocks
        return {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": i, "name": n, "input": inp}
            for i, n, inp in tus]}}

    def WR(c):
        return {"file_path": "/x/f.txt", "content": c}

    long_ = "\n".join(["pad " * 12] * 40)

    def probe(scr):
        body = [scr.get(r, "") for r in range(3, 29)]
        ni = [r for r, s in enumerate(body) if "needle" in s]
        if any("/x/f.txt" in s for s in body[:ni[0] if ni else None]):
            return "path"
        for s in body[ni[0]:] if ni else []:
            m = re.search(r"PLACE-(\w+)", s)
            if m:
                return m.group(1)
        for s in body:
            m = re.search(r"START-(\w+)", s)
            if m:
                return "START-" + m.group(1)
        return None

    for name, sid, objs, wants in (
            ("xv-su", "b61", turn(cutout("U", "y\n", 3100), ts % 0,
                                  at(say(place("R")), ts % 1),
                                  *bash(2, place("O"))),
             ("O", "R", "START-U")),
            ("xv-sb", "b62", turn("start", ts % 0, *bash(1, cutout("F1")),
                                  at(say(place("R")), ts % 2),
                                  *bash(3, place("F3"))),
             ("F3", "R", "START-F1")),
            ("xv-sg", "b63", turn("start", ts % 0,
                                  at(call("g1", "Grep", {"pattern": "zz"}),
                                     ts % 1),
                                  at(result("g1", cutout("G1")), ts % 1),
                                  *(bash(2, place("F2")) +
                                    bash(3, place("F3")))),
             ("F3", "F2", "START-G1")),
            ("xv-sw", "b64", turn("start", ts % 0,
                                  at(call("w1", "Write",
                                          {"file_path": "/x/f.txt",
                                           "content": place("W")}), ts % 1),
                                  at(result("w1", "File created"), ts % 1),
                                  *(bash(2, place("B1")) +
                                    bash(3, "START-L\n" + "z\n" * 3200) +
                                    bash(4, place("B2")))),
             ("B2", "B1", "path")),
            # #370: one line with a Bash call and a Write call; a needle in
            # the Write's content only (the reader does not show it), then
            # in the Bash command only: each hit opens its own tool's card
            ("xv-sl", "b65", turn("start", ts % 0,
                                  at(multi(("l1", "Bash", {"command": long_}),
                                           ("l2", "Write", WR(place("W")))),
                                     ts % 1),
                                  at(result("l2", "File created"), ts % 1)),
             ("path",)),
            ("xv-sm", "b66", turn("start", ts % 0,
                                  at(multi(("l1", "Bash",
                                            {"command": place("C")}),
                                           ("l2", "Write", WR("plain"))),
                                     ts % 1),
                                  at(result("l2", "File created"), ts % 1)),
             ("C",))):
        record(name, "11111111-2222-4333-8444-555555555" + sid,
               objs + [at(say("end"), ts % 9)], proj="-mem-proj", cwd=MP)
        v = Pty([DCH, "--xray", name], rows=30, cols=100)
        try:
            v.wait(b"Prompts 1")
            for _ in range(3):
                v.key(b"\t", 0.3)
            v.key(b"needle\r", 0.3)
            ok = tabbar(v, "Search %d" % len(wants))
            for k, want in enumerate(wants):
                if k:
                    v.key(b"j", 0.3)
                mark = len(v.out)
                v.key(b"\r", 0)
                until(lambda: (v.pump(0.1),
                               probe(rows(v.out[mark:])) == want)[1], 10)
                v.pump(0.3)
                got = probe(rows(v.out[mark:]))
                check("%s: hit %d opens at its own entry (%s) (LOW-V34, "
                      "LOW-V35)" % (name, k, want),
                      ok and got == want, (got, screen(v, mark)))
                v.key(b"q", 0)
                tabbar(v, "Search %d" % len(wants))
            v.key(b"q")
            v.exit()
        finally:
            v.close()


def test_memory_cmd():
    """memory_command rows on xv-me: a session row with no turn (LOW-V30),
    a cut text with no path (LOW-V31), card fields cut with an ellipsis,
    never in a wide character (NIT-V11)."""
    rf = os.path.join(T, "mrows")
    with open(rf, "w") as f:
        for r in ({"title": "Cmd wrote", "id": "c1", "session": "wrote",
                   "note": "n" * 84 + "設" * 10 + "/END"},
                  {"title": "Cmd big", "id": "c2", "body": "y" * 20000}):
            f.write(json.dumps(r) + "\n")
    cfg = os.path.join(T, "config-mcmd")
    with open(cfg, "w") as f:
        f.write("memory_command = /bin/cat %s\n" % rf)
    v = Pty([DCH, "--xray", "xv-me"], rows=30, cols=100,
            env={"DCH_CONFIG": cfg})
    try:
        ok = tabbar(v, "Memory 2")
        mem_tab(v)
        mark = len(v.out)
        v.key(b"\r", 0)
        ok = v.wait(b"this session wrote it", start=mark) and ok
        v.pump(0.3)
        scr = rows(v.out[mark:])
        ses = [r for r in scr.values() if "Session" in r]
        note = [r for r in scr.values() if r.lstrip().startswith("Note")]
        check("memory cmd: the card says this session wrote it, no turn to "
              "read", ok and len(ses) == 1 and "wrote it" in ses[0] and
              "enter reads" not in ses[0], ses)
        check("memory cmd: a long Note cut with an ellipsis before a wide "
              "character it cannot hold (NIT-V11)", len(note) == 1 and
              note[0].rstrip().endswith("n…") and
              "/END" not in note[0], note)
        mark = len(v.out)
        v.key(b"\r", 0)
        v.wait(b"to read", 3, start=mark)
        v.pump(0.3)
        s = screen(v, mark)
        check("memory cmd: Enter agrees with the card: no prompt to read, "
              "not 'did not write' (LOW-V30)", "no prompt to read" in s and
              "did not write" not in s, s)
        v.key(b"q", 0.3)
        mark = len(v.out)
        v.key(b"j\r", 0)
        v.wait(b"16 KB", start=mark)
        v.pump(0.3)
        txt = [r for r in rows(v.out[mark:]).values()
               if r.lstrip().startswith("Text")]
        check("memory cmd: a cut text with no path says only that it is "
              "cut (LOW-V31)", len(txt) == 1 and
              re.sub(" +", " ", txt[0]).strip() == "Text cut at 16 KB", txt)
        v.key(b"q", 0.3)
        v.key(b"q")
        v.exit()
    finally:
        v.close()


def test_tabbar():
    """NIT-V9: six tabs at 24 (the view's least), 30, 40 and 60 columns,
    each selected in turn: no word cut, the selected one shown; at 20 the
    view says it is too small."""
    v = Pty([DCH, "--xray", "xv-sp"], rows=24, cols=100)
    try:
        ok = tabbar(v, "Memory") and tabbar(v, "Tasks")
        check("tab bar: xv-sp has six tabs", ok and all(
            t in rows(v.out).get(2, "") for t in TABS), rows(v.out).get(2))
        for w in (60, 40, 30, 24):
            mark = len(v.out)
            v.resize(24, w)
            v.pump(0.5)
            bars = []
            for k in range(len(TABS)):
                if k:
                    mark = len(v.out)
                    v.key(b"\t", 0)
                # the bar redrawn since the resize or Tab with this tab
                # selected: on a slow runner the old bar, at the old width,
                # was taken for it; a bar that never selects it fails below
                until(lambda: (v.pump(0.1), "[" + TABS[k] in
                               rows(v.out[mark:]).get(2, ""))[1], 10)
                v.pump(0.1)  # the rest of that redraw
                bar = rows(v.out[mark:]).get(2, "")
                bars.append((TABS[k], bar))
            check("tab bar at %d columns: every tab selected in turn, no word "
                  "cut, the selected one shown (NIT-V9)" % w,
                  all(bar_ok(b, t, w) for t, b in bars), bars)
            mark = len(v.out)
            v.key(b"\t", 0)  # back to Prompts
            # its 24-column bar must land before the next mark: a late one
            # would be taken for a tab drawn at 20 columns
            until(lambda: (v.pump(0.1), "[Prompts" in
                           rows(v.out[mark:]).get(2, ""))[1], 10)
            v.pump(0.1)
        mark = len(v.out)
        v.resize(24, 20)
        until(lambda: (v.pump(0.1), "too small" in screen(v, mark))[1], 10)
        v.pump(0.3)  # the rest of that redraw
        text = screen(v, mark)
        check("tab bar at 20 columns: the view says it is too small, no tab "
              "drawn", "too small" in text and "[" not in text and
              not any(t[:3] in text for t in TABS), text)
        v.resize(24, 100)
        v.key(b"q", 0.3)
        v.exit()
    finally:
        v.close()


def main():
    run_steps(fixture_tasks, test_tasks, test_tasks_big, fixture_memory,
              test_memory, test_memory_big, fixture_place, test_place,
              test_twins, test_memory_cmd, test_tabbar)


if __name__ == "__main__":
    main()
