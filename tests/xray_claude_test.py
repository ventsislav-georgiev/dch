#!/usr/bin/env python3
"""`dch --xray <n> --json` on a live and a History Claude: the transcript, the
open seam's counting test, the INFO-2 name rule, refs, --search, tasks,
--chapter, caps, latest, scope and the light anchors.

Split from xray_cli_test.py; helpers in tests/xray_cli_lib.py.
Run:  python3 tests/xray_claude_test.py          (uses ./dch; run.sh isolates)
"""
import json, os, random, re, shutil, socket, stat, struct, subprocess, sys
import tempfile, threading, time

from xray_cli_lib import *  # noqa: E402,F401,F403
from xray_cli_lib import X  # noqa: E402,F401

# #467 took the near-cap rows from 16.0-17.0 MB to 13-14.3 MB: 15 MiB
# (15.7 MB) holds that gain (NIT-UD1)
NEARCAP = 15 << 20


def test_claude():
    n = "xc-cl"
    bindir = os.path.join(T, "bin")
    fdir = os.path.join(T, "fake")
    proj = os.path.join(T, "proj")
    for d in (bindir, fdir, proj):
        os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    trace = os.path.join(T, "trace")
    env = dict(ENV, PATH=bindir + ":" + ENV.get("PATH", "/usr/bin:/bin"),
               FAKE_CLAUDE_DIR=fdir)
    r = subprocess.run([DCH, "--spawn", n, "--size", "80x24", "--cwd", proj,
                        "claude"], capture_output=True, text=True, timeout=30,
                       env=env)
    check("claude: spawned", r.returncode == 0, r.stderr)
    try:
        sdir = os.path.join(HOME, ".claude", "sessions")
        rec = wait_for(lambda: [f for f in os.listdir(sdir)
                                if f.endswith(".json")]
                       if os.path.isdir(sdir) else None, 10)
        sid = json.load(open(os.path.join(sdir, rec[0])))["sessionId"]
        pdir = os.path.join(HOME, ".claude", "projects", key(proj))
        os.makedirs(pdir, exist_ok=True)
        tp = os.path.join(pdir, sid + ".jsonl")
        with open(tp, "w") as f:
            for i, t in enumerate(("first ask", "second ask")):
                f.write(json.dumps({"type": "user", "cwd": proj,
                                    "timestamp": "2026-11-10T10:0%d:00Z" % i,
                                    "message": {"content": t}}) + "\n")
                f.write(json.dumps({"type": "assistant", "message": {
                    "content": [{"type": "text", "text": "ok"}]}}) + "\n")
        tmpd = os.path.join(T, "qtmp")
        os.makedirs(tmpd)
        dbg = dict(ENV, DCH_DEBUG=trace, TMPDIR=tmpd)
        state = os.path.join(T, "state")
        before = files(T)
        state0 = files(state)
        rc, o, err = wait_for(lambda: (lambda x: x if x[1] and
                                       x[1]["corpus"] == "claude" else None)
                              (xray(n, "--json", env=dbg)), 10) or (1, None,
                                                                    "")
        check("claude: a live session's transcript chapters",
              rc == 0 and o and o["live"] and o["session_id"] == sid and
              [c["title"] for c in o["chapters"]] == ["first ask",
                                                      "second ask"] and
              o["project"] == key(proj) and o["project_label"] == proj,
              (rc, o, err))
        st = os.stat(tp)
        check("claude: anchors are f<ino hex>:<offset>",
              o and o["chapters"][0]["anchor"] == "f%x:0" % st.st_ino, o)
        rc, o2, _ = xray(sid, "--json", env=dbg)
        check("claude: the agent UUID answers the same session",
              rc == 0 and o2 and o2["session"] == n and
              o2["chapters"] == o["chapters"], o2)
        # --chapter on a live Claude (3.5, #156): one seam open, no write
        open(trace, "w").close()
        rc, c, err, _ = chapter(n, o["chapters"][1]["anchor"], env=dbg)
        tr = open(trace).read()
        check("chapter: a live Claude's chapter, from the transcript",
              rc == 0 and c and c["session"] == n and
              c["session_id"] == sid and c["title"] == "second ask" and
              c["entries"] == [ent("user", "second ask"),
                               ent("assistant", "ok")], (rc, c, err))
        check("chapter: one seam open, no transcript text traced",
              tr.count("xray seam: open") == 1 and "second ask" not in tr,
              tr[-400:])
        wait_for(lambda: "Claude Code" in screen(n), 10)
        scr0 = screen(n)
        rec0 = dch("--read", n, "--recent", "1000").stdout
        rc, c, err = xray(n, "--clear", "--json", env=dbg)
        check("clear: an agent session has no ring (ring_cleared false)",
              rc == 0 and c == {"schema": 1, "session": n, "session_id": sid,
                                "corpus": "claude", "ring_cleared": False},
              (c, err))
        # MED-1 (QA 1.44): ring_cleared false means the mirror is untouched
        check("clear: an agent session's screen and scrollback survive "
              "(--read, --read --recent)", scr0.strip() and
              screen(n) == scr0 and dch("--read", n, "--recent",
                                        "1000").stdout == rec0,
              (scr0[-200:], screen(n)[-200:]))
        # an older or foreign client still sends the op: the master ignores
        # it while an agent runs (ACK, nothing cleared)
        check("clear: a raw MSG_RING_CLEAR to an agent session is ACKed and "
              "clears nothing", X.clear(n) and screen(n) == scr0,
              screen(n)[-200:])

        # the seam's counting test
        open(trace, "w").close()
        xray(n, "--json", env=dbg)
        tr = open(trace).read()
        check("seam: one Claude query, exactly one open, none refused",
              tr.count("xray seam: open") == 1 and
              "xray seam: refused" not in tr, tr[-600:])
        check("seam: no transcript text in a trace line",
              "first ask" not in tr and "second ask" not in tr)
        open(trace, "w").close()
        xray(n, "--clear", "--json", env=dbg)
        check("seam: --clear opens nothing",
              "xray seam:" not in open(trace).read())
        # two <sid>.jsonl under two project dirs: an ambiguous lookup
        other = os.path.join(HOME, ".claude", "projects", "-elsewhere")
        os.makedirs(other)
        shutil.copy(tp, os.path.join(other, sid + ".jsonl"))
        open(trace, "w").close()
        rc, o3, _ = xray(n, "--json", env=dbg)
        tr = open(trace).read()
        check("seam: two hits open nothing (corpus none, session_id kept)",
              rc == 0 and o3["corpus"] == "none" and
              o3["session_id"] == sid and "xray seam:" not in tr and
              "xray lookup: 2 hits" in tr, (o3, tr[-400:]))
        check("seam: two hits report the cwd's project key, not a hit's "
              "(QA 1.44 LOW-3)", o3 and o3["project"] ==
              key(o3["project_label"]) and o3["project_label"] ==
              os.path.realpath(proj), o3)
        shutil.rmtree(other)
        # a symlink in the transcript's place: lstat'ed, never opened
        os.rename(tp, tp + ".real")
        os.symlink(tp + ".real", tp)
        open(trace, "w").close()
        rc, o4, _ = xray(n, "--json", env=dbg)
        tr = open(trace).read()
        check("seam: a symlinked transcript is not followed",
              rc == 0 and o4["corpus"] == "none" and "xray seam: open" not in
              tr, (o4, tr[-400:]))
        os.unlink(tp)
        os.rename(tp + ".real", tp)
        after = files(T)
        new = sorted(p for p in set(after) - set(before)
                     if p != trace and "/fake/" not in p and
                     "/-elsewhere/" not in p and "/state/" not in p and
                     # the master's own sidecars (.act on fake claude output)
                     not p.startswith(sock(n) + "."))
        check("nothing written: no file appears outside the trace", not new,
              new)
        state1 = files(state)
        check("nothing written: the state dir is as it was (names, sizes, "
              "mtimes)", state1 == state0, sorted(
                  set(state1.items()) ^ set(state0.items())))
        check("nothing written: TMPDIR stays empty (spec 9.5)",
              os.listdir(tmpd) == [], os.listdir(tmpd))
        check("nothing written: the transcript is untouched",
              after.get(tp, (0, 0))[0] == before[tp][0])

        # QA 1.44 LOW-4: there but unreadable is exit 3, not "gone"
        if os.geteuid() != 0:
            os.chmod(tp, 0)
            r = dch("--xray", n, "--json")
            os.chmod(tp, 0o600)
            check("claude: an unreadable transcript exits 3", r.returncode ==
                  3 and r.stderr == "dch: X-ray failed on this session\n",
                  (r.returncode, r.stderr))
        # QA 1.44 LOW-7: a symlinked project dir is not followed
        moved = os.path.join(T, "moved-proj")
        os.rename(pdir, moved)
        os.symlink(moved, pdir)
        rc, o5, _ = xray(n, "--json")
        check("claude: a symlinked project dir is not followed (corpus "
              "none)", rc == 0 and o5 and o5["corpus"] == "none", o5)
        os.unlink(pdir)
        os.rename(moved, pdir)
        rc, o5, _ = xray(n, "--json")
        check("claude: the real dir back, the transcript answers",
              rc == 0 and o5 and o5["corpus"] == "claude", o5)
        # MED-2 (QA 1.44): ids are kept as a bounded key, never whole
        def put(lines):
            with open(tp, "w") as f:
                f.write(json.dumps({"type": "user", "cwd": proj, "message":
                                    {"content": "start"}}) + "\n")
                for ln in lines:
                    f.write(json.dumps(ln) + "\n")
        big = lambda i: "%08d" % i + "x" * 1000000
        for what, mk in (
                ("tool_use ids", lambda i: {"type": "assistant", "message": {
                    "content": [{"type": "tool_use", "id": big(i),
                                 "name": "Bash", "input": {}}]}}),
                ("agentIds", lambda i: {"type": "assistant",
                                        "isSidechain": True,
                                        "agentId": big(i),
                                        "message": {"content": "s"}})):
            put(mk(i) for i in range(24))
            rc, out, rss = maxrss([DCH, "--xray", n, "--json"], env)
            try:
                o = json.loads(out)
            except ValueError:
                o = None
            check("claude: 24 x 1 MB %s answer" % what, rc == 0 and o and
                  o["corpus"] == "claude", (rc, out[:200]))
            rss_ok("claude: 24 x 1 MB %s" % what, rss)
        # QA 1.44 LOW-8: lines near every cap at once (6000 chapters, then
        # 20 lines just under 1 MiB of ~100k tokens and one paste each)
        z = ",".join(["0"] * 99960)
        room = 1048576 - len(z) - 300
        heavy = ('{"type":"user","x":[' + z + '],"message":{"content":'
                 '"<pasted_content id=\\"k\\">' + "a" * (room - 60) +
                 '</pasted_content id=\\"k\\">"}}')
        with open(tp, "w") as f:
            f.write((json.dumps({"type": "user", "message": {
                "content": "\u0001" * 130}}) + "\n" +
                '{"type":"assistant","message":{"content":"s"}}\n') * 6000)
            f.write((heavy + '\n{"type":"assistant","message":{"content":'
                     '"s"}}\n') * 20)
        rc, out, rss = maxrss([DCH, "--xray", n, "--json"], env)
        check("claude: near-cap lines answer", rc == 0 and
              out.startswith("{"), (rc, out[:200]))
        rss_ok("claude: near-cap lines (6000 chapters, 20 x 1 MiB pastes)",
               rss, NEARCAP)
        # #192: the same with every chapter auto and its summary at the
        # cap (120 4-byte scalars, 480 bytes): summaries are held out of
        # line, at most 256 KiB of them (XR_SUMMARIES), so only these
        # chapters pay, and at most that
        tn = ("<task-notification>\n<task-id>t</task-id>\n<summary>" +
              "\U0001F600" * 130 + "</summary>\n</task-notification>")
        with open(tp, "w") as f:
            f.write((json.dumps({"type": "user", "origin": {
                "kind": "task-notification"}, "message": {
                "content": tn}}) + "\n" +
                '{"type":"assistant","message":{"content":"s"}}\n') * 6000)
            f.write((heavy + '\n{"type":"assistant","message":{"content":'
                     '"s"}}\n') * 20)
        rc, out, rss = maxrss([DCH, "--xray", n, "--json"], env)
        try:
            o = json.loads(out)
        except ValueError:
            o = None
        check("claude: near-cap auto chapters answer, the newest summary at "
              "480 bytes, the oldest given up (still auto)",
              rc == 0 and o and len(o["chapters"]) > 21 and
              o["chapters"][-21].get("auto") is True and
              len(o["chapters"][-21].get("summary", "").encode()) == 480 and
              o["chapters"][0].get("auto") is True and
              "summary" not in o["chapters"][0], (rc, out[:200]))
        rss_ok("claude: near-cap auto lines (6000 auto chapters with 480-byte "
               "summaries, 20 x 1 MiB pastes)", rss, NEARCAP)
        # the long ids still pair: a quiet Grep's failed result shows no
        # error; an orphan result (no pairing) would mark the chapter
        put([{"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": big(1), "name": "Grep",
                 "input": {"pattern": "x"}}]}},
             {"type": "user", "message": {"content": [
                 {"type": "tool_result", "tool_use_id": big(1),
                  "is_error": True, "content": "x"}]}}])
        rc, o, err = xray(n, "--json")
        check("claude: a 1 MB tool_use id still pairs with its result",
              rc == 0 and o and [(c["title"], c["error"]) for c in
                                 o["chapters"]] == [("start", False)],
              (rc, o and o["chapters"], err))
        # MED-3 (QA 1.44): unclosed <pasted_content> openers are linear
        # (each line under the 1 MiB line cap)
        for what, op, cnt in (
                ("one id", lambda k: '<pasted_content id="a">', 40000),
                ("unique ids", lambda k: '<pasted_content id="a%d">' % k,
                 30000)):
            ask = {"type": "user", "message": {"content": "".join(
                op(k) for k in range(cnt))}}
            put([{"type": "assistant", "message": {"content": [
                {"type": "text", "text": "ok"}]}}, ask] * 3)
            t0 = time.time()
            rc, o, err = xray(n, "--json")
            dt = time.time() - t0
            check("claude: 3 lines of %dk unclosed openers (%s) in %.2fs "
                  "(< 2 s)" % (cnt // 1000, what, dt), rc == 0 and o and dt < 2 and
                  [c["title"] for c in o["chapters"][1:]] == [
                      "".join(op(k) for k in range(6))[:120]] * 3,
                  (rc, dt, o and o["chapters"][:2], err))
        # #155: a live Claude as <n>, --scope project: its own transcript
        # (live), then a History session of its project dir
        put([{"type": "user", "message": {"content": "see #941"}}])
        osid = "33333333-4444-4555-8666-777777777777"
        with open(os.path.join(pdir, osid + ".jsonl"), "w") as f:
            f.write(json.dumps({"type": "user", "cwd": proj, "message": {
                "content": "older #941"}}) + "\n")
        history("xc-cl-old", osid, os.path.join(HOME, ".claude"),
                os.path.realpath(proj))
        open(trace, "w").close()
        r = dch("--xray", n, "--ref", "#941", "--scope", "project", "--full",
                "--json", env=dbg)
        ls = ref_lines(r.stdout) or [{}]
        tl = [(l["tally"]["session"], l["tally"]["live"]) for l in ls
              if "tally" in l]
        check("scope: a live Claude <n>, then its project's History session",
              r.returncode == 0 and tl == [(n, True), ("xc-cl-old", False)]
              and ls[-1]["counts"]["project"] == 1 and
              open(trace).read().count("xray seam: open") == 2,
              (tl, r.stdout[-400:], r.stderr))
    finally:
        dch("-k", n, env=env)


# ---- INFO-2: the name rule ------------------------------------------------

def test_names():
    def refused(r, msg):
        check("name rule: %s exits 2 'name looks like a session_id'" % msg,
              r.returncode == 2 and
              "dch: name looks like a session_id" in r.stderr, r.stderr)
        check("name rule: %s starts nothing" % msg,
              not os.path.exists(sock(ID)))

    refused(dch("-n", ID), "-n")
    refused(dch("--spawn", ID, "sh"), "--spawn")
    for pad in (" " + ID, ID + " ", "\t" + ID):
        r = dch("--spawn", pad, "sh", "-c", "sleep 5")
        check("name rule: --spawn %r (padded) exits 2" % pad,
              r.returncode == 2 and "looks like a session_id" in r.stderr,
              (r.returncode, r.stderr))
        if r.returncode == 0:
            dch("-k", pad)
    r = dch("--spawn", "xc-old", "sh", "-c", "exit 0")
    time.sleep(0.5)
    refused(dch("--revive", "xc-old", "--as", ID), "--revive --as")
    r = dch("--spawn", "xc-live", "sh", "-c", "sleep 30")
    try:
        r = dch("-m", "xc-live", ID)
        check("name rule: -m (rename) exits 2", r.returncode == 2 and
              "looks like a session_id" in r.stderr, r.stderr)
        check("name rule: the alias is not written",
              ID not in dch("-ls").stdout)
        for pad in ("  " + ID, ID + " ", "\t" + ID + "\n"):
            r = dch("-m", "xc-live", pad)
            check("name rule: -m with whitespace padding %r exits 2" % pad,
                  r.returncode == 2 and "looks like a session_id" in r.stderr,
                  (r.returncode, r.stderr))
        # LOW-1 (QA 1.45.1): any case, since a case-insensitive file
        # system answers the id with the variant's socket
        for v in ("m0123456789abcdeF", UP):
            r = dch("-m", "xc-live", v)
            check("name rule: -m %s (a case variant) exits 2" % v,
                  r.returncode == 2 and "looks like a session_id" in r.stderr,
                  r.stderr)
            r = dch("--spawn", v, "sh", "-c", "sleep 5")
            check("name rule: --spawn %s (a case variant) exits 2" % v,
                  r.returncode == 2 and "looks like a session_id" in r.stderr,
                  r.stderr)
            if r.returncode == 0:
                dch("-k", v)
        r = dch("--spawn", "m0123456789abcdeg", "sh", "-c", "sleep 1")
        check("name rule: a non-hex letter is a name", r.returncode == 0,
              r.stderr)
    finally:
        dch("-k", "xc-live")
        dch("-k", "m0123456789abcdeg")

    # a session that already has such a name (an older dch let it) keeps
    # it, by its exact name only: its case variant (the id) is no name
    legacy = subprocess.Popen([DCH, "--master-of", sock(UP), "--", "sh",
                               "-c", "sleep 60"], env=ENV,
                              stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,
                              start_new_session=True)
    try:
        wait_for(lambda: os.path.exists(sock(UP)) and
                 dch("--status", UP).returncode == 0, 10)
        rc, o, err = xray(UP, "--json")
        check("legacy %s: --xray by its exact name answers" % UP,
              rc == 0 and o and o["session"] == UP, (rc, err))
        r = dch("-n", UP)
        check("legacy %s: -n still finds it (attaching needs a terminal)"
              % UP, r.returncode == 1 and "terminal" in r.stderr,
              (r.returncode, r.stderr))
        rc, o, err = xray(ID, "--json")
        check("legacy: --xray %s is not that session by name" % ID,
              rc == 1 and "no such session" in err, (rc, o, err))
        rc, o, err = xray(ID, "--clear", "--json")
        check("legacy: --xray %s --clear clears nothing of it" % ID,
              rc == 1 and "no such session" in err, (rc, o, err))
        r = dch("-n", ID)
        check("legacy: -n %s is refused, not attached to it" % ID,
              r.returncode == 2 and "looks like a session_id" in r.stderr,
              (r.returncode, r.stderr))
    finally:
        dch("-k", UP)
        try:
            legacy.wait(10)
        except subprocess.TimeoutExpired:
            legacy.kill()
            legacy.wait()


# ---- two Claudes under one shell: the master ignores the clear -----------

def test_clear_after_claude():
    """LOW-3 (QA 1.45.1): a Claude typed into a shell pauses the shell's
    ring on the master's next agent tick; once it exits, a --clear clears
    the ring at once, not only after the next tick (DCH_RECORD_MS apart)."""
    n = "xc-after"
    bindir = os.path.join(T, "bin")
    fdir = os.path.join(T, "fake")
    for d in (bindir, fdir):
        os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    env = dict(ENV, PATH=bindir + ":" + ENV.get("PATH", "/usr/bin:/bin"),
               FAKE_CLAUDE_DIR=fdir, DCH_RECORD_MS="10000")
    r = subprocess.run([DCH, "--spawn", n, "--size", "80x24", "sh"],
                       capture_output=True, text=True, timeout=30, env=env)
    check("after claude: shell spawned", r.returncode == 0, r.stderr)

    def status():
        try:
            s = X.connect(n)
            try:
                return X.read(s, 0, 1)[0].get("status", 0)
            finally:
                s.close()
        except (OSError, EOFError):
            return 0
    try:
        wait_for(lambda: "$" in screen(n) or "#" in screen(n), 5)
        dch("--send", n, "claude\r", env=env)
        paused = wait_for(lambda: status() & X.S_PAUSED, 25)
        t0 = time.time()
        log = os.path.join(fdir, n + ".log")
        dch("--send", n, "/exit\r", env=env)
        quit_ = wait_for(lambda: os.path.exists(log) and
                         '"exit"' in open(log).read(), 5)
        sdir = os.path.join(HOME, ".claude", "sessions")
        wait_for(lambda: not [f for f in os.listdir(sdir)
                              if f.endswith(".json")], 5)
        rc, c, err = xray(n, "--clear", "--json")
        secs = time.time() - t0
        check("after claude: --clear at once clears the shell's ring "
              "(%.1f s after the pause, the tick is 10 s)" % secs,
              paused and quit_ and rc == 0 and c and
              c["corpus"] == "shell" and c["ring_cleared"] is True and
              secs < 8, (paused, quit_, rc, c, err))
    finally:
        dch("-k", n)


def test_two_claudes():
    """claude_find answers 2: the master's probe says agent and ignores the
    clear, so the client must say ring_cleared false (review LOW-1)."""
    n = "xc-two"
    bindir = os.path.join(T, "bin2")
    fdir = os.path.join(T, "fake2")
    for d in (bindir, fdir):
        os.makedirs(d, exist_ok=True)
    shutil.copy(os.path.join(HERE, "fake_claude.py"),
                os.path.join(bindir, "claude"))
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    env = dict(ENV, PATH=bindir + ":" + ENV.get("PATH", "/usr/bin:/bin"),
               FAKE_CLAUDE_DIR=fdir)
    sdir = os.path.join(HOME, ".claude", "sessions")
    had = set(os.listdir(sdir)) if os.path.isdir(sdir) else set()
    r = subprocess.run([DCH, "--spawn", n, "--size", "80x24", "sh", "-c",
                        "seq 1 300; exec 3<&0; claude <&3 & sleep 0.5; "
                        "claude"], capture_output=True, text=True,
                       timeout=30, env=env)
    check("two claudes: spawned", r.returncode == 0, r.stderr)
    try:
        if dch("--read", n).returncode == 3:
            print("  skip two claudes: dch-lite keeps no ring")
            return
        wait_for(lambda: len([f for f in os.listdir(sdir) if f not in had
                              and f.endswith(".json")]) >= 2
                 if os.path.isdir(sdir) else None, 10)
        _, r0 = X.read_all(n, 0)
        rc, c, err = xray(n, "--clear", "--json")
        check("two claudes: --clear says ring_cleared false",
              rc == 0 and c and c["ring_cleared"] is False, (rc, c, err))
        h, r1 = X.read_all(n, 0)
        # at least seq's rows that scrolled off the 24-row pane
        check("two claudes: the ring keeps its rows",
              len(r1) == len(r0) and len(r1) >= 276, (len(r0), len(r1)))
        # the read probes for the agent (QA 1.54.0 MED-2): paused, so the
        # client, which maps no one Claude here, answers no shell corpus
        _, o, _ = xray(n, "--json")
        check("two claudes: the ring reads paused, no shell answer",
              h["status"] & X.S_PAUSED and o and o["corpus"] == "none",
              (h, o))
    finally:
        dch("-k", n)


def history(name, sid, cfg, cwd="/home/demo/proj"):
    sockdir = os.path.join(T, "dch-%d" % os.getuid())
    hdir = os.path.join(T, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    os.makedirs(hdir, exist_ok=True)
    rec = {"name": name, "cwd": cwd, "end": "exited",
           "argv": ["claude"], "started": 1794299400, "updated": 1794300000,
           "ended": 1794300000, "sock_dir": sockdir,
           "agent": {"session_id": sid, "cwd": cwd,
                     "config_dir": cfg}}
    with open(os.path.join(hdir, name + ".json"), "w") as f:
        f.write(json.dumps(rec) + "\n")


def test_refs():
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, RSID + ".jsonl")
    canary = "Canary passage 7f3a"

    def put(lines):
        with open(tp, "w") as f:
            for o in lines:
                f.write(json.dumps(o) + "\n")

    def bash(id, cmd, out):
        return [{"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": id, "name": "Bash",
                     "input": {"command": cmd}}]}},
                {"type": "user", "toolUseResult": {"stdout": out},
                 "message": {"content": [{"type": "tool_result",
                                          "tool_use_id": id,
                                          "content": out}]}}]
    put([{"type": "user", "cwd": "/home/demo/proj",
          "timestamp": "2026-11-10T10:00:00Z",
          "message": {"content": "look at #831 and o/r#7"}},
         {"type": "assistant", "message": {"content": [
             {"type": "text", "text": "checking #831 now"}]}}] +
        bash("b1", "gh issue view 831", "# #831 " + canary) +
        bash("b2", "gh pr view https://github.com/o/r/pull/7",
             "title of o/r#7"))
    history("xr-refs", RSID, cfg)
    trace = os.path.join(T, "trace-refs")
    dbg = dict(ENV, DCH_DEBUG=trace)

    # mutant 3 (K3): the index goes with its query
    open(trace, "w").close()
    rc, o, err = xray("xr-refs", "--json", env=dbg)
    tr = open(trace).read()
    m = re.search(r"xray refs: keys=(\d+) live=(\d+)", tr)
    check("refs: the index answers the transcript's keys",
          rc == 0 and o and [r["key"] for r in o["refs"]] ==
          ["issue:#831", "pull:o/r#7"] and o["refs"][0]["defined"], (o, err))
    check("refs: the index is freed with its query (K3: live=0)",
          m and int(m.group(1)) == 2 and m.group(2) == "0", tr[-400:])

    # nothing written by an index build and a full --ref (9.5)
    before = files(T)
    open(trace, "w").close()
    r = dch("--xray", "xr-refs", "--ref", "#831", "--scope", "session", "--full",
            "--json", env=dbg)
    ls = ref_lines(r.stdout) or []
    check("ref: a full --ref finds the definition",
          r.returncode == 0 and any(l.get("definition") for l in ls) and
          ls[-1].get("done") is True and ls[-1]["why"] is None,
          r.stdout[-600:] + r.stderr)
    # #469: who/tool of each mention; a result carries its call's tool name
    r = dch("--xray", "xr-refs", "--ref", "o/r#7", "--scope", "session",
            "--full", "--json", env=dbg)
    wt = sorted((l["mention"]["line"][:8], l["mention"].get("who"),
                 l["mention"].get("tool"))
                for l in ref_lines(r.stdout) or [] if "mention" in l)
    r2 = dch("--xray", "xr-refs", "--ref", "#831", "--scope", "session",
             "--full", "--json", env=dbg)
    wt += sorted((l["mention"]["line"][:8], l["mention"].get("who"),
                  l["mention"].get("tool"))
                 for l in ref_lines(r2.stdout) or [] if "mention" in l)
    check("ref: who/tool per mention (user, assistant, Bash call and result)",
          wt == [("gh pr vi", "tool", "Bash"), ("look at ", "user", None),
                 ("title of", "tool", "Bash"), ("# #831 C", "tool", "Bash"),
                 ("checking", "assistant", None), ("look at ", "user", None)],
          (wt, r.stdout[-600:]))
    tr = open(trace).read()
    check("ref: the trace holds no passage or transcript text",
          canary not in tr and "look at" not in tr, tr[-400:])
    new = sorted(set(files(T)) - set(before) - {trace})
    check("ref: nothing written by a --ref", not new, new)

    # #477: a long non-ASCII tool name is cut on a character boundary
    tn = "mcp_" + "\u00e9" * 70
    put([{"type": "user", "cwd": "/home/demo/proj",
          "timestamp": "2026-11-10T10:00:00Z",
          "message": {"content": "start"}},
         {"type": "assistant", "message": {"content": [
             {"type": "tool_use", "id": "m1", "name": tn, "input": {"q": "x"}}]}},
         {"type": "user", "toolUseResult": {"stdout": "# #831 note"},
          "message": {"content": [
              {"type": "tool_result", "tool_use_id": "m1",
               "content": "# #831 note"}]}}])
    r = dch("--xray", "xr-refs", "--ref", "#831", "--scope", "session",
            "--full", "--json")
    tl = [l["mention"].get("tool") for l in ref_lines(r.stdout) or []
          if "mention" in l and l["mention"].get("who") == "tool"]
    check("ref: a long non-ASCII tool name is cut at a character, <= 127 bytes",
          len(tl) == 1 and tl[0] and "\ufffd" not in tl[0] and
          len(tl[0].encode()) <= 127 and tn.startswith(tl[0]),
          (tl, r.stdout[-400:]))

    # NIT-7 (#150): a relative path that only outgrows the key against the
    # session's cwd is no ref, as main's check says for a short one (exit 2)
    history("xr-deep", RSID, cfg, "/" + "d" * 1000)
    r = dch("--xray", "xr-deep", "--ref", "src/" + "a" * 40 + ".c:3",
            "--scope", "session", "--json")
    check("ref: a relative path past the key cap against a deep cwd is "
          "not a ref (exit 2)",
          r.returncode == 2 and r.stderr == "dch: not a ref\n" and
          not r.stdout, (r.returncode, r.stderr, r.stdout[:200]))

    # a big transcript: light stops at its budget, full reads it all. Every
    # line holds 515151 as an id value, no ref, so the prefilter (#293)
    # decodes every window and the budget is what stops the read
    pad = "x" * 900
    with open(tp, "w") as f:
        f.write(json.dumps({"type": "user", "cwd": "/home/demo/proj",
                            "timestamp": "2026-11-10T10:00:00Z",
                            "message": {"content": "start #1 #515151"}})
                + "\n")
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "PROJ-5 o/r#3 id: 515151 " + pad}]}}) + "\n"
        f.write(line * 40000)
        f.write(json.dumps({"type": "user", "message": {
            "content": "the end #424242"}}) + "\n")
    t0 = time.time()
    r = dch("--xray", "xr-refs", "--ref", "#515151", "--scope", "session",
            "--budget-ms", "50", "--json")
    ls = ref_lines(r.stdout) or [{}]
    d = ls[-1]
    check("ref: light past its budget stops, partial, why budget: the first "
          "line's ref, read newest first, is not reached (%.2fs)" %
          (time.time() - t0),
          r.returncode == 0 and d.get("done") is True and d["partial"] and
          d["why"] == "budget" and d["bytes"] < os.path.getsize(tp) and
          not any("mention" in l for l in ls), r.stdout[-600:] + r.stderr)
    # #262: the last line's ref is the newest window's, found at once;
    # bytes < 2 MiB of 40 MB is what says newest first. The clock is off
    # (#332): the newest window's cwd is line 1's, 40 MB back, and a slow
    # CI runner spent all 300 ms on that walk, bytes 0, why budget; the row
    # above already proves the budget stops a light scan
    r = dch("--xray", "xr-refs", "--ref", "#424242", "--scope", "session",
            "--json", env=dict(ENV, DCH_XRAY_NOCLOCK="1"))
    ls = ref_lines(r.stdout) or [{}]
    d = ls[-1]
    check("ref: light reads newest first: the last line's ref is found in "
          "the newest window, under 2 MiB of 40 MB read, why first",
          r.returncode == 0 and d.get("done") is True and d["partial"] and
          d["why"] == "first" and d["bytes"] < 2 << 20 and
          [l["mention"]["line"] for l in ls if "mention" in l] ==
          ["the end #424242"], r.stdout[-600:] + r.stderr)
    t0 = time.time()
    r = dch("--xray", "xr-refs", "--ref", "#424242", "--scope", "session",
            "--full", "--json")
    wall = time.time() - t0
    ls = ref_lines(r.stdout) or [{}]
    d = ls[-1]
    check("ref: full reads the whole transcript, one mention",
          r.returncode == 0 and d.get("done") is True and not d["partial"] and
          d["bytes"] == os.path.getsize(tp) and
          sum("mention" in l for l in ls) == 1, r.stdout[-600:] + r.stderr)
    # #333: light's walk back to a window's cwd reads at most 16 MB
    # (1 MiB here), so a far cwd line no longer spends the budget before
    # the newest window: that window has no cwd, and the last line's
    # relative path does not resolve; --full does
    head = dict(ENV, DCH_XRAY_NOCLOCK="1", DCH_XRAY_CAPS="0:0:0:0:0:1048576")
    with open(tp, "a") as f:
        f.write(json.dumps({"type": "user", "message": {
            "content": "see src/far.c:3"}}) + "\n")
    rl = dch("--xray", "xr-refs", "--ref", "src/far.c", "--scope", "session",
             "--json", env=head)
    rf = dch("--xray", "xr-refs", "--ref", "src/far.c", "--scope", "session",
             "--full", "--json")
    ml = [l for l in ref_lines(rl.stdout) if "mention" in l]
    mf = [l for l in ref_lines(rf.stdout) if "mention" in l]
    check("ref: light's cwd walk stops 16 MB back (1 MiB here): a "
          "relative path past it is not resolved; --full resolves it",
          rl.returncode == 0 and rf.returncode == 0 and not ml and
          [l["mention"]["line"] for l in mf] == ["see src/far.c:3"],
          rl.stdout[-600:] + rl.stderr + rf.stdout[-300:])
    # LOW-X28: so light's answer says partial; one for a path no window
    # can hold does not, nor does --full's
    rn = dch("--xray", "xr-refs", "--ref", "src/none.c", "--scope",
             "session", "--json", env=head)
    dl, dn = (ref_lines(rl.stdout) or [{}])[-1], (ref_lines(rn.stdout) or
                                                   [{}])[-1]
    df = (ref_lines(rf.stdout) or [{}])[-1]
    check("ref: light's cwd walk stopped at its cap before a window that "
          "may hold the path: partial; a path no window holds, and --full: "
          "not partial",
          dl.get("done") is True and dl["partial"] is True and
          dn.get("done") is True and dn["partial"] is False and
          df.get("done") is True and df["partial"] is False,
          (dl, dn, df))
    prog = [l for l in ls if "progress" in l]
    check("ref: schema 1 on every line; a progress line per 250 ms "
          "(%d in %.2fs)" % (len(prog), wall),
          all(l.get("schema") == 1 for l in ls) and
          (prog or wall < 0.5), r.stdout[:600])
    os.unlink(tp)


