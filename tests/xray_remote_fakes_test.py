#!/usr/bin/env python3
"""The X-ray view of a remote session (#188, XR5c) against fake hosts:
failures, stopping a child, links and turns that drop, Responses, Search,
Tasks and Memory.

The host, the fake tailscale and the harness: xray_remote_view_lib.py.

Covers:
  4. a host without "xray" (exit 6), a closed port (exit 7) and a gate that
     refuses us (exit 4): the line in place of the tabs, the view open until
     q, then the exit code and the line on stderr;
  10. stopping a remote child (#204): a fake host whose answers stream
      without end (more than a pipe holds) and never take a 0x34: q while
      the follow streams (3 times), 20 cards each left with Esc while its
      lines stream (every other one after f replaced the light card), Esc
      then q; a silent host: Esc from a pending card, f over a pending
      card, Esc then q, q with no snapshot. Each within STOP_S (2 s; it used
      to hang for good, or wait 3 s per child), only the follow child after
      each card, no zombie, the view's fd count flat over the 20 cards, the
      terminal restored, exit 0, and no child the test saw left running.
  11. a link that drops and comes back (QA 1.57.0 LOW-R3): Refs shows the
      host screen's o/r#5, which the index does not hold, under the group
      line, the index's o/r#41 under the rest line (#294); the status says
      live updates stopped and how to get them back, and no failed read
      replaces it; a failed read takes the group away, never said; once
      the host answers the group comes back (o/r#6) and no unreachable
      line is left.
  16. Tasks (#320): srv:xv-tk, two tasks on the host: the tab last with
      its open count, Pending then Done, Enter reads the turn that last
      changed the task from the host at 2 of 2, q back to it; the follow
      (the lists in it, #448) and the chapter each a ctl --xray. A fake
      host that refuses --tasks as a dch before it: no tab, nothing said.
      A fake host older than #446 refuses --follow with the lists (exit 2):
      the follow alone, then the lists' own queries (#448). A fake
      answer in two parts 1.5 s apart: the tab with the first part's rows,
      a line over 64 KB dropped, the list's title, warning and partial line,
      PARKED and blocked by 9, 12 on a row, ESC, BEL, C1, bidi and invalid
      UTF-8 in a title drawn as ?; the second part's active row on top with
      the selection still on its task; Enter on a task with no turn opens
      its card (status and label, note, times, blocked by, group, URL), q
      back to it; Enter on the active one reads its turn from the host.
      50000 tasks from a fake host: listed, G to the last, the done
      line's warn under the rows (#321), q.
  17. Memory (#323): srv:xv-me, memories on the host: the tab last with
      every row counted, This session (wrote) then Memories; Enter opens
      the card with its text; Enter on the card reads the turn that wrote
      it from the host, q back to the card, q back to the row; the follow
      (the two lists in it, #448) and the chapter each a ctl --xray. A
      fake host that refuses --memory: no tab, nothing
      said. A fake command answer: the list's title as the group's name,
      its header warn and done-line warn, a kind of any word, a free-text
      updated with no age, a row with no path; 1100 texts of 16 KB: the
      rows past 16 MB keep no text and their card says so with the path.
  Also: 13 and 14 (Responses, Search): the parts on a fake host run here, the
  rest in xray_remote_view_test.py.

Split for run time: xray_remote_fakes_more_test.py runs 10, this file
the rest.

Run:  python3 tests/xray_remote_fakes_test.py      (uses ./dch)
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from xray_remote_view_lib import *  # noqa: E402,F401,F403


# ---- 11: a link that drops and comes back (QA 1.57.0 LOW-R3) ---------------

class Flaky(Fake):
    """A host with ctl, ctl-cancel and xray whose link drops: --follow
    answers a live snapshot and holds the stream, --read a screen with
    self.word; down() cuts every connection and each new one at once, up()
    answers again. Its dch is older than #446: --follow with --tasks or
    --memory is a usage refusal (exit 2), so the view asks the lists with
    their own queries (#448)."""

    def __init__(self, snap=None, chapters=None):
        super().__init__("ok", ops=["ctl", "ctl-cancel", "xray"])
        self.up, self.word = True, b"LINK-UP"
        # --chapter <anchor>'s answers (#288), and the snapshot
        self.snap, self.chapters = snap or SNAP, chapters or {}
        # --search's answer (#291): bytes, or "old", an older dch's refusal
        self.search = None
        # --tasks' (#320), --memory's (#323): bytes, a list of parts sent
        # 1.5 s apart, or "old"
        self.tasks = self.memory = None

    def down(self):
        self.up = False
        for c in list(self.conns):
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def serve(self, c):
        if not self.up:
            c.close()
            return
        buf = b""
        try:
            while True:
                d = c.recv(65536)
                if not d:
                    return
                buf += d
                while len(buf) >= 5:
                    t, n = struct.unpack(">BI", buf[:5])
                    if len(buf) < 5 + n:
                        break
                    body, buf = buf[5:5 + n], buf[5 + n:]
                    if t == 0x08:
                        c.sendall(frame(0x18, {"proto": "1.0",
                                               "server": "fake",
                                               "ops": self.ops}))
                    elif t == 0x30:
                        argv = json.loads(body)["argv"]
                        self.bodies.append(argv)
                        if "--follow" in argv and ("--tasks" in argv or
                                                   "--memory" in argv):
                            fl = "--tasks" if "--tasks" in argv \
                                else "--memory"
                            c.sendall(frame(0x32, ("dch: bad usage: %s and "
                                                   "--follow\n" % fl)
                                            .encode()) +
                                      frame(0x33, {"exit": 2}))
                            continue
                        if "--follow" in argv:
                            c.sendall(frame(0x31, self.snap))
                            continue    # a stream: held until the close
                        if "--search" in argv and self.search == "old":
                            c.sendall(frame(0x13, {
                                "message": "--search is not allowed with "
                                           "--xray"}))
                            c.shutdown(socket.SHUT_WR)
                            continue
                        if "--search" in argv and self.search:
                            c.sendall(frame(0x31, self.search))
                        fl = "--tasks" if "--tasks" in argv else "--memory"
                        lst = self.tasks if fl == "--tasks" else self.memory
                        if fl in argv and lst == "old":
                            c.sendall(frame(0x13, {
                                "message": fl + " is not allowed with "
                                           "--xray"}))
                            c.shutdown(socket.SHUT_WR)
                            continue
                        if fl in argv and lst:
                            parts = lst if isinstance(lst, list) else [lst]
                            for i, part in enumerate(parts):
                                if i:
                                    time.sleep(1.5)
                                for o in range(0, len(part), 1 << 18):
                                    c.sendall(frame(0x31,
                                                    part[o:o + (1 << 18)]))
                        if "--chapter" in argv:
                            a = argv[argv.index("--chapter") + 1]
                            if a in self.chapters:
                                c.sendall(frame(0x31, self.chapters[a]))
                        if argv[0] == "--read":
                            c.sendall(frame(0x31, b"$ echo hi\r\n" +
                                            self.word + b"\r\n"))
                        c.sendall(frame(0x33, {"exit": 0}))
        except OSError:
            return


def redraw(v, cols):
    """The whole frame, drawn again after a resize."""
    v.resize(30, cols)
    mark = len(v.out)
    v.pump(1.0)
    return v.out[mark:]


def test_link_drop():
    h = Flaky()
    hosts("q=127.0.0.1:%d" % h.port)
    h.word = b"see o/r#5"
    v = Pty([DCH, "--xray", "q:fk"])
    stop = b"live updates stopped (reopen the view for them): dch: q: "
    try:
        check("link drop: the view opens on the snapshot",
              v.wait(b"X-ray  q:fk") and v.wait(b"Prompts 1"),
              v.out[-600:])
        mark = len(v.out)
        v.key(b"\033[Z\033[Z", 0.05)
        v.wait(b"o/r#5", start=mark)
        v.pump(0.3)
        text = shown(v, 0)
        at = [text.find(x) for x in ("on screen now", "o/r#5", "the rest",
                                     "o/r#41")]
        check("link drop #294: Refs shows the host screen's o/r#5 (the "
              "index holds none: no count) under the group line, the "
              "index's o/r#41 under the rest line",
              -1 not in at and at == sorted(at) and
              re.search(r"(?m)^\W*pull +o/r#5\b", text), text)
        mark = len(v.out)
        h.down()
        v.pump(3.5)
        down = v.out[mark:]
        frame_ = redraw(v, 101)
        # lines are drawn whole: each host line in the bytes is one drawn
        check("link drop: the status says live updates stopped and how to "
              "get them back, and no failed read replaces it over 3 s",
              stop in frame_ and
              all(down[:m.start()].endswith(stop[:-8])
                  for m in re.finditer(b"dch: q: ", down)),
              (frame_[-400:], down[-600:]))
        check("link drop #294: a failed read takes the group away, unsaid",
              b"on screen now" not in frame_ and b"No screen" not in frame_,
              frame_[-600:])
        h.word = b"see o/r#6"
        h.up = True
        mark = len(v.out)
        v.wait(b"o/r#6", start=mark)
        frame_ = redraw(v, 100)
        check("link drop, host back: Refs reads the screen again, o/r#6 "
              "in the group", v.wait(b"o/r#6", start=mark) and
              b"on screen now" in frame_, v.out[mark:][-600:])
        check("link drop, host back: no line says the host is unreachable "
              "but the stop's own, which stays",
              stop in frame_ and b"No screen" not in frame_ and
              frame_.count(b"unreachable") ==
              frame_.count(stop + b"unreachable"), frame_[-600:])
        v.key(b"q", 0)
        check("link drop: q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()
        h.close()


def test_responses_fake():
    """Chapters without #296's fields (a host before 1.63.0, or a current
    one whose ended turns have no answer and no tool yet: the same bytes):
    Responses says none yet, never that the host is older (LOW-V19); with
    the fields: a cut-dropped answer reads Enter reads it, the running turn
    working."""
    two = (b'"offset":0},{"anchor":"s2","title":"second","kind":"prompt",'
           b'"at":1794300100,"offset":9}]')
    h = Flaky(SNAP.replace(b'"offset":0}]', two))
    hosts("q=127.0.0.1:%d" % h.port)
    v = Pty([DCH, "--xray", "q:fk"])
    try:
        v.wait(b"Prompts 2")
        mark = len(v.out)
        v.key(b"\t", 1.0)
        ok = v.wait(b"Responses 0", start=mark) and \
            v.wait(b"No responses yet", start=mark)
        v.pump(0.3)
        check("Responses, no answer and no tool in any ended turn: No "
              "responses yet, not an older host (LOW-V19)",
              ok and b"older than" not in v.out[mark:], v.out[mark:][-600:])
        v.key(b"j\r", 0.5)
        v.key(b"q", 0)
        check("Responses, an older host: keys do no harm, q exits 0",
              v.exit() == 0, v.out[-80:])
    finally:
        v.close()
        h.close()
    two = (b'"offset":0,"response_offset":5,"tools":"Bash 2"},{"anchor":'
           b'"s2","title":"second","kind":"prompt","at":1794300100,'
           b'"offset":9,"tools":"Read 1"}]')
    ch = {"s1": json.dumps({"title": "first", "at": 1794300000,
                            "entries": [{"role": "user", "text": "FIRST"},
                                        {"role": "assistant",
                                         "text": "THE-DROPPED-ANSWER"}]})
          .encode() + b"\n"}
    h = Flaky(SNAP.replace(b'"offset":0}]', two), ch)
    hosts("q=127.0.0.1:%d" % h.port)
    v = Pty([DCH, "--xray", "q:fk"])
    try:
        v.wait(b"Prompts 2")
        mark = len(v.out)
        v.key(b"\t", 0.05)
        v.wait(b"Responses 2", start=mark)
        it = items(v)
        check("Responses, a host with the fields: the running turn working, "
              "an answer the cap dropped says Enter reads it",
              it == [("second", ["Read 1", "working\u2026"]),
                     ("first", ["Bash 2", "Enter reads it"])], it)
        v.key(b"j", 0.3)
        mark = len(v.out)
        v.key(b"\r", 0.05)
        check("Responses: Enter on it reads it from the host",
              v.wait(b"THE-DROPPED-ANSWER", start=mark) and
              v.wait(b"1 of 2", start=mark), v.out[mark:][-600:])
        v.key(b"q", 0.5, want=b"Responses 2")
        v.key(b"q", 0)
        check("Responses, a host with the fields: q, q exits 0",
              v.exit() == 0, v.out[-80:])
    finally:
        v.close()
        h.close()


# ---- Search (#291): the host's --search --------------------------------------

def rows_now(v):
    scr = screen_rows(v)
    return "\n".join(scr[k] for k in sorted(scr))


def test_search_fake():
    """An older host refuses --search: its line in the list. A host's answer
    with a null chapter, a tool hit and partial: said as such."""
    h = Flaky()
    h.search = "old"
    hosts("q=127.0.0.1:%d" % h.port)
    v = Pty([DCH, "--xray", "q:fk"])
    try:
        v.wait(b"Prompts 1")
        v.pump(0.3)
        mark = len(v.out)
        v.key(b"\033[Z", 0.5, want=b"[Search")
        v.key(b"needle\r", 1.0)
        check("Search, an older host: the list says its dch does not know "
              "--search", v.wait(b"dch: q: the host's dch does not know "
                                 b"--search", start=mark) and
              not re.search(r"Search \d", screen_rows(v).get(2, "")),
              v.out[mark:][-600:])
        v.key(b"j\r", 0.5)
        v.key(b"q", 0)
        check("Search, an older host: keys do no harm, q exits 0",
              v.exit() == 0, v.out[-80:])
    finally:
        v.close()
        h.close()
    ans = {"schema": 1, "session": "fk", "session_id": "m0000000000000001",
           "corpus": "claude", "text": "needle", "hits": [
               {"chapter": None, "offset": 900, "at": 1794300200,
                "who": "assistant", "tool": None,
                "snippet": "a needle \u001b]0;pwned\u0007 in the middle"},
               {"chapter": "s1", "offset": 5, "at": 1794300100,
                "who": "tool", "tool": "Grep", "snippet": "grep NEEDLE"}],
           "more": False, "partial": True, "why": "time", "bytes": 99}
    ch = {"s1": json.dumps({"title": "first", "at": 1794300000,
                            "entries": [{"role": "user", "text": "FIRST"},
                                        {"role": "tool", "tool": "Grep",
                                         "text": "grep NEEDLE"}]})
          .encode() + b"\n"}
    h = Flaky(chapters=ch)
    h.search = json.dumps(ans).encode() + b"\n"
    hosts("q=127.0.0.1:%d" % h.port)
    v = Pty([DCH, "--xray", "q:fk"])
    try:
        v.wait(b"Prompts 1")
        v.pump(0.3)
        v.key(b"\033[Z", 0.5, want=b"[Search")
        v.key(b"needle\r", 0.05)
        ok = tabbar(v, "Search 2")
        v.pump(0.3)
        hs, text = shits(v), rows_now(v)
        check("Search, a host's answer: a null chapter's hit is outside the "
              "prompts shown, a tool hit by its tool under its prompt",
              ok and [(w, t) for w, t, _ in hs] ==
              [("Reply", "(outside the prompts shown)"), ("Grep", "first")],
              hs)
        check("Search, a host's answer: partial: a faint line says it "
              "stopped early, why, and the hits stand",
              "the search stopped early (it took too long); the hits shown "
              "stand" in text, text)
        check("Search, a host's answer: its snippet's ESC and BEL show as ?",
              hs and "a needle ?]0;pwned? in the middle" in " ".join(hs[0][2])
              and b"\033]0;pwned" not in v.out, hs)
        mark = len(v.out)
        v.key(b"\r", 1.0)
        check("Search: Enter on a hit with no chapter says so, no reader",
              v.wait(b"this place is outside the prompts X-ray can open",
                     start=mark) and b"Reading" not in v.out[mark:],
              v.out[mark:][-600:])
        mark = len(v.out)
        v.key(b"j\r", 0.05)
        check("Search: Enter on the tool hit reads its prompt from the host",
              v.wait(b"1 of 1", start=mark) and
              v.wait(b"grep NEEDLE", start=mark), v.out[mark:][-600:])
        v.key(b"q", 0.5)
        v.key(b"q", 0)
        check("Search, a host's answer: q, q exits 0", v.exit() == 0,
              v.out[-80:])
    finally:
        v.close()
        h.close()

# ---- Tasks (#320): the host's --tasks -----------------------------------------

TKSID = "11111111-2222-4333-8444-5555555555ca"


def fixture_tasks():
    """srv:xv-tk: two tasks on the host, one done in the second prompt."""
    ts = lambda m: "2026-11-10T10:%02d:00Z" % m
    use = lambda tid, name, inp, m: {
        "type": "assistant", "timestamp": ts(m), "message": {"content": [
            {"type": "tool_use", "id": tid, "name": name, "input": inp}]}}
    res = lambda tid, n, m: {
        "type": "user", "timestamp": ts(m),
        "toolUseResult": {"task": {"id": n}},
        "message": {"content": [{"type": "tool_result", "tool_use_id": tid,
                                 "content": "ok"}]}}
    ask = lambda text, m: {"type": "user", "cwd": "/home/demo/proj",
                           "timestamp": ts(m), "message": {"content": text}}
    record_o("xv-tk", TKSID, [
        ask("plan", 0),
        use("c1", "TaskCreate", {"subject": "Ship the parser",
                                 "description": ""}, 1), res("c1", "1", 1),
        use("c2", "TaskCreate", {"subject": "Write the notes",
                                 "description": ""}, 2), res("c2", "2", 2),
        ask("go", 5),
        use("u1", "TaskUpdate", {"taskId": "1", "status": "completed"}, 6)])


def trows(v):
    """Tasks' lines on screen over the rule, blanks folded."""
    out, scr = [], screen_rows(v)
    for k in sorted(x for x in scr if x >= 4):
        r = scr[k]
        if r.startswith(("\u2500", "-")):
            break
        if r.strip():
            out.append(re.sub(" +", " ", r).strip())
    return out


def test_tasks(port):
    """The host's task list: the tab, its rows, Enter reads the turn from
    the host, q back; the follow (the lists in it) and the chapter each a
    ctl."""
    hosts("srv=127.0.0.1:%d" % port)
    n0 = ctls("--xray")
    late = []
    v = Pty([DCH, "--xray", "srv:xv-tk"])
    try:
        ok = tabbar(v, "Tasks 1")
        v.key(b"\033[Z", 0.8, want=b"[Tasks")
        r = trows(v)
        check("Tasks, remote: the host's list, the tab last with its open "
              "count; Pending, then Done, each row with its age",
              ok and re.match(r"Pending 1\n\u258c\u25cb 2 Write the notes \d+d\n"
                              r"Done 1\n\u2713 1 Ship the parser \d+d$",
                              "\n".join(r)), r)
        mark = len(v.out)
        v.key(b"j\r", 0.05)
        check("Tasks, remote: Enter reads the turn that last changed the "
              "task from the host, 2 of 2", v.wait(b"2 of 2", start=mark) and
              v.wait(b"TaskUpdate", start=mark), v.out[mark:][-600:])
        v.key(b"q", 1.0, want=b"Ship the parser")
        check("Tasks, remote: q back to the same task",
              "Ship the parser" in head(v), head(v))
        # q quits: every query sent first, the other list too (#374)
        host_ran(v, n0 + 2, "follow with the lists, chapter", late)
        v.key(b"q", 0)
        check("Tasks, remote: q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()
    until(lambda: ctls("--xray") - n0 >= 2, 5)
    check("Tasks, remote: the follow (the task and memory lists in it, "
          "#448) and the chapter, each its own ctl --xray on the host",
          ctls("--xray") - n0 == 2,
          (ctls("--xray") - n0, "not on the host in time: %s" % late))


def tk(id, title, status, **kw):
    t = {"id": id, "title": title, "status": status, "label": None,
         "active": None, "note": None, "created": None, "updated": None,
         "chapter": None, "created_chapter": None, "blocked_by": [],
         "url": None, "group": None}
    t.update(kw)
    return json.dumps({"task": t}).encode() + b"\n"


def test_tasks_fake():
    """An older host refuses --tasks: no tab. A host's answer streamed in
    two parts: the tab as its first rows come, the selection kept on its
    task as more arrive above it; the list's title, warning and partial
    line; a label that says more, blocked by; a task with no turn opens its
    card, q back; Enter on one with a turn reads it from the host; hostile
    text drawn as ?; a line over 64 KB dropped, the next one kept. 50000
    tasks: listed, G, q."""
    h = Flaky()
    h.tasks = "old"
    hosts("q=127.0.0.1:%d" % h.port)
    v = Pty([DCH, "--xray", "q:fk"])
    try:
        v.wait(b"Prompts 1")
        v.pump(1.5)
        tabs = []
        for _ in range(4):
            v.key(b"\t", 0.4)
            tabs.append(screen_rows(v).get(2, ""))
        check("Tasks, an older host: no Tasks tab, nothing said, Tab never "
              "lands on one", all("Tasks" not in t for t in tabs) and
              b"does not know --tasks" not in v.out, tabs)
        v.key(b"q", 0)
        check("Tasks, an older host: q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()
        h.close()
    now = time.time()
    iso = lambda ago: time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                    time.gmtime(now - ago))
    head1 = json.dumps({"schema": 1, "session": "fk", "source": "command",
                        "title": "Sprint 12", "warn": "tasks_command "
                        "failed: exit 3", "partial": True}).encode() + b"\n"
    part1 = (head1 +
             tk("d1", "Done thing", "done", label="completed",
                updated=iso(7200)) +
             tk("p1", "Parked thing", "pending", label="PARKED",
                note="waits on the vendor", created=iso(90000),
                updated=iso(3600), blocked_by=["9", "12"],
                url="https://example.invalid/1", group="infra") +
             tk("big", "B" * 70000, "pending") +
             b'{"task":{"id":"p2","title":"bad \xff\xfe utf \\u001b]0;pwned'
             b'\\u0007 \\u009b2J bi\\u202edi","status":"pending"}}\n')
    part2 = (tk("a1", "Run the build", "active", active="Running the build",
                chapter="s1", updated=iso(30)) +
             b'{"done":true,"count":4,"dropped":0}\n')
    ch = {"s1": json.dumps({"title": "first", "at": 1794300000,
                            "entries": [{"role": "user", "text": "FIRST"},
                                        {"role": "tool", "tool": "TaskUpdate",
                                         "text": "status: in_progress"}]})
          .encode() + b"\n"}
    h = Flaky(chapters=ch)
    h.tasks = [part1, part2]
    hosts("q=127.0.0.1:%d" % h.port)
    v = Pty([DCH, "--xray", "q:fk"])
    try:
        ok = tabbar(v, "Tasks 2")
        # the lists' own queries go out one by one: a slow (sanitizer)
        # runner may not have sent the memory one yet
        end = time.time() + 10
        while time.time() < end and not [a for a in h.bodies if
                                          "--memory" in a and
                                          "--follow" not in a]:
            v.pump(0.1)
        fol = [a for a in h.bodies if "--follow" in a]
        check("Tasks, a host older than #446 (#448): the follow with the "
              "lists refused (exit 2), then the follow alone and the task "
              "and memory lists' own queries", ok and len(fol) == 2 and
              "--tasks" in fol[0] and "--memory" in fol[0] and
              "--tasks" not in fol[1] and "--memory" not in fol[1] and
              [a for a in h.bodies if "--tasks" in a and "--follow" not in a]
              and [a for a in h.bodies if "--memory" in a and
                   "--follow" not in a], h.bodies)
        v.key(b"\033[Z", 0.5, want=b"[Tasks")
        v.key(b"j", 0.3)
        r = trows(v)
        check("Tasks, a host's answer: the tab with its first part's rows, "
              "the line over 64 KB dropped", ok and "p2" in head(v) and
              not any("BBBB" in x for x in r), (head(v), r))
        check("Tasks, a host's answer: the list's title on top, its warning "
              "and partial read faint under the rows",
              r and r[0] == "Sprint 12" and "tasks_command failed: exit 3"
              in r and "the history was too long to read whole; older tasks "
              "may be missing" in r, r)
        p1 = next((x for x in r if "Parked thing" in x), "")
        check("Tasks, a host's answer: a label that says more and blocked by "
              "on the row, its age", re.search(r"\u25cb p1 Parked thing "
                                               r"PARKED blocked by 9, 12 1h$",
                                               p1), p1)
        p2 = next((x for x in r if "bad" in x), "")
        bad = [b for b in (b"\033]0;pwned", b"\007", b"\xe2\x80\xae",
                           b"\xc2\x9b", b"\xff") if b in v.out]
        check("Tasks, a host's answer: ESC, BEL, C1, bidi and invalid UTF-8 "
              "in a title drawn as ?, none reach the terminal",
              "?]0;pwned?" in p2 and not bad, (p2, bad))
        ok = tabbar(v, "Tasks 3", 5)
        v.pump(0.3)
        r = trows(v)
        check("Tasks, a host's answer: the second part's active row on top, "
              "the selection still on its task", ok and r[1:3] ==
              ["In progress 1", "\u25b6 a1 Running the build now"] and
              "p2" in head(v), (head(v), r))
        mark = len(v.out)
        v.key(b"k\r", 0.8, want=b"to read")
        text = "\n".join(trows(v))
        check("Tasks, a host's answer: Enter on a task with no turn opens its "
              "card: title, status and label, note, times, blocked by, "
              "group, URL", "Task p1" in screen_rows(v).get(2, "") and
              "Parked thing" in text and "Status Pending PARKED" in text and
              "Note waits on the vendor" in text and
              re.search(r"Created \d\d-\d\d \d\d:\d\d", text) and
              "Blocked by 9, 12" in text and "Group infra" in text and
              "URL https://example.invalid/1" in text and
              "Turn none to read" in text, text)
        v.key(b"q", 0.8, want=b"Parked thing")
        check("Tasks, a host's answer: q back to the same task",
              "Parked thing" in head(v), head(v))
        mark = len(v.out)
        v.key(b"g\r", 0.05)
        check("Tasks, a host's answer: Enter on the active task reads its "
              "turn from the host", v.wait(b"1 of 1", start=mark) and
              v.wait(b"status: in_progress", start=mark), v.out[mark:][-600:])
        v.key(b"q", 0.5, want=b"Parked thing")
        v.key(b"q", 0)
        check("Tasks, a host's answer: q exits 0", v.exit() == 0,
              v.out[-80:])
    finally:
        v.close()
        h.close()
    n = 50000
    many = (b'{"schema":1,"session":"fk","source":"claude","title":null,'
            b'"partial":false}\n' +
            b"".join(tk(str(i), "many task %05d" % i, "pending")
                     for i in range(1, n + 1)) +
            b'{"done":true,"count":50000,"dropped":0,"warn":"tasks_command '
            b'stopped early: 50000 rows"}\n')
    h = Flaky()
    h.tasks = many
    hosts("q=127.0.0.1:%d" % h.port)
    t0 = time.time()
    v = Pty([DCH, "--xray", "q:fk"])
    try:
        ok = tabbar(v, "Tasks 50000", 30)
        dt = time.time() - t0
        v.key(b"\033[Z", 0.5, want=b"[Tasks")
        mark = len(v.out)
        s = time.time()
        v.key(b"G", 0)
        ok = v.wait(b"many task 50000", 5, start=mark) and ok
        check("Tasks, 50000 from a host (%.1f MB): listed in %.2f s, G to "
              "the last in %.3f s" % (len(many) / 1e6, dt, time.time() - s),
              ok and "many task 50000" in head(v), head(v))
        check("Tasks, 50000: the done line's warn under the rows (#321)",
              "tasks_command stopped early: 50000 rows" in trows(v),
              trows(v))
        v.key(b"q", 0)
        check("Tasks, 50000: q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()
        h.close()


# ---- Memory (#323): the host's --memory ---------------------------------------

MESID = "11111111-2222-4333-8444-5555555555cb"


def fixture_memory():
    """srv:xv-me: two memories on the host, the first written in the
    session's second prompt."""
    cfg = os.path.join(SENV["HOME"], ".claude")
    mdir = os.path.join(cfg, "projects", "-mem-proj", "memory")
    os.makedirs(mdir, exist_ok=True)
    for name, ago in (("alpha.md", 3600), ("beta.md", 7200)):
        with open(os.path.join(mdir, name), "w") as f:
            f.write("---\nname: %s\ndescription: %s hook\nmetadata:\n"
                    "  type: feedback\n---\n%s text on the host\n" %
                    ((name[:-3],) * 3))
        os.utime(os.path.join(mdir, name), (time.time() - ago,) * 2)
    ts = lambda m: "2026-11-10T10:%02d:00Z" % m
    ask = lambda text, m: {"type": "user", "cwd": "/home/demo/proj",
                           "timestamp": ts(m), "message": {"content": text}}
    record_o("xv-me", MESID, [
        ask("hello", 0), say_o("hi", ts(1)),
        ask("remember it", 5),
        {"type": "assistant", "timestamp": ts(6), "message": {"content": [
            {"type": "tool_use", "id": "w1", "name": "Write",
             "input": {"file_path": mdir + "/alpha.md", "content": "x"}}]}}])
    # record_o writes under -home-demo-proj: the memory dir is the
    # transcript's own project's
    pdir = os.path.join(cfg, "projects")
    os.replace(os.path.join(pdir, "-home-demo-proj", MESID + ".jsonl"),
               os.path.join(pdir, "-mem-proj", MESID + ".jsonl"))


def test_memory(port):
    """The host's memory list: the tab, its groups, the card's text; Enter
    on the card reads the turn from the host, q back; each query a ctl."""
    hosts("srv=127.0.0.1:%d" % port)
    n0 = ctls("--xray")
    late = []
    v = Pty([DCH, "--xray", "srv:xv-me"])
    try:
        ok = tabbar(v, "Memory 2")
        v.key(b"\033[Z", 0.8, want=b"[Memory")
        r = [re.sub(r" \d+[mhd]$", "", x).lstrip("\u258c ") for x in
             trows(v)]
        check("Memory, remote: the host's list, the tab last; This session "
              "(wrote), then Memories", ok and r ==
              ["This session 1", "alpha feedback alpha hook wrote",
               "Memories 1", "beta feedback beta hook"], r)
        mark = len(v.out)
        v.key(b"\r", 0.05)
        check("Memory, remote: Enter opens the card with the host's text",
              v.wait(b"alpha text on the host", start=mark) and
              v.wait(b"this session wrote it", start=mark),
              v.out[mark:][-600:])
        mark = len(v.out)
        v.key(b"\r", 0.05)
        check("Memory, remote: Enter on the card reads the turn that wrote "
              "it from the host, 2 of 2", v.wait(b"2 of 2", start=mark) and
              v.wait(b"remember it", start=mark), v.out[mark:][-600:])
        mark = len(v.out)
        v.key(b"q", 0.05)
        check("Memory, remote: q back to the card", v.wait(b"Memory alpha",
                                                           start=mark),
              v.out[mark:][-600:])
        v.key(b"q", 0.8, want=b"alpha")
        check("Memory, remote: q back to the row", "alpha" in head(v),
              head(v))
        # q quits: every query sent first, the other list too (#374)
        host_ran(v, n0 + 2, "follow with the lists, chapter", late)
        v.key(b"q", 0)
        check("Memory, remote: q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()
    until(lambda: ctls("--xray") - n0 >= 2, 5)
    check("Memory, remote: the follow (the task and memory lists in it, "
          "#448) and the chapter, each its own ctl --xray on the host",
          ctls("--xray") - n0 == 2,
          (ctls("--xray") - n0, "not on the host in time: %s" % late))


def mem(id, title, **kw):
    m = {"id": id, "title": title, "note": None, "kind": None,
         "scope": None, "updated": None, "path": None, "session": None,
         "chapter": None, "body": None}
    m.update(kw)
    return json.dumps({"memory": m}).encode() + b"\n"


def test_memory_fake():
    """An older host refuses --memory: no tab. A command's answer: the
    title names the group, both warns shown, a kind of any word, a free
    updated with no age, no path. Texts past 16 MB are not kept."""
    h = Flaky()
    h.memory = "old"
    hosts("q=127.0.0.1:%d" % h.port)
    v = Pty([DCH, "--xray", "q:fk"])
    try:
        v.wait(b"Prompts 1")
        v.pump(1.5)
        check("Memory, an older host: no Memory tab, nothing said",
              "Memory" not in screen_rows(v).get(2, "") and
              b"does not know --memory" not in v.out,
              screen_rows(v).get(2, ""))
        v.key(b"q", 0)
        check("Memory, an older host: q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()
        h.close()
    ans = (json.dumps({"schema": 1, "source": "command", "title": "Team "
                       "notes", "warn": "memory_command said: slow",
                       "partial": False}).encode() + b"\n" +
           mem("s1", "Decided thing", kind="decision", session="wrote",
               chapter="s1", note="why we did it", updated="yesterday",
               body="SESSION BODY") +
           mem("n1", "Loose note", kind="two words", body="NOTE BODY") +
           b'{"done":true,"count":2,"dropped":0,"warn":"memory_command '
           b'stopped early"}\n')
    ch = {"s1": json.dumps({"title": "first", "at": 1794300000,
                            "entries": [{"role": "user", "text": "FIRST"}]})
          .encode() + b"\n"}
    h = Flaky(chapters=ch)
    h.memory = ans
    hosts("q=127.0.0.1:%d" % h.port)
    v = Pty([DCH, "--xray", "q:fk"])
    try:
        ok = tabbar(v, "Memory 2")
        v.key(b"\033[Z", 0.8, want=b"[Memory")
        r = [x.lstrip("\u258c ") for x in trows(v)]
        check("Memory, a command's answer: This session, the list's title "
              "as the group's name, a free updated with no age, a kind of "
              "two words not shown; the done line's warn under the rows",
              ok and r[:4] == ["This session 1",
                               "Decided thing decision why we did it wrote",
                               "Team notes 1", "Loose note"] and
              "memory_command stopped early" in r, r)
        mark = len(v.out)
        v.key(b"\r", 0.8)
        v.key(b"\r", 0.05)
        check("Memory, a command's answer: Enter, Enter reads its turn from "
              "the host", v.wait(b"1 of 1", start=mark) and
              v.wait(b"FIRST", start=mark), v.out[mark:][-600:])
        v.key(b"q", 0.5)
        v.key(b"q", 0.5)
        v.key(b"q", 0)
        v.exit()
    finally:
        v.close()
        h.close()
    head1 = json.dumps({"schema": 1, "source": "command", "title": None,
                        "warn": "memory_command failed: exit 3",
                        "partial": False}).encode() + b"\n"
    h = Flaky()
    h.memory = head1 + mem("m1", "Only one") + b'{"done":true}\n'
    hosts("q=127.0.0.1:%d" % h.port)
    v = Pty([DCH, "--xray", "q:fk"])
    try:
        ok = tabbar(v, "Memory 1")
        v.key(b"\033[Z", 0.8, want=b"[Memory")
        r = trows(v)
        check("Memory, a header's warn under the rows", ok and
              "memory_command failed: exit 3" in r, r)
        v.key(b"q", 0)
        v.exit()
    finally:
        v.close()
        h.close()
    n = 1100
    many = (b'{"schema":1,"source":"claude","title":null,"partial":false}\n'
            + b"".join(mem("b%04d" % i, "big %04d" % i, path="/m/b%04d.md" % i,
                           body="%04d" % i + "z" * 16000)
                       for i in range(n)) +
            b'{"done":true,"count":1100,"dropped":0}\n')
    h = Flaky()
    h.memory = many
    hosts("q=127.0.0.1:%d" % h.port)
    v = Pty([DCH, "--xray", "q:fk"])
    try:
        ok = tabbar(v, "Memory 1100", 30)
        v.key(b"\033[Z", 0.05)
        until(lambda: (v.pump(0.1), trows(v))[1], 30)
        mark = len(v.out)
        v.key(b"G\r", 0.05)
        until(lambda: (v.pump(0.1), "b1099.md" in "\n".join(trows(v)))[1], 30)
        text = "\n".join(trows(v))
        check("Memory, 1100 texts of 16 KB: all listed, the last row's card "
              "says its text was not kept and names the path", ok and
              "not kept (the list keeps 16 MB of text): read /m/b1099.md"
              in text, text)
        v.key(b"q", 0.5)
        v.key(b"g\r", 0.05)
        until(lambda: (v.pump(0.1), "0000zzzz" in "\n".join(trows(v)))[1], 30)
        text = "\n".join(trows(v))
        check("Memory, 1100 texts: the first row's card shows its text",
              "0000zzzz" in text and "not kept" not in text, text)
        v.key(b"q", 0.5)
        v.key(b"/1099z", 0.05)
        # the filter line under the rule says the last key came in; the
        # rows drawn with it are the filtered ones
        until(lambda: (v.pump(0.1), "/1099z" in rows_now(v))[1], 30)
        v.pump(0.3)
        check("Memory, 1100 texts: a text not kept is not filtered on",
              not any("big 1099" in x for x in trows(v)), trows(v))
        v.key(b"\033", 0.3)
        v.key(b"\033", 0.3)
        v.key(b"q", 0)
        check("Memory, 1100 texts: q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()
        h.close()


def test_turn_drop():
    """#288: the reader's [ on a host that has gone: the status says so and
    the reader stays on its chapter."""
    snap2 = SNAP.replace(b'"offset":0}]', b'"offset":0},{"anchor":"s2",'
                         b'"title":"second","kind":"prompt","at":1794300100,'
                         b'"offset":9}]')
    ch = {"s2": json.dumps({"title": "second", "at": 1794300100,
                            "entries": [{"role": "user",
                                         "text": "SECOND-TEXT"}]}).encode()
          + b"\n"}
    h = Flaky(snap2, ch)
    hosts("q=127.0.0.1:%d" % h.port)
    v = Pty([DCH, "--xray", "q:fk"])
    try:
        check("turn drop: the view opens on two chapters",
              v.wait(b"Prompts 2"), v.out[-600:])
        mark = len(v.out)
        v.key(b"\r", 0.05)
        check("turn drop: Enter reads the newest, 2 of 2",
              v.wait(b"2 of 2", start=mark) and
              v.wait(b"SECOND-TEXT", start=mark), v.out[mark:])
        h.down()
        mark = len(v.out)
        v.key(b"[", 0.05)
        ok = v.wait(b"could not read that prompt: dch: q: ", 15, start=mark)
        frame_ = redraw(v, 101)
        check("turn drop: [ on a gone host says so, the reader stays on 2 "
              "of 2", ok and b"2 of 2" in frame_ and b"SECOND-TEXT" in frame_,
              (v.out[mark:][-600:], frame_[-600:]))
        v.key(b"q")
        v.key(b"q", 0)
        check("turn drop: q, q exits 0", v.exit() == 0, v.out[-80:])
    finally:
        v.close()
        h.close()


# ---- 4: the three failure lines ----------------------------------------------

def fail_case(what, port, want, line, prefix=False):
    hosts("q=127.0.0.1:%d" % port)
    v = Pty([DCH, "--xray", "q:x"])
    try:
        ok = v.wait(line.encode())
        v.pump(1.0)
        i = v.out.find(line.encode())
        check("%s: the line in place of the tabs, the view kept open" % what,
              ok and v.reap() is None and b"Prompts" not in v.out[i:] and
              v.out.rfind(b"\033[2;1H", 0, i) == i - 6 and
              v.out.rfind(b"\033[?1049h", 0, i) >= 0, v.out[-600:])
        mark = len(v.out)
        v.key(b"\t")
        check("%s: other keys do nothing" % what, v.reap() is None and
              b"Prompts" not in v.out[mark:], v.out[mark:])
        v.key(b"q", 0)
        rc = v.exit()
        i = v.out.rfind(line.encode())
        check("%s: q leaves it with exit %d, the line on stderr after the "
              "terminal is restored" % (what, want),
              rc == want and v.out.rfind(RESTORE, 0, i) >= 0,
              (rc, v.out[-300:]))
    finally:
        v.close()


def test_failures():
    nox = Fake("ctlerr", ops=["ctl", "ctl-cancel", "watch"])
    fail_case("a host without xray (exit 6)", nox.port, 6,
              "dch: q: does not support --xray (needs dch serve 1.55.0+)")
    check("a host without xray: nothing sent", not nox.bodies, nox.bodies)
    nox.close()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = s.getsockname()[1]
    s.close()
    fail_case("an unreachable host (exit 7)", dead, 7,
              "dch: q: unreachable: ")
    ref = Fake("refuse")
    fail_case("a gate that refuses us (exit 4)", ref.port, 4,
              "dch: q refused: " + REFUSAL["message"])
    ref.close()


# ---- 10: stopping a remote child (#204) --------------------------------------

# the bug is a hang or RM_CANCEL_MS (3 s) per child: under 3 s, one child
# sitting out its cancel fails the row. The view's own stop is 50 ms after
# SIGTERM and up to 250 ms after SIGKILL per child, up to 5 on Esc then q;
# the ASan lite build on a macOS runner took 1.00 s there
STOP_S = 2.0
SNAP = ('{"schema":1,"snapshot":{"session":"fk","session_id":'
        '"m0000000000000001","corpus":"claude","live":true,"lines":3,'
        '"project_label":"/x","chapters":[{"anchor":"s1","title":"first",'
        '"kind":"prompt","at":1794300000,"offset":0}],"refs":[{"key":'
        '"pull:o/r#41","shape":"pull","text":"o/r#41","title":"a ref",'
        '"count":2,"underline":true}]}}\n').encode()
PAD = b"".join(b'{"pad":"%s"}\n' % (b"p" * 240) for _ in range(128))
MENTIONS = b"".join(json.dumps({"mention": {
    "session": "fk", "line": "mention %d " % k + "m" * 200, "chapter": "s1",
    "at": k}}).encode() + b"\n" for k in range(128))


class Stopper(Fake):
    """A host with ctl, ctl-cancel and xray that never answers a 0x34.
    stream: --follow's snapshot then pad lines without end (follow=1, else
    held), --ref's mention lines without end, both until the close. silent:
    the snapshot (snap=1), then nothing at all."""

    def __init__(self, mode, follow=0, snap=1):
        super().__init__("ok", ops=["ctl", "ctl-cancel", "xray"])
        self.smode, self.follow, self.snap = mode, follow, snap

    def flood(self, c, data):
        while True:
            c.sendall(frame(0x31, data))
            time.sleep(0.01)    # ~3 MB/s: a pipe's worth in ~20 ms

    def run(self, c, argv):
        try:
            if "--follow" in argv:
                if self.snap:
                    c.sendall(frame(0x31, SNAP))
                if self.smode == "stream" and self.follow:
                    self.flood(c, PAD)
            elif "--ref" in argv and self.smode == "stream":
                self.flood(c, MENTIONS)
            elif self.smode == "stream":
                c.sendall(frame(0x33, {"exit": 0}))
        except OSError:
            pass

    def serve(self, c):
        buf = b""
        try:
            while True:
                d = c.recv(65536)
                if not d:
                    return
                buf += d
                while len(buf) >= 5:
                    t, n = struct.unpack(">BI", buf[:5])
                    if len(buf) < 5 + n:
                        break
                    body, buf = buf[5:5 + n], buf[5 + n:]
                    if t == 0x08:
                        c.sendall(frame(0x18, {"proto": "1.0",
                                               "server": "fake",
                                               "ops": self.ops}))
                    elif t == 0x30:
                        argv = json.loads(body)["argv"]
                        self.bodies.append(argv)
                        threading.Thread(target=self.run, args=(c, argv),
                                         daemon=True).start()
        except OSError:
            return


def nfds(pid):
    """The view's open fds, or None when neither /proc nor lsof says."""
    if os.path.isdir("/proc/%d/fd" % pid):
        return len(os.listdir("/proc/%d/fd" % pid))
    if not os.path.exists("/usr/sbin/lsof"):
        return None
    o = subprocess.run(["/usr/sbin/lsof", "-p", str(pid)],
                       capture_output=True, text=True).stdout
    return len(o.splitlines()) - 1


def gone(what, v, rc, seen):
    check("%s: the terminal restored, exit 0" % what,
          rc == 0 and RESTORE in v.out[-60:], (rc, v.out[-80:]))
    time.sleep(0.2)
    left = alive(sorted(seen))
    check("%s: no child of the view left running" % what, not left, left)


def card(v, tab=False):
    if tab:
        v.key(b"\033[Z\033[Z", 0.3)  # to Refs; Esc from a card keeps the tab
    mark = len(v.out)
    v.key(b"\r", 0.1)
    return v.wait(b"light", start=mark, secs=5)


def esc_q(v):
    """Esc, the list back, then q (one write would be Alt-q): the exit
    code or None, and the seconds both took."""
    mark = len(v.out)
    t0 = time.time()
    v.key(b"\033", 0)
    if not v.wait(b"Prompts 1", start=mark, secs=STOP_S):
        return None, time.time() - t0
    v.key(b"q", 0)
    return v.exit(STOP_S), time.time() - t0


def shut(v):
    """Pty.close, reading the pty meanwhile: a hung view's output fills it,
    and its exit then waits for the drain."""
    if v.reap() is None:
        os.kill(v.pid, signal.SIGKILL)
        end = time.time() + 10
        while v.reap() is None and time.time() < end:
            v.pump(0.1)
    os.close(v.fd)
    end = time.time() + 10
    while v.reap() is None and time.time() < end:
        time.sleep(0.05)
    if v.rc is None:
        print("  leaving view %d unreaped" % v.pid)


def test_stop():
    # (a) an answer larger than a pipe still streaming
    for i in range(3):
        h = Stopper("stream", follow=1)
        seen = set()
        hosts("q=127.0.0.1:%d" % h.port)
        v = Pty([DCH, "--xray", "q:fk"])
        try:
            v.wait(b"Prompts 1")
            v.pump(0.5)
            seen.update(kids_of(v.pid))
            t0 = time.time()
            v.key(b"q", 0)
            rc = v.exit(STOP_S)
            dt = time.time() - t0
            check("stream %d: q while the follow streams exits within %.1f s "
                  "(%.2f s)" % (i, STOP_S, dt), rc is not None, v.out[-200:])
            v.exit(10)
            gone("stream %d" % i, v, v.rc, seen)
        finally:
            shut(v)
            h.close()
    h = Stopper("stream")
    seen = set()
    hosts("q=127.0.0.1:%d" % h.port)
    v = Pty([DCH, "--xray", "q:fk"])
    try:
        check("stream: the view opens", v.wait(b"Prompts 1"), v.out[-300:])
        seen.update(kids_of(v.pid))
        n0 = None
        worst = [0.0, 0.0]
        bad = []
        for i in range(20):
            ok = card(v, i == 0)
            v.pump(0.2)     # the mention lines stream
            seen.update(kids_of(v.pid))
            if i % 2:
                # f: the full card stops the light one still streaming
                mark = len(v.out)
                t0 = time.time()
                v.key(b"f", 0)
                ok = v.wait(b"full scan", start=mark, secs=STOP_S) and ok
                worst[1] = max(worst[1], time.time() - t0)
                v.pump(0.2)
                seen.update(kids_of(v.pid))
            mark = len(v.out)
            t0 = time.time()
            v.key(b"\033", 0)
            ok = v.wait(b"Prompts 1", start=mark, secs=STOP_S) and ok
            worst[0] = max(worst[0], time.time() - t0)
            # A card child still alive XV_KILL_MS after its SIGKILL is kept
            # and reaped by a later loop turn (xview.c xv_stop, #212): on a
            # slow runner wait for it to go, up to 10 s; a zombie or a
            # second child at the end is still a failure.
            end = time.time() + 10
            while True:
                v.pump(0.1)
                k = kids_of(v.pid)
                if len(k) == 1 and "Z" not in "".join(k.values()) or \
                        time.time() > end:
                    break
            if not ok or len(k) != 1 or "Z" in "".join(k.values()):
                bad.append((i, ok, k))
            if v.reap() is not None or len(bad) > 2:
                break
            if i == 0:
                n0 = nfds(v.pid)
        n1 = nfds(v.pid)
        check("stream: 20 cards, each left with Esc while its lines stream "
              "(odd ones after f replaced the light card) within %.1f s "
              "(worst Esc %.2f s, f %.2f s), only the follow child after "
              "each, no zombie" % (STOP_S, worst[0], worst[1]),
              not bad and i == 19, bad)
        if n0 is None or n1 is None:
            print("  skip the view's fd count (no /proc, no lsof)")
        else:
            check("stream: the view's fds do not grow over 20 cards "
                  "(%s, %s)" % (n0, n1), n1 <= n0, (n0, n1))
        ok = card(v)
        v.pump(0.2)
        seen.update(kids_of(v.pid))
        rc, dt = esc_q(v)
        check("stream: Esc then q while a card streams exits within %.1f s "
              "(%.2f s)" % (STOP_S, dt), ok and rc is not None and dt < STOP_S,
              v.out[-200:])
        v.exit(10)
        gone("stream", v, v.rc, seen)
    finally:
        shut(v)
        h.close()
    # (b) a silent host: each stop used to wait RM_CANCEL_MS
    h = Stopper("silent")
    seen = set()
    hosts("q=127.0.0.1:%d" % h.port)
    v = Pty([DCH, "--xray", "q:fk"])
    try:
        check("silent: the view opens", v.wait(b"Prompts 1"), v.out[-300:])
        seen.update(kids_of(v.pid))
        ok = card(v, True)
        seen.update(kids_of(v.pid))
        mark = len(v.out)
        t0 = time.time()
        v.key(b"\033", 0)
        ok = v.wait(b"Prompts 1", start=mark, secs=STOP_S) and ok
        check("silent: Esc from a pending card within %.1f s (%.2f s)" % (
            STOP_S, time.time() - t0), ok, v.out[mark:][-300:])
        ok = card(v)
        seen.update(kids_of(v.pid))
        mark = len(v.out)
        t0 = time.time()
        v.key(b"f", 0)
        ok = v.wait(b"full scan", start=mark, secs=STOP_S) and ok
        check("silent: f (a new card) over a pending one within %.1f s "
              "(%.2f s)" % (STOP_S, time.time() - t0), ok,
              v.out[mark:][-300:])
        seen.update(kids_of(v.pid))
        k = kids_of(v.pid)
        # #294: Refs' screen read is a third, pending on the silent host
        # since the Tab to Refs (one at a time; the view's end stops it);
        # the task and memory lists come in the follow (#448)
        check("silent: only the follow, the new card's child and Refs' "
              "screen read run, no zombie", len(k) == 3 and
              "Z" not in "".join(k.values()), k)
        rc, dt = esc_q(v)
        check("silent: Esc then q (card and follow pending) exits within "
              "%.1f s (%.2f s)" % (STOP_S, dt), rc is not None and dt < STOP_S,
              v.out[-200:])
        v.exit(10)
        gone("silent", v, v.rc, seen)
    finally:
        shut(v)
        h.close()
    h = Stopper("silent", snap=0)
    seen = set()
    hosts("q=127.0.0.1:%d" % h.port)
    v = Pty([DCH, "--xray", "q:fk"])
    try:
        v.pump(1.0)
        seen.update(kids_of(v.pid))
        t0 = time.time()
        v.key(b"q", 0)
        rc = v.exit(STOP_S)
        dt = time.time() - t0
        check("silent, no snapshot: q exits within %.1f s (%.2f s)" % (
            STOP_S, dt), len(seen) == 1 and rc is not None,
            (seen, v.out[-200:]))
        v.exit(10)
        gone("silent, no snapshot", v, v.rc, seen)
    finally:
        shut(v)
        h.close()


def main(port, part="a"):
    if part == "b":
        test_stop()
        return
    test_failures()
    test_link_drop()
    test_turn_drop()
    test_responses_fake()
    test_search_fake()
    fixture_tasks()
    test_tasks(port)
    test_tasks_fake()
    fixture_memory()
    test_memory(port)
    test_memory_fake()


if __name__ == "__main__":
    sys.exit(run(main))
