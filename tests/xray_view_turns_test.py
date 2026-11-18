#!/usr/bin/env python3
"""The `dch --xray <n>` view, part 2 of 4 (#365): auto
and compacted rows, ended chapters, Responses (History, live, Codex),
Codex tasks, Search.

The harness, the fixtures and the list of what the four parts cover
are in xray_view_lib.py.

Run:  python3 tests/xray_view_turns_test.py      (uses ./dch)
"""
from xray_view_lib import *  # noqa: F401,F403 (harness, fixtures, T)


def test_auto():
    o = json.loads(dch("--xray", "xv-au", "--json").stdout)
    ch = o["chapters"]
    check("auto fixture: five chapters, the three notifications auto, two "
          "with a summary", len(ch) == 5 and
          [c.get("auto", False) for c in ch] ==
          [False, True, True, True, False] and
          [("summary" in c) for c in ch] == [False, True, False, True, False],
          ch)
    bare = ch[2]["title"]
    # colours on: faint is an SGR
    v = Pty([DCH, "--xray", "xv-au"], env={"NO_COLOR": ""})
    try:
        v.wait(b"deploy it")
        v.pump(0.5)
        text = screen(v, 0)
        check("Prompts: the tab counts the prompts shown by default (2)",
              "Prompts 2" in text, text)
        check("Prompts: auto chapters are hidden",
              "fix the login bug" in text and "deploy it" in text and
              "Build finished" not in text and "task-notification" not in
              text and "Lint" not in text, text)
        check("Prompts: the footer shows n and how many are hidden",
              "n auto rows 3 hidden" in text, text)
        check("Prompts: o is offered only for a live session (none here)",
              "open session" not in text, text)
        sel = [r for r in text.split("\n") if r.startswith("\u258c")]
        check("#358: Prompts newest first, the newest selected on the first "
              "row", sel and "deploy it" in sel[0] and
              text.find("deploy it") < text.find("fix the login bug"),
              (sel, text))
        v.key(b"j", 0.3)  # #358: down to the oldest prompt
        mark = len(v.out)
        n0 = re.search(r"Prompts (\d+)", screen(v, 0))
        v.key(b"n", 1.0)
        text = screen(v, mark)
        n1 = re.search(r"Prompts (\d+)", screen(v, mark))
        check("#372: n raises the Prompts count by the auto rows (3)",
              n0 and n1 and int(n1.group(1)) == int(n0.group(1)) + 3,
              (n0, n1))
        check("Prompts: n shows them, titled by their summary",
              "Build finished: 3 passed" in text and
              "Lint ?]0;pwned? evil?done" in text, text)
        check("Prompts: an auto chapter with no summary keeps its title",
              bare[:30] in text, (bare, text))
        check("Prompts: shown, an auto chapter is faint, a prompt is not",
              re.search(rb"\033\[0;2m[^\033]*Build finished: 3 passed",
                        v.out[mark:]) and
              re.search(rb"\033\[0m[^\033]*deploy it", v.out),
              v.out[mark:][-800:])
        text = screen(v, 0)  # the rows n left as they were too
        check("Prompts: n shown, the footer says n hides them, the tab "
              "count includes them (5)", "n hide auto rows" in text and
              "hidden" not in text and "Prompts 5" in text, text)
        sel = [r for r in text.split("\n") if r.startswith("\u258c")]
        check("Prompts: n keeps the selection on its chapter",
              sel and "fix the login bug" in sel[0], (sel, text))
        mark = len(v.out)
        v.key(b"k", 0.3)  # #358: the auto row after it is above it
        v.key(b"\r", 1.0)
        check("Prompts: Enter on a shown auto chapter reads it",
              v.wait(b"reply 1", start=mark), v.out[mark:])
        v.key(b"\033", 0.5)
        mark = len(v.out)
        v.key(b"n", 1.0)
        n2 = re.search(r"Prompts (\d+)", screen(v, mark))
        check("#372: the second n drops the count back",
              n0 and n2 and n2.group(1) == n0.group(1), (n0, n2))
        v.key(b"/")
        v.key(b"Build", 1.0)
        text = screen(v, mark)
        check("Prompts: the filter matches a summary; only a hidden "
              "chapter matches: said, with the key",
              "Only auto rows: n shows them" in text, text)
        mark = len(v.out)
        v.key(b"\r", 0.5)
        v.key(b"n", 1.0)
        text = screen(v, mark)
        check("Prompts: n shows the one that matched the filter",
              "Build finished: 3 passed" in text and
              "fix the login bug" not in text, text)
        v.key(b"\033", 0.5)
        mark = len(v.out)
        v.key(b"o", 1.0, full=True)
        text = screen(v, mark)
        check("Prompts: o on an ended session says so and stays",
              "xv-au has ended: no session to open" in text and
              v.reap() is None, text)
        mark = len(v.out)
        v.key(b"\r", 1.0)
        v.wait(b"reply", start=mark)
        mark = len(v.out)
        v.key(b"o", 1.0, full=True)
        text = screen(v, mark)
        check("reader: o on an ended session says so and stays",
              "xv-au has ended: no session to open" in text and
              v.reap() is None, text)
        v.key(b"q", 0.3)
        v.key(b"q")
        check("Prompts: q exits 0", v.exit() == 0, v.out[-80:])
        bad = [b for b in (b"\033]0;pwned", b"\007 evil", b"\xe2\x80\xae")
               if b in v.out]
        check("mutant 11: no summary ESC, BEL or bidi byte in the output",
              not bad, bad)
    finally:
        v.close()
    v = Pty([DCH, "--xray", "xv-aa"])
    try:
        v.wait(b"Prompts")
        v.pump(0.8)
        text = screen(v, 0)
        check("Prompts: every chapter auto: the empty state says so and "
              "names n; the tab count is 0", "Only auto rows: n shows "
              "them" in text and "Prompts 0" in text and
              "n auto rows 1 hidden" in text, text)
        mark = len(v.out)
        v.key(b"n", 1.0)
        text = screen(v, mark)
        check("Prompts: n shows it, selected", any(
            r.startswith("\u258c") and "Only a notice" in r
            for r in text.split("\n")), text)
        v.key(b"q")
        v.exit()
    finally:
        v.close()