def caps_ref(i):
    """Key i of test_claude_caps: every kind's room filled (#262, XR_ROOMS
    300 tickets, 400 issues, 500 pulls, 250 commits, 350 files, 200 URLs),
    then 100 tickets past it."""
    for n, f in ((300, "PROJ-%d"), (700, "#%d"), (1200, "o/r#%d"),
                 (1450, "c%06x"), (1800, "/home/demo/proj/f%d.c:1"),
                 (2000, "https://e.com/u/%d")):
        if i < n:
            return f % (i + 1000)
    return "PROJ-%d" % (i + 1000)


def test_claude_caps():
    """Retained memory at the index's caps (3.8, l.717): 5000 chapters of
    120-scalar titles, 2000 keys (the 1200 tickets, issues and pulls each
    with 8 definitions of 120-scalar titles), and 100 keys past the cap."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, RSID + ".jsonl")
    pad = "\u00e9" * 100
    with open(tp, "w") as f:
        for i in range(5000):
            f.write(json.dumps({"type": "user", "cwd": "/home/demo/proj",
                                "message": {"content": "prompt %d %s" %
                                            (i, pad)}}) + "\n")
            if i >= 2100:
                continue
            out = "\n".join("# %s %s %d" % (caps_ref(i), pad, j)
                            for j in range(8))
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "b%d" % i, "name": "Bash",
                 "input": {"command": "gh issue view %d" % i}}]}}) + "\n")
            f.write(json.dumps({"type": "user", "toolUseResult": {
                "stdout": out}, "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "b%d" % i,
                     "content": out}]}}) + "\n")
    history("xr-caps", RSID, cfg)
    try:
        t0 = time.time()
        rc, out, rss = maxrss([DCH, "--xray", "xr-caps", "--json"], ENV)
        wall = time.time() - t0
        try:
            o = json.loads(out)
        except ValueError:
            o = None
        refs = (o or {}).get("refs", [])
        shapes = {}
        for x in refs:
            shapes[x["shape"]] = shapes.get(x["shape"], 0) + 1
        # a heading defines a ticket, an issue or a pull, never the rest
        check("caps: 2000 keys, every room full, the 1200 tickets, issues "
              "and pulls of 8 definitions each, 100 dropped (%.2fs)" % wall,
              rc == 0 and len(refs) == 2000 and
              all(x["definitions"] == 8 for x in refs
                  if x["shape"] in ("ticket", "issue", "pull")) and
              shapes == {"ticket": 300, "issue": 400, "pull": 500,
                         "commit": 250, "file": 350, "url": 200} and
              o["dropped_keys"] == 100, (rc, shapes, set((x["shape"], x["definitions"]) for x in refs)))
        rss_ok("caps: --json with 5000 chapters and 2000 x 8 definitions",
               rss)
    finally:
        os.unlink(tp)


# ---- #262: the latest first, as Kindle's X-ray ---------------------------------

LSID = "11111111-2222-4333-8444-5555555555a9"


def test_latest():
    """A generated transcript past the index's cap: 2100 commits, about 4 MB
    of turns, then a last turn naming OQ-32 and o/r#14383. The index keeps
    them (each kind its own room) with the last turn's offset; light --ref
    reads newest first, so a 2 MB bytes cap still finds OQ-32 in the last
    turn, and the answer stays partial."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, LSID + ".jsonl")
    user = lambda t: json.dumps({"type": "user", "cwd": "/home/demo/proj",
                                 "message": {"content": t}}) + "\n"
    said = lambda t: json.dumps({"type": "assistant", "message": {
        "content": [{"type": "text", "text": t}]}}) + "\n"
    with open(tp, "w") as f:
        f.write(user("start with OQ-1"))
        for i in range(21):
            f.write(said(" ".join("a%06x" % (i * 100 + j)
                                  for j in range(100))))
        for i in range(4000):
            if i % 400 == 0:
                f.write(user("turn %d" % i))
            f.write(said("pad %d %s" % (i, "y" * 1000)))
        f.write(user("where are OQ-32 and o/r#14383?"))
        last = f.tell()
        f.write(said("OQ-32 is open; o/r#14383 is in flight"))
    history("xr-latest", LSID, cfg)
    try:
        rc, o, err = xray("xr-latest", "--json")
        refs = {x["key"]: x for x in (o or {}).get("refs", [])}
        check("latest: past 2000 keys (2100 commits first), the last turn's "
              "ticket and pull are in the index",
              rc == 0 and "ticket:OQ-32" in refs and "pull:o/r#14383" in refs
              and "ticket:OQ-1" in refs, (rc, len(refs), err))
        check("latest: last_offset is the offset of the newest mention's line",
              refs.get("ticket:OQ-32", {}).get("last_offset") == last and
              refs.get("ticket:OQ-1", {}).get("last_offset") == 0,
              (last, refs.get("ticket:OQ-32"), refs.get("ticket:OQ-1")))
        ask = user("where are OQ-32 and o/r#14383?")
        caps = dict(ENV, DCH_XRAY_CAPS="0:2000000")
        r = dch("--xray", "xr-latest", "--ref", "OQ-32", "--json", env=caps)
        ls = ref_lines(r.stdout) or [{}]
        men = [l["mention"]["offset"] for l in ls if "mention" in l]
        check("latest: light --ref reads newest first: under a 2 MB bytes cap "
              "both mentions of the last turn are found, partial, why bytes",
              r.returncode == 0 and men == [last - len(ask), last] and
              ls[-1].get("done") and ls[-1]["partial"] and
              ls[-1]["why"] == "bytes" and
              ls[-1]["counts"]["session"] == 2, (r.returncode, ls[-4:]))
        r = dch("--xray", "xr-latest", "--ref", "OQ-32", "--scope", "session",
                "--json", env=caps)
        ls = ref_lines(r.stdout) or [{}]
        men = [l["mention"]["offset"] for l in ls if "mention" in l]
        check("latest: light --scope session stops after the newest "
              "window that holds a mention, with all its mentions (#379), "
              "why first",
              r.returncode == 0 and men == [last - len(ask), last] and
              ls[-1]["partial"] and ls[-1]["why"] == "first",
              (r.returncode, ls[-3:]))
        r = dch("--xray", "xr-latest", "--ref", "OQ-1", "--json", env=caps)
        ls = ref_lines(r.stdout) or [{}]
        check("latest: light --ref of a ref only in the first turn, past the "
              "bytes cap from the end: not found, partial, why bytes",
              r.returncode == 0 and ls[-1].get("done") and
              ls[-1]["partial"] and ls[-1]["why"] == "bytes" and
              ls[-1]["counts"]["session"] == 0, (r.returncode, ls[-2:]))
        r = dch("--xray", "xr-latest", "--ref", "OQ-1", "--json",
                env=dict(ENV, DCH_XRAY_NOCLOCK="1"))
        ls = ref_lines(r.stdout) or [{}]
        men = [l["mention"]["offset"] for l in ls if "mention" in l]
        check("latest: light --ref with no cap reads every window, down to "
              "the first turn; not partial",
              r.returncode == 0 and men == [0] and not ls[-1]["partial"] and
              ls[-1]["bytes"] == os.path.getsize(tp), (ls[-2:],))
        r = dch("--xray", "xr-latest", "--ref", "OQ-32", "--full", "--json")
        ls = ref_lines(r.stdout) or [{}]
        men = [l["mention"]["offset"] for l in ls if "mention" in l]
        check("latest: --full still reads oldest first, both mentions, not "
              "partial", r.returncode == 0 and
              men == [last - len(ask), last] and not ls[-1]["partial"],
              (ls[-3:],))
    finally:
        os.unlink(tp)


# ---- v3.2.2 Q5: D2 parses the whole Claude result, <= 256 KiB ---------------

