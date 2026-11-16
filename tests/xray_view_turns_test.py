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
        ok = tabbar(v, "Search 5")
        v.pump(0.5)
        scr = rows(v.out)
        h = hits(scr)
        check("Search: Enter runs it; the tab counts 5 hits",
              ok and "Search 5" in scr.get(2, ""), scr.get(2, ""))
        check("Search: the text's line stays above the hits, typing ended",
              scr.get(4, "").strip() == "Search: needle", scr.get(4))
        check("Search: the hits newest first, each with who said it and the "
              "prompt it is under", [(w, t) for w, t, _ in h] ==
              [("Reply", "a long chapter"),
               ("Reply", "first: where is the Needle kept?"),
               ("Bash", "first: where is the Needle kept?"),
               ("Bash", "first: where is the Needle kept?"),
               ("You", "first: where is the Needle kept?")], h)
        check("Search: each hit's snippet under it, the time on its head",
              len(h) == 5 and "the deep NEEDLE line" in " ".join(h[0][2])
              and "src/a.c: needle here" in " ".join(h[2][2]) and
              re.search(r"11-10 \d\d:07", next((r for r in scr.values()
                                               if "a long chapter" in r), "")),
              (h, scr))
        check("Search: the matched text stands out (accent bold), in any "
              "case, the rest plain",
              re.search(rb"the deep \033\[0;1;38;5;\d+mNEEDLE\033\[0m line",
                        v.out) and
              re.search(rb"where is the \033\[0;1;38;5;\d+mNeedle\033\[0m",
                        v.out), v.out[-3000:])
        check("Search: the newest hit is selected", "a long chapter" in
              head(v), head(v))
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
              tabbar(v, "Search 5") and "a long chapter" in head(v)
              and "Search: needle" in rows(v.out).get(4, ""), head(v))
        v.key(b"jj", 0.5)
        check("Search: j by hit", "Bash" in head(v) and
              "first: where" in head(v), head(v))
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
              hits(rows(v.out))[2][0] == "Bash" and "Bash" in head(v),
              (head(v), hits(rows(v.out))))
        v.key(b"G", 0.5)
        check("Search: G the oldest", "You" in head(v), head(v))
        v.key(b"g", 0.5)
        check("Search: g the newest", "a long chapter" in head(v), head(v))
        mark = len(v.out)
        v.key(b"/\x15x\r", 1.0)
        text = screen(v, mark)
        check("Search: a refused text (1 byte) is said in the status line, "
              "nothing run, the hits kept",
              "bad search: 2 to 200 bytes, no control character" in text and
              "Search 5" in rows(v.out).get(2, ""), text)
        mark = len(v.out)
        v.key(b"\033", 1.0)
        check("Search: Esc leaves typing, the text back to the last search",
              rows(v.out).get(4, "").strip() == "Search: needle" and
              len(hits(rows(v.out))) == 5, rows(v.out).get(4))
        mark = len(v.out)
        v.key(b"/\x15qwertyzz\r", 1.0)
        v.wait(b"No match", start=mark)
        text = screen(v, mark)
        check("Search: no hit: No match for the text, the tab 0",
              'No match for "qwertyzz"' in text and
              "Search 0" in rows(v.out).get(2, ""), text)
        mark = len(v.out)
        v.key(b"\t", 1.0)
        check("Search: Tab wraps to Prompts", until(lambda: v.wait(
              b"[Prompts", 0.1, mark) or "Prompts" in head(v) or
              "a long chapter" in screen(v, mark)), screen(v, mark))
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
        h = hits(rows(v.out))
        check("Search: over 50 hits: the tab says 50+, the newest first, a "
              "faint last line says the 50 newest are shown", ok and
              "the 50 newest are shown" in text and h and
              h[0][1] == "zebra prompt 54", (h, text))
        v.key(b"G", 0.5)
        check("Search: G selects the 50th", "zebra prompt 05" in head(v),
              head(v))
        v.key(b"q")
        v.exit()
    finally:
        v.close()


def main():
    run_steps(fixture_auto, test_auto, test_compact, test_gone, fixture_resp,
              test_responses, test_responses_live, test_responses_codex,
              test_tasks_codex, fixture_search, test_search)


if __name__ == "__main__":
    main()