def test_compact():
    o = json.loads(dch("--xray", "xv-ac", "--json").stdout)
    ch = o["chapters"]
    check("compact fixture: the engine reads one compaction, not auto",
          [c["kind"] for c in ch] == ["prompt", "compact", "prompt", "prompt"] and
          [c.get("auto", False) for c in ch] == [False, False, True, False],
          ch)
    v = Pty([DCH, "--xray", "xv-ac"], env={"NO_COLOR": ""})
    try:
        v.wait(b"finish the work")
        v.pump(0.5)
        text = screen(v, 0)
        check("Prompts: a compaction is hidden by default, the tab count "
              "the prompts (2)", "start the work" in text and
              "Conversation compacted" not in text and "Prompts 2" in text,
              text)
        check("Prompts: the footer counts a compaction with the auto rows",
              "n auto rows 2 hidden" in text, text)
        mark = len(v.out)
        v.key(b"n", 1.0)
        text = screen(v, mark)
        check("Prompts: n shows the compaction, its = marker, faint",
              "= Conversation compacted" in text and
              re.search(rb"\033\[0;2m[^\033]*Conversation compacted",
                        v.out[mark:]), v.out[mark:][-800:])
        v.key(b"j", 0.3)  # #358: the newest on top; down past the notification
        v.key(b"j", 0.3)
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("Prompts: Enter on a shown compaction reads its summary",
              v.wait(b"the compact summary", start=mark), v.out[mark:])
        v.key(b"\033", 0.5)
        v.key(b"q")
        check("Prompts: q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()
    v = Pty([DCH, "--xray", "xv-ca"])
    try:
        v.wait(b"Prompts")
        v.pump(0.8)
        text = screen(v, 0)
        check("Prompts: only a compaction and an auto chapter: the empty "
              "state of xv-aa", "Only auto rows: n shows them" in text
              and "Prompts 0" in text and "n auto rows 2 hidden" in text,
              text)
        v.key(b"q")
        v.exit()
    finally:
        v.close()


def test_gone():
    """A prompt the transcript no longer holds when it is read (a stale
    anchor; for a shell, the ring evicted it): the engine says "chapter
    gone" (kept for --json), the view says it in its own word, never
    "chapter" (#295, QA 1.63.0 LOW-V17). Opening it goes back to the
    list; turning to it with ] keeps the reader."""
    cfg = os.path.join(HOME, ".claude")
    tr = os.path.join(cfg, "projects", "-home-demo-proj", GSID + ".jsonl")
    sockdir = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    os.makedirs(os.path.dirname(tr), exist_ok=True)
    os.makedirs(hdir, exist_ok=True)
    rec = {"name": "xv-gn", "cwd": P, "end": "exited", "argv": ["claude"],
           "started": 1794299400, "updated": 1794300000,
           "ended": 1794300000, "sock_dir": sockdir,
           "agent": {"session_id": GSID, "cwd": P, "config_dir": cfg}}
    with open(os.path.join(hdir, "xv-gn.json"), "w") as f:
        f.write(json.dumps(rec) + "\n")
    full = gone_lines(["the first ask", "the second ask"])
    for what, keys in (("Enter on it", [b"\r"]),
                       ("] to it", [b"j", b"\r", b"]"])):
        with open(tr, "w") as f:
            f.write(full)
        v = Pty([DCH, "--xray", "xv-gn"])
        try:
            v.wait(b"the second ask")
            v.pump(0.8)
            for k in keys[:-1]:
                v.key(k, 0.5)
            if keys[:-1]:
                v.wait(b"reply 0")
            # the second prompt's anchor goes: a shorter transcript
            with open(tr, "w") as f:
                f.write(gone_lines(["other"]))
            mark = len(v.out)
            v.key(keys[-1], 1.0)
            ok = until(lambda: (v.pump(0.1), "is gone" in
                                screen(v, mark))[1], 5)
            text = screen(v, mark)
            check("LOW-V17: %s, a prompt gone: the view says \"X-ray: that "
                  "prompt is gone\", never \"chapter\"" % what,
                  ok and "X-ray: that prompt is gone" in text and
                  "chapter" not in text, text)
            v.key(b"q", 0.3)
            v.key(b"q")
            v.exit()
        finally:
            v.close()
    with open(tr, "w") as f:
        f.write(full)
    a = json.loads(dch("--xray", "xv-gn", "--json").stdout)["chapters"]
    with open(tr, "w") as f:
        f.write(gone_lines(["other"]))
    r = dch("--xray", "xv-gn", "--chapter", a[-1]["anchor"], "--json")
    check("LOW-V17: --chapter's own text stays \"dch: chapter gone\" for "
          "API clients", r.returncode == 1 and r.stderr ==
          "dch: chapter gone\n", (r.returncode, r.stderr))


def test_responses():
    o = json.loads(dch("--xray", "xv-rs", "--json").stdout)
    ch = o["chapters"]
    check("Responses fixture: the engine's answers, the cut one, the tools, "
          "none for the turn that ended on a call",
          [bool(c.get("response")) for c in ch] ==
          [True, True, True, True, False] and ch[3].get("response_cut") and
          "Edit 1" in ch[0].get("tools", "") and
          ch[4].get("tools") == "Read 1", ch)
    v = Pty([DCH, "--xray", "xv-rs"], rows=44, cols=100)
    try:
        v.wait(b"Prompts 4")
        v.pump(0.5)
        mark = len(v.out)
        v.key(b"\t", 1.0)
        scr = rows(v.out)
        tabs = scr.get(2, "")
        check("Responses: Tab from Prompts opens it; the tabs Prompts, "
              "Responses, Refs in that order, each with its count",
              0 <= tabs.find("Prompts 4") < tabs.find("Responses 4") <
              tabs.find("Refs"), tabs)
        it = items(scr)
        check("Responses: newest first, the auto row hidden",
              [h for h, _ in it][:4] ==
              ["the last, ended mid-tool", "write a big one",
               "explain at length", "fix the parser"], it)
        check("Responses: the newest item is selected",
              "the last, ended mid-tool" in head(v), head(v))
        d = dict(it)
        check("Responses: a turn that ended on a call: its tool line, no "
              "text, no working line (an ended session)",
              d.get("the last, ended mid-tool") == ["Read 1"], d)
        check("Responses: the tool line, then the answer's lines",
              d.get("fix the parser", [""])[0].startswith("Edit 1 (1 file") and
              d["fix the parser"][1:] == ["Fixed the parser.",
                                          "All 12 tests pass."], d)
        check("Responses: a long answer: 8 lines, then a faint ellipsis",
              d.get("explain at length") ==
              LONGA.split("\n")[:8] + ["\u2026"], d)
        check("Responses: hostile bytes in an answer show as ?",
              d.get("write a big one", [""])[0] == "BIG ?]0;pwned? evil?end",
              d)
        text = screen(v, 0)
        check("Responses: the footer names Enter read, filter, n and tabs",
              "read" in text and "/ filter" in text and
              "n auto rows 1 hidden" in text and "tabs" in text, text)
        v.key(b"n", 1.0)
        it = items(rows(v.out))
        check("Responses: n shows the auto row, its summary as the title, "
              "the selection kept", any("Build finished" in h and
                                        b == ["noticed the build"]
                                        for h, b in it) and
              "the last, ended mid-tool" in head(v), (it, head(v)))
        v.key(b"n", 1.0)
        v.key(b"j", 0.5)
        check("Responses: j selects the next (older) item",
              "write a big one" in head(v), head(v))
        v.resize(20, 100)   # the chapter longer than the window
        v.pump(0.5)
        mark = len(v.out)
        v.key(b"\r", 1.5)
        v.wait(b"Reply", start=mark)
        v.pump(0.5)
        scr = rows(v.out[mark:])
        check("Responses: Enter reads that prompt, at its answer (the "
              "reader's first row is the Reply)",
              scr.get(2, "").startswith("4 of 5") and
              scr.get(4, "").startswith("Reply") and
              "BIG ?]0;pwned" in scr.get(5, ""), scr)
        mark = len(v.out)
        v.key(b"q", 1.0)
        v.resize(44, 100)
        v.pump(0.5)
        check("Responses: q goes back to the item it came from",
              v.wait(b"Responses 4", start=mark) and
              "write a big one" in head(v), head(v))
        v.key(b"j", 0.5)
        mark = len(v.out)
        v.key(b"\r", 1.5)
        v.wait(b"Reply", start=mark)
        v.pump(0.5)
        scr = rows(v.out[mark:])
        r = [k for k in scr if scr[k].startswith("Reply")]
        check("Responses: Enter on the long answer reads it, its first line "
              "under its Reply", r and
              "line 01 of the long answer" in scr.get(r[0] + 1, ""), scr)
        v.key(b"\033", 1.0)
        v.key(b"/line 05", 1.0)
        it = items(rows(v.out))
        check("Responses: / matches an answer's text",
              [h for h, _ in it] ==
              ["explain at length"], it)
        v.key(b"\033", 0.5)
        v.key(b"G", 0.5)
        check("Responses: G selects the oldest", "fix the parser" in head(v),
              head(v))
        v.key(b"g", 0.5)
        check("Responses: g the newest",
              "the last, ended mid-tool" in head(v), head(v))
        mark = len(v.out)
        v.key(b"\t", 1.0)
        check("Responses: Tab goes on to Refs", v.wait(b"[Refs", start=mark)
              or b"Pull requests, issues" in v.out[mark:], v.out[mark:][-400:])
        mark = len(v.out)
        v.key(b"\t", 1.0)
        check("Refs: Tab goes on to Search (#291)",
              v.wait(b"[Search]", start=mark), v.out[mark:][-400:])
        mark = len(v.out)
        v.key(b"\t", 1.0)
        check("Search: Tab wraps to Prompts", v.wait(b"[Prompts", start=mark),
              v.out[mark:][-400:])
        mark = len(v.out)
        v.key(b"\033[Z\033[Z", 1.0)
        v.key(b"h", 1.0)
        check("Shift-Tab from Prompts to Search, again to Refs, h back to "
              "Responses",
              v.wait(b"[Responses", start=mark) and
              "the last, ended mid-tool" in head(v), v.out[mark:][-400:])
        v.key(b"q")
        check("Responses: q exits 0", v.exit() == 0, v.out[-80:])
        bad = [b for b in (b"\033]0;pwned", b"\007 evil", b"\xe2\x80\xae")
               if b in v.out]
        check("Responses: no answer ESC, BEL or bidi byte reached the "
              "terminal", not bad, bad)
    finally:
        v.close()
    # wide: the cut answer's 2048 bytes fit in 8 lines, so only
    # response_cut puts the ellipsis there
    v = Pty([DCH, "--xray", "xv-rs"], rows=50, cols=300)
    try:
        v.wait(b"Prompts 4")
        v.pump(0.5)
        v.key(b"\t", 1.0)
        d = dict(items(rows(v.out)))
        big = d.get("write a big one", [])
        check("Responses: an answer the engine cut ends in an ellipsis "
              "though its lines fit", len(big) == 9 and big[-1] == "\u2026"
              and big[-2].startswith("y"), big)
        check("Responses: a whole answer has none",
              d.get("fix the parser", [""])[-1] == "All 12 tests pass.", d)
        v.key(b"q")
        v.exit()
    finally:
        v.close()


def test_responses_live():
    """xv-lv: a live Claude (a sleep with a session record): its running
    turn says working; --follow's re-sent chapters keep the selection on
    its prompt, the first item on the first."""
    cwd = os.path.realpath(T)
    path = os.path.join(HOME, ".claude", "projects", cwd.replace("/", "-"),
                        VSID + ".jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for o in (turn("first live prompt", "2026-11-10T10:00:00Z",
                       say("answer one")) +
                  turn("second, running", "2026-11-10T11:00:00Z",
                       call("b1", "Bash", {"command": "sleep 1"}))):
            f.write(json.dumps(dict(o, cwd=cwd) if "cwd" in o else o) + "\n")
    dch("--spawn", "xv-lv", "/bin/sleep", "300")
    pid = until(lambda: json.loads(dch("--cmd", "xv-lv").stdout or "{}")
                .get("pid"), 5)
    start = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(pid)],
                           capture_output=True, text=True,
                           env={"LC_ALL": "C", "TZ": "UTC"}).stdout.strip()
    sd = os.path.join(HOME, ".claude", "sessions")
    os.makedirs(sd, exist_ok=True)
    with open(os.path.join(sd, "%d.json" % pid), "w") as f:
        json.dump({"pid": pid, "sessionId": VSID, "cwd": cwd,
                   "procStart": start, "version": "2.1.0",
                   "kind": "interactive", "status": "busy",
                   "statusUpdatedAt": 1}, f)

    def add(*recs):
        with open(path, "a") as f:
            for o in recs:
                f.write(json.dumps(o) + "\n")

    v = Pty([DCH, "--xray", "xv-lv"], rows=40, cols=100)
    try:
        v.wait(b"Prompts 2")
        v.pump(0.5)
        v.key(b"\t", 1.0)
        it = items(rows(v.out))
        check("Responses, live: the running turn says working, under its "
              "tool line", it and it[0][0] == "second, running" and
              it[0][1] == ["Bash 1", "working\u2026"], (it, screen(v, 0)))
        check("Responses, live: the footer offers o", "o open session" in
              screen(v, 0), screen(v, 0))
        add(result("b1", "ok"), say("the second answer"))
        ok = until(lambda: (v.pump(0.1), any(
            h == "second, running" and "the second answer" in b
            for h, b in items(rows(v.out))))[1], 10)
        check("Responses, live: the answer replaces working when the turn "
              "ends (--follow re-sends the chapter)", ok, items(rows(v.out)))
        v.key(b"j", 0.5)
        add(*turn("third prompt", "2026-11-10T12:00:00Z", say("answer 3")))
        ok = until(lambda: (v.pump(0.1), any(
            h == "third prompt" for h, b in items(rows(v.out))))[1], 10)
        check("Responses, live: a new turn on top, the selection stays on "
              "its prompt", ok and "first live prompt" in head(v),
              (items(rows(v.out)), head(v)))
        v.key(b"g", 0.5)
        add(*turn("fourth prompt", "2026-11-10T13:00:00Z", say("answer 4")))
        ok = until(lambda: (v.pump(0.1), any(
            h == "fourth prompt" for h, b in items(rows(v.out))))[1], 10)
        check("Responses, live: the first item selected stays the first",
              ok and "fourth prompt" in head(v), head(v))
        # #358: Prompts the way Responses works, live
        v.key(b"\033[Z")
        ok = tabbar(v, "[Prompts") and until(
            lambda: (v.pump(0.1), "fourth prompt" in head(v))[1], 5)
        text = screen(v, 0)
        at = [text.find(x) for x in ("fourth prompt", "third prompt",
                                     "second, running", "first live prompt")]
        check("#358: Prompts, live: newest first, the newest selected",
              ok and -1 not in at and at == sorted(at), (head(v), text))
        v.key(b"j", 0.5)
        add(*turn("fifth prompt", "2026-11-10T14:00:00Z", say("answer 5")))
        ok = until(lambda: (v.pump(0.1), "fifth prompt" in screen(v, 0))[1],
                   10)
        check("#358: Prompts, live: a new prompt shows on top without a key, "
              "the selection stays on its prompt", ok and "third prompt" in
              head(v), (head(v), screen(v, 0)))
        v.key(b"g", 0.5)
        add(*turn("sixth prompt", "2026-11-10T15:00:00Z", say("answer 6")))
        ok = until(lambda: (v.pump(0.1), "sixth prompt" in head(v))[1], 10)
        check("#358: Prompts, live: the first row selected stays the first",
              ok, head(v))
        mark = len(v.out)
        v.key(b"\r")
        check("#358: Prompts: Enter opens the prompt under the cursor",
              v.wait(b"answer 6", start=mark), screen(v, mark))
        v.key(b"q")
        ok = until(lambda: (v.pump(0.1), "[Prompts" in rows(v.out).get(
            2, "") and "sixth prompt" in head(v))[1], 5)
        check("#358: Prompts: q from the reader back on its row", ok,
              head(v))
        v.key(b"\t")
        ok = until(lambda: (v.pump(0.1), items(rows(v.out))[:1] and
                            items(rows(v.out))[0][0] == "sixth prompt")[1],
                   5)
        check("#358: Responses, live: the turn added while on Prompts is "
              "the first item", ok, items(rows(v.out)))
        v.key(b"q")
        v.exit()
    finally:
        v.close()
    # #358, the jump: over the head and tail range (lowered), --follow sends
    # the capped index's snapshot, then the whole file's after a reset
    # (full); the cursor kept the first one's last index, mid-list
    v = Pty([DCH, "--xray", "xv-lv"], rows=40, cols=100,
            env={"DCH_XRAY_CAPS": "0:0:0:300:700"})
    try:
        ok = tabbar(v, "Prompts 6", 15) and until(
            lambda: (v.pump(0.1), "sixth prompt" in head(v))[1], 5)
        text = screen(v, 0)
        check("#358: Prompts after a capped then a full snapshot: the newest "
              "still selected, on the first row", ok and
              0 <= text.find("sixth prompt") < text.find("fifth prompt"),
              (head(v), text))
        v.key(b"q")
        v.exit()
    finally:
        v.close()
        dch("-k", "xv-lv")