def test_d2_whole():
    """A gh --json object past the 3000-scalar clip defines through D2 in
    the index and in --ref alike; one over 256 KiB defines nothing."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, RSID + ".jsonl")

    def gh(id, n, title, body, late=False, pad=""):
        o = {"title": title, "body": body, "number": n} if late else \
            {"number": n, "title": title, "body": body}
        out = json.dumps(o) + pad
        return out, [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": id, "name": "Bash", "input": {
                    "command": "gh pr view %d --json number,title,body" %
                    n}}]}},
            {"type": "user", "toolUseResult": {"stdout": out},
             "message": {"content": [{"type": "tool_result",
                                      "tool_use_id": id, "content": out}]}}]
    small, a = gh("d1", 12, "Fix login", "Body line.\n" + "y" * 10240)
    big, b = gh("d2", 13, "Too big", "z" * (300 << 10))
    # the id past the clip: its anchor still counts once (Q8)
    late, c = gh("d3", 15, "Late id", "w" * 10240, late=True)
    # small JSON, the whole result over 256 KiB: no D2 off its clip
    _, e = gh("d4", 14, "Padded", "p", pad=" " * (260 << 10))
    with open(tp, "w") as f:
        for o in [{"type": "user", "cwd": "/home/demo/proj",
                   "timestamp": "2026-11-10T10:00:00Z",
                   "message": {"content": "see #12 and #13 and #14"}}] + \
                a + b + c + e:
            f.write(json.dumps(o) + "\n")
    history("xr-d2", RSID, cfg)
    try:
        rc, o, err = xray("xr-d2", "--json")
        refs = {r["key"]: r for r in (o or {}).get("refs", [])}
        r12, r13 = refs.get("issue:#12", {}), refs.get("issue:#13", {})
        check("d2: a %d KiB object defines in the index, one of %d KiB "
              "does not" % (len(small) >> 10, len(big) >> 10),
              rc == 0 and r12.get("defined") is True and
              r12.get("definitions") == 1 and
              r12.get("title") == "Fix login" and
              r13.get("defined") is False and r13.get("definitions") == 0,
              (r12, r13, err))
        r14, r15 = refs.get("issue:#14", {}), refs.get("issue:#15", {})
        check("d2: a small object padded past 256 KiB does not define; an "
              "id past the clip defines and counts once",
              r14.get("defined") is False and r15.get("defined") is True and
              r15.get("title") == "Late id" and r15.get("count") == 1,
              (r14, r15))

        def defs(ref):
            r = dch("--xray", "xr-d2", "--ref", ref, "--scope", "session",
                    "--full", "--json")
            ls = ref_lines(r.stdout) or [{}]
            return (r.returncode, ls[-1].get("done"),
                    [l["definition"] for l in ls if "definition" in l])
        rc12, done12, d12 = defs("#12")
        rc13, done13, d13 = defs("#13")
        rc15, done15, d15 = defs("#15")
        r = dch("--xray", "xr-d2", "--ref", "#15", "--scope", "session",
                "--full", "--json")
        m15 = [l["mention"] for l in ref_lines(r.stdout) or [] if
               "mention" in l]
        check("d2: --ref agrees on an id past the clip: one D2, its "
              "anchor's line", rc15 == 0 and done15 and len(d15) == 1 and
              d15[0]["title"] == "Late id" and len(m15) == 1 and
              '"number": 15' in m15[0]["line"], (d15, m15))
        check("d2: --ref agrees: #12 has its D2, #13 none",
              rc12 == 0 and done12 and len(d12) == 1 and
              d12[0]["rule"] == "D2" and d12[0]["title"] == "Fix login" and
              d12[0]["passage"].startswith("Body line.") and
              rc13 == 0 and done13 and d13 == [], (d12, d13))
        rc, out, rss = maxrss([DCH, "--xray", "xr-d2", "--ref", "#13",
                               "--scope", "session", "--full", "--json"],
                              ENV)
        rss_ok("d2: --ref over a 300 KiB result", rss)
    finally:
        os.unlink(tp)


# ---- v3.2.2 Q14: EPIPE is exit 0, silent, for every form ---------------------

def to_fd(argv, how):
    """argv with stdout a pipe whose reader has left ("epipe"), or a
    read-only fd ("ebadf", a write error that is not EPIPE)."""
    if how == "epipe":
        r, w = os.pipe()
        os.close(r)
    else:
        w = os.open(os.devnull, os.O_RDONLY)
    try:
        p = subprocess.run(argv, stdout=w, stderr=subprocess.PIPE, env=ENV,
                           timeout=30)
    finally:
        os.close(w)
    return p.returncode, p.stderr.decode()


def test_write_fail():
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, RSID + ".jsonl")
    with open(tp, "w") as f:
        f.write(json.dumps({"type": "user", "cwd": "/home/demo/proj",
                            "message": {"content": "see #831"}}) + "\n")
    history("xr-wf", RSID, cfg)
    n = "xc-wf"
    try:
        r = dch("--spawn", n, "--size", "80x24", "sh", "-c",
                "echo WF''UP; sleep 60")
        check("write fail: spawned", r.returncode == 0 and
              wait_for(lambda: "WFUP" in screen(n), 10), r.stderr)
        forms = [("--json", ["xr-wf", "--json"]),
                 ("--ref", ["xr-wf", "--ref", "#831", "--scope", "session",
                            "--full", "--json"]),
                 ("--clear", [n, "--clear", "--json"]),
                 ("--search", ["xr-wf", "--search", "#831", "--json"]),
                 ("--tasks", ["xr-wf", "--tasks", "--json"]),
                 ("--chapter", ["xr-wf", "--chapter", "f%x:0" %
                                os.stat(tp).st_ino, "--json"])]
        for what, a in forms:
            rc, err = to_fd([DCH, "--xray"] + a, "epipe")
            check("write fail: %s into a closed pipe: exit 0, silent" % what,
                  rc == 0 and err == "", (rc, err))
            rc, err = to_fd([DCH, "--xray"] + a, "ebadf")
            check("write fail: %s on a read-only stdout: exit 3, the "
                  "failure line" % what, rc == 3 and
                  err == "dch: X-ray failed on this session\n", (rc, err))
    finally:
        dch("-k", n)
        os.unlink(tp)


# ---- --search <text> --json (#297) ----------------------------------------

SSID = "11111111-2222-4333-8444-5555555555b1"
SSID2 = "11111111-2222-4333-8444-5555555555b2"
S_KEYS = ["schema", "session", "session_id", "corpus", "text", "hits",
          "more", "partial", "why", "bytes"]
H_KEYS = ["chapter", "offset", "at", "who", "tool", "snippet"]


def search(n, text, env=None):
    r = dch("--xray", n, "--search", text, "--json", env=env)
    try:
        return r.returncode, json.loads(r.stdout), r.stderr
    except ValueError:
        return r.returncode, None, r.stdout + r.stderr


def test_search():
    """The places a word was said, newest first: typed prompts, answers,
    tool inputs and results; never thinking, a sidechain, a meta record,
    toolUseResult or a compaction summary. 50 turns' hits (at most 500),
    then more (#457); a bytes cap
    is partial; the needle's rules; nothing written."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, SSID + ".jsonl")
    tp2 = os.path.join(pdir, SSID2 + ".jsonl")
    ts = lambda s: "2026-11-10T10:%02d:%02d.000Z" % divmod(s, 60)
    recs = [
        {"type": "user", "cwd": "/home/demo/proj", "timestamp": ts(0),
         "message": {"content": "where is the Needle?"}},
        {"type": "assistant", "timestamp": ts(1), "message": {"content": [
            {"type": "thinking", "thinking": "needle thought onlyinthink"},
            {"type": "text", "text": "the NEEDLE is in a.c, \"quoted\" "
             "back\\slash caf\u00e9"},
            {"type": "tool_use", "id": "toolu_1", "name": "Bash",
             "input": {"command": "grep -n needle a.c"}}]}},
        {"type": "user", "timestamp": ts(2),
         "toolUseResult": {"stdout": "needle onlyintur"},
         "message": {"content": [{"type": "tool_result",
                                  "tool_use_id": "toolu_1",
                                  "content": "a.c:3: needle\nnext"}]}},
        {"type": "user", "isSidechain": True, "timestamp": ts(3),
         "message": {"content": "needle onlyinside"}},
        {"type": "user", "isMeta": True, "timestamp": ts(3),
         "message": {"content": "needle onlyinmeta"}},
        {"type": "user", "isCompactSummary": True, "timestamp": ts(3),
         "message": {"content": "needle onlyinsummary"}},
        {"type": "user", "timestamp": ts(4),
         "message": {"content": "next turn: thread the needle"}}]
    with open(tp, "w") as f:
        for o in recs:
            f.write(json.dumps(o) + "\n")
    history("xr-sr", SSID, cfg)
    rc, o, err = xray("xr-sr", "--json")
    anchors = [c["anchor"] for c in (o or {}).get("chapters", [])]
    check("search: the fixture has its two chapters", len(anchors) == 2,
          (o, err))
    before = files(T)
    rc, o, err = search("xr-sr", "needle")
    hits = (o or {}).get("hits", [])
    check("search: one object, its keys in order, each hit's keys in order",
          rc == 0 and o and list(o) == S_KEYS and
          all(list(h) == H_KEYS for h in hits) and o["schema"] == 1 and
          o["session"] == "xr-sr" and o["session_id"] == SSID and
          o["corpus"] == "claude" and o["text"] == "needle" and
          o["bytes"] == os.path.getsize(tp), (rc, o, err))
    check("search: newest first, one per record per kind: the turn, the "
          "tool result (its call's name), the answer, the call's input, "
          "the prompt",
          [(h["who"], h["tool"], h["snippet"]) for h in hits] ==
          [("user", None, "next turn: thread the needle"),
           ("tool", "Bash", "a.c:3: needle next"),
           ("assistant", None,
            'the NEEDLE is in a.c, "quoted" back\\slash caf\u00e9'),
           ("tool", "Bash", "grep -n needle a.c"),
           ("user", None, "where is the Needle?")], hits)
    check("search: each hit's chapter is its turn's anchor, at its "
          "timestamp", len(anchors) == 2 and len(hits) == 5 and
          [h["chapter"] for h in hits] == [anchors[1]] + [anchors[0]] * 4 and
          [h["at"] for h in hits] == [1794304804, 1794304802, 1794304801,
                                      1794304801, 1794304800] and
          o["more"] is False and o["partial"] is False and o["why"] is None,
          hits)
    for word in ("onlyinthink", "onlyintur", "onlyinside", "onlyinmeta",
                 "onlyinsummary", "absent word"):
        rc, o, err = search("xr-sr", word)
        check("search: %s gives no hit" % word,
              rc == 0 and o and o["hits"] == [] and not o["more"], (o, err))
    for text, want in (('"quoted"', 1), ("back\\slash", 1),
                       ("CAF\u00e9", 1), ("caf\u00c9", 0),
                       ("needle\nnext", 0), ("needle next", 0)):
        r = dch("--xray", "xr-sr", "--search", text, "--json")
        if "\n" in text:
            check("search: a control character in the needle is bad search "
                  "(exit 2)", r.returncode == 2 and r.stdout == "" and
                  r.stderr == "dch: bad search: 2 to 200 bytes, no control "
                  "character\n", (r.stdout, r.stderr))
            continue
        n = len(json.loads(r.stdout)["hits"]) if r.returncode == 0 else -1
        check("search: %r gives %d hit(s) (escapes decoded; ASCII letters "
              "fold, others not)" % (text, want), n == want, r.stdout)
    for text in ("x", "", "y" * 201, "\t\t"):
        r = dch("--xray", "xr-sr", "--search", text, "--json")
        check("search: %d bytes %r is bad search (exit 2)" %
              (len(text.encode()), text[:8]),
              r.returncode == 2 and r.stdout == "" and
              r.stderr == "dch: bad search: 2 to 200 bytes, no control "
              "character\n", (r.returncode, r.stdout, r.stderr))
    rc, o, err = search("xr-sr", "\u00e9")
    check("search: 2 bytes of UTF-8 is a needle", rc == 0 and o and
          len(o["hits"]) == 1, (rc, o, err))
    rc, o, err = search("xr-sr", "n" * 200)
    check("search: 200 bytes is a needle", rc == 0 and o and
          o["hits"] == [], (rc, o, err))
    for a, other in ((["--ref", "#1"], "--ref"),
                     (["--chapter", anchors[0] if anchors else "f1:0"],
                      "--chapter"), (["--clear"], "--clear"),
                     (["--follow"], "--follow")):
        r = dch("--xray", "xr-sr", "--search", "needle", *a, "--json")
        check("search: with %s is bad usage (exit 2)" % other,
              r.returncode == 2 and r.stdout == "" and
              r.stderr == "dch: bad usage: --search and %s\n" % other,
              (r.stdout, r.stderr))
    trace = os.path.join(T, "trace-search")
    open(trace, "w").close()
    rc, o, err = search("xr-sr", "Needle", env=dict(ENV, DCH_DEBUG=trace))
    tr = open(trace).read()
    check("search: traced, one seam open, the needle never in a trace line",
          rc == 0 and "xray seam: open" in tr and "needle" not in tr.lower(),
          tr[-600:])
    os.unlink(trace)
    new = sorted(set(files(T)) - set(before))
    check("search: nothing written", not new, new)

    # 60 prompts, 2.5 MB of answers, then one more: 50 hits newest first,
    # each prompt its own chapter across windows; a 51st is more
    user = lambda t: json.dumps({"type": "user", "cwd": "/home/demo/proj",
                                 "message": {"content": t}}) + "\n"
    said = lambda t: json.dumps({"type": "assistant", "message": {
        "content": [{"type": "text", "text": t}]}}) + "\n"
    offs = []
    with open(tp2, "w") as f:
        for i in range(60):
            offs.append(f.tell())
            f.write(user("needle %d" % i))
            f.write(said("ok"))
        for i in range(2500):
            f.write(said("pad %d %s" % (i, "y" * 1000)))
        offs.append(f.tell())
        f.write(user("last Needle"))
    history("xr-sr2", SSID2, cfg)
    ino = "%x" % os.stat(tp2).st_ino
    rc, o, err = search("xr-sr2", "needle")
    hits = (o or {}).get("hits", [])
    want = offs[::-1][:50]
    check("search: 61 hits: the newest 50, newest first, more, not partial",
          rc == 0 and o and [h["offset"] for h in hits] == want and
          hits[0]["snippet"] == "last Needle" and
          hits[1]["snippet"] == "needle 59" and o["more"] is True and
          o["partial"] is False and o["why"] is None, (rc, err, hits[:3]))
    check("search: each prompt is its own chapter, past a window",
          [h["chapter"] for h in hits] == ["f%s:%d" % (ino, x) for x in want],
          [h["chapter"] for h in hits][:3])
    # #457: the cap counts turns: 60 turns saying kiwi in the prompt and
    # the reply are the newest 50 turns' 100 hits, then more
    with open(tp2, "w") as f:
        for i in range(60):
            f.write(user("kiwi %d" % i))
            f.write(said("kiwi answer %d" % i))
    rc, o, err = search("xr-sr2", "kiwi")
    hits = (o or {}).get("hits", [])
    ch = [h["chapter"] for h in hits]
    check("search: 60 turns of 2 hits: the newest 50 turns, all 100 hits, "
          "newest first, more (#457)",
          rc == 0 and o and len(hits) == 100 and len(set(ch)) == 50 and
          None not in ch and hits[0]["snippet"] == "kiwi answer 59" and
          hits[-1]["snippet"] == "kiwi 10" and o["more"] is True and
          o["partial"] is False and [k for k in o] == S_KEYS and
          all([k for k in h] == H_KEYS for h in hits), (rc, err, hits[:2]))
    with open(tp2, "w") as f:
        for i in range(10):
            f.write(user("kiwi %d" % i))
            f.write(said("kiwi answer %d" % i))
    rc, o, err = search("xr-sr2", "kiwi")
    check("search: 10 turns of 2 hits: all 20, no more (#457)",
          rc == 0 and o and len(o["hits"]) == 20 and o["more"] is False,
          (rc, err, o))
    # one turn with 600 hits: the hard cap keeps its newest 500, more
    with open(tp2, "w") as f:
        f.write(user("older kiwi"))
        f.write(user("big turn"))
        for i in range(600):
            f.write(said("kiwi %03d" % i))
    rc, o, err = search("xr-sr2", "kiwi")
    hits = (o or {}).get("hits", [])
    check("search: one turn of 600 hits: its newest 500, more, the older "
          "turn not reached (#457)",
          rc == 0 and o and len(hits) == 500 and
          hits[0]["snippet"] == "kiwi 599" and
          hits[-1]["snippet"] == "kiwi 100" and
          len(set(h["chapter"] for h in hits)) == 1 and o["more"] is True,
          (rc, err, hits[:1], hits[-1:]))
    with open(tp2, "w") as f:   # back as the rows below read it
        for i in range(60):
            f.write(user("needle %d" % i))
            f.write(said("ok"))
        for i in range(2500):
            f.write(said("pad %d %s" % (i, "y" * 1000)))
        f.write(user("last Needle"))
    caps = dict(ENV, DCH_XRAY_CAPS="0:1500000")
    rc, o, err = search("xr-sr2", "needle", env=caps)
    check("search: under a 1.5 MB bytes cap, the newest hit, partial, why "
          "bytes, no more",
          rc == 0 and o and [h["snippet"] for h in o["hits"]] ==
          ["last Needle"] and o["partial"] is True and o["why"] == "bytes"
          and o["more"] is False and o["bytes"] < os.path.getsize(tp2),
          (rc, o, err))
    # "-": the text on stdin, off ps (a pipe; 256 bytes at most)
    def piped(flag, text, n="xr-sr"):
        return subprocess.run([DCH, "--xray", n, flag, "-", "--json"],
                              input=text, capture_output=True, text=True,
                              timeout=30, env=ENV)
    want = dch("--xray", "xr-sr", "--search", "needle", "--json")
    r = piped("--search", "needle")
    check("search -: a pipe's word finds the hits --search word does",
          r.returncode == 0 and r.stdout == want.stdout and
          '"hits":[{' in r.stdout, (r.returncode, r.stdout[:200], r.stderr))
    for t in ("needle\n", "needle\r\n"):
        r = piped("--search", t)
        check("search -: one trailing %r is dropped, same hits" % t[6:],
              r.returncode == 0 and r.stdout == want.stdout, (r.returncode,
              r.stderr))
    r = piped("--search", "needle\n\n")
    check("search -: a second newline is a bad search (only one dropped)",
          r.returncode == 2 and "bad search" in r.stderr, r.stderr)
    # NIT-SI6: the cap's edges on --ref (256 bytes; a search stops at 200)
    url = "https://example.com/" + "a" * 236
    for t, ok in ((url, 1), (url[:-1] + "\n", 1), (url + "\n", 0),
                  (url + "b", 0)):
        r = piped("--ref", t)
        f = ok and r.returncode == 0 and ref_lines(r.stdout)
        check("ref -: %d bytes%s %s" % (len(t), " (newline counted)" * (
              t[-1] == "\n"), "accepted, whole" if ok else "is a bad ref"),
              f and f[0]["first"].get("text") == t.rstrip("\n") if ok else
              r.returncode == 2 and "dch: bad ref" in r.stderr,
              (r.returncode, r.stdout[:200], r.stderr))
    for flag, t, msg in (("--search", "nee\0dle", "bad search"),
                         ("--ref", "#8\x0031", "bad ref")):
        r = piped(flag, t)
        check("%s -: an embedded NUL is a %s, never cut at it" % (flag, msg),
              r.returncode == 2 and msg in r.stderr, r.stderr)
    # LOW-SI1: a writer that never closes ends at the deadline, no hang
    def open_writer(flag, ms):
        env = dict(ENV, DCH_XRAY_STDIN_MS=ms) if ms else ENV
        p = subprocess.Popen([DCH, "--xray", "xr-sr", flag, "-", "--json"],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, text=True, env=env)
        t0 = time.time()
        p.stdin.write("needle\n")
        p.stdin.flush()     # and left open (communicate would close it)
        try:
            rc = p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
            rc = p.wait()
        dt = time.time() - t0
        e = p.stderr.read()
        p.stdin.close()
        p.stderr.close()
        return (rc if dt < 30 else None), e, dt
    for flag, msg in (("--search", "bad search"), ("--ref", "bad ref")):
        rc, e, dt = open_writer(flag, "300")
        check("%s -: an open writer ends at the deadline (300 ms knob) with "
              "%s" % (flag, msg), rc == 2 and msg in e and dt < 4.5,
              (rc, e, dt))
    rc, e, dt = open_writer("--search", "60000")
    check("search -: the 5 s deadline is the ceiling (a larger knob is "
          "ignored), then bad search", rc == 2 and "bad search" in e and
          4.5 < dt < 20, (rc, e, dt))
    # LOW-SI2: a non-blocking stdin waits for its text, no spurious error
    rd, wr = os.pipe()
    os.set_blocking(rd, False)
    late = threading.Timer(0.5, lambda: (os.write(wr, b"needle\n"),
                                         os.close(wr)))
    late.start()
    try:
        r = subprocess.run([DCH, "--xray", "xr-sr", "--search", "-",
                            "--json"], stdin=rd, capture_output=True,
                           text=True, timeout=30, env=ENV)
    finally:
        late.join()
        os.close(rd)
    want = dch("--xray", "xr-sr", "--search", "needle", "--json")
    check("search -: a non-blocking stdin written 0.5 s late is read",
          r.returncode == 0 and r.stdout == want.stdout,
          (r.returncode, r.stderr))
    # LOW-SI3: a local session with ':' in its name reads stdin, as the
    # query routes it locally
    history("loc:xr-sr", SSID, cfg)
    try:
        want = dch("--xray", "loc:xr-sr", "--search", "needle", "--json")
        r = piped("--search", "needle\n", n="loc:xr-sr")
        check("search -: a local loc:xr-sr reads stdin like its argv word",
              want.returncode == 0 and r.returncode == 0 and
              r.stdout == want.stdout and '"hits":[{' in r.stdout,
              (want.returncode, want.stderr, r.returncode, r.stderr))
    finally:
        os.unlink(os.path.join(T, "state", "dch", "sessions", os.path.join(
            T, "dch-%d" % os.getuid()).replace("/", "-"), "loc:xr-sr.json"))
    def stable(out):    # the rows without the timing lines (first, progress, done)
        return [l for l in ref_lines(out) or [None]
                if l is None or not {"first", "progress", "done"} & set(l)]
    want = dch("--xray", "xr-sr", "--ref", "#831", "--json")
    r = piped("--ref", "#831")
    check("ref -: a pipe's ref works like --ref <ref>",
          r.returncode == want.returncode == 0 and r.stdout and
          stable(r.stdout) == stable(want.stdout) and
          ref_lines(r.stdout)[0]["first"].get("key") == "issue:#831" and
          ref_lines(r.stdout)[0]["first"].get("text") == "#831",
          (r.returncode, r.stdout[:200], r.stderr))
    r = piped("--ref", "#831" + " " * 300)
    check("ref -: over 256 bytes is a bad ref, not cut to #831",
          r.returncode == 2, (r.returncode, r.stderr))
    r = piped("--search", "x" * 300)
    check("search -: over 256 bytes is a bad search, not cut",
          r.returncode == 2 and "bad search" in r.stderr, r.stderr)
    for flag, msg in (("--search", "bad search"), ("--ref", "bad ref")):
        r = piped(flag, "")
        check("%s -: empty stdin is the value rule's error" % flag,
              r.returncode == 2 and msg in r.stderr, r.stderr)
        # LOW-SI3: with remotes off an unknown host:name is served locally,
        # so it reads stdin and answers as its argv word (a remote-routed
        # one stays unread: serve_xray_env_test.py)
        w = dch("--xray", "nosuch:xr-sr", flag, "#831", "--json")
        r = piped(flag, "#831\n", n="nosuch:xr-sr")
        check("%s -: a locally routed nosuch:xr-sr reads stdin, as its "
              "argv word" % flag, (r.returncode, r.stdout, r.stderr) ==
              (w.returncode, w.stdout, w.stderr) and
              "no such session" in r.stderr, (r.returncode, r.stderr))
        r = subprocess.run([DCH, "--xray", "xr-sr", flag, "-", "--json"],
                           stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=30, env=ENV)
        check("%s -: /dev/null is empty stdin, error" % flag,
              r.returncode == 2, r.stderr)
    m, sl = os.openpty()
    try:
        for flag, msg in (("--search", "bad search"), ("--ref", "not a ref")):
            r = subprocess.run([DCH, "--xray", "xr-sr", flag, "-", "--json"],
                               stdin=sl, capture_output=True, text=True,
                               timeout=10, env=ENV)
            check("%s -: a terminal's - is the value rule's error, no read"
                  % flag, r.returncode == 2 and msg in r.stderr, r.stderr)
    finally:
        os.close(m)
        os.close(sl)
    os.unlink(tp)
    os.unlink(tp2)


# ---- --tasks --json (#319) -------------------------------------------------

TSID = "11111111-2222-4333-8444-5555555555c1"
TSID2 = "11111111-2222-4333-8444-5555555555c2"
TSID3 = "11111111-2222-4333-8444-5555555555c3"
TK_HEAD = ["schema", "session", "session_id", "source", "title", "partial"]
TK_KEYS = ["id", "title", "status", "label", "active", "note", "created",
           "updated", "chapter", "created_chapter", "blocked_by", "url",
           "group"]


def tasks(n, env=None):
    """(rc, header, [task], done, stderr) of --tasks --json's lines."""
    r = dch("--xray", n, "--tasks", "--json", env=env)
    ls = ref_lines(r.stdout) or [{}]
    return (r.returncode, ls[0], [l.get("task") for l in ls[1:-1]], ls[-1],
            r.stderr)


def tk_use(id, name, inp):
    return {"type": "tool_use", "id": id, "name": name, "input": inp}


def tk_said(t, *parts, side=False):
    o = {"type": "assistant", "timestamp": t,
         "message": {"content": list(parts)}}
    if side:
        o["isSidechain"] = True
    return o


def tk_made(t, tuid, tid, side=False):
    o = {"type": "user", "timestamp": t,
         "toolUseResult": {"task": {"id": tid, "subject": "x"}},
         "message": {"content": [{"type": "tool_result", "tool_use_id": tuid,
                                  "content": "Task #%s created" % tid}]}}
    if side:
        o["isSidechain"] = True
    return o


def test_tasks():
    """The session's task list (#319): TaskCreate/TaskUpdate rows with
    their real ids from the results, times and chapters; a create still
    waiting for its result is provisional; an unknown id, a sidechain, a
    bad line change nothing; deleted stays as dropped; TodoWrite replaces
    the list and keeps a row's times while its text stands; the kind used
    last wins; a shell is source none; the read range is partial; nothing
    written; the flag's exclusions."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, TSID + ".jsonl")
    tp2 = os.path.join(pdir, TSID2 + ".jsonl")
    ts = lambda s: "2026-11-14T07:%02d:%02d.000Z" % divmod(s, 60)
    iso = lambda s: "2026-11-14T07:%02d:%02dZ" % divmod(s, 60)
    ask = lambda s, t: {"type": "user", "cwd": "/home/demo/proj",
                        "timestamp": ts(s), "message": {"content": t}}
    recs = [
        ask(0, "plan the fix"),
        tk_said(ts(1), tk_use("toolu_a", "TaskCreate", {
            "subject": "Fix the login test",
            "description": "\nThe login test fails\nsecond line",
            "activeForm": "Fixing the login test"})),
        tk_made(ts(2), "toolu_a", "1"),
        tk_said(ts(3), tk_use("toolu_b", "TaskCreate", {
            "subject": "Write docs", "description": ""})),
        tk_made(ts(4), "toolu_b", "2"),
        tk_said(ts(5), tk_use("toolu_s", "TaskCreate", {
            "subject": "a subagent's own", "description": ""}), side=True),
        tk_made(ts(5), "toolu_s", "7", side=True),
        ask(10, "go on"),
        tk_said(ts(11),
                tk_use("toolu_c", "TaskUpdate",
                       {"taskId": "1", "status": "in_progress"}),
                tk_use("toolu_d", "TaskUpdate",
                       {"taskId": "2", "addBlockedBy": ["1"],
                        "owner": "me"}),
                tk_use("toolu_e", "TaskUpdate",
                       {"taskId": "99", "status": "completed"}),
                tk_use("toolu_f", "TaskUpdate",
                       {"taskId": "1", "status": "bogus"})),
        "{not json",
        {"type": "system", "subtype": "compact_boundary",
         "timestamp": ts(12)},
        {"type": "user", "isCompactSummary": True, "timestamp": ts(12),
         "message": {"content": "summary of the work"}},
        ask(20, "finish"),
        tk_said(ts(21),
                tk_use("toolu_g", "TaskUpdate",
                       {"taskId": "1", "status": "completed"}),
                tk_use("toolu_h", "TaskUpdate",
                       {"taskId": 2, "status": "deleted"}),
                tk_use("toolu_i", "TaskCreate", {"subject": "Ship it",
                                                 "description": "soon"})),
        tk_said(ts(22), tk_use("toolu_s2", "TaskUpdate",
                               {"taskId": "1", "status": "pending"}),
                side=True),
        # a result whose id is no usable id: left out, counted (#344)
        tk_said(ts(23), tk_use("toolu_n", "TaskCreate", {
            "subject": "id 3.5", "description": ""})),
        tk_made(ts(23), "toolu_n", 3.5),
        # a null id is given too, so counted (#350)
        tk_said(ts(23), tk_use("toolu_n0", "TaskCreate", {
            "subject": "id null", "description": ""})),
        tk_made(ts(23), "toolu_n0", None)]
    with open(tp, "w") as f:
        for o in recs:
            f.write((o if isinstance(o, str) else json.dumps(o)) + "\n")
    history("xr-tk", TSID, cfg)
    rc, o, err = xray("xr-tk", "--json")
    anchors = [c["anchor"] for c in (o or {}).get("chapters", [])]
    # the prompt after the compaction rides in its chapter
    check("tasks: the fixture has its three chapters", len(anchors) == 3,
          (o, err))
    if len(anchors) != 3:
        return
    a0, a3 = anchors[0], anchors[2]
    before = files(T)
    rc, h, tk, done, err = tasks("xr-tk")
    check("tasks: a header line, its keys in order, source claude",
          rc == 0 and list(h) == TK_HEAD and h["schema"] == 1 and
          h["session"] == "xr-tk" and h["session_id"] == TSID and
          h["source"] == "claude" and h["title"] is None and
          h["partial"] is False, (rc, h, err))
    check("tasks: one task line per task, keys in order, then done",
          all(t and list(t) == TK_KEYS for t in tk) and
          done == {"done": True, "count": 3, "dropped": 2}, (tk, done))
    want = [
        {"id": "1", "title": "Fix the login test", "status": "done",
         "label": "completed", "active": "Fixing the login test",
         "note": "The login test fails", "created": iso(1),
         "updated": iso(21), "chapter": a3, "created_chapter": a0,
         "blocked_by": [], "url": None, "group": None},
        {"id": "2", "title": "Write docs", "status": "dropped",
         "label": "deleted", "active": None, "note": None,
         "created": iso(3), "updated": iso(21), "chapter": a3,
         "created_chapter": a0, "blocked_by": ["1"], "url": None,
         "group": None},
        {"id": "?3", "title": "Ship it", "status": "pending",
         "label": "pending", "active": None, "note": "soon",
         "created": iso(21), "updated": iso(21), "chapter": a3,
         "created_chapter": a3, "blocked_by": [], "url": None,
         "group": None}]
    check("tasks: ids from the results, statuses mapped, deleted stays as "
          "dropped, a create with no result yet provisional; the sidechain's "
          "task, the unknown id, the unknown status and the bad line change "
          "nothing; across a compaction",
          tk == want, json.dumps(tk, indent=1))
    new = sorted(set(files(T)) - set(before))
    check("tasks: nothing written", not new, new)

    # the 16 MB + 256 MB rule, lowered: a range that skipped bytes is partial
    caps = dict(ENV, DCH_XRAY_CAPS="0:0:0:%d:%d" % (200, 600))
    rc, h, tk, done, err = tasks("xr-tk", env=caps)
    check("tasks: a read range that skipped bytes is partial",
          rc == 0 and h.get("partial") is True and done.get("done"),
          (rc, h, done, err))

    # TodoWrite: the whole list each call, rows by position; a Task tool
    # used before it loses to it (the kind used last)
    todo = lambda s, items: tk_said(ts(s), tk_use(
        "toolu_t%d" % s, "TodoWrite", {"todos": [
            {"content": c, "status": st, "activeForm": c + "ing"}
            for c, st in items]}))
    with open(tp2, "w") as f:
        for o in [ask(0, "one"),
                  tk_said(ts(1), tk_use("toolu_x", "TaskCreate",
                                        {"subject": "old kind",
                                         "description": ""})),
                  tk_made(ts(1), "toolu_x", "1"),
                  todo(2, [("lint", "pending"), ("test", "pending")]),
                  ask(10, "two"),
                  todo(11, [("lint", "completed"), ("test", "pending")]),
                  ask(20, "three"),
                  todo(21, [("lint", "completed"), ("ship", "in_progress")])]:
            f.write(json.dumps(o) + "\n")
    history("xr-tk2", TSID2, cfg)
    rc, o, err = xray("xr-tk2", "--json")
    b = [c["anchor"] for c in (o or {}).get("chapters", [])]
    rc, h, tk, done, err = tasks("xr-tk2")
    check("tasks: TodoWrite, used last, wins over the Task tools; a row "
          "keeps its created times while its text stands at its position, "
          "its updated ones until its status changes; a new text there is "
          "a new row",
          rc == 0 and len(b) == 3 and h.get("source") == "claude" and
          [(t["id"], t["title"], t["status"], t["label"], t["active"],
            t["created"], t["created_chapter"], t["updated"], t["chapter"])
           for t in tk] ==
          [("1", "lint", "done", "completed", "linting", iso(2), b[0],
            iso(11), b[1]),
           ("2", "ship", "active", "in_progress", "shiping", iso(21), b[2],
            iso(21), b[2])] and
          done == {"done": True, "count": 2, "dropped": 0},
          (rc, h, json.dumps(tk), done, err))

    # no task record: source none, no task line
    history("xr-tk-plain", SSID2, cfg)
    with open(os.path.join(pdir, SSID2 + ".jsonl"), "w") as f:
        f.write(json.dumps(ask(0, "no tasks here")) + "\n")
    rc, h, tk, done, err = tasks("xr-tk-plain")
    check("tasks: a transcript with no task record is source none, no task "
          "line, done count 0", rc == 0 and h.get("source") == "none" and
          tk == [] and done == {"done": True, "count": 0, "dropped": 0},
          (rc, h, tk, done, err))
    os.unlink(os.path.join(pdir, SSID2 + ".jsonl"))
    # the transcript gone: no corpus, as --search and --chapter say
    history("xr-tk-gone", TSID3, cfg)
    r = dch("--xray", "xr-tk-gone", "--tasks", "--json")
    check("tasks: no transcript is no x-ray corpus (exit 1)",
          r.returncode == 1 and r.stdout == "" and
          r.stderr == "dch: no x-ray corpus\n", (r.returncode, r.stderr))

    # a live shell keeps no task list: source none, its ring's id
    n = "xc-tk"
    try:
        r = dch("--spawn", n, "--size", "80x24", "sh", "-c",
                "echo TK''UP; sleep 60")
        check("tasks: shell spawned", r.returncode == 0 and
              wait_for(lambda: "TKUP" in screen(n), 10), r.stderr)
        rc, o, err = xray(n, "--json")
        rc, h, tk, done, err = tasks(n)
        check("tasks: a live shell is source none, no task line",
              rc == 0 and h.get("source") == "none" and
              h.get("session") == n and
              h.get("session_id") == (o or {}).get("session_id") and
              tk == [] and done == {"done": True, "count": 0, "dropped": 0},
              (rc, h, tk, done, err))
    finally:
        dch("-k", n)

    for a, other in ((["--ref", "#1"], "--ref"),
                     (["--search", "needle"], "--search"),
                     (["--chapter", a0], "--chapter"),
                     (["--clear"], "--clear")):
        r = dch("--xray", "xr-tk", "--tasks", *a, "--json")
        check("tasks: with %s is bad usage (exit 2)" % other,
              r.returncode == 2 and r.stdout == "" and
              r.stderr == "dch: bad usage: --tasks and %s\n" % other,
              (r.stdout, r.stderr))
    # #446: --follow carries it; a History record's follow is its snapshot,
    # then the one-shot --tasks lines, and exit 0
    r = dch("--xray", "xr-tk", "--follow", "--tasks", "--json")
    one = dch("--xray", "xr-tk", "--tasks", "--json")
    ls = r.stdout.splitlines()
    check("tasks: with --follow, a record's snapshot then its --tasks "
          "lines (exit 0)", r.returncode == 0 and ls and
          "snapshot" in json.loads(ls[0]) and
          "\n".join(ls[1:]) + "\n" == one.stdout, (r.returncode, r.stderr,
                                                  ls[1:3]))
    r = dch("--xray", "xr-tk", "--tasks", "--tasks", "--json")
    check("tasks: twice is bad usage", r.returncode == 2 and
          r.stderr == "dch: bad usage: --tasks twice\n", r.stderr)
    r = dch("--xray", "xr-tk", "--tasks")
    check("tasks: without --json refused as the other one-shot forms",
          r.returncode == 2 and r.stdout == "" and
          r.stderr == "dch: bad usage: --json is required (the --xray view "
          "takes no flag)\n", (r.returncode, r.stderr))
    os.unlink(tp)
    os.unlink(tp2)


def test_tasks_cap():
    """XR_TASKS: 50005 TaskCreates, each updated twice, keep the newest
    50000 (dropped 5), in time and memory linear in the count (ids found
    through the hash, never a scan per update)."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, TSID3 + ".jsonl")
    N = 50005
    with open(tp, "w") as f:
        f.write(json.dumps({"type": "user", "cwd": "/home/demo/proj",
                            "timestamp": "2026-11-14T07:00:00Z",
                            "message": {"content": "many"}}) + "\n")
        for i in range(1, N + 1):
            f.write('{"type":"assistant","message":{"content":[{"type":'
                    '"tool_use","id":"tu%d","name":"TaskCreate","input":'
                    '{"subject":"task %d","description":""}}]}}\n' % (i, i))
            f.write('{"type":"user","toolUseResult":{"task":{"id":"%d"}},'
                    '"message":{"content":[{"type":"tool_result",'
                    '"tool_use_id":"tu%d","content":"ok"}]}}\n' % (i, i))
        for st in ("in_progress", "completed"):
            for i in range(1, N + 1):
                f.write('{"type":"assistant","message":{"content":[{"type":'
                        '"tool_use","id":"tv%d","name":"TaskUpdate","input":'
                        '{"taskId":"%d","status":"%s"}}]}}\n' % (i, i, st))
    history("xr-tk-cap", TSID3, cfg)
    t0 = time.time()
    rc, out, rss = maxrss([DCH, "--xray", "xr-tk-cap", "--tasks", "--json"],
                          ENV)
    secs = time.time() - t0
    ls = ref_lines(out) or [{}]
    tk = [l.get("task") for l in ls[1:-1]]
    check("tasks cap: %.1f MB, 50005 tasks: the newest 50000, all done, "
          "dropped 5, in %.2f s (under 10), %.1f MB RSS" %
          (os.path.getsize(tp) / 1e6, secs, rss / 1e6),
          rc == 0 and len(tk) == 50000 and tk[0]["id"] == "6" and
          tk[-1]["id"] == str(N) and
          all(t["status"] == "done" for t in tk) and
          ls[-1] == {"done": True, "count": 50000, "dropped": 5} and
          secs < 10, (rc, ls[:2], ls[-1:], secs))
    if not SANITIZED and HELPER:
        check("tasks cap: under 128 MB RSS: %.1f MB" % (rss / 1e6),
              rss < 128 << 20, rss)
    os.unlink(tp)


def test_chapter():
    """A History Claude chapter's entries, the value rules, mutual
    exclusion, stale anchors (K14.2, K14.3), the 6000/3000 caps and the
    1 MiB cap (K14.4), an ended shell (no corpus), nothing written."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, RSID + ".jsonl")

    def put(lines):
        with open(tp, "w") as f:
            for o in lines:
                f.write(json.dumps(o) + "\n")

    def use(id, name, inp):
        return {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": id, "name": name, "input": inp}]}}

    def res(id, out, err=False):
        return {"type": "user", "toolUseResult": {"stdout": out},
                "message": {"content": [{"type": "tool_result",
                                         "tool_use_id": id, "is_error": err,
                                         "content": out}]}}
    put([{"type": "user", "cwd": "/home/demo/proj",
          "timestamp": "2026-11-10T10:00:00Z",
          "message": {"content": "first ask"}},
         {"type": "assistant", "message": {"content": [
             {"type": "text", "text": "one"}]}},
         use("b1", "Bash", {"command": "ls -la"}), res("b1", "a\nb"),
         {"type": "user", "timestamp": "2026-11-10T10:01:00Z",
          "message": {"content": "second \u0007ask"}},
         use("e1", "Edit", {"file_path": "/home/demo/proj/x.c",
                            "old_string": "a", "new_string": "b"}),
         res("e1", "boom", True),
         {"type": "assistant", "message": {"content": [
             {"type": "text", "text": "done"}]}}])
    history("xr-ch", RSID, cfg)
    rc, o, err = xray("xr-ch", "--json")
    ch = (o or {}).get("chapters", [])
    check("chapter: the index has two chapters", rc == 0 and len(ch) == 2,
          (rc, o, err))
    if len(ch) != 2:
        return
    before = files(T)
    rc, c, err, out = chapter("xr-ch", ch[1]["anchor"])
    check("chapter: History Claude: one object, its keys in order",
          rc == 0 and c and list(c) == ["schema", "session", "session_id",
                                        "anchor", "title", "at", "kind",
                                        "entries", "truncated"] and
          c["schema"] == 1 and c["session"] == "xr-ch" and
          c["session_id"] == RSID and c["anchor"] == ch[1]["anchor"] and
          c["title"] == ch[1]["title"] and c["at"] == ch[1]["at"] and
          c["kind"] == ch[1]["kind"] and c["truncated"] is False and
          out.endswith("}\n") and out.count("\n") == 1, (rc, out, err))
    check("chapter: its entries: the prompt, the call, its error, the text",
          c and c["entries"] == [
              ent("user", "second \u0007ask"),
              ent("tool", "/home/demo/proj/x.c", "Edit"),
              ent("output", "boom", "Edit", True),
              ent("assistant", "done")], c and c["entries"])
    check("chapter: text escaped like --ls-json (K14.5: BEL as \\u0007)",
          "second \\u0007ask" in out, out[:300])
    rc, c, _, _ = chapter("xr-ch", ch[0]["anchor"])
    check("chapter: the first chapter: a Bash call is its command",
          rc == 0 and c and c["entries"] == [
              ent("user", "first ask"), ent("assistant", "one"),
              ent("tool", "ls -la", "Bash"), ent("output", "a\nb", "Bash")],
          c and c["entries"])
    new = sorted(set(files(T)) - set(before))
    check("chapter: nothing written", not new, new)

    # stale anchors (K14.2, K14.3, mutant 10): no seek on the anchor's say
    ino, off = ch[1]["anchor"][1:].split(":")
    for bad in ("f%s:%d" % (ino, int(off) + 1), "f%s:%d" % (ino, int(off) - 1),
                "f%s:0%s" % (ino, off), "f%x:%s" % (int(ino, 16) + 1, off),
                "F%s:%s" % (ino, off), "s1", "x", "f" + "1" * 127):
        rc, c, err, out = chapter("xr-ch", bad)
        check("chapter: %r exits 1 chapter gone" % bad[:40], rc == 1 and
              err == "dch: chapter gone\n" and out == "", (rc, err, out))
    put([{"type": "user", "message": {"content": "first ask"}}])
    rc, c, err, _ = chapter("xr-ch", ch[1]["anchor"])
    check("chapter: a rewritten transcript (new inode): chapter gone",
          rc == 1 and err == "dch: chapter gone\n", (rc, err))

    # the value rules, mutual exclusion, --json (3.1)
    for bad in ("", "a b", "a/b", "é", "a" * 129, "-", "a\n"):
        r = dch("--xray", "xr-ch", "--chapter", bad, "--json")
        ok = bad == "-"
        check("chapter: %r: %s" % (bad[:20], "an anchor (gone)" if ok else
                                   "exit 2 bad anchor"),
              (r.returncode, r.stderr) == ((1, "dch: chapter gone\n") if ok
                                           else (2, "dch: bad anchor\n")),
              (r.returncode, r.stderr))
    r = dch("--xray", "xr-ch", "--chapter", "a" * 128, "--json")
    check("chapter: 128 bytes is an anchor", r.returncode == 1 and
          r.stderr == "dch: chapter gone\n", (r.returncode, r.stderr))
    for a, msg in ((["--ref", "#1"], "--chapter and --ref"),
                   (["--clear"], "--chapter and --clear"),
                   (["--follow"], "--chapter and --follow")):
        r = dch("--xray", "xr-ch", "--chapter", "s1", "--json", *a)
        check("chapter: with %s: exit 2" % a[0], r.returncode == 2 and
              r.stderr == "dch: bad usage: %s\n" % msg and not r.stdout,
              (r.returncode, r.stderr))
    for a, e in ((["--chapter", "s1"], "dch: bad usage: --json is required "
                  "(the --xray view takes no flag)\n"),
                 (["--chapter"], "dch: bad usage: --chapter needs a value\n"),
                 (["--chapter", "s1", "--chapter", "s2", "--json"],
                  "dch: bad usage: --chapter twice\n"),
                 (["--chapter", "s1", "--scope", "session", "--json"],
                  "dch: bad usage: --scope, --full and --budget-ms come with "
                  "--ref\n")):
        r = dch("--xray", "xr-ch", *a)
        check("chapter: %s: exit 2" % " ".join(a)[:50], r.returncode == 2 and
              r.stderr == e, (r.returncode, r.stderr))
    r = dch("--xray", "nope-ch", "--chapter", "s1", "--json")
    check("chapter: no such session (exit 1)", r.returncode == 1 and
          r.stderr == "dch: no such session\n", (r.returncode, r.stderr))
    r = dch("--xray", "bad/name", "--chapter", "a b", "--json")
    check("chapter: a bad session is told before a bad anchor",
          r.returncode == 2 and r.stderr == "dch: bad session\n", r.stderr)

    # the scalar caps (v2.1 3.1: 6000 and 3000) and the 1 MiB cap (K14.4)
    put([{"type": "user", "cwd": "/home/demo/proj",
          "message": {"content": "é" * 10000}},
         use("b1", "Bash", {"command": "é" * 7000}),
         res("b1", "é" * 4000),
         {"type": "assistant", "message": {"content": [
             {"type": "text", "text": "é" * 10000}]}}])
    rc, o, _ = xray("xr-ch", "--json")
    rc, c, err, _ = chapter("xr-ch", o["chapters"][0]["anchor"])
    # #451: a prompt and a reply keep 6000 + 3000 with the cut line
    hm = "é" * 6000 + "\n\u2026 1000 characters cut \u2026\n" + "é" * 3000
    check("chapter: text clipped in scalars: 6000 (call), 3000 (output); "
          "a prompt and a reply 6000 + 3000 with a cut line, cut 1000",
          rc == 0 and c and [e["text"] for e in c["entries"]] ==
          [hm, "é" * 6000, "é" * 3000, hm] and
          [e.get("cut") for e in c["entries"]] == [1000, None, None, 1000],
          (rc, err, c and [len(e["text"]) for e in c["entries"]]))
    out = "\u0001" * 3000    # 18000 escaped bytes an entry
    put([{"type": "user", "cwd": "/home/demo/proj",
          "message": {"content": "big"}}] +
        [x for i in range(100) for x in (use("c%d" % i, "Bash",
                                             {"command": "c%d" % i}),
                                         res("c%d" % i, out))] +
        [{"type": "user", "message": {"content": "next"}}])
    rc, o, _ = xray("xr-ch", "--json")
    r = dch("--xray", "xr-ch", "--chapter", o["chapters"][0]["anchor"],
            "--json")
    try:
        c = json.loads(r.stdout)
    except ValueError:
        c = None
    n = len(r.stdout.encode())
    check("chapter: 100 x 18 KB escaped outputs: cut at 1 MiB, truncated "
          "(%d bytes, %d entries)" % (n, len((c or {}).get("entries", []))),
          r.returncode == 0 and c and c["truncated"] is True and
          n <= 1 << 20 and n > (1 << 20) - 20000 and
          c["entries"][-1]["role"] in ("tool", "output") and
          all(e["text"] == out for e in c["entries"]
              if e["role"] == "output"), (r.returncode, r.stderr, n))

    # an ended shell has no corpus (3.5)
    r = dch("--spawn", "xc-chend", "sh", "-c", "echo UP; sleep 60")
    wait_for(lambda: "UP" in screen("xc-chend"), 5)
    dch("-k", "xc-chend")
    wait_for(lambda: not os.path.exists(sock("xc-chend")), 5)
    rc, c, err, out = chapter("xc-chend.json", "s1")
    check("chapter: an ended shell: exit 1 no x-ray corpus", rc == 1 and
          err == "dch: no x-ray corpus\n" and out == "", (rc, err))
    os.unlink(tp)
    rc, c, err, out = chapter("xr-ch", "s1")
    check("chapter: a History Claude with no transcript: no x-ray corpus",
          rc == 1 and err == "dch: no x-ray corpus\n", (rc, err))


def test_chapter_seek():
    """#300: --chapter reads from a little before the anchor's offset, not
    the whole range: a 5.8 MB transcript (several 1 MiB windows) answers
    as its whole read would (the expected entries are the generator's):
    the newest chapter, one after a chapter longer than a window, prompts
    merged across a window, a late result 2 MB after its call (in its
    call's chapter, not the one it is read in), and stale anchors are
    chapter gone (not a line start, another inode, past the end, the
    unread middle of the range). The spec change: an anchor older than the
    newest 5000 chapters opens."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    sid = "11111111-2222-4333-8444-5555555553a0"
    tp = os.path.join(pdir, sid + ".jsonl")
    history("xr-sk", sid, cfg)
    trace = os.path.join(T, "sk.trace")
    dbg = dict(ENV, DCH_DEBUG=trace)
    rows, exp, at = [], {}, [0]

    def put(o):
        b = (json.dumps(o) + "\n").encode()
        rows.append(b)
        at[0] += len(b)
        return at[0] - len(b)

    def ask(t):
        return put({"type": "user", "message": {"content": t}})

    def say(t):
        put({"type": "assistant", "message": {"content": [
            {"type": "text", "text": t}]}})

    def use(id, cmd):
        put({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": id, "name": "Bash",
             "input": {"command": cmd}}]}})

    def res(id, out):
        put({"type": "user", "toolUseResult": {"stdout": out},
             "message": {"content": [{"type": "tool_result",
                                      "tool_use_id": id, "content": out}]}})

    def turn(i, pad=3000):
        o = ask("ask %d" % i)
        say("p" * pad)
        use("toolu_t%d" % i, "cmd %d" % i)
        res("toolu_t%d" % i, "out %d" % i)
        exp[o] = [ent("user", "ask %d" % i), ent("assistant", "p" * pad),
                  ent("tool", "cmd %d" % i, "Bash"),
                  ent("output", "out %d" % i, "Bash")]
        return o

    for i in range(60):
        turn(i)
    # a chapter of 15 outputs of 100 KB (longer than a window), then one
    big = ask("big")
    for k in range(15):
        use("toolu_g%d" % k, "gen %d" % k)
        res("toolu_g%d" % k, "%d" % k + "g" * 100000)
    after_big = turn(150)
    # a late call: its result comes 2 MB on, in another chapter
    late = ask("late ask")
    use("toolu_late", "sleep 9")
    say("waiting")
    exp[late] = [ent("user", "late ask"), ent("tool", "sleep 9", "Bash"),
                 ent("assistant", "waiting")]
    for i in range(151, 300):
        turn(i)
    # prompts merged across a window: 13 of 100 KB, one chapter
    merged = ask("m0")
    inner = [ask("m%d " % k + "y" * 100000) for k in range(1, 13)]
    say("merged done")
    after_merged = turn(300)
    for i in range(301, 500):
        turn(i)
    exp[late].append(ent("output", "woke", "Bash"))
    landed = turn(500)
    res("toolu_late", "woke")    # in chapter 500, but its call's entry
    for i in range(501, 540):
        turn(i)
    newest = turn(540)
    with open(tp, "wb") as f:
        f.write(b"".join(rows))
    st = os.stat(tp)
    size = st.st_size
    an = lambda off, ino=st.st_ino: "f%x:%d" % (ino, off)

    def seeks():
        try:
            with open(trace) as f:
                t = f.read()
        except OSError:
            return []
        os.unlink(trace)
        return re.findall(r"xray chapter: seek=(-?\d)", t)

    seeks()
    for what, off in (("the newest chapter", newest),
                      ("the first", 0),
                      ("the one after a chapter longer than a window",
                       after_big),
                      ("a late call's: its result 2 MB on", late),
                      ("the chapter the late result is read in "
                       "(its call's, not this)", landed),
                      ("the one after prompts merged across a window",
                       after_merged)):
        rc, c, err, out = chapter("xr-sk", an(off), env=dbg)
        how = seeks()
        check("chapter seek (%.1f MB): %s at %d: as the whole read, by the "
              "seek" % (size / 1e6, what, off),
              rc == 0 and c and c["entries"] == exp[off] and
              c["anchor"] == an(off) and how == ["1"],
              (rc, err, how, c and c["entries"][-2:], exp[off][-2:]))
    rc, c, err, out = chapter("xr-sk", an(merged), env=dbg)
    check("chapter seek: 13 prompts of 100 KB merged across a window: one "
          "chapter, every prompt's entry",
          rc == 0 and c and [e["role"] for e in c["entries"]] ==
          ["user"] * 13 + ["assistant"] and
          c["entries"][0]["text"] == "m0" and seeks() == ["1"],
          (rc, err, c and len(c["entries"])))
    rc, c, err, out = chapter("xr-sk", an(big), env=dbg)
    check("chapter seek: the chapter longer than a window: its 15 calls "
          "and outputs (each clipped to 3000)",
          rc == 0 and c and len(c["entries"]) == 31 and
          c["entries"][-1] == ent("output", "14" + "g" * 2998, "Bash") and
          seeks() == ["1"], (rc, err, c and len(c["entries"])))
    for what, a in (("a merged prompt (no chapter of its own)",
                     an(inner[5])),
                    ("not a line start", an(newest + 1)),
                    ("the byte before a line start", an(newest - 1)),
                    ("another inode", an(newest, st.st_ino + 1)),
                    ("past the end", an(size + 10)),
                    ("at the end", an(size))):
        rc, c, err, out = chapter("xr-sk", a)
        check("chapter seek: %s: chapter gone" % what, rc == 1 and
              err == "dch: chapter gone\n" and out == "", (rc, err, out))
    # past a lowered range (head 1 MiB, tail 2 MiB): the unread middle is
    # gone, the head and the tail open (the tail read on from the head)
    caps = dict(dbg, DCH_XRAY_CAPS="0:0:0:%d:%d" % (1 << 20, 2 << 20))
    mid = max(o for o in exp if o < size - (2 << 20))
    tail1 = min(o for o in exp if o >= size - (2 << 20))
    rc, c, err, out = chapter("xr-sk", an(mid), env=caps)
    check("chapter seek: past the range, a chapter in the unread middle: "
          "chapter gone", rc == 1 and err == "dch: chapter gone\n",
          (rc, err))
    for what, off in (("the head's first", 0),
                      ("the tail's first whole chapter", tail1),
                      ("the newest", newest)):
        rc, c, err, out = chapter("xr-sk", an(off), env=caps)
        check("chapter seek: past the range, %s opens" % what,
              rc == 0 and c and c["entries"] == exp[off], (rc, err))
    seeks()
    os.unlink(tp)

    # the spec change: an anchor older than the newest 5000 chapters opens
    # (the whole read's ring had dropped it: chapter gone before #300)
    sid = "11111111-2222-4333-8444-5555555553a1"
    tp = os.path.join(pdir, sid + ".jsonl")
    history("xr-sk5k", sid, cfg)
    rows[:], at[0] = [], 0
    offs = [turn(i, pad=20) for i in range(5100)]
    with open(tp, "wb") as f:
        f.write(b"".join(rows))
    st = os.stat(tp)
    an = lambda off: "f%x:%d" % (st.st_ino, off)
    rc, o, err = xray("xr-sk5k", "--json")
    check("chapter seek: 5100 chapters, --json lists the newest 5000",
          rc == 0 and o and len(o["chapters"]) == 5000 and
          o["chapters"][0]["anchor"] == an(offs[100]), (rc, err))
    for k in (0, 99, 100, 5099):
        rc, c, err, out = chapter("xr-sk5k", an(offs[k]))
        check("chapter seek: chapter %d of 5100 opens%s" %
              (k + 1, " (older than the newest 5000)" if k < 100 else ""),
              rc == 0 and c and c["entries"] == exp[offs[k]], (rc, err))
    os.unlink(tp)

    # the second: past 256 open calls the whole read forgets the oldest,
    # and its result with it; the seek still pairs it in its call's chapter
    rows[:], at[0] = [], 0
    first = ask("first")
    use("toolu_X", "x")
    for k in range(300):
        ask("p%d" % k)
        use("toolu_o%d" % k, "o%d" % k)
    res("toolu_X", "late x")
    with open(tp, "wb") as f:
        f.write(b"".join(rows))
    st = os.stat(tp)
    rc, c, err, _ = chapter("xr-sk5k", an(first))
    check("chapter seek: past 256 open calls, a late result attaches where "
          "the whole read dropped it", rc == 0 and c and c["entries"] == [
              ent("user", "first"), ent("tool", "x", "Bash"),
              ent("output", "late x", "Bash")], (rc, err, c))

    # the third exception: past 256 KiB of summaries the whole read's
    # oldest auto chapters give theirs up (--json shows none); --chapter
    # reads the chapter alone and keeps it
    rows[:], at[0] = [], 0
    for k in range(2300):
        put({"type": "user", "origin": {"kind": "task-notification"},
             "message": {"content": "<task-notification><summary>%s"
                         "</summary></task-notification>" %
                         ("first" if not k else "%05d" % k + "s" * 115)}})
        say("ok")
    with open(tp, "wb") as f:
        f.write(b"".join(rows))
    st = os.stat(tp)
    rc, o, err = xray("xr-sk5k", "--json")
    rc2, c, err2, _ = chapter("xr-sk5k", an(0))
    check("chapter seek: an auto chapter past 256 KiB of newer summaries: "
          "--json lists it with no summary, --chapter gives it",
          rc == 0 and o and o["chapters"][0]["anchor"] == an(0) and
          o["chapters"][0].get("auto") is True and
          "summary" not in o["chapters"][0] and rc2 == 0 and c and
          c.get("auto") is True and c.get("summary") == "first",
          (rc, err, o and o["chapters"][0], rc2, err2, c and list(c)))
    os.unlink(tp)


# ---- 3.4: SIGTERM and EPIPE cancel a --ref (FROZEN l.464, l.635) ---------

def test_cancel():
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, RSID + ".jsonl")
    line = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "PROJ-5 o/r#3 " + "x" * 900}]}}) + "\n"
    with open(tp, "w") as f:
        f.write(json.dumps({"type": "user", "cwd": "/home/demo/proj",
                            "message": {"content": "start #1"}}) + "\n")
        for _ in range(16):
            f.write(line * 10000)
    history("xr-cancel", RSID, cfg)
    argv = [DCH, "--xray", "xr-cancel", "--ref", "#424242", "--scope",
            "session", "--full", "--json"]
    try:
        t0 = time.time()
        r = subprocess.run(argv, capture_output=True, text=True, env=ENV,
                           timeout=60)
        full = time.time() - t0
        ls = ref_lines(r.stdout) or [{}]
        check("cancel: the whole scan of %d MB ends with done (%.2fs)" %
              (os.path.getsize(tp) >> 20, full),
              r.returncode == 0 and ls[-1].get("done") is True, r.stderr)

        p = subprocess.Popen(argv, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=ENV)
        first = p.stdout.readline()
        running = p.poll() is None
        t0 = time.time()
        p.send_signal(15)
        p.wait(10)
        dt = time.time() - t0
        rest = p.stdout.read()
        p.stdout.close()
        p.stderr.close()
        check("cancel: SIGTERM mid-scan ends it at once (%.3fs, the whole "
              "scan %.2fs), no done" % (dt, full),
              running and b'"first"' in first and p.returncode == -15 and
              dt < 0.5 and b'"done"' not in rest, (p.returncode, rest[-300:]))

        p = subprocess.Popen(argv, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=ENV)
        first = p.stdout.readline()
        running = p.poll() is None
        t0 = time.time()
        p.stdout.close()
        p.wait(10)
        dt = time.time() - t0
        err = p.stderr.read()
        p.stderr.close()
        # dch learns of the close at its next write, at most 250 ms later
        # (the progress cadence; v3.2.2 Q15). The bound protects that the
        # scan stops early instead of running to its end: 500 ms, or a
        # quarter of the whole scan on a runner slow enough that it needs
        # more (CI's 5.27 s scan saw 0.514 s), capped at 5 s. xray_unit
        # pins the progress write's own EPIPE check.
        lim = min(max(0.5, full / 4), 5.0)
        check("cancel: a closed pipe (EPIPE) ends it within %.2fs, well "
              "before the whole scan would (%.3fs, the whole scan %.2fs), "
              "exit 0, nothing on stderr" % (lim, dt, full),
              running and b'"first"' in first and p.returncode == 0 and
              err == b"" and dt < lim, (p.returncode, err))
    finally:
        os.unlink(tp)