def test_responses_codex():
    """xv-cx: a live fake Codex over the codex-0167 rollout: its answer
    and tool line on Responses, its last turn working."""
    tid = "019a1111-2222-7333-8444-555566667702"
    src = os.path.join(HERE, "xray", "fixtures", "codex-0167", "codex",
                       tid + ".jsonl")
    sess = os.path.join(HOME, ".codex", "sessions", "2026", "11", "10")
    snaps = os.path.join(HOME, ".codex", "shell_snapshots")
    os.makedirs(sess, mode=0o700, exist_ok=True)
    os.makedirs(snaps, mode=0o700, exist_ok=True)
    shutil.copy(src, os.path.join(
        sess, "rollout-2026-11-10T10-00-00-%s.jsonl" % tid))
    bindir = os.path.join(T, "bin")
    os.makedirs(bindir, exist_ok=True)
    fake = os.path.join(bindir, "codex")
    b = subprocess.run(["cc", "-o", fake, os.path.join(HERE, "fake_codex.c")],
                       capture_output=True, text=True, timeout=60)
    check("codex: the fake codex builds", b.returncode == 0, b.stderr)
    log = os.path.join(T, "codex.log")
    cwd = os.path.join(T, "work", "xv-cx")
    os.makedirs(cwd, exist_ok=True)
    r = subprocess.run([DCH, "--spawn", "xv-cx", "--size", "80x24", "--cwd",
                        cwd, fake], capture_output=True, text=True,
                       timeout=30, env=dict(ENV, FAKE_CODEX_LOG=log))
    got = until(lambda: os.path.exists(log) and
                open(log).read().endswith("\n") and open(log).read().split(),
                10)
    check("codex: the fake codex runs", r.returncode == 0 and got, r.stderr)
    marker = got[1] if got and got[1] != "-" else None
    p = os.path.join(snaps, tid + ".1.sh")
    with open(p, "w") as f:
        f.write("# Snapshot file\nexport DCH_SESSION=xv-cx\n%s" % (
            "export DCH_NATIVE_BRIDGE_ID=%s\n" % marker if marker else ""))
    os.chmod(p, 0o600)
    v = Pty([DCH, "--xray", "xv-cx"], rows=40, cols=100)
    try:
        ok = v.wait(b"Prompts 2", 15)
        v.pump(0.5)
        v.key(b"\t", 1.0)
        it = items(rows(v.out))
        check("Responses, Codex: the tab, the answer and the tool line, the "
              "running turn working", ok and len(it) == 2 and
              "Ship it" in it[0][0] and it[0][1] == ["working\u2026"] and
              it[1][1] == ["exec 1, wait 1",
                           "PR #78 fixes it (commit 9f8e7d6c)."], it)
        v.key(b"q")
        v.exit()
    finally:
        v.close()
        subprocess.run([DCH, "-k", "xv-cx"], capture_output=True, timeout=30,
                       env=ENV)