def test_auto():
    """#192: a harness-written prompt (a task-notification) is auto and
    carries its summary in --json and --follow's snapshot; a prompt is
    neither."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, RSID + ".jsonl")
    tn = ("<task-notification>\n<task-id>b1</task-id>\n<summary>Background "
          "job done</summary>\n</task-notification>")
    with open(tp, "w") as f:
        for o in ({"type": "user", "cwd": "/home/demo/proj",
                   "message": {"content": "start the job"}},
                  {"type": "assistant", "message": {"content": [
                      {"type": "text", "text": "ok"}]}},
                  {"type": "user", "origin": {"kind": "task-notification"},
                   "message": {"content": tn}}):
            f.write(json.dumps(o) + "\n")
    history("xr-auto", RSID, cfg)
    try:
        rc, o, err = xray("xr-auto", "--json")
        ch = (o or {}).get("chapters", [])
        check("auto: --json marks the notification auto with its summary, "
              "the prompt neither",
              rc == 0 and len(ch) == 2 and "auto" not in ch[0] and
              "summary" not in ch[0] and ch[1].get("auto") is True and
              ch[1].get("summary") == "Background job done" and
              ch[1]["title"] == "<task-notification>", (rc, o, err))
        r = dch("--xray", "xr-auto", "--follow", "--json")
        ls = ref_lines(r.stdout) or []
        check("auto: --follow's snapshot carries the same",
              r.returncode == 0 and len(ls) == 1 and
              ls[0] == {"schema": 1, "snapshot": o},
              (r.returncode, r.stdout[-400:], r.stderr))
    finally:
        os.unlink(tp)


def test_peer():
    """#359: another agent's prompt, by its "origin" only. A subagent's
    hand-back is auto, its summary "subagent <name>: <first line>"; a
    cross-session message is a prompt titled by its body, with "peer" (the
    sender's name, "" for an id or a path) and the body alone as its text.
    Neither shows the socket, the hex id or the wrapper. Typed imitations
    and hostile origins stay what they were."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, RSID + ".jsonl")
    sock_ = "uds:/tmp/cc-socks/74069.sock"
    hexid = "a0c9e72b4fd05827e"
    advice = "\n\nReply with the send tool if a reply is useful."
    xs_body = "Release 1.71 is green\nthe details follow"
    xs = ("Another Claude session sent a message:\n<cross-session-message "
          'from="%s" from-name="dch-qa" from-mode="auto">\n%s\n'
          "</cross-session-message>%s" % (sock_, xs_body, advice))
    hb_rep = "PASS. Report: all rows green\nmore lines"
    hb_body = "[Subagent hand-back] " + hb_rep
    hb = ('Another Claude session sent a message:\n<agent-message from="%s">'
          "\n  %s\n</agent-message>" % (hexid, hb_body))

    def u(content, origin=None, meta=True):
        o = {"type": "user", "cwd": "/home/demo/proj",
             "message": {"content": content}}
        if meta:
            o["isMeta"] = True
        if origin is not None:
            o["origin"] = origin
        return o
    a = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "ok"}]}}
    recs = [u("start", meta=False), a,
            u(hb, {"kind": "peer", "from": hexid, "senderTaskId": "t1",
                   "name": "worker-a", "body": hb_body, "handback": True,
                   "handbackReportText": hb_rep}), a,
            u(xs, {"kind": "peer", "from": sock_, "verifiedPeerPid": 74069,
                   "msg_id": "m1", "name": "dch-qa", "fromMode": "auto",
                   "body": xs_body, "hopChain": ["x"]}),
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "thanks qa"}]}},
            # a sender with an id for a name, delivered mid-turn
            {"type": "attachment", "attachment": {
                "type": "queued_command", "commandMode": "prompt",
                "prompt": xs, "origin": {"kind": "peer", "from": sock_,
                                         "name": "0123456789abcdef",
                                         "body": "from an id"}}}, a,
            # typed imitations: no origin, or a human one
            u(xs, meta=False), a,
            u(hb, {"kind": "human"}, meta=False), a]
    # hostile origins: today's behaviour (meta: hidden; not meta: a prompt)
    bad = [{"kind": "peer"}, {"kind": "peer", "body": 7},
           {"kind": "peer", "body": ["x"]}, "peer", ["peer"], 5,
           {"kind": ["peer"], "body": "x"}]
    for b in bad:
        recs += [u("hostile meta", b), a]
    recs += [u("hostile typed", {"kind": "peer", "body": 7}, meta=False), a,
             u("odd", {"kind": "peer", "body": "odd shapes", "name": {"x": 1},
                       "handback": "true"}), a,
             u("hb1", {"kind": "peer", "body": "numeric flag", "handback": 1,
                       "name": "/tmp/x.sock"}), a,
             u("big", {"kind": "peer", "name": "big",
                       "body": "b" * 1048000 + "</cross-session-message>"}),
             a]
    with open(tp, "w") as f:
        for o in recs:
            f.write(json.dumps(o) + "\n")
    history("xr-peer", RSID, cfg)
    try:
        rc, o, err = xray("xr-peer", "--json")
        ch = (o or {}).get("chapters", [])
        got = [(c["title"], c.get("auto"), c.get("summary"), c.get("peer"))
               for c in ch]
        want = [("start", None, None, None),
                ("PASS. Report: all rows green", True,
                 "subagent worker-a: PASS. Report: all rows green",
                 "worker-a"),
                ("Release 1.71 is green", None, None, "dch-qa"),
                ("from an id", None, None, ""),
                ("Another Claude session sent a message:", None, None, None),
                ("Another Claude session sent a message:", None, None, None),
                ("hostile typed", None, None, None),
                ("odd shapes", None, None, ""),
                ("numeric flag", None, None, ""),
                ("b" * 120, None, None, "big")]
        check("peer: the hand-back is auto with a clean summary, the "
              "cross-session prompts carry their body and sender, typed "
              "imitations and hostile origins stay as they were",
              rc == 0 and got == want, (rc, got, err))
        check("peer: --json shows no socket path, hex id or wrapper tag",
              rc == 0 and "cc-socks" not in json.dumps(ch) and
              hexid not in json.dumps(ch[:4]) and
              "<cross-session" not in json.dumps(ch[:4]), ch[:4])
        check("peer: \"peer\" comes after the chapter's other keys up to "
              "kind and auto/summary",
              rc == 0 and len(ch) > 2 and
              list(ch[1])[:10] == ["anchor", "offset", "i", "at", "title",
                                   "kind", "partial", "error", "auto",
                                   "summary"] and
              list(ch[1])[10] == "peer" and
              list(ch[2])[8] == "peer", ch[1:3])
        if len(ch) > 2:
            rc2, c, err2, raw = chapter("xr-peer", ch[2]["anchor"])
            check("peer: the reader's text is the body alone, the header "
                  "carries peer",
                  rc2 == 0 and c and c.get("peer") == "dch-qa" and
                  c["entries"][:2] == [ent("user", xs_body),
                                       ent("assistant", "thanks qa")] and
                  "cc-socks" not in raw and "Reply with" not in raw,
                  (rc2, raw[:600], err2))
            rc2, c, err2, raw = chapter("xr-peer", ch[1]["anchor"])
            check("peer: the hand-back's text is its report",
                  rc2 == 0 and c and c.get("auto") is True and
                  c.get("peer") == "worker-a" and
                  c["entries"][:1] == [ent("user", hb_rep)] and
                  hexid not in raw, (rc2, raw[:600], err2))
        rc, s, err = search("xr-peer", "is green")
        hits = (s or {}).get("hits", [])
        check("peer: --search finds the body in its chapter, never the "
              "wrapper (the typed imitation keeps its text)",
              rc == 0 and len(ch) > 4 and
              [(h["who"], h["chapter"], h["snippet"]) for h in hits] ==
              [("user", ch[4]["anchor"], hits[0]["snippet"]),
               ("user", ch[2]["anchor"],
                "Release 1.71 is green the details follow")] and
              hits[0]["snippet"].startswith("\u2026 ") and
              "from-name=" in hits[0]["snippet"], (rc, s, err))
        rc, s, err = search("xr-peer", "cc-socks")
        check("peer: --search does not find the socket path of a peer's "
              "record (the typed imitations' text still matches)",
              rc == 0 and s and all(h["chapter"] not in
                                    (ch[2]["anchor"], ch[3]["anchor"])
                                    for h in s.get("hits", [])), (rc, s, err))
    finally:
        os.unlink(tp)


def test_handback_search():
    """#382: --search reads a hand-back's text as the reader shows it: the
    report when there is one, else the body without "[Subagent hand-back]"
    and the blank after it, which is the reader's text too."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, RSID + ".jsonl")

    def hb(origin):
        origin.update(kind="peer", handback=True, name="worker-b")
        return {"type": "user", "cwd": "/home/demo/proj", "isMeta": True,
                "message": {"content": "wrapped"}, "origin": origin}
    a = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "ok"}]}}
    recs = [{"type": "user", "cwd": "/home/demo/proj",
             "message": {"content": "start"}}, a,
            hb({"body": "[Subagent hand-back] body-only delta",
                "handbackReportText": "report-only echo\nmore"}), a,
            hb({"body": "[Subagent hand-back] REPORT plugh\nline2"}), a]
    with open(tp, "w") as f:
        for o in recs:
            f.write(json.dumps(o) + "\n")
    history("xr-hb", RSID, cfg)
    try:
        rc, o, err = xray("xr-hb", "--json")
        ch = (o or {}).get("chapters", [])
        check("handback: three chapters", rc == 0 and len(ch) == 3,
              (rc, ch, err))
        if len(ch) == 3:
            rc, s, err = search("xr-hb", "echo")
            check("handback: --search finds a word of the report alone",
                  rc == 0 and s and
                  [(h["chapter"], h["snippet"]) for h in s["hits"]] ==
                  [(ch[1]["anchor"], "report-only echo more")], (rc, s, err))
            rc, s, err = search("xr-hb", "Subagent hand-back")
            check("handback: --search never finds the wrapper",
                  rc == 0 and s and s["hits"] == [], (rc, s, err))
            rc, s, err = search("xr-hb", "plugh")
            check("handback: a body's snippet has no wrapper",
                  rc == 0 and s and
                  [h["snippet"] for h in s["hits"]] == ["REPORT plugh line2"],
                  (rc, s, err))
            rc2, c, err2, raw = chapter("xr-hb", ch[2]["anchor"])
            check("handback: no report: the reader's text has no leading "
                  "blank",
                  rc2 == 0 and c and
                  c["entries"][:1] == [ent("user", "REPORT plugh\nline2")],
                  (rc2, raw[:400], err2))
    finally:
        os.unlink(tp)


def test_responses():
    """#296: each chapter's response (the last assistant text after the
    last tool result) with its record's offset and the tool line, in --json
    and --follow's snapshot; none while the turn runs. Past 4 MiB the
    oldest chapters' responses go first: their offset and tools stay,
    every chapter and ref with them."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, RSID + ".jsonl")
    lines = [
        {"type": "user", "cwd": "/home/demo/proj",
         "message": {"content": "fix #4242"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Looking."},
            {"type": "tool_use", "id": "e1", "name": "Edit", "input": {
                "file_path": "/home/demo/proj/a.c", "old_string": "a",
                "new_string": "b"}}]}},
        {"type": "user", "toolUseResult": {"structuredPatch": [
            {"lines": ["-a", "+b", "+c"]}]}, "message": {"content": [
                {"type": "tool_result", "tool_use_id": "e1",
                 "content": "ok"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "done?"},
            {"type": "text", "text": "Fixed #4242 in a.c."}]}},
        {"type": "user", "message": {"content": "deploy it"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Deploying."},
            {"type": "tool_use", "id": "b1", "name": "Bash",
             "input": {"command": "make deploy"}}]}}]
    raw = "".join(json.dumps(o) + "\n" for o in lines)
    with open(tp, "w") as f:
        f.write(raw)
    history("xr-resp", RSID, cfg)
    try:
        rc, o, err = xray("xr-resp", "--json")
        ch = (o or {}).get("chapters", [])
        off = len("".join(json.dumps(x) + "\n" for x in lines[:3]).encode())
        check("responses: --json carries the response, its offset and the "
              "tool line; a running turn its tools only",
              rc == 0 and len(ch) == 2 and
              ch[0].get("response") == "Fixed #4242 in a.c." and
              ch[0].get("response_offset") == off and
              "response_cut" not in ch[0] and
              ch[0].get("tools") == "Edit 1 (1 file, +2 -1)" and
              "response" not in ch[1] and "response_offset" not in ch[1] and
              ch[1].get("tools") == "Bash 1", (rc, ch, err))
        r = dch("--xray", "xr-resp", "--follow", "--json")
        ls = ref_lines(r.stdout) or []
        check("responses: --follow's snapshot carries the same",
              r.returncode == 0 and len(ls) == 1 and
              ls[0] == {"schema": 1, "snapshot": o},
              (r.returncode, r.stdout[-400:], r.stderr))
        # 600 responses of 2000 bytes that JSON writes 6 bytes each
        with open(tp, "w") as f:
            for i in range(600):
                for x in ({"type": "user", "cwd": "/home/demo/proj",
                           "message": {"content": "turn %d #4242" % i}},
                          {"type": "assistant", "message": {"content": [
                              {"type": "tool_use", "id": "r%d" % i,
                               "name": "Read", "input": {}}]}},
                          {"type": "user", "message": {"content": [
                              {"type": "tool_result", "tool_use_id":
                               "r%d" % i, "content": "ok"}]}},
                          {"type": "assistant", "message": {"content": [
                              {"type": "text", "text": "\x01" * 2000}]}}):
                    f.write(json.dumps(x) + "\n")
        r = dch("--xray", "xr-resp", "--json")
        try:
            o = json.loads(r.stdout)
        except ValueError:
            o = {}
        ch = o.get("chapters", [])
        has = [("response" in c) for c in ch]
        k = has.index(True) if True in has else len(has)
        check("responses: past 4 MiB the oldest responses go first, their "
              "offset and tools stay; every chapter and the ref kept "
              "(%d of 600 kept, %d bytes)" % (len(ch) - k, len(r.stdout)),
              r.returncode == 0 and len(r.stdout.encode()) <= 4 << 20 and
              o["complete"] is False and o["why"] == "output" and
              len(ch) == 600 and 0 < k < 600 and all(has[k:]) and
              all("response_offset" in c and c.get("tools") == "Read 1"
                  for c in ch) and
              [x["key"] for x in o["refs"]] == ["issue:#4242"] and
              o["dropped_keys"] == 0,
              (r.returncode, len(r.stdout), k, r.stderr))
        f = dch("--xray", "xr-resp", "--follow", "--json")
        ls = ref_lines(f.stdout) or []
        check("responses: a capped --follow snapshot is the --json",
              f.returncode == 0 and len(ls) == 1 and
              ls[0] == {"schema": 1, "snapshot": o}, f.returncode)
    finally:
        os.unlink(tp)


def test_follow_history():
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, RSID + ".jsonl")
    with open(tp, "w") as f:
        f.write(json.dumps({"type": "user", "cwd": "/home/demo/proj",
                            "message": {"content": "ended #31"}}) + "\n")
    history("xr-fh", RSID, cfg)
    try:
        _, one, _ = xray("xr-fh", "--json")
        r = dch("--xray", "xr-fh", "--follow", "--json")
        ls = ref_lines(r.stdout) or []
        check("follow: a History session writes one snapshot, exit 0",
              r.returncode == 0 and len(ls) == 1 and
              ls[0] == {"schema": 1, "snapshot": one} and one["refs"],
              (r.returncode, r.stdout[-400:], r.stderr))
        r = dch("--xray", "xr-fh", "--follow", "--clear", "--json")
        check("follow: --follow with --clear is usage (exit 2)",
              r.returncode == 2 and "--follow and --clear" in r.stderr,
              r.stderr)
        r = dch("--xray", "xr-fh", "--follow", "--ref", "#31", "--json")
        check("follow: --follow with --ref is usage (exit 2)",
              r.returncode == 2 and "--follow and --ref" in r.stderr,
              r.stderr)
        r = dch("--xray", "xr-fh", "--follow", "--follow", "--json")
        check("follow: --follow twice is usage (exit 2)",
              r.returncode == 2 and "--follow twice" in r.stderr, r.stderr)
        r = dch("--xray", "nosuch", "--follow", "--json")
        check("follow: no such session, exit 1, nothing on stdout",
              r.returncode == 1 and not r.stdout and
              "no such session" in r.stderr, r.stderr)
    finally:
        os.unlink(tp)


# ---- --ref across sessions (#155, XR3a): scope, order, the 3.8 caps -------

def test_scope():
    """A store of its own: <n>, Claude sessions in its project dir and in
    another, an unmapped transcript beside them. The seam counts exactly the
    mapped files a scope reads; 65 files scan 64; the bytes, time and
    per-file range caps stop with their why; light stops at its budget across
    files; nothing is written."""
    S = os.path.join(T, "scope")
    home = os.path.join(S, "home")
    cfg = os.path.join(home, ".claude")
    sockdir = os.path.join(S, "dch-%d" % os.getuid())
    hdir = os.path.join(S, "state", "dch", "sessions",
                        sockdir.replace("/", "-"))
    for d in (home, hdir):
        os.makedirs(d)
    env = dict(ENV, XDG_RUNTIME_DIR=S, XDG_STATE_HOME=os.path.join(S, "state"),
               HOME=home)
    trace = os.path.join(T, "trace-scope")
    dbg = dict(env, DCH_DEBUG=trace)
    sid = lambda k: "22222222-3333-4444-8555-%012d" % k

    def put(k, cwd, lines, updated):
        pdir = os.path.join(cfg, "projects", key(cwd))
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, sid(k) + ".jsonl"), "w") as f:
            f.write("".join(json.dumps({"type": "user", "cwd": cwd,
                                        "message": {"content": t}}) + "\n"
                            for t in lines))
        if updated is None:
            return              # unmapped: a file with no record
        rec = {"name": "s%d" % k, "cwd": cwd, "end": "exited",
               "argv": ["claude"], "started": 1794299400,
               "updated": updated, "ended": updated, "sock_dir": sockdir,
               "agent": {"session_id": sid(k), "cwd": cwd,
                         "config_dir": cfg}}
        with open(os.path.join(hdir, "s%d.json" % k), "w") as f:
            f.write(json.dumps(rec) + "\n")

    def ref(*a, env=dbg):
        open(trace, "w").close()
        r = dch("--xray", "s0", "--ref", *a, "--json", env=env)
        ls = ref_lines(r.stdout) or [{}]
        tr = open(trace).read()
        return (r.returncode, ls, tr.count("xray seam: open"),
                [l["tally"]["session"] for l in ls if "tally" in l], r.stderr)

    P, O = "/home/demo/scope", "/home/demo/elsewhere"
    put(0, P, ["mine #931"], 1794300000)
    put(1, P, ["older #931"], 1794300010)
    put(2, P, ["newer #931", "again #931"], 1794300020)
    put(3, O, ["far #931"], 1794300030)
    put(4, P, ["unmapped #931"], None)
    rc, ls, opens, order, err = ref("#931", "--scope", "project", "--full")
    d = ls[-1]
    check("scope: project reads <n>, then its dir newest first; the seam "
          "opens exactly those 3 files",
          rc == 0 and order == ["s0", "s2", "s1"] and opens == 3 and
          d.get("counts") == {"session": 1, "project": 3,
                              "project_sessions": 2, "all": None,
                              "all_sessions": None} and not d["partial"],
          (rc, order, opens, d, err))
    rc, ls, opens, order, err = ref("#931", "--scope", "all", "--full")
    d = ls[-1]
    check("scope: all adds the other dir after; still never the unmapped "
          "file (4 opens)",
          rc == 0 and order == ["s0", "s2", "s1", "s3"] and opens == 4 and
          d["counts"]["all"] == 1 and d["counts"]["all_sessions"] == 1,
          (rc, order, opens, d, err))
    rc, ls, opens, order, err = ref("#931", "--full")
    check("scope: no --scope is project (a remote caller's default too: "
          "all only when passed)",
          rc == 0 and ls[0]["first"]["scope"] == "project" and
          order == ["s0", "s2", "s1"] and opens == 3, (order, opens, err))
    rc, ls, opens, order, err = ref("#931", "--scope", "session", "--full")
    check("scope: session stops after <n> (1 open)",
          rc == 0 and order == ["s0"] and opens == 1, (order, opens))
    # #176: --full's resolved line comes right after <n>'s own file, before
    # the other sessions are read, so a capped or timed-out wide read keeps
    # it (s0's cwd is absolute: /etc/hosts is outside_project, nothing read)
    for sc, rest in (("project", ["s2", "s1"]), ("all", ["s2", "s1", "s3"])):
        for caps, why in ((None, None), ("1", "files"), ("0:0:1", "time")):
            rc, ls, opens, order, err = ref(
                "/etc/hosts:1", "--scope", sc, "--full",
                env=dict(dbg, DCH_XRAY_CAPS=caps) if caps else dbg)
            seq = ["resolved" if "resolved" in l else
                   "done" if l.get("done") else l["tally"]["session"]
                   for l in ls if "resolved" in l or "done" in l or
                   "tally" in l]
            rv = [l["resolved"]["state"] for l in ls if "resolved" in l]
            d = ls[-1]
            check("resolved order, --scope %s, %s: right after s0's tally, "
                  "before the others; done last%s" % (
                      sc, "no cap" if not caps else "capped (%s)" % why,
                      ", partial" if caps else ""),
                  rc == 0 and rv == ["outside_project"] and
                  seq[:2] == ["s0", "resolved"] and seq[-1] == "done" and
                  (seq[2:-1] == rest if not caps else
                   why == "time" or seq[2:-1] == []) and
                  d.get("partial") == bool(caps) and d.get("why") == why,
                  (rc, seq, d, err))
    rc, o, err = xray("s0", "--json", env=env)
    check("scope: other_projects counts the mapped session in the other "
          "dir, from names only", rc == 0 and o and o["other_projects"] == 1,
          (o and o.get("other_projects"), err))

    # the per-file range of another session's file (3.8: head + newest),
    # lowered by DCH_XRAY_CAPS: --ref reads the middle too (#301)
    pad = ["x" * 900] * 40
    put(5, O, ["head #932"] + pad + ["middle #932"] + pad + ["tail #932"],
        1794300040)
    rc, ls, opens, order, err = ref("#932", "--scope", "all", "--full",
                                    env=dict(dbg, DCH_XRAY_CAPS=
                                             "0:0:0:4096:8192"))
    t5 = [l["tally"] for l in ls if "tally" in l and
          l["tally"]["session"] == "s5"]
    lines = [l["mention"]["line"] for l in ls if "mention" in l]
    check("caps: past the per-file range --ref finds the head, the middle "
          "and the tail (#301); tally not partial",
          rc == 0 and t5 and t5[0]["count"] == 3 and not t5[0]["partial"]
          and lines == ["head #932", "middle #932", "tail #932"],
          (t5, lines, ls[-1], err))
    rc, ls, opens, order, err = ref("#932", "--scope", "all", "--full",
                                    env=dict(dbg, DCH_XRAY_CAPS="0:1000"))
    check("caps: the bytes cap stops the scan before the next file, why "
          "bytes", rc == 0 and ls[-1]["partial"] and
          ls[-1]["why"] == "bytes" and opens < 5 and
          ls[-1]["bytes"] < 2000, (opens, ls[-1], err))
    rc, ls, opens, order, err = ref("#932", "--scope", "all", "--full",
                                    env=dict(dbg, DCH_XRAY_CAPS="0:0:1"))
    check("caps: the wall-time cap stops it, why time",
          rc == 0 and ls[-1]["partial"] and ls[-1]["why"] == "time",
          (ls[-1], err))
    os.unlink(os.path.join(hdir, "s5.json"))

    # #236: a reader paused past --full's time cap (lowered to 3 s) or the
    # light budget gets the whole answer, each file's ~160 KB far past the
    # pipe's buffer: time blocked writing to it is not the scan's
    for k in range(6, 10):
        put(k, P, ["#934 line %d %s" % (j, "\U0001f600" * 200)
                   for j in range(300)], 1794300050 + k)
    caps = dict(env, DCH_XRAY_CAPS="0:0:3000")

    def paused(n, secs, *a):
        p = subprocess.Popen([DCH, "--xray", n, "--ref", "#934", "--scope",
                              "all", *a, "--json"], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=caps)
        time.sleep(secs)
        out, err = p.communicate(timeout=60)
        return p.returncode, len(out), ref_lines(out.decode()) or [{}], err

    whole = ref("#934", "--scope", "all", "--full", env=caps)[1]
    rc, n, ls, err = paused("s0", 6, "--full")
    same = lambda ls: [l for l in ls if "progress" not in l and
                       "first" not in l]
    check("#236: a reader paused 6 s, past --full's 3 s cap, gets the whole "
          "answer (%d bytes), no why time" % n,
          rc == 0 and n > 300000 and ls[-1].get("done") and
          not ls[-1]["partial"] and ls[-1]["why"] is None and
          same(ls) == same(whole), (rc, n, ls[-1], err[:200]))
    rc, n, ls, err = paused("s6", 2)
    check("#236: light: a reader paused 2 s, past the 300 ms budget, behind "
          "s6's own lines (%d bytes) still gets s9's result, why first" % n,
          rc == 0 and n > 100000 and ls[-1].get("why") == "first" and
          ls[-1]["files"] == 2 and ls[-1]["counts"]["project"] > 0,
          (rc, n, ls[-1], err[:200]))
    for k in range(6, 10):
        os.unlink(os.path.join(hdir, "s%d.json" % k))

    # 65 files scan 64 (3.8, 9.5); light over 1 MB files stops at its budget
    # (933 as an id value, no ref: the prefilter decodes every window, #293)
    big = "".join(json.dumps({"type": "user", "cwd": P, "message": {
        "content": "pad id: 933 " + "y" * 1000}}) + "\n" for _ in range(1000))
    for k in range(10, 71):
        put(k, P, ["#931 in %d" % k], 1794300100 + k)
        with open(os.path.join(cfg, "projects", key(P), sid(k) + ".jsonl"),
                  "a") as f:
            f.write(big)
    before = files(S)
    rc, ls, opens, order, err = ref("#931", "--scope", "all", "--full")
    d = ls[-1]
    check("caps: 65 files scan 64, why files, the newest first",
          rc == 0 and opens == 64 and d["files"] == 64 and d["partial"] and
          d["why"] == "files" and order[:3] == ["s0", "s70", "s69"] and
          "s3" not in order, (opens, d, order[:4], err))
    t0 = time.time()
    # untraced: DCH_DEBUG's lines cost more than the 50 ms budget
    rc, ls, opens, order, err = ref("#933", "--scope", "all",
                                    "--budget-ms", "50", env=env)
    d = ls[-1]
    check("caps: light stops at its budget across files (%d files, %.2fs)"
          % (d.get("files", -1), time.time() - t0),
          rc == 0 and d.get("why") == "budget" and d["partial"] and
          d["files"] < 64, (d, err))
    rc, ls, opens, order, err = ref("#931", "--scope", "all")
    d = ls[-1]
    check("light: stops at the first other session's result, titles and "
          "counts only for it",
          rc == 0 and d["why"] == "first" and order == ["s0", "s70"] and
          not any("mention" in l and l["mention"]["session"] != "s0"
                  for l in ls), (order, d, err))
    after = files(S)
    check("scope: nothing written by a full and a light --scope all "
          "(names, sizes, mtimes)", after == before,
          sorted(set(after.items()) ^ set(before.items()))[:6])


# ---- #299: light --ref's anchors in old windows ---------------------------

ASID = "11111111-2222-4333-8444-5555555555c9"