def test_tasks_codex():
    """#320: xv-cxt, a live fake Codex whose rollout has two update_plan
    calls: the Tasks tab from the newest plan, Enter reads its turn. #323:
    its AGENTS.md on Memory, the last tab; Enter shows its text."""
    tid = "019a1111-2222-7333-8444-555566667720"
    src = os.path.join(HERE, "xray", "fixtures", "codex-0167", "codex",
                       "019a1111-2222-7333-8444-555566667702.jsonl")
    sess = os.path.join(HOME, ".codex", "sessions", "2026", "11", "10")
    snaps = os.path.join(HOME, ".codex", "shell_snapshots")
    fake = os.path.join(T, "bin", "codex")

    def plan(ts, cid, steps):
        return json.dumps({"timestamp": ts, "type": "response_item",
                           "payload": {"type": "function_call",
                                       "name": "update_plan", "call_id": cid,
                                       "arguments": json.dumps({"plan": [
                                           {"step": a, "status": b}
                                           for a, b in steps]})}}) + "\n"
    with open(src) as f:
        body = f.read().replace("019a1111-2222-7333-8444-555566667702", tid)
    with open(os.path.join(
            sess, "rollout-2026-11-10T10-00-00-%s.jsonl" % tid), "w") as f:
        f.write(body + plan("2026-11-10T10:00:03.000Z", "pl1",
                            [("read the spec", "in_progress"),
                             ("port the parser", "pending")]) +
                plan("2026-11-10T10:00:04.000Z", "pl2",
                     [("read the spec", "completed"),
                      ("port the parser", "in_progress"),
                      ("test it", "pending")]))
    log = os.path.join(T, "codex-t.log")
    cwd = os.path.join(T, "work", "xv-cxt")
    os.makedirs(cwd, exist_ok=True)
    with open(os.path.join(cwd, "AGENTS.md"), "w") as f:
        f.write("codex agents text\n")
    r = subprocess.run([DCH, "--spawn", "xv-cxt", "--size", "80x24",
                        "--cwd", cwd, fake], capture_output=True, text=True,
                       timeout=30, env=dict(ENV, FAKE_CODEX_LOG=log))
    got = until(lambda: os.path.exists(log) and
                open(log).read().endswith("\n") and open(log).read().split(),
                10)
    check("Tasks, Codex: the fake codex runs", r.returncode == 0 and got,
          r.stderr)
    marker = got[1] if got and got[1] != "-" else None
    p = os.path.join(snaps, tid + ".1.sh")
    with open(p, "w") as f:
        f.write("# Snapshot file\nexport DCH_SESSION=xv-cxt\n%s" % (
            "export DCH_NATIVE_BRIDGE_ID=%s\n" % marker if marker else ""))
    os.chmod(p, 0o600)
    v = Pty([DCH, "--xray", "xv-cxt"], rows=30, cols=100)
    try:
        ok = tabbar(v, "Tasks 2", 15) and tabbar(v, "Memory 1", 15)
        v.key(b"\033[Z\033[Z", 0.8)
        r = tasks_rows(v)
        check("Tasks, Codex: the newest plan, in progress, pending, done",
              ok and [re.sub(r" (now|\d+[mhd])$", "", x).lstrip("▌")
                     for x in r] ==
              ["In progress 1", "▶ 2 port the parser", "Pending 1",
               "○ 3 test it", "Done 1", "✓ 1 read the spec"], r)
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("Tasks, Codex: Enter reads the turn of the plan",
              v.wait(b" of ", start=mark) and v.wait(b"update_plan",
                                                     start=mark),
              screen(v, mark))
        v.key(b"q", 0.8)
        v.key(b"\t", 0.8)
        r = [re.sub(r" (now|\d+[mhd])$", "", x).lstrip("▌ ")
             for x in tasks_rows(v)]
        check("Memory, Codex: its AGENTS.md under Instructions, the last tab",
              r == ["Instructions 1", "AGENTS.md"], r)
        mark = len(v.out)
        v.key(b"\r", 0.8)
        check("Memory, Codex: Enter shows the file's text",
              "codex agents text" in screen(v, mark), screen(v, mark))
        v.key(b"q", 0.5)
        v.key(b"q")
        v.exit()
    finally:
        v.close()
        subprocess.run([DCH, "-k", "xv-cxt"], capture_output=True,
                       timeout=30, env=ENV)