def test_light_anchors():
    """Light --ref reads 1 MiB windows newest first, each by a fresh decoder:
    a mention before its window's first prompt, or under a prompt the full
    decode merges into an older one, has a null chapter; every anchor it
    does give is --full's and opens with --chapter. --search gives the
    older (right) anchor or null, never its window's own. 1024 lines of
    4096 bytes, so the windows start at lines 0, 256, 512 and 768."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-p299")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, ASID + ".jsonl")
    special = {0: (0, "a0 CT-12"), 255: (1, "q255"), 256: (0, "a256 CT-12"),
               257: (1, "q257 CT-12"), 383: (0, "a383"),
               384: (1, "q384 CT-12"), 385: (0, "a385 CT-12"),
               386: (1, "q386 CT-12"), 448: (0, "a448 CT-12"),
               510: (0, "a510"), 511: (1, "q511"), 512: (1, "q512 CT-12"),
               513: (0, "a513 CT-12"), 767: (0, "a767"),
               768: (1, "q768 CT-12")}

    def line(prompt, text):
        def obj(t):
            return json.dumps({"type": "user", "message": {"content": t}}
                              if prompt else
                              {"type": "assistant", "message": {"content": [
                                  {"type": "text", "text": t}]}},
                              separators=(",", ":"))
        return obj(text + " " + "z" * (4094 - len(obj(text)))) + "\n"

    with open(tp, "w") as f:
        for i in range(1024):
            f.write(line(*special.get(i, (i % 3 == 0, "%s%d" % (
                "q" if i % 3 == 0 else "a", i)))))
    history("xr-anch", ASID, cfg, "/home/demo/p299")
    ino = "%x" % os.stat(tp).st_ino
    at = lambda i: "f%s:%d" % (ino, i * 4096)

    def run(caps):
        env = dict(ENV, DCH_XRAY_NOCLOCK="1")
        if caps:
            env["DCH_XRAY_CAPS"] = caps
        got = {}
        for form in ("light", "full"):
            r = dch("--xray", "xr-anch", "--ref", "CT-12", "--json",
                    *(["--full"] if form == "full" else []), env=env)
            ls = ref_lines(r.stdout) or []
            got[form] = {l["mention"]["offset"] // 4096: l["mention"]["chapter"]
                         for l in ls if "mention" in l}
        rc, o, err = search("xr-anch", "CT-12", env=env)
        got["search"] = {h["offset"] // 4096: h["chapter"]
                         for h in (o or {}).get("hits", [])}
        bad = [(form, i, a, got["full"].get(i))
               for form in ("light", "search")
               for i, a in sorted(got[form].items())
               if a is not None and a != got["full"].get(i)]
        gone = []
        for a in sorted({a for a in got["light"].values() if a}):
            rc, c, err, out = chapter("xr-anch", a, env=env)
            if rc != 0 or not c or c.get("anchor") != a:
                gone.append((a, rc, err.strip()))
        return got, bad, gone

    got, bad, gone = run(None)
    L, F, S = got["light"], got["full"], got["search"]
    check("light anchors: --full has the 10 mentions, a512 and a513 merged "
          "into q511's chapter, a256 in q255's",
          len(F) == 10 and F.get(512) == at(511) and F.get(513) == at(511)
          and F.get(256) == at(255), F)
    check("light anchors: the window at offset 0 keeps its partial chapter; "
          "a prompt its window opened past a step keeps its own (q257, q384, "
          "q386)", L.get(0) == at(0) and L.get(257) == at(257) and
          L.get(384) == at(384) and L.get(386) == at(386), L)
    check("light anchors: a mention before its window's first prompt "
          "(a256), a prompt merged across a window boundary and its answer "
          "(q512, a513): chapter null",
          256 in L and L[256] is None and 512 in L and L[512] is None and
          513 in L and L[513] is None, L)
    check("light anchors: --search gives q512 and a513 the older chapter or "
          "null, never their window's own",
          512 in S and S[512] in (None, at(511)) and 513 in S and
          S[513] in (None, at(511)), S)
    check("light anchors: every light and --search anchor is --full's", not bad,
          bad)
    check("light anchors: every light anchor opens with --chapter", not gone,
          gone)
    # past the range: head 1 MiB (lines 0-255, q255's lead open), the tail
    # from q384, then from a448: --full reads on from the head there
    for tail, first in ((640 * 4096, 384), (576 * 4096, 448)):
        got, bad, gone = run("0:0:0:%d:%d" % (1 << 20, tail))
        L, F, S = got["light"], got["full"], got["search"]
        check("light anchors: a tail range from line %d: --full puts it in "
              "q255's chapter; light null, --search null or that" % first,
              F.get(first) == at(255) and first in L and L[first] is None
              and first in S and S[first] in (None, at(255)), got)
        check("light anchors: tail from line %d: every light and --search "
              "anchor is --full's and opens with --chapter" % first,
              not bad and not gone, (bad, gone))
    os.unlink(tp)


NSID = "11111111-2222-4333-8444-5555555555d3"


def test_light_303():
    """#303 (QA 1.62.0 MED-X1, MED-X2, NIT-X3), light --ref against --full
    on 4 MiB transcripts of 1024 lines of 4096 bytes (windows start at lines
    0, 256, 512 and 768): light keeps the file's newest 200 mention lines,
    newest window first; a window resolves a relative path with the cwd the
    full read has at its start; the line the head range's end cuts is read
    by neither."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-p303")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, NSID + ".jsonl")
    env = dict(ENV, DCH_XRAY_NOCLOCK="1")

    def line(text, cwd=None):
        def obj(t):
            o = ({"type": "user", "cwd": cwd, "message": {"content": t}}
                 if cwd else {"type": "assistant", "message": {"content": [
                     {"type": "text", "text": t}]}})
            return json.dumps(o, separators=(",", ":"))
        return obj(text + " " + "z" * (4094 - len(obj(text)))) + "\n"

    def plant(texts):
        with open(tp, "w") as f:
            for t in texts:
                f.write(line(*t) if isinstance(t, tuple) else line(t))
        history("xr-303", NSID, cfg, "/home/demo/p303")

    def ref(r, *a, caps=None):
        e = dict(env, DCH_XRAY_CAPS=caps) if caps else env
        x = dch("--xray", "xr-303", "--ref", r, "--json", *a, env=e)
        ls = ref_lines(x.stdout) or []
        m = [l["mention"]["offset"] // 4096 for l in ls if "mention" in l]
        t = [l["tally"] for l in ls if "tally" in l]
        return x.returncode, m, (t[0] if t else {}), ls[-1:] or [{}]

    # MED-X1: CT-12 on every 4th line (256), CT-34 on every line from 700
    plant(["l%d%s%s" % (i, " CT-12" if i % 4 == 0 else "",
                        " CT-34" if i >= 700 else "") for i in range(1024)])
    rc, m, t, d = ref("CT-12", "--scope", "project")
    want = [i for w in (768, 512, 256, 0) for i in range(w, w + 256, 4)
            if i >= 224]
    check("303 newest: light keeps CT-12's newest 200 of 256 mention lines, "
          "the newest window first, file order within one; 56 dropped",
          rc == 0 and m == want and t.get("count") == 256 and
          t.get("mentions_dropped") == 56, (m[:3], m[-12:], t))
    rc, m, t, d = ref("CT-34", "--scope", "project")
    check("303 newest: 324 mention lines in the newest windows: light keeps "
          "lines 824 to 1023, the last line too",
          rc == 0 and m == list(range(824, 1024)) and
          t.get("mentions_dropped") == 124, (m[:3], m[-3:], t))
    rc, m, t, d = ref("CT-12", "--full")
    check("303 newest: --full keeps the first and the newest 199, as before",
          rc == 0 and sorted(m) == [0] + list(range(228, 1024, 4)), m[:3])

    # MED-X2: the cwd on line 0 (/home/demo/p303), on line 600 /home/demo/q;
    # src/main.c:12 on every 50th line
    plant([("start", "/home/demo/p303")] +
          [("moved", "/home/demo/q") if i == 600 else
           "edit src/main.c:12 at %d" % i if i % 50 == 0 else "pad %d" % i
           for i in range(1, 1024)])
    got = {}
    for k in ("src/main.c:12", "/home/demo/q/src/main.c"):
        for form in ("light", "full"):
            rc, m, t, d = ref(k, *(("--full",) if form == "full"
                                   else ("--scope", "project")))
            got[(k, form)] = (rc, t.get("count"), sorted(m),
                              d[0].get("partial"))
    check("303 cwd: light resolves src/main.c with the cwd in force at each "
          "window's start, as --full: 11 under p303, 8 under q (QA: 17 of 80)",
          got[("src/main.c:12", "light")] == got[("src/main.c:12", "full")]
          and got[("src/main.c:12", "full")][1] == 11 and
          got[("/home/demo/q/src/main.c", "light")] ==
          got[("/home/demo/q/src/main.c", "full")] and
          got[("/home/demo/q/src/main.c", "full")][1] == 8, got)

    # NIT-X3: head 1 MiB + 2 KiB (line 256 straddles its end), tail 1 MiB;
    # CT-56 on lines 100, 256 and 1000
    plant(["l%d%s" % (i, " CT-56" if i in (100, 256, 1000) else "")
           for i in range(1024)])
    caps = "0:0:0:%d:%d" % ((1 << 20) + 2048, 1 << 20)
    got = {form: ref("CT-56", *(("--full",) if form == "full"
                                else ("--scope", "project")), caps=caps)
           for form in ("light", "full")}
    check("303 head end: --ref reads past the range (#301), the line the "
          "head's end cuts too, light as --full: lines 100, 256 and 1000, "
          "not partial, no line too long",
          sorted(got["light"][1]) == sorted(got["full"][1]) ==
          [100, 256, 1000] and got["light"][3][0].get("lines_too_long") ==
          got["full"][3][0].get("lines_too_long") == 0 and
          got["light"][3][0].get("partial") is False, got)
    os.unlink(tp)


XSID = "11111111-2222-4333-8444-5555555555e6"


def test_light_326():
    """#326 (QA 1.64.0 MED-XF1, MED-XF2). XF1: a D2 definition whose
    '{"number":' ends one JSON string and whose 12 begins the next (two
    tool_result parts, or stdout and stderr, joined) is in a window the
    prefilter now decodes: light --ref counts it as --full does. XF2: a
    chapter a light window or a --search window opened only after a step
    the full decode may not take (a sidechain thread's first block here, a
    result whose call is older) is null; every chapter light --ref and
    --search do give is --full's and opens with --chapter. 4096-byte lines,
    so the windows start at lines 0, 256, 512 and 768."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-p326")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, XSID + ".jsonl")
    env = dict(ENV, DCH_XRAY_NOCLOCK="1")
    J = lambda o: json.dumps(o, separators=(",", ":"))

    def pad(o, at, size=4095):
        """o's JSON, the string at key path `at` padded to size bytes"""
        n = size - len(J(o))
        x = o
        for k in at[:-1]:
            x = x[k]
        x[at[-1]] += " " + "z" * (n - 1)
        return J(o) + "\n"

    U = lambda t, **k: pad(dict({"type": "user", "message": {
        "content": t}}, **k), ("message", "content"))
    A = lambda t, **k: pad(dict({"type": "assistant", "message": {
        "content": [{"type": "text", "text": t}]}}, **k),
        ("message", "content", 0, "text"))

    def call(i, n):
        return pad({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t%d" % i, "name": "Bash",
             "input": {"command": "gh issue view %d" % n,
                       "description": ""}}]}},
            ("message", "content", 0, "input", "description"))

    def result(i, parts=None, tur=None):
        o = {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t%d" % i,
             "content": parts or "x"}]}, "pad": ""}
        if tur:
            o["toolUseResult"] = tur
        return pad(o, ("pad",))

    def plant(lines):
        with open(tp, "w") as f:
            f.write("".join(lines))
        history("xr-326", XSID, cfg, "/home/demo/p326")

    def refs(r, *a):
        x = dch("--xray", "xr-326", "--ref", r, "--json", *a, env=env)
        ls = ref_lines(x.stdout) or []
        return {"count": [l["tally"].get("count") for l in ls
                          if "tally" in l],
                "mentions": {l["mention"]["offset"]: l["mention"]["chapter"]
                             for l in ls if "mention" in l},
                "defs": [(l["definition"]["offset"] // 4096,
                          l["definition"]["title"]) for l in ls
                         if "definition" in l]}

    # XF1: lines 300-305, in the second window; the rest no digit
    t = lambda o: [{"type": "text", "text": s} for s in o]
    mid = {300: call(1, 12),
           301: result(1, t(['{"number":', '12,"title":"Fix it"}'])),
           302: call(2, 13),
           303: result(2, t(['{"number"', ':13,"title":"Fix it"}'])),
           304: call(3, 14),
           305: result(3, tur={"stdout": '{"id":',
                               "stderr": '"14","title":"Fix it"}',
                               "interrupted": False})}
    plant([mid[i] if i in mid else U("start") if not i else A("pad")
           for i in range(1024)])
    for n, how in ((12, "two text parts, the ':' in the first"),
                   (13, "two text parts, the ':' in the second"),
                   (14, "stdout and stderr")):
        lt = refs("#%d" % n, "--scope", "project")
        fu = refs("#%d" % n, "--full")
        check("326 joined: #%d split across %s: light --ref counts its D2 "
              "definition and mention as --full does (QA: count 0)" % (n, how),
              lt["count"] == fu["count"] == [1] and
              lt["defs"] == fu["defs"] == [(n * 2 + 277, "Fix it")] and
              list(lt["mentions"]) == list(fu["mentions"]) and
              [o // 4096 for o in fu["mentions"]] == [n * 2 + 277],
              (lt, fu))

    # XF2: a sidechain thread first seen in an older window (lines 2 and
    # 769); the older window ending past one (lines 510-511); a result whose
    # call is in an older window (lines 255-257)
    side = lambda s: A(s, isSidechain=True, agentId="ag1")
    special = {0: U("P0"), 1: A("reply"), 2: side("sub starts"),
               510: U("P5 one"), 511: side("sub goes on"),
               512: U("P5 two"), 513: A("see o/r#6"),
               255: call(9, 99), 256: U("P6 one"), 257: result(9),
               258: U("P6 two"), 259: A("see o/r#8"),
               768: U("P7 one"), 769: side("sub more"),
               770: U("P7 two"), 771: A("see o/r#5"), 772: A("more o/r#5")}
    plant([special.get(i) or A("pad") for i in range(1024)])
    ino = "%x" % os.stat(tp).st_ino
    at = lambda i: "f%s:%d" % (ino, i * 4096)

    def audit(r):
        lt = refs(r, "--scope", "project")["mentions"]
        fu = refs(r, "--full")["mentions"]
        rc, o, err = search("xr-326", r, env=env)
        se = {h["offset"]: h["chapter"] for h in (o or {}).get("hits", [])}
        bad = [(f, i, a, fu.get(i)) for f, g in (("light", lt), ("search", se))
               for i, a in sorted(g.items()) if a and a != fu.get(i)]
        gone = []
        for a in sorted({a for g in (lt, se) for a in g.values() if a}):
            rc, c, err, out = chapter("xr-326", a, env=env)
            if rc != 0 or not c or c.get("anchor") != a:
                gone.append((a, rc, err.strip()))
        return lt, fu, se, bad, gone

    for r, first, last, why in (
            ("o/r#5", 771, 772, "a sidechain thread an older window began"),
            ("o/r#6", 513, 513, "the older window ends past such a thread"),
            ("o/r#8", 259, 259, "a result whose call is older")):
        res = audit(r)
        lt, fu, se = ({o // 4096: a for o, a in g.items()} for g in res[:3])
        bad, gone = res[3:]
        P1 = {"o/r#5": 768, "o/r#6": 510, "o/r#8": 256}[r]
        check("326 side: %s: --full merges the second prompt into %d's "
              "chapter; light and --search say null (QA: the second "
              "prompt's, chapter gone)" % (why, P1),
              all(fu.get(i) == at(P1) for i in (first, last)) and
              all(i in lt and lt[i] is None for i in (first, last)) and
              all(i in se and se[i] in (None, at(P1))
                  for i in (first, last)), (lt, fu, se))
        check("326 side: %s: every light and --search chapter is --full's "
              "and opens with --chapter" % why, not bad and not gone,
              (bad, gone))

    # the QA generator (ch299.py, seed 299; its files 0 and 6 failed):
    # prompts, steps, sidechains of four threads, images, compactions and
    # mentions over 3 to 4 MiB
    rnd = random.Random(299)
    worst, seen = [], [0, 0]
    for k in range(8):
        ls = [J({"type": "user", "message": {"content": "start"}})]
        size, end = len(ls[0]) + 1, (3 << 20) + rnd.randrange(1 << 20)
        while size < end:
            x = rnd.random()
            m = lambda: "o/r#7" if rnd.random() < 0.3 else ""
            if x < 0.12:
                o = {"type": "user", "message": {
                    "content": "prompt %d %s" % (len(ls), m())}}
            elif x < 0.2:
                o = {"type": "assistant", "isSidechain": True,
                     "agentId": None, "message": {"content": [
                         {"type": "text", "text": "side %d %s" % (
                             len(ls), m())}]}}
                o["agentId"] = "ag%d" % rnd.randrange(4)
            elif x < 0.23:
                o = {"type": "user", "message": {"content": [
                    {"type": "image", "source": {"type": "base64",
                                                 "data": "AAAA"}}]}}
            elif x < 0.24:
                o = {"type": "system", "subtype": "compact_boundary",
                     "content": "Conversation compacted"}
            elif x < 0.25:
                o = {"type": "user", "isCompactSummary": True,
                     "message": {"content": "summary o/r#7"}}
            else:
                t = ("step %d o/r#7" % len(ls) if x < 0.45 else
                     "pad %d " % len(ls))
                o = {"type": "assistant", "message": {"content": [
                    {"type": "text", "text": t}]}}
                if x >= 0.45:
                    n = rnd.choice((800, 5000, 40000, 200000))
                    t += "w" * max(0, n - len(J(o)))
                    o["message"]["content"][0]["text"] = t
            ls.append(J(o))
            size += len(ls[-1]) + 1
        plant([l + "\n" for l in ls])
        lt, fu, se, bad, gone = audit("o/r#7")
        seen[0] += len(lt) + len(se)
        seen[1] += sum(1 for g in (lt, se) for a in g.values() if a)
        if bad or gone:
            worst.append((k, bad[:2], gone[:2]))
    check("326 side: 8 random transcripts with sidechains, %d light and "
          "--search mentions, %d with a chapter: each is --full's and opens "
          "with --chapter" % tuple(seen), not worst and seen[1], worst)
    os.unlink(tp)


PSID = "11111111-2222-4333-8444-5555555555d4"


def test_prefilter_reach():
    """#293: light --ref does not decode a 1 MiB window whose raw bytes
    cannot hold the ref, so its 300 ms reach the whole of a 96 MB
    transcript: a ticket named only in the first line is found. Without the
    prefilter light decodes about 20-30 MB in its budget at load 10 and
    40-95 MB on a fast idle machine. With the clock off (#332: a stalled CI
    runner reached 6 MB of 96 in 300 ms, three tries in a row) this asserts
    what was found and the CPU it cost, which a stall does not inflate: a
    twin with a \\/ on every line, which the prefilter never skips
    (pf_escape), gives the CPU per byte of decoding every window; the
    prefilter's read costs under a quarter of that (30 times less on an
    idle M-series)."""
    import resource

    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, PSID + ".jsonl")
    words = ("the parser reads a line and then the decoder builds chapters "
             "from turns while tools run").split()
    said = lambda i: json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": " ".join(words[(i * 7 + j) % len(words)]
                                          for j in range(20 + i % 300))}]}},
        separators=(",", ":")) + "\n"
    block = "".join(said(i) for i in range(400))

    def plant(blk, mb):
        with open(tp, "w") as f:
            f.write(json.dumps({"type": "user", "cwd": "/home/demo/proj",
                                "message": {"content": "start on MARK-1"}},
                               separators=(",", ":")) + "\n")
            for _ in range((mb << 20) // len(blk)):
                f.write(blk)

    def reach():
        c = resource.getrusage(resource.RUSAGE_CHILDREN)
        r = dch("--xray", "xr-reach", "--ref", "MARK-1", "--scope",
                "session", "--json", env=dict(ENV, DCH_XRAY_NOCLOCK="1"))
        e = resource.getrusage(resource.RUSAGE_CHILDREN)
        ls = ref_lines(r.stdout) or [{}]
        return (r, ls, e.ru_utime + e.ru_stime - c.ru_utime - c.ru_stime,
                ls[-1].get("bytes") or 1)

    history("xr-reach", PSID, cfg)
    try:
        plant(block.replace('"text":"', '"text":"\\/'), 32)
        tw = reach()
        plant(block, 96)
        r, ls, cpu, n = reach()
        men = [l["mention"]["offset"] for l in ls if "mention" in l]
        check("prefilter: light --ref finds a ref named only in the first "
              "line of a 96 MB transcript: the mention at offset 0, "
              "nothing unread, all but its oldest window counted, at under a quarter "
              "of the CPU per byte of decoding every window (%.0f ms for "
              "96 MB; decoded, %.0f ms for %d MB)"
              % (cpu * 1000, tw[2] * 1000, tw[3] >> 20),
              r.returncode == 0 and men == [0] and
              ls[-1].get("done") and ls[-1]["why"] is None and
              n >= os.path.getsize(tp) - (2 << 20) and
              tw[3] > 16 << 20 and cpu / n * 4 < tw[2] / tw[3],
              (r.returncode, ls[-2:], r.stderr, tw[1][-1:]))
    finally:
        os.unlink(tp)


def test_ref_middle():
    """#301: --ref (light and --full) reads past the range rule (first
    16 MiB + newest 256 MiB), so a ref in the middle of a 300 MiB
    transcript is found, with a null chapter; --json and --search keep the
    rule. Sparse: the holes are over-long lines, cheap to read."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, RSID + ".jsonl")
    # a prompt and its answer: each prompt opens a chapter of its own
    u = lambda t: (json.dumps({"type": "user", "cwd": "/home/demo/proj",
                               "message": {"content": t}}) + "\n" +
                   '{"type":"assistant","message":{"content":"ok"}}\n'
                   ).encode()
    head, mid, tail = 0, (30 << 20) + 1, (299 << 20) + 1
    with open(tp, "wb") as f:
        f.write(u("head WIDE-1"))
        f.seek(mid - 1)
        f.write(b"\n" + u("middle WIDE-1 and MID-2"))
        f.seek(tail - 1)
        f.write(b"\n" + u("tail WIDE-1"))
    history("xr-mid", RSID, cfg)
    nc = dict(ENV, DCH_XRAY_NOCLOCK="1")

    def ref(key, *a, env=ENV):
        t0 = time.time()
        r = dch("--xray", "xr-mid", "--ref", key, "--scope", "session", *a,
                "--json", env=env)
        ls = ref_lines(r.stdout) or [{}]
        return (r.returncode, ls, time.time() - t0,
                {l["mention"]["offset"]: l["mention"]["chapter"]
                 for l in ls if "mention" in l})
    try:
        rc, ls, dt, fm = ref("WIDE-1", "--full")
        d = ls[-1].get("done") and ls[-1]
        tl = [l["tally"] for l in ls if "tally" in l]
        check("ref middle: --full over 300 MiB finds the head, middle and "
              "tail mentions in %.2f s, or says partial (time)" % dt,
              rc == 0 and d and (sorted(fm) == [head, mid, tail] and
                                 tl and tl[0]["count"] == 3 and
                                 not tl[0]["partial"] and not d["partial"]
                                 or d["partial"] and d["why"] == "time"),
              (rc, fm, tl, ls[-1:]))
        # #387: --full reads the head, the tail, then the middle, so a
        # byte cap (20 MiB: the head and tail lines read, ~1 MiB, then the
        # middle's 30 MiB line) cuts only the middle; 1.73.0 read the head,
        # then stopped in the middle: the head's mention alone
        rc, ls, _, cm = ref("WIDE-1", "--full",
                            env=dict(ENV, DCH_XRAY_CAPS="0:%d" % (20 << 20)))
        ol = [l["mention"]["offset"] for l in ls if "mention" in l]
        check("387 ref middle: the bytes cap cuts --full in the middle: "
              "the newest (tail) mention kept with its chapter, the head's "
              "too, oldest first, why bytes",
              rc == 0 and ol == [head, tail] and cm[tail] is not None and
              ls[-1].get("partial") and ls[-1].get("why") == "bytes",
              (rc, ol, cm, ls[-1:]))
        rc2, ls2, dt2, lm = ref("MID-2", env=nc)
        rc3, ls3, _, lw = ref("WIDE-1", env=nc)
        check("ref middle: light finds the middle-only ref at --full's "
              "offset (%.2f s), and the tail's first for one in all three"
              % dt2, rc2 == 0 and rc3 == 0 and list(lm) == [mid] and
              mid in fm and list(lw) == [tail] and
              ls3[-1].get("why") == "first", (lm, lw, fm, ls2[-1:]))
        check("ref middle: a chapter opened in the middle --json does not "
              "read is null, light and --full",
              fm.get(mid, 0) is None and lm.get(mid, 0) is None, (fm, lm))
        rc, o, err = xray("xr-mid", "--json")
        anchors = [c.get("anchor") for c in (o or {}).get("chapters", [])]
        keys = [r["key"] for r in (o or {}).get("refs", [])]
        check("ref middle: --json keeps the rule: no middle chapter or ref, "
              "the tail mention's chapter is one of its",
              rc == 0 and o and len(anchors) == 2 and
              not any(a and a.endswith(":%d" % mid) for a in anchors) and
              "ticket:MID-2" not in keys and fm.get(tail) in anchors,
              (anchors, keys, fm))
        rc, so, err = search("xr-mid", "MID-2")
        check("ref middle: --search (unchanged, it reads the whole file) "
              "has the middle hit at --ref's offset, chapter null",
              rc == 0 and so and [(h["offset"], h["chapter"]) for h in
                                  so["hits"]] == [(mid, None)], (so, err))
        rc, out, rss = maxrss([DCH, "--xray", "xr-mid", "--ref", "WIDE-1",
                               "--scope", "session", "--full", "--json"], ENV)
        rss_ok("ref middle: --full over 300 MiB", rss)
        # #387: --full reads the middle after the tail, yet keeps the file's
        # oldest mention and its newest 199, oldest first (range lowered:
        # head 4 KiB, tail 16 KiB)
        with open(tp, "wb") as f:
            f.write(u("start"))
            f.write(u("pad " + "x" * 9000))
            f.write(u("middle MID-3"))
            f.write(u("pad " + "x" * 9000))
            for i in range(250):
                f.write(('{"type":"assistant","message":{"content":'
                         '"t MID-3 %03d"}}\n' % i).encode())
        rc, ls, _, fm = ref("MID-3", "--full",
                            env=dict(ENV, DCH_XRAY_CAPS="0:0:0:4096:16384"))
        ml = [l["mention"]["line"] for l in ls if "mention" in l]
        tl = [l["tally"] for l in ls if "tally" in l]
        check("387 ref middle: --full keeps the middle's oldest mention and "
              "the newest 199 of the tail, oldest first; count exact",
              rc == 0 and ml == ["middle MID-3"] +
              ["t MID-3 %03d" % i for i in range(51, 250)] and tl and
              tl[0]["count"] == 251 and tl[0]["mentions_dropped"] == 51 and
              fm[min(fm)] is None, (rc, ml[:3], ml[-2:], tl, err))
    finally:
        os.unlink(tp)


def test_light_377():
    """#377: light's budget waits for the scan's first window (and its cwd
    walk), so a cut keeps the newest window's newest mentions, not its
    oldest. 800 KB of lines that look like a cwd but name none: the walk
    to the newest window's cwd spends the 50 ms budget, and 1.72.0 cut
    that window after its oldest line (here no mention; xv-o3: 001 alone)."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-p377")
    os.makedirs(pdir, exist_ok=True)
    sid = "11111111-2222-4333-8444-555555555377"
    tp = os.path.join(pdir, sid + ".jsonl")
    walk = '{"type":"progress","xcwd":0,"pad":"%s"}\n' % ("z" * 20)
    with open(tp, "w") as f:
        f.write(json.dumps({"type": "user", "cwd": "/home/demo/p377",
                            "message": {"content": "start"}}) + "\n")
        f.write(walk * (800000 // len(walk)))
        for i in range(1, 251):
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "note %03d about PROJ-77 %s" %
                 (i, "z" * 3900)}]}}) + "\n")
    history("xr-377", sid, cfg, "/home/demo/p377")
    try:
        r = dch("--xray", "xr-377", "--ref", "PROJ-77", "--budget-ms", "50",
                "--json")
        ls = ref_lines(r.stdout) or [{}]
        m = [l["mention"]["line"][5:8] for l in ls if "mention" in l]
        check("377 budget: a cut keeps the newest window's newest 200 "
              "(notes 051 to 250), why budget, not its oldest",
              r.returncode == 0 and
              m == ["%03d" % i for i in range(51, 251)] and
              ls[-1].get("why") == "budget",
              (r.returncode, m[:3], m[-3:], ls[-1:], r.stderr[-300:]))
        # #387: the budget ends that walk, not the scan: a 15.5 MiB walk
        # (near the 16 MiB cap) held 1.73.0 for ~4 s; the window is read
        # with no cwd, so its notes still come
        with open(tp, "w") as f:
            f.write(json.dumps({"type": "user", "cwd": "/home/demo/p377",
                                "message": {"content": "start"}}) + "\n")
            f.write(walk * ((31 << 19) // len(walk)))
            for i in range(1, 251):
                f.write(json.dumps({"type": "assistant", "message": {
                    "content": [{"type": "text", "text":
                                 "note %03d about PROJ-77 %s" %
                                 (i, "z" * 3900)}]}}) + "\n")
        # host-relative: --full reads the same file without the cwd walk, so
        # its time scales with the host (and ASan). 1.73.0's walk was ~33x
        # it (4 s vs 0.12 s), the budgeted one ~3x; the bound is 15x the fastest
        # of 3 runs, 1 s floor
        fulls = []
        for _ in range(3):  # the fastest of 3: one stalled run can't lift the bound
            t0 = time.time()
            dch("--xray", "xr-377", "--ref", "PROJ-77", "--full",
                "--json")
            fulls.append(time.time() - t0)
        full = min(fulls)
        t0 = time.time()
        r = dch("--xray", "xr-377", "--ref", "PROJ-77", "--budget-ms", "50",
                "--json")
        dt = time.time() - t0
        lim = max(15 * full, 1.0)
        ls = ref_lines(r.stdout) or [{}]
        m = [l["mention"]["line"][5:8] for l in ls if "mention" in l]
        check("387 budget: the budget ends a 15.5 MiB cwd walk (%.2f s, "
              "under %.2f s = 15x --full's %.2f s); the newest window's "
              "notes still come, why budget" % (dt, lim, full),
              r.returncode == 0 and dt < lim and m and m[-1] == "250"
              and ls[-1].get("why") == "budget",
              (r.returncode, dt, full, m[:3], m[-3:], ls[-1:],
               r.stderr[-300:]))
    finally:
        os.unlink(tp)


def test_light_379():
    """#379: light --scope session stops at its first hit, but only once
    that hit's window is read to its end, so it answers with the window's
    newest mention, not its oldest (1.73.0: note 001 alone)."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-p379")
    os.makedirs(pdir, exist_ok=True)
    sid = "11111111-2222-4333-8444-555555555379"
    tp = os.path.join(pdir, sid + ".jsonl")
    with open(tp, "w") as f:
        f.write(json.dumps({"type": "user", "cwd": "/home/demo/p379",
                            "message": {"content": "start"}}) + "\n")
        for i in range(1, 51):
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "note %03d about PROJ-79" % i}]}})
                    + "\n")
    history("xr-379", sid, cfg, "/home/demo/p379")
    try:
        r = dch("--xray", "xr-379", "--ref", "PROJ-79", "--scope", "session",
                "--json")
        ls = ref_lines(r.stdout) or [{}]
        m = [l["mention"]["line"][5:8] for l in ls if "mention" in l]
        check("379 scope session: the first hit's window is read to its "
              "end, so note 050 (the newest) is there, nothing unread",
              r.returncode == 0 and
              m == ["%03d" % i for i in range(1, 51)] and
              ls[-1].get("why") is None,
              (r.returncode, m[:3], m[-3:], ls[-1:], r.stderr[-300:]))
        # a scan that read every file it had to is complete (LOW-X5)
        check("379 scope session: the whole file read, so partial false, "
              "why null", ls[-1].get("partial") is False and
              ls[-1].get("why") is None, ls[-1:])
        # another session's result stops the scan: the third is left unread
        tps = []
        for k in (80, 81):
            sk = "11111111-2222-4333-8444-5555555553%02d" % k
            tps.append(os.path.join(pdir, sk + ".jsonl"))
            with open(tps[-1], "w") as f:
                f.write(json.dumps({"type": "user", "cwd": "/home/demo/p379",
                                    "message": {"content": "PROJ-79 too"}})
                        + "\n")
            history("xr-379-%d" % k, sk, cfg, "/home/demo/p379")
        try:
            r = dch("--xray", "xr-379", "--ref", "PROJ-79", "--scope",
                    "project", "--json")
            d = (ref_lines(r.stdout) or [{}])[-1]
            check("379 scope project: another session's result stops the "
                  "scan before the next file, partial true, why first",
                  d.get("partial") is True and d.get("why") == "first", d)
        finally:
            for q in tps:
                os.unlink(q)
    finally:
        os.unlink(tp)


HTSID = "11111111-2222-4333-8444-5555555555f1"


def test_long_answer():
    """#451: a long answer's end is kept. Refs are indexed over the whole
    text block; a --chapter prose entry past 9000 scalars keeps its first
    6000 and last 3000 with a "… N characters cut …" line and "cut": N; the
    --json response past 2048 bytes keeps its first and last 1024, cut on a
    character, with a "\\n…\\n" between and response_cut_chars."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, HTSID + ".jsonl")
    short = "Short answer with é and #500."
    # "€" (3 bytes) straddles byte 1024 and byte n - 1024: a byte cut
    # there would split it
    head = "HEADSTART #100 " + "a" * 1008 + "€"
    tail = "€" + ("detail " * 200 +
                       "\nNext steps: merge #999 TAILMARK")[-1022:]
    body = "\n".join("- point %d: detail words detail words" % i
                     for i in range(300))
    long_ = head + "\n" + body + "\n" + tail
    prompt = "PROMPTHEAD " + "q" * 12000 + " #777 PROMPTTAIL"
    lines = [
        {"type": "user", "cwd": "/home/demo/proj",
         "timestamp": "2026-11-10T10:00:00Z",
         "message": {"content": "short ask"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": short}]}},
        {"type": "user", "timestamp": "2026-11-10T10:01:00Z",
         "message": {"content": prompt}},
        {"type": "assistant", "message": {"content": long_}}]
    with open(tp, "w") as f:
        f.write("".join(json.dumps(o) + "\n" for o in lines))
    history("xr-long", HTSID, cfg)
    sc = len(long_)
    b = long_.encode()
    try:
        def count(r):
            x = dch("--xray", "xr-long", "--ref", r, "--json")
            t = [l["tally"] for l in ref_lines(x.stdout) or [] if "tally" in l]
            return t[0].get("count") if t else None
        check("long answer: a ref at the end of a 12 KB answer is indexed "
              "(--ref #999 count 1)", count("#999") == 1, count("#999"))
        check("long answer: a ref at the end of a 12000-char prompt is "
              "indexed (--ref #777 count 1)", count("#777") == 1,
              count("#777"))
        rc, o, err = xray("xr-long", "--json")
        # the Refs tab's order (README): latest mention, then count, then
        # later in the index; #999 and #100 share their answer's line
        rs = (o or {}).get("refs", [])
        keys = [r["key"] for _, r in sorted(
            enumerate(rs), key=lambda x: (-x[1].get("last_offset", -1),
                                          -x[1]["count"], -x[0]))]
        check("long answer: Refs put #999 first, the latest mention",
              rc == 0 and keys[:2] == ["issue:#999", "issue:#100"] and
              rs and rs[-1]["key"] == "issue:#999", (keys, err))
        ch = (o or {}).get("chapters", [])
        r = ch[1].get("response", "") if len(ch) == 2 else ""
        want = b[:1023].decode() + "\n…\n" + b[-1022:].decode()
        check("long answer: --json response is head + tail, cut on a "
              "character: it starts with the head and holds TAILMARK",
              r.startswith("HEADSTART #100 ") and r.endswith("TAILMARK") and
              r == want and "�" not in r and
              ch[1].get("response_cut") is True and
              ch[1].get("response_cut_chars") ==
              len(b[1023:-1022].decode()), (len(r), r[1000:1040],
                                            ch[1:] and {k: v for k, v in
                                                        ch[1].items()
                                                        if k != "response"}))
        check("long answer: a short response is unchanged, no response_cut",
              ch and ch[0].get("response") == short and
              "response_cut" not in ch[0] and
              "response_cut_chars" not in ch[0], ch[:1])
        rc, c, err, raw = chapter("xr-long", ch[1]["anchor"]) \
            if len(ch) == 2 else (1, None, "", "")
        es = (c or {}).get("entries", [])
        a = es[1] if len(es) == 2 else {}
        mark = "\n… %d characters cut …\n" % (sc - 9000)
        check("long answer: the --chapter entry is the first 6000 and last 3000 "
              "characters with the cut line, and cut: N",
              a.get("role") == "assistant" and
              a.get("text") == long_[:6000] + mark + long_[-3000:] and
              "TAILMARK" in a.get("text", "") and
              a.get("cut") == sc - 9000, (err, {k: v for k, v in a.items()
                                                 if k != "text"},
                                          a.get("text", "")[2990:3040]))
        u = es[0] if es else {}
        check("long prompt: the user entry is head + tail too",
              u.get("text") == prompt[:6000] + "\n… %d characters cut "
              "…\n" % (len(prompt) - 9000) + prompt[-3000:] and
              u.get("cut") == len(prompt) - 9000,
              {k: v for k, v in u.items() if k != "text"})
        rc, c0, err, raw0 = chapter("xr-long", ch[0]["anchor"]) \
            if ch else (1, None, "", "")
        check("short answer: its --chapter entries are unchanged, no cut",
              c0 and c0["entries"] == [ent("user", "short ask"),
                                       ent("assistant", short)], c0)
    finally:
        os.unlink(tp)


def test_ent_bounds():
    """#451: an entry of 9000 scalars or fewer is whole (7000: no cut field);
    one of 12000 keeps scalars 0-6000 and the last 3000."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    sid = "11111111-2222-4333-8444-5555555555f2"
    tp = os.path.join(pdir, sid + ".jsonl")
    mk = lambda n: "".join(chr(0x4e00 + i % 500) for i in range(n))
    a7, a9, a12 = mk(7000), mk(9000), mk(12000)
    lines = [{"type": "user", "cwd": "/home/demo/proj",
              "timestamp": "2026-11-10T10:00:00Z",
              "message": {"content": "ask"}}]
    for a in (a7, a9, a12):
        lines.append({"type": "assistant", "message": {"content": a}})
    with open(tp, "w") as f:
        f.write("".join(json.dumps(o) + "\n" for o in lines))
    history("xr-bound", sid, cfg)
    try:
        rc, o, err = xray("xr-bound", "--json")
        ch = (o or {}).get("chapters", [])
        rc, c, err, raw = chapter("xr-bound", ch[0]["anchor"]) \
            if ch else (1, None, "", "")
        es = [e for e in (c or {}).get("entries", [])
              if e.get("role") == "assistant"]
        t = [e.get("text") for e in es]
        check("entry bounds: 7000 and 9000 scalars are whole, no cut field; "
              "12000 keeps 0-6000 and the last 3000 with the marker",
              len(es) == 3 and t[0] == a7 and t[1] == a9 and
              "cut" not in es[0] and "cut" not in es[1] and
              t[2] == a12[:6000] + "\n… 3000 characters cut …\n" +
              a12[-3000:] and es[2].get("cut") == 3000,
              (err, [(len(x or ""), e.get("cut")) for x, e in zip(t, es)]))
    finally:
        os.unlink(tp)


def test_many_mentions():
    """#463: past XR_SPANS (128) mentions in one text block the LAST 128
    are kept (recency first), not the first."""
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, HTSID + ".jsonl")
    big = " ".join("#%d" % i for i in range(1000, 1200)) + "\nNext: #999 TAILMARK"
    mid = " ".join("#%d" % i for i in range(2000, 2100))
    lines = [
        {"type": "user", "cwd": "/home/demo/proj",
         "timestamp": "2026-11-10T10:00:00Z",
         "message": {"content": "ask"}},
        {"type": "assistant", "message": {"content": big}},
        {"type": "user", "timestamp": "2026-11-10T10:01:00Z",
         "message": {"content": "ask2"}},
        {"type": "assistant", "message": {"content": mid}}]
    with open(tp, "w") as f:
        f.write("".join(json.dumps(o) + "\n" for o in lines))
    history("xr-many", HTSID, cfg)
    try:
        def count(r):
            x = dch("--xray", "xr-many", "--ref", r, "--json")
            t = [l["tally"] for l in ref_lines(x.stdout) or [] if "tally" in l]
            return t[0].get("count") if t else None
        check("many mentions: --ref #999 after 200 mentions counts 1",
              count("#999") == 1, count("#999"))
        check("many mentions: the last of 200 (#1199, #1073) are kept, "
              "the first (#1000, #1072) dropped",
              count("#1199") == 1 and count("#1073") == 1 and
              count("#1000") != 1 and count("#1072") != 1,
              [count(r) for r in ("#1199", "#1073", "#1000", "#1072")])
        rc, o, err = xray("xr-many", "--json")
        ks = {r["key"] for r in (o or {}).get("refs", [])}
        check("many mentions: --json refs hold #999 and #1199; 100 "
              "mentions keep all (#2000, #2099)",
              rc == 0 and {"issue:#999", "issue:#1199", "issue:#2000",
                           "issue:#2099"} <= ks and
              "issue:#1000" not in ks, (err, len(ks)))
    finally:
        os.unlink(tp)


def test_terms():
    """#458: a term (a 2+ part CONST_CASE or snake_case identifier, or a
    backticked span) is a ref of its own kind, from prose only.
    ERR_LEDGER_SKEW is in turns 3, 9 and 30, each time beside PAY-219: its
    --ref answers as the ticket's does (count, places), and its definition is
    the turn-3 sentence, marked "source":"first mention"."""
    sid = "11111111-2222-4333-8444-5555555555c8"
    cfg = os.path.join(HOME, ".claude")
    pdir = os.path.join(cfg, "projects", "-home-demo-proj")
    os.makedirs(pdir, exist_ok=True)
    tp = os.path.join(pdir, sid + ".jsonl")
    first = ("The ledger raised ERR_LEDGER_SKEW when the totals drifted on "
             "PAY-219.")
    say = {3: "We ran it. " + first + " More on that later.",
           9: None,
           30: "Fixed: ERR_LEDGER_SKEW no longer fires for PAY-219.",
           5: "Set `dryRun` first, then `shadow_mode`."}
    offs = {}
    with open(tp, "w") as f:
        for t in range(1, 31):
            ask = ("is ERR_LEDGER_SKEW back in PAY-219?" if t == 9
                   else "turn %d" % t)
            if t == 9:
                offs[t] = f.tell()
            f.write(json.dumps({"type": "user", "cwd": "/home/demo/proj",
                                "timestamp": "2026-11-10T10:%02d:00Z" % t,
                                "message": {"content": ask}}) + "\n")
            if say.get(t):
                offs.setdefault(t, f.tell())
            f.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": say.get(t) or "ok %d" % t}]}}) + "\n")
            # tool output never makes a term
            f.write(json.dumps({"type": "user", "toolUseResult": {
                "stdout": "TOOL_ONLY_TERM"}, "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "t%d" % t,
                     "content": "TOOL_ONLY_TERM"}]}}) + "\n")
    history("xr-terms", sid, cfg)
    try:
        def ref(r, *a):
            x = dch("--xray", "xr-terms", "--ref", r, "--scope", "session",
                    *a, "--json")
            return x.returncode, ref_lines(x.stdout) or []

        for mode in ((), ("--full",)):
            rc, ls = ref("ERR_LEDGER_SKEW", *mode)
            _, tk = ref("PAY-219", *mode)
            men = [l["mention"]["offset"] for l in ls if "mention" in l]
            tmen = [l["mention"]["offset"] for l in tk if "mention" in l]
            ds = [l["definition"] for l in ls if "definition" in l]
            tally = [l["tally"] for l in ls if "tally" in l]
            check("terms: --ref ERR_LEDGER_SKEW%s counts 3, its places are "
                  "turns 3, 9 and 30, in the order a ticket's come" %
                  " ".join(("",) + mode), rc == 0 and tally and
                  tally[0]["count"] == 3 and ls[-1]["counts"]["session"] == 3
                  and men == tmen and sorted(men) == [offs[3], offs[9],
                                                      offs[30]], (ls, tmen))
            check("terms: its definition is the turn-3 sentence, "
                  "source first mention%s" % " ".join(("",) + mode),
                  len(ds) == 1 and ds[0]["title"] == first and
                  ds[0]["passage"] == first and ds[0]["offset"] == offs[3] and
                  ds[0]["source"] == "first mention" and
                  ds[0]["rule"] == "term" and tally[0]["definitions"] == 1,
                  ds)
        rc, ls = ref("`dryRun`")
        check("terms: a backtick-only term is asked for in its backticks",
              rc == 0 and ls[0]["first"]["key"] == "term:dryRun" and
              ls[-1]["counts"]["session"] == 1, ls)
        rc, o, err = xray("xr-terms", "--json")
        refs = {r["key"]: r for r in (o or {}).get("refs", [])}
        t = refs.get("term:ERR_LEDGER_SKEW", {})
        check("terms: --json refs list the term, shape term, count 3",
              rc == 0 and t.get("shape") == "term" and t.get("count") == 3
              and t.get("text") == "ERR_LEDGER_SKEW" and
              "term:shadow_mode" in refs and "term:dryRun" in refs, (o, err))
        check("terms: never from tool output, never one part",
              "term:TOOL_ONLY_TERM" not in refs and
              not [k for k in refs if k in ("term:turn", "term:ok")], refs)
    finally:
        os.unlink(tp)


def main():
    try:
        test_names()
        test_claude()
        test_clear_after_claude()
        test_two_claudes()
        test_refs()
        test_d2_whole()
        test_write_fail()
        test_search()
        test_tasks()
        test_tasks_cap()
        test_chapter()
        test_chapter_seek()
        test_cancel()
        test_claude_caps()
        test_latest()
        test_follow_history()
        test_auto()
        test_peer()
        test_handback_search()
        test_responses()
        test_scope()
        test_light_anchors()
        test_light_303()
        test_light_326()
        test_prefilter_reach()
        test_ref_middle()
        test_light_377()
        test_light_379()
        test_long_answer()
        test_ent_bounds()
        test_many_mentions()
        test_terms()
    finally:
        shutil.rmtree(T, ignore_errors=True)
    if fails[0]:
        print("%d failed" % fails[0])
        sys.exit(1)


if __name__ == "__main__":
    main()