def test_search():
    """#291: the Search tab: type, Enter, the hits newest first with who
    and the prompt; the match stands out; Enter reads there, q back to the
    hit; a refused text; no match; ^R from the Prompts filter; more."""
    v = Pty([DCH, "--xray", "xv-se"], rows=40, cols=100,
            env={"NO_COLOR": ""})
    try:
        tabbar(v, "Prompts 3")  # its count is faint, its own SGR: no raw match
        v.pump(0.5)
        tabs = []
        for _ in range(3):
            mark = len(v.out)
            v.key(b"\t", 0.8)
            tabs.append(rows(v.out).get(2, ""))
        t = tabs[-1]
        check("Search: Tab, Tab, Tab from Prompts: Responses, Refs, then "
              "Search, the fourth tab, last and with no count before a "
              "search", 0 <= t.find("Prompts 3") < t.find("Responses 3") <
              t.find("Refs") < t.find("Search") and
              re.search(r"Search\s*$", t), tabs)
        text = screen(v, mark)
        check("Search: opens typing (no search yet), the hint and the "
              "footer's search and cancel", "Search: \u2588" in text and
              "Type a word or phrase; Enter finds where this session said "
              "it" in text and "search" in text and "cancel" in text, text)
        mark = len(v.out)
        v.key(b"needle", 0.3)
        v.key(b"\r", 0.5)
        ok = tabbar(v, "Search 2")
        v.pump(0.5)
        scr = rows(v.out)
        h, g = hits(scr), groups(scr)
        check("Search: Enter runs it; the tab counts its 2 turns (#457)",
              ok and "Search 2" in scr.get(2, ""), scr.get(2, ""))
        check("Search: the text's line stays above the hits, typing ended",
              scr.get(4, "").strip() == "Search: needle", scr.get(4))
        check("Search: one group per turn, newest first, its head the "
              "prompt's number and title, each place under it by who said "
              "it; the You hit that is its title is no line (#457)",
              [(n, t, [w for w, _ in ps]) for n, t, ps in g] ==
              [("#3", "a long chapter", ["Reply"]),
               ("#1", "first: where is the Needle kept?",
                ["Reply", "Bash", "Bash"])], g)
        check("Search: each place's snippet on its line, the turn's time on "
              "its head",
              len(h) == 4 and "the deep NEEDLE line" in " ".join(h[0][2])
              and "src/a.c: needle here" in " ".join(h[2][2]) and
              re.search(r"11-10 \d\d:06", next((r for r in scr.values()
                                               if "a long chapter" in r), "")),
              (h, scr))
        check("Search: the matched text stands out (accent bold), in any "
              "case, the rest plain",
              re.search(rb"the deep \033\[0;1;38;5;\d+mNEEDLE\033\[0m line",
                        v.out) and
              re.search(rb"The \033\[0;1;38;5;\d+mneedle\033\[0m is",
                        v.out), v.out[-3000:])
        check("Search: the newest hit is selected", "Reply" in head(v) and
              "line 51" in head(v), head(v))
        text = screen(v, 0)
        check("Search: the footer names Enter read, / search, o not "
              "(ended), tabs", "read" in text and "/ search" in text and
              "tabs" in text and "open session" not in text, text)
        mark = len(v.out)
        v.key(b"\r", 1.0)
        v.wait(b"NEEDLE", start=mark)
        v.pump(0.5)
        scr = rows(v.out[mark:])
        text = "\n".join(scr[k] for k in sorted(scr))
        check("Search: Enter reads the hit's prompt, scrolled to the text "
              "(deep in the chapter)", scr.get(2, "").startswith("3 of 3")
              and "the deep NEEDLE line" in text and
              "filler line 00" not in text, text)
        mark = len(v.out)
        v.key(b"q", 1.0)
        check("Search: q back to the same hit",
              tabbar(v, "Search 2") and "line 51" in head(v)
              and "Search: needle" in rows(v.out).get(4, ""), head(v))
        v.key(b"j", 0.5)
        check("Search: j onto the next turn's head, its prompt's hit",
              "first: where is the Needle kept?" in head(v), head(v))
        v.key(b"jj", 0.5)
        check("Search: j by place", "Bash" in head(v) and
              "src/a.c: needle here" in head(v), head(v))
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("Search: Enter on the tool result reads its prompt at it",
              v.wait(b"1 of 3", start=mark) and
              v.wait(b"src/a.c: needle here", start=mark), v.out[mark:])
        mark = len(v.out)
        v.key(b"q", 1.0)
        v.pump(0.3)
        check("Search: q back to that hit, the third",
              [r for r in rows(v.out).values() if r.startswith("\u258c")] and
              len(hits(rows(v.out))) > 2 and
              hits(rows(v.out))[2][0] == "Bash" and
              "src/a.c: needle here" in head(v),
              (head(v), hits(rows(v.out))))
        v.key(b"G", 0.5)
        check("Search: G the oldest", "grep -r needle" in head(v), head(v))
        v.key(b"g", 0.5)
        check("Search: g the newest", "line 51" in head(v), head(v))
        mark = len(v.out)
        v.key(b"/\x15x\r", 1.0)
        text = screen(v, mark)
        check("Search: a refused text (1 byte) is said in the status line, "
              "nothing run, the hits kept",
              "bad search: 2 to 200 bytes, no control character" in text and
              "Search 2" in rows(v.out).get(2, ""), text)
        mark = len(v.out)
        v.key(b"\033", 1.0)
        check("Search: Esc leaves typing, the text back to the last search",
              rows(v.out).get(4, "").strip() == "Search: needle" and
              len(hits(rows(v.out))) == 4, rows(v.out).get(4))
        mark = len(v.out)
        v.key(b"/\x15qwertyzz\r", 1.0)
        v.wait(b"No match", start=mark)
        text = screen(v, mark)
        check("Search: no hit: No match for the text, the tab 0",
              'No match for "qwertyzz"' in text and
              "Search 0" in rows(v.out).get(2, ""), text)
        mark = len(v.out)
        v.key(b"\t", 1.0)
        # colour: the chosen tab alone has a background (SGR 0;..;bg)
        check("Search: Tab wraps to Prompts", until(lambda: (v.pump(0.1),
              re.search(rb"\033\[0;[0-9;]*m Prompts", v.out[mark:]))[1]),
              screen(v, mark))
        mark = len(v.out)
        v.key(b"\033[Z", 1.0)
        text = screen(v, mark)
        check("Search: Shift-Tab back: the text and its answer kept, not "
              "typing", "Search: qwertyzz" in text and
              "Search: qwertyzz\u2588" not in text and
              'No match for "qwertyzz"' in text, text)
        v.key(b"\t", 1.0)
        mark = len(v.out)
        v.key(b"/filler line", 1.0)
        text = screen(v, mark)
        check("Search: words in the Prompts filter that no title has: the "
              "empty line and the footer say ^R searches",
              "No match; ^R searches the session for it" in text and
              "^R search" in text, text)
        mark = len(v.out)
        v.key(b"\x12", 1.0)
        ok = tabbar(v, "Search 1")
        v.pump(0.3)
        h = hits(rows(v.out))
        check("Search: ^R lands on Search with the words and their hit",
              ok and "Search: filler line" in screen(v, mark) and
              [(w, t) for w, t, _ in h] == [("Reply", "a long chapter")], h)
        v.key(b"q")
        check("Search: q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()
    v = Pty([DCH, "--xray", "xv-sm"], rows=30, cols=100)
    try:
        v.wait(b"Prompts 55")
        v.pump(0.3)
        mark = len(v.out)
        v.key(b"\033[Z", 0.5)
        v.key(b"zebra\r", 1.0)
        ok = v.wait(b"Search 50+", start=mark)
        v.pump(0.5)
        text = screen(v, mark)
        g = groups(rows(v.out))
        check("Search: over 50 turns: the tab says 50+, the newest first, a "
              "faint last line says the 50 newest prompts are shown", ok and
              "the 50 newest prompts are shown" in text and g and
              g[0][:2] == ("#55", "zebra prompt 54") and not g[0][2],
              (g, text))
        v.key(b"G", 0.5)
        check("Search: G selects the 50th", "zebra prompt 05" in head(v),
              head(v))
        v.key(b"q")
        v.exit()
    finally:
        v.close()


def fixture_tail():
    """xv-tl8: a short answer, then a 14 KB final one ending in TAILMARK
    (#451 cuts it to head + tail)."""
    big = "\n".join("paragraph %03d of the long final answer, filler words"
                    % i for i in range(1, 260))
    big += "\n\nNext steps: ship it. TAILMARK"
    record("xv-tl8", TSID, turn("short one", "2026-11-10T10:00:00Z",
                                say("Short answer.")) +
           turn("the long one", "2026-11-10T10:10:00Z", say(big)))


def test_tail():
    ch = json.loads(dch("--xray", "xv-tl8", "--json").stdout)["chapters"]
    check("tail fixture: the 14 KB answer is cut by the engine, the short "
          "one not", ch[1].get("response_cut") and
          not ch[0].get("response_cut"), ch)
    v = Pty([DCH, "--xray", "xv-tl8"], rows=70, cols=100,
            env={"NO_COLOR": ""})
    try:
        v.wait(b"Prompts 2")
        v.pump(0.5)
        v.key(b"\t", 1.0)
        raw = bytes(v.out)
        scr = rows(v.out)
        it = dict(items(scr))
        long_ = it.get("the long one", [])
        check("Responses: the newest item shows the head, a lone ellipsis "
              "and the tail (TAILMARK), more than 8 lines",
              long_ and long_[0].startswith("paragraph 001") and
              "\u2026" in long_ and long_[-1].endswith("TAILMARK") and
              len(long_) > 9, long_)
        check("Responses: the joiner ellipsis is faint",
              re.search(rb"\033\[0;2m +\xe2\x80\xa6", raw) is not None,
              raw[-600:])
        check("Responses: the short answer stays as it was",
              it.get("short one") == ["Short answer."], it)
        mark = len(v.out)
        v.key(b"\r", 1.5)
        v.wait(b"Reply", start=mark)
        v.pump(0.5)
        v.key(b"G", 0.8)
        mark = len(v.out)
        v.key(b"g", 0.5)
        v.key(b"G", 0.8)
        raw = bytes(v.out[mark:])
        scr = rows(v.out[mark:])
        text = "\n".join(scr.values())
        check("reader: G shows TAILMARK", "TAILMARK" in text, text)
        check("reader: one cut line, faint (not plain text)",
              len(re.findall(r"\d+ characters cut", text)) == 1 and
              re.search(rb"\033\[0;2m +\xe2\x80\xa6 \d+ characters cut "
                        rb"\xe2\x80\xa6", raw) is not None, (text, raw))
        v.key(b"q", 0.3)
        v.key(b"q")
        v.exit()
    finally:
        v.close()


LSID = "11111111-2222-4333-8444-5555555555e4"   # xv-lk: look up what you read
LK = ["alpha task", "bravo task", "charlie task", "delta task", "echo task",
      "foxtrot task", "golf 5 holes", "wrap it up"]


def fixture_look():
    """xv-lk (#454): eight turns; the last answer has no ref in its first
    30 lines, "jitter" on lines 32 and 50, then #377 and, last, PR #418;
    the second answer names #377, then PAY-219 on its next line."""
    body = ["Summary of the work."]
    body += ["point %02d: nothing to see here" % i for i in range(1, 31)]
    body += ["the backoff jitter is 20 percent"]
    body += ["point %02d: more filler words" % i for i in range(32, 50)]
    body += ["Jitter stays off in tests", "- #377 is still open.",
             "Then merge PR #418."]
    lines = []
    for i, p in enumerate(LK):
        lines += turn(p, "2026-11-10T1%d:00:00Z" % i,
                      say("\n".join(body) if i == len(LK) - 1
                          else "first #377\nthen PAY-219" if i == 1
                          else "answer to %s" % p))
    os.makedirs(os.path.dirname(tl(LSID)), exist_ok=True)
    record("xv-lk", LSID, lines)


def body_rows(v):
    """The rows between the tab line and the rule, as now on screen."""
    scr = rows(v.out)
    out = []
    for k in sorted(x for x in scr if x >= 4):
        if scr[k].startswith(("\u2500", "-")):
            break
        out.append(scr[k].rstrip())
    return out


def shows(v, fn, secs=10.0):
    """Read until fn(the screen's rows) holds, by a deadline."""
    end = time.time() + secs
    while time.time() < end:
        if fn(rows(v.out)):
            return True
        v.pump(0.1)
    return bool(fn(rows(v.out)))


def test_look():
    v = Pty([DCH, "--xray", "xv-lk"], rows=30, cols=100,
            env={"NO_COLOR": ""})
    try:
        v.wait(b"Prompts 8")
        v.pump(0.3)
        v.key(b"\t", 1.0)
        v.key(b"/5", 0.5)
        filt = "\n".join(rows(v.out).values())
        check("#454: /5 still filters while typed (golf 5 holes shown)",
              "golf 5 holes" in filt, filt)
        v.key(b"\r", 0.5)
        check("#454: /5 Enter on Responses selects item 5",
              shows(v, lambda s: "echo task" in head(v) and
                    re.match(r"^.\s*5  ", head(v))), head(v))
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("#454: Enter then reads prompt 5",
              v.wait(b"5 of 8", start=mark), v.out[mark:][-400:])
        v.key(b"q", 0.5)
        v.key(b"/2\r", 0.5)
        shows(v, lambda s: "bravo task" in head(v))
        mark = len(v.out)
        v.key(b"r", 0.5)
        check("#454: r on a Responses item lists its refs, the lowest "
              "first", shows(v, lambda s: any(
                  r.startswith("\u258c") and "PAY-219" in r
                  for r in s.values()) and any("#377" in r for r in
                                               s.values()) and
                  "refs in view, the lowest first" in "".join(s.values())),
              screen(v, mark))
        v.key(b"\033", 0.5)
        v.key(b"g", 0.5)
        mark = len(v.out)
        v.key(b"r", 0.5)
        check("#454: r on an item with no ref in its lines says so",
              shows(v, lambda s: any("no refs in view" in r
                                     for r in s.values())) and
              v.reap() is None, screen(v, mark))
        mark = len(v.out)
        v.key(b"\r", 1.0)
        v.wait(b"Reply", start=mark)
        v.pump(0.3)
        v.key(b"G", 0.8)
        shows(v, lambda s: any("PR #418" in r for r in s.values()))
        before = body_rows(v)
        mark = len(v.out)
        v.key(b"r", 0.5)
        check("#454: r in the reader opens the strip, the lowest ref "
              "selected", shows(v, lambda s: any(
                  r.startswith("\u258c") and "#418" in r
                  for r in s.values())), screen(v, mark))
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("#454: Enter opens the card of the lowest ref on screen",
              shows(v, lambda s: "#418" in s.get(2, "") and
                    "light" in s.get(2, "")),
              rows(v.out).get(2))
        v.key(b"q", 0.8)
        v.key(b"q", 0.8)
        check("#454: q q returns to the reader at the same line",
              shows(v, lambda s: body_rows(v) == before), (before,
                                                           body_rows(v)))
        v.key(b"g", 0.8)
        mark = len(v.out)
        v.key(b"r", 0.8)
        check("#454: no ref in view: r says so, faint, the reader stays",
              shows(v, lambda s: any("no refs in view" in r
                                     for r in s.values())) and
              re.search(rb"\033\[0;2m[^\033]*no refs in view",
                        v.out[mark:]) is not None and
              "Summary of the work." in screen(v, 0), v.out[mark:][-400:])
        assert "jitter" not in screen(v, 0).lower(), screen(v, 0)
        mark = len(v.out)
        v.key(b"/", 0.3)
        v.key(b"jitter", 0.3)
        v.key(b"\r", 0.8)
        check("#454: / jitter Enter moves to the first line holding it",
              shows(v, lambda s: any("backoff jitter" in r
                                     for r in s.values()) and
                    any("jitter: 1 of 2" in r for r in s.values())),
              screen(v, mark))
        check("#454: the match stands out",
              re.search(rb"backoff \033\[0;1[0-9;]*mjitter\033\[0m is",
                        v.out[mark:]) is not None,
              v.out[mark:][-600:])
        mark = len(v.out)
        v.key(b"n", 0.8)
        check("#454: n moves to the next (case folded)",
              shows(v, lambda s: any("Jitter stays off" in r
                                     for r in s.values()) and
                    any("jitter: 2 of 2" in r for r in s.values())),
              screen(v, mark))
        mark = len(v.out)
        v.key(b"N", 0.8)
        check("#454: N goes back to the previous",
              shows(v, lambda s: any("jitter: 1 of 2" in r
                                     for r in s.values())), screen(v, mark))
        mark = len(v.out)
        v.key(b"\033", 0.8)
        check("#454: Esc clears the find, the reader stays",
              shows(v, lambda s: not any(r.startswith("/jitter")
                                         for r in s.values()) and
                    any("next prompt" in r for r in s.values())),
              screen(v, mark))
        v.key(b"q", 0.3)
        v.key(b"q")
        v.exit()
    finally:
        v.close()


WSID = "11111111-2222-4333-8444-5555555555f6"


def fixture_wrap():
    """xv-wr (#456): one answer of 7-character words, one of 5-character
    CJK words (10 cells each), one 200-character word with text around."""
    w7 = " ".join("w%06d" % i for i in range(1, 80))
    cj = " ".join("\u65e5\u672c\u8a9e\u65e5\u672c" for _ in range(30))
    ts = "2026-11-10T1%d:00:00Z"
    os.makedirs(os.path.dirname(tl(WSID)), exist_ok=True)
    record("xv-wr", WSID,
           turn("seven", ts % 0, at(say(w7), ts % 0)) +
           turn("wide", ts % 1, at(say(cj), ts % 1)) +
           turn("huge", ts % 2, at(say("lead " + "x" * 200 + " tail"),
                                   ts % 2)) +
           turn("early", ts % 3, at(say("Opened PR #412 early on " +
                                        " ".join("v%06d" % i
                                                 for i in range(60)) +
                                        " " +
                                        " ".join("caf\u00e9%04d" % i
                                                 for i in range(60))),
                                    ts % 3)))


def wrap_rows(v, skip):
    """The text rows between the tab line and the rule, trimmed of the
    gutter, those that do not hold one of the fixture's words dropped."""
    out = []
    for r in body_rows(v):
        t = r.strip()
        if t and not t.startswith(skip) and re.fullmatch(r"[\w\u2026. ]+", t):
            out.append(t)
    return out


def test_wrap():
    """#456 (audit gap 6): rows break at the last space that fits; a hard
    break only for a word wider than the row; Search snippets whole words
    with a leading ellipsis."""
    v = Pty([DCH, "--xray", "xv-wr"], rows=40, cols=100,
            env={"NO_COLOR": ""})
    try:
        v.wait(b"Prompts 4")
        v.pump(0.3)
        v.key(b"\t", 1.0)
        v.key(b"j", 0.3)
        shows(v, lambda s: "huge" in head(v))
        v.pump(0.5)
        hx = [t for t in wrap_rows(v, ("huge", "wide"))
              if "x" in t or t == "lead"]
        check("#456: a word wider than the row breaks hard: 200 x all "
              "shown, rows within 92 cells, the lead on its own row",
              "".join(hx).count("x") == 200 and "lead" in hx and
              all(cells(t) <= 92 for t in hx) and
              hx[0] == "lead" and hx[-1].endswith(" tail"), hx)
        v.key(b"j", 0.5)
        shows(v, lambda s: "wide" in head(v))
        v.pump(0.5)
        cj = [t for t in wrap_rows(v, ("wide",)) if "\u65e5" in t]
        check("#456: wide CJK counts 2 cells: rows within 92 cells, ending "
              "on a whole word",
              len(cj) >= 2 and all(cells(t) <= 92 for t in cj) and
              all(t.split()[-1] == "\u65e5\u672c\u8a9e\u65e5\u672c"
                  for t in cj), cj)
        v.key(b"j", 0.5)
        shows(v, lambda s: "seven" in head(v))
        v.pump(0.5)
        rs = [t for t in wrap_rows(v, ("seven",))
              if t.startswith("w0")]
        toks = " ".join(rs).split()
        check("#456: Responses: every row of 7-character words ends on a "
              "whole word (the break at a space), more than one row",
              len(rs) >= 2 and toks and
              all(re.fullmatch(r"w\d{6}", t) for t in toks),
              (rs, body_rows(v)))
        mark = len(v.out)
        v.key(b"\r", 1.0)
        v.wait(b"1 of 4", start=mark)
        v.pump(0.5)
        rs = [t for t in wrap_rows(v, ("seven",)) if t.startswith("w0")]
        toks = " ".join(rs).split()
        check("#456: the reader: rows end on a whole word, all 79 words "
              "present, a row at most 98 cells",
              len(toks) == 79 and len(rs) >= 2 and
              all(re.fullmatch(r"w\d{6}", t) for t in toks) and
              all(cells(t) <= 98 for t in rs), (rs, body_rows(v)))
        v.key(b"q", 0.5)
        v.key(b"\t", 0.8)
        v.key(b"\t", 0.8)
        v.key(b"w000040\r", 0.5)
        tabbar(v, "Search 1")
        v.pump(0.5)
        h = hits(rows(v.out))
        sn = h[0][2] if h else []
        toks = " ".join(sn).split()
        check("#456: Search: the snippet is whole words, a leading "
              "ellipsis, no word cut at its ends",
              h and re.fullmatch(r"(\u2026|\.\.\.)", toks[0]) and
              all(re.fullmatch(r"w\d{6}", t) for t in toks[1:]) and
              "w000040" in toks,
              (h, rows(v.out)))
        v.key(b"/\x15#412\r", 0.5)
        tabbar(v, "Search 1")
        v.pump(0.5)
        h = hits(rows(v.out))
        sn = " ".join(h[0][2] if h else [])
        check("#456: Search: a match in the first words of a long text "
              "shows its first word, no leading ellipsis",
              sn.startswith("Opened PR #412 early on v000000"), sn)
        # a window that starts inside a word with a non-ASCII letter: the
        # word test must read bytes unsigned (char is signed on arm64 macOS)
        v.key("/\x15caf\u00e90030\r".encode(), 0.5)
        tabbar(v, "Search 1")
        v.pump(0.5)
        h = hits(rows(v.out))
        toks = " ".join(h[0][2] if h else []).split()
        check("#456: Search: a window starting inside caf\u00e9NNNN words "
              "begins on a whole word",
              h and re.fullmatch(r"(\u2026|\.\.\.)", toks[0]) and
              all(re.fullmatch(r"caf\u00e9\d{4}|v\d{6}", t)
                  for t in toks[1:]) and "caf\u00e90030" in toks,
              (h, rows(v.out)))
        v.key(b"\033", 0.3)
        v.key(b"q", 0.3)
        v.exit()
    finally:
        v.close()


GSID = "11111111-2222-4333-8444-555555555701"   # xv-gr: shadow mode
CSID = "11111111-2222-4333-8444-555555555702"   # xv-gc: 60 turns


def fixture_groups():
    """xv-gr (audit gap 7): shadow mode said 8 times in 6 turns, a prompt
    and its reply in 3 of them, among turns that do not say it; xv-gc: 60
    turns saying kiwi in the prompt and the reply."""
    said = (("what is shadow mode in our flags", "shadow mode in "
             "config/flags.yml logs the differences."),
            ("unrelated: the cart session", "nothing here"),
            ("put the retry backoff behind shadow mode", "Added shadow_mode: "
             "true; the backoff runs in shadow."),
            ("lunch break, summarize", "PAY-219 parked; shadow mode on."),
            ("ship the ledger fix in shadow mode first", "Ledger fix runs "
             "under shadow mode in config/flags.yml."),
            ("how long should shadow mode run", "Two days of clean runs."),
            ("anything else open", "PAY-219 can close after shadow mode is "
             "off."),
            ("one more unrelated", "ok"))
    os.makedirs(os.path.dirname(tl(GSID)), exist_ok=True)
    record("xv-gr", GSID, sum((turn(p, "2026-11-16T15:%02d:00Z" % i,
                                    at(say(r), "2026-11-16T15:%02d:30Z" % i))
                               for i, (p, r) in enumerate(said)), []))
    record("xv-gc", CSID, sum((turn("kiwi prompt %02d" % i,
                                    "2026-11-10T11:%02d:00Z" % i,
                                    say("kiwi reply %02d" % i))
                               for i in range(60)), []))


def test_groups():
    """#457: shadow mode's 8 hits are 6 groups, the tab counts turns, the
    You hits that repeat their titles are no lines; 60 turns that each hit
    twice: the 50 newest turns, more."""
    v = Pty([DCH, "--xray", "xv-gr"], rows=50, cols=100)
    try:
        v.wait(b"Prompts 8")
        v.pump(0.3)
        v.key(b"\033[Z", 0.5)
        v.key(b"shadow mode\r", 1.0)
        ok = tabbar(v, "Search 6")
        v.pump(0.5)
        g = groups(rows(v.out))
        check("#457: shadow mode: 6 groups, newest first, the tab says "
              "Search 6", ok and [n for n, _, _ in g] ==
              ["#7", "#6", "#5", "#4", "#3", "#1"], (g, rows(v.out).get(2)))
        # #3's reply "Added shadow_mode: true…" holds both words: a hit since
        # the words match in any order (#499), not only as the phrase
        check("#457: a You hit that is its title is no line; a reply's hit "
              "is a Reply line under its turn",
              [[w for w, _ in ps] for _, _, ps in g] ==
              [["Reply"], [], ["Reply"], ["Reply"], ["Reply"], ["Reply"]] and
              g[2][1] == "ship the ledger fix in shadow mode first" and
              g[2][2][0][1][0].startswith("Ledger fix runs"), g)
        v.key(b"q")
        v.exit()
    finally:
        v.close()
    v = Pty([DCH, "--xray", "xv-gc"], rows=30, cols=100)
    try:
        v.wait(b"Prompts 60")
        v.pump(0.3)
        mark = len(v.out)
        v.key(b"\033[Z", 0.5)
        v.key(b"kiwi\r", 1.0)
        ok = v.wait(b"Search 50+", start=mark)
        v.pump(0.5)
        text = screen(v, mark)
        g = groups(rows(v.out))
        check("#457: 60 turns, 120 hits: the tab says 50+ (turns), the "
              "newest turn first with its reply, the 50 newest prompts "
              "said", ok and "the 50 newest prompts are shown" in text and
              g and g[0][:2] == ("#60", "kiwi prompt 59") and
              [w for w, _ in g[0][2]] == ["Reply"], (g, text))
        v.key(b"G", 0.5)
        g = groups(rows(v.out))
        check("#457: G: the 50th turn last, its reply kept (the cap counts "
              "turns, not hits)", "kiwi reply 10" in head(v) and g and
              g[-1][:2] == ("#11", "kiwi prompt 10"), (head(v), g))
        v.key(b"q")
        v.exit()
    finally:
        v.close()


UCSID = "11111111-2222-4333-8444-555555555703"   # xv-uc: 60 turns x 12 hits
UBSID = "11111111-2222-4333-8444-555555555704"   # xv-ub: snippet breaks


def fixture_uc():
    """NIT-UC2: 60 turns, a prompt and 11 answers each saying kiwi; NIT-UC3:
    a text with NBSP between words, a long word before the match."""
    os.makedirs(os.path.dirname(tl(UCSID)), exist_ok=True)
    record("xv-uc", UCSID, sum((turn("kiwi prompt %02d" % i,
                                     "2026-11-10T11:%02d:00Z" % i,
                                     *[say("kiwi reply %02d.%02d" % (i, j))
                                       for j in range(11)])
                                for i in range(60)), []))
    nb = "\u00a0".join("w%06d" % i for i in range(60))
    record("xv-ub", UBSID,
           turn("nbsp", "2026-11-10T12:00:00Z",
                say(nb + "\u00a0ZEBRA " + nb)) +
           turn("long", "2026-11-10T12:01:00Z",
                say("x" * 300 + " QUOKKA tail")))


def test_uc():
    """NIT-UC2: the 500-hit cap keeps whole turns; NIT-UC3: NBSP is a word
    break, a head fragment longer than the window is dropped."""
    o = json.loads(dch("--xray", "xv-uc", "--search", "kiwi",
                       "--json").stdout)
    h = o["hits"]
    last = h[-1]["chapter"] if h else None
    check("NIT-UC2: 60 turns x 12 hits: the oldest kept turn is whole (12 "
          "hits), more", o.get("more") and len(h) == 41 * 12 and
          sum(1 for x in h if x["chapter"] == last) == 12,
          (len(h), o.get("more")))
    v = Pty([DCH, "--xray", "xv-uc"], rows=30, cols=100)
    try:
        v.wait(b"Prompts 60")
        v.pump(0.3)
        mark = len(v.out)
        v.key(b"\033[Z", 0.5)
        v.key(b"kiwi\r", 1.0)
        ok = v.wait(b"Search 41+", start=mark)
        check("NIT-UC2: the tab counts the 41 whole turns", ok,
              screen(v, mark))
        v.key(b"q")
        v.exit()
    finally:
        v.close()
    for q, want in (("ZEBRA", lambda t: t.startswith("\u2026 w") and
                     re.fullmatch(r"\u2026( w\d{6})+ ZEBRA( w\d{6})*", t)),
                    ("QUOKKA", lambda t: t.startswith("\u2026 QUOKKA"))):
        o = json.loads(dch("--xray", "xv-ub", "--search", q,
                           "--json").stdout)
        sn = o["hits"][0]["snippet"] if o["hits"] else ""
        sn = sn.replace("\u00a0", "\x00")
        check("NIT-UC3: %s snippet: NBSP is a word break / a long head "
              "fragment is dropped" % q, want(sn) and "\x00" not in sn, sn)


def main():
    run_steps(fixture_auto, test_auto, test_compact, test_gone, fixture_resp,
              test_responses, test_responses_live, test_responses_codex,
              test_tasks_codex, fixture_search, test_search, fixture_tail,
              test_tail, fixture_look, test_look,
              fixture_wrap, test_wrap, fixture_groups, test_groups,
              fixture_uc, test_uc)


if __name__ == "__main__":
    main()
